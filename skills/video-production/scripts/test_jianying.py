"""Offline tests for editable cuts, text, audio and portable draft installation."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import wave

from bailian_media import load, tool_path
from export_jianying import export_project, find_draft_root, install_project, normalize


def plan(path='raw.mp4'):
    return dict(revision='cut-test', width=360, height=640, fps={'num':25,'den':1},
                duration_frames=100, source={'path':str(path),'duration_s':6}, segments=[
                    dict(id='s1',source_in_s=.5,source_out_s=2.5,final_in_s=0,final_out_s=2),
                    dict(id='s2',source_in_s=3,source_out_s=5,final_in_s=2,final_out_s=4)])


def captions():
    return dict(revision='cut-test',fps={'num':25,'den':1},captions=[
        dict(start_frame=0,end_frame=50,lines=['可编辑字幕一']),
        dict(start_frame=50,end_frame=100,lines=['可编辑字幕二'])])


class TimelineTests(unittest.TestCase):
    def test_portrait_plan_cannot_export_a_landscape_canvas(self):
        p = plan(); p.update(width=640, height=360, output_orientation='portrait')
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'draft'
            with self.assertRaisesRegex(ValueError, 'canvas must be portrait'):
                export_project(p, out)
            self.assertFalse(out.exists())

    def test_custom_draft_location_takes_precedence(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);data=root/'User Data';config=data/'Config';config.mkdir(parents=True)
            default=data/'Projects'/'com.lveditor.draft';default.mkdir(parents=True)
            custom=root/'Custom Drafts';custom.mkdir()
            value=str(custom).replace('\\','\\\\')
            (config/'globalSetting').write_text('[General]\ncurrentCustomDraftPath='+value,encoding='utf-8')
            self.assertEqual(find_draft_root(data),custom)
            custom.rmdir()
            with self.assertRaises(FileNotFoundError): find_draft_root(data)

    def test_default_draft_location_when_setting_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            data=Path(temp);default=data/'Projects'/'com.lveditor.draft';default.mkdir(parents=True)
            self.assertEqual(find_draft_root(data),default)

    def test_cuts_keep_source_handles(self):
        t=normalize(plan(),captions())
        clips=t['tracks']['原片与同期声']
        self.assertEqual([c['source_us'] for c in clips],[500000,3000000])
        self.assertEqual([c['duration_us'] for c in clips],[2000000,2000000])
        self.assertEqual(t['tracks']['字幕'][1]['start_us'],2000000)

    def test_stale_captions(self):
        c=captions();c['revision']='old'
        with self.assertRaisesRegex(ValueError,'Stale'): normalize(plan(),c)

    def test_existing_source_path_and_rolling_caption_schema(self):
        p=plan();p['source']['source_path']=p['source'].pop('path')
        c=captions();c['lines']=c.pop('captions')
        t=normalize(p,c)
        self.assertEqual(t['tracks']['原片与同期声'][0]['path'],'raw.mp4')
        self.assertEqual(len(t['tracks']['字幕']),2)
        self.assertTrue(any('滚动字幕' in warning for warning in t['warnings']))

    def test_speed_change_and_gap_rejected(self):
        for key,value in [('source_out_s',2.4),('final_in_s',.01)]:
            p=plan();p['segments'][0][key]=value
            with self.assertRaises(ValueError): normalize(p)

    def test_nonfinite_time_rejected(self):
        p=plan();p['segments'][0]['source_in_s']=float('nan')
        with self.assertRaises(ValueError): normalize(p)

    def test_fractional_fps_preserves_exact_times_and_reports_draft_grid(self):
        p=plan();p.update(fps={'num':60000,'den':1001},duration_frames=240)
        c=captions();c['fps']=p['fps']
        c['captions'][0].update(start_frame=0,end_frame=120)
        c['captions'][1].update(start_frame=120,end_frame=240)
        original=copy.deepcopy(p)
        result=normalize(p,c)
        self.assertEqual(p,original)
        self.assertEqual(result['fps'],60)
        self.assertEqual(result['timeline_fps'],p['fps'])
        self.assertEqual(result['duration_us'],4004000)
        self.assertEqual(result['tracks']['字幕'][1]['start_us'],2002000)
        self.assertEqual(result['tracks']['原片与同期声'][1]['start_us'],2000000)
        self.assertTrue(any('60000/1001' in w for w in result['warnings']))

    def test_layers_under_text_and_overlap_rejected(self):
        layer=dict(kind='video',track='画面',path='image.png',start_s=0,end_s=2)
        layers={'revision':'cut-test','clips':[layer]}
        self.assertEqual(list(normalize(plan(),captions(),layers)['tracks'])[-1],'字幕')
        layers['clips'].append(copy.deepcopy(layer))
        with self.assertRaisesRegex(ValueError,'Overlapping'): normalize(plan(),layers=layers)

    def test_unknown_effect_cannot_silently_disappear(self):
        layers={'revision':'cut-test','clips':[
            dict(kind='text',track='标题',text='标题',start_s=0,end_s=4,typewriter=True)]}
        with self.assertRaisesRegex(ValueError,'Unsupported'): normalize(plan(),layers=layers)

    def test_hook_without_raw(self):
        p=plan();p.pop('source');p['segments']=[]
        layers={'revision':'cut-test','clips':[
            dict(kind='text',track='标题',text='推广标题',start_s=0,end_s=4),
            dict(kind='audio',track='配音',path='voice.wav',start_s=0,end_s=4)]}
        self.assertEqual(len(normalize(p,layers=layers)['tracks']),2)


@unittest.skipUnless(importlib.util.find_spec('pyJianYingDraft'), 'Optional Jianying dependency not installed')
class DraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.root=Path(cls.temp.name)
        cls.raw=cls.root/'原片.mp4'
        subprocess.run([tool_path('ffmpeg'),'-v','error','-f','lavfi','-i',
                        'testsrc2=size=360x640:rate=25','-t','6','-c:v','libx264','-pix_fmt','yuv420p',
                        str(cls.raw)],check=True)
        cls.music=cls.root/'bgm.wav'
        with wave.open(str(cls.music),'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(48000)
            f.writeframes(b'\0\0'*48000*6)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def export(self,name):
        out=self.root/name
        layers={'revision':'cut-test','clips':[
            dict(kind='audio',track='BGM',path=str(self.music),start_s=0,end_s=4,
                 source_in_s=1,volume=.2,fade_in_s=.2,fade_out_s=.3)]}
        report=export_project(plan(self.raw),out,captions(),layers)
        return out,report

    def test_real_writer_editability_and_template_readback(self):
        out,report=self.export('editable')
        doc=load(out/'draft_content.json')
        tracks={t['name']:t for t in doc['tracks']}
        video=tracks['原片与同期声']['segments']
        self.assertEqual(len(video),2)
        self.assertEqual(video[1]['source_timerange'],{'start':3000000,'duration':2000000})
        self.assertEqual(tracks['BGM']['segments'][0]['volume'],.2)
        self.assertEqual(len(tracks['字幕']['segments']),2)
        texts=[json.loads(t['content'])['text'] for t in doc['materials']['texts']]
        self.assertEqual(texts,['可编辑字幕一','可编辑字幕二'])
        self.assertEqual(report['app_open'],'not_checked')
        from pyJianYingDraft import DraftFolder
        loaded=DraftFolder(str(self.root)).load_template('editable')
        self.assertEqual(loaded.duration,4000000)
        self.assertEqual(load(out/'draft_meta_info.json')['draft_name'],'editable')
        material=doc['materials']['videos'][0]
        self.assertEqual(Path(material['path']).read_bytes(),self.raw.read_bytes())

    def test_move_install_rebinds_without_source_and_preserves_existing(self):
        out,_=self.export('portable')
        moved=self.root/'moved';shutil.copytree(out,moved)
        shutil.rmtree(out)  # only our disposable fixture
        draft_root=self.root/'drafts';draft_root.mkdir()
        sentinel=draft_root/'existing.txt';sentinel.write_text('keep')
        installed=install_project(moved,draft_root)
        for group in ('videos','audios'):
            for material in load(installed/'draft_content.json')['materials'][group]:
                self.assertTrue(Path(material['path']).is_file())
                self.assertTrue(Path(material['path']).is_relative_to(installed))
        self.assertEqual(sentinel.read_text(),'keep')
        with self.assertRaises(FileExistsError): install_project(moved,draft_root)

    def test_bad_input_leaves_no_partial_project(self):
        p=plan(self.raw);p['segments'][1]['source_out_s']=8;p['source']['duration_s']=10
        p['segments'][1]['final_out_s']=7;p['duration_frames']=175
        out=self.root/'bad'
        with self.assertRaises(ValueError): export_project(p,out)
        self.assertFalse(out.exists())

    def test_tampered_asset_refuses_install(self):
        out,_=self.export('tampered')
        asset=load(out/'package.json')['assets'][0]
        (out/asset['relative_path']).write_bytes(b'changed')
        draft_root=self.root/'tamper-drafts';draft_root.mkdir()
        with self.assertRaisesRegex(ValueError,'changed'): install_project(out,draft_root)
        self.assertFalse((draft_root/out.name).exists())


if __name__=='__main__': unittest.main()
