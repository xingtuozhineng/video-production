"""Review freshness, shared audio evidence, and rejection before video encoding."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import delivery
import pre_render_review as review
from bailian_media import load, save, file_hash
from map_words import remap
from recording_review import draft_review, transcript_hash


class PreRenderReviewTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('VIDEO_TEST_FFMPEG'), 'Set VIDEO_TEST_FFMPEG/FFPROBE for real media tests')
    def test_reviewed_portrait_timeline_renders_with_real_ffmpeg(self):
        from render_timeline import main as render
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project, plan, captions = self.fixture(root)
            source = root/'source.mp4'
            ffmpeg, ffprobe = os.environ['VIDEO_TEST_FFMPEG'], os.environ['VIDEO_TEST_FFPROBE']
            subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=s=96x160:r=50:d=10',
                '-f', 'lavfi', '-i', 'sine=sample_rate=48000:duration=10', '-c:v', 'libx264',
                '-preset', 'ultrafast', '-c:a', 'aac', '-y', str(source)], check=True)
            (captions/'fonts/test.ttf').unlink()
            font = review.HERE.parent/'assets/fonts/lxgw-wenkai/LXGWWenKai-Regular.ttf'
            shutil.copy2(font, captions/'fonts'/font.name)
            (captions/'captions.ass').write_text('[Script Info]\nScriptType: v4.00+\nPlayResX: 96\nPlayResY: 160\n'
                '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, '
                'Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, '
                'Alignment, MarginL, MarginR, MarginV, Encoding\n'
                'Style: Default,LXGW WenKai,12,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,4,4,8,1\n'
                '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
                'Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,正式正文\n', encoding='utf-8')
            review.prepare(root, project, plan, captions)
            self.approve(review.inputs(project, plan, captions))
            output = root/'final.mp4'
            self.assertEqual(render(['--source', str(source), '--plan', str(plan), '--captions-ass',
                str(captions/'captions.ass'), '--out', str(output), '--ffmpeg', ffmpeg,
                '--encoder', 'libx264', '--preset', 'ultrafast']), 0)
            metadata = json.loads(subprocess.check_output([ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(output)]))
            video = next(s for s in metadata['streams'] if s['codec_type'] == 'video')
            self.assertEqual((video['width'], video['height'], video['r_frame_rate']), (96, 160, '50/1'))
            self.assertEqual(int(video['nb_frames']), 100)

    def fixture(self, root):
        project = root/'projects/talking-head/test'
        source = root/'source.mp4'; source.write_bytes(b'source')
        save(root/'video-production-deps/tools.json', {'ffmpeg': 'ffmpeg', 'ffprobe': 'ffprobe'})
        save(project/'production.json', {'route': 'talking-head'})
        plan = {'revision': 'r', 'width': 96, 'height': 160, 'sample_rate': 48000,
                'source': {'path': str(source), 'duration_s': 10, 'fps': {'num': 50, 'den': 1}},
                'fps': {'num': 50, 'den': 1}, 'duration_s': 2, 'duration_frames': 100,
                'segments': []}
        for i, start in enumerate((7.2, 8.7)):
            plan['segments'].append({'id': f'k{i}', 'source_in_s': start, 'source_out_s': start+1,
                'source_in_frame': round(start*50), 'source_out_frame': round((start+1)*50),
                'source_frame_count': 50, 'final_in_s': i, 'final_out_s': i+1,
                'final_in_frame': i*50, 'final_out_frame': (i+1)*50, 'duration_frames': 50})
        doc = {'revision': 'source', 'words': [{'id': f'w{i}', 'text': text,
            'source_start_s': start+.05, 'source_end_s': start+.6,
            'speaker_id': 0, 'utterance_id': f'u{i}'}
            for i, (start, text) in enumerate(zip((7.2, 8.7), ('正式', '正文')))]}
        roles = draft_review(doc)
        roles.update(status='reviewed', mode='solo', evidence='测试角色明确')
        roles['content_review'] = {'status': 'reviewed', 'evidence': '测试正文'}
        roles['speaker_roles'][0].update(role='performer', evidence='测试主角')
        save(project/'work/asr/transcript.source.json', doc)
        save(project/'work/index/selection.json', {'source_revision': 'source',
            'ranges': [{'first': 1, 'last': 2, 'reason': '正文'}]})
        save(project/'work/index/recording-review.json', roles)
        plan_path = project/'work/compiled/edit-plan.json'
        save(plan_path, plan)
        save(project/'work/compiled/mapped-words.json', remap(doc, plan))
        captions = project/'work/captions'
        save(captions/'captions.json', {'revision': 'r', 'captions': [
            {'start_frame': 0, 'end_frame': 50, 'lines': ['正式']},
            {'start_frame': 50, 'end_frame': 100, 'lines': ['正文']}]})
        (captions/'captions.ass').write_text('test-ass', encoding='utf-8')
        (captions/'captions.srt').write_text('test-srt', encoding='utf-8')
        save(captions/'authoring.resolved.json', {'revision': 'r', 'pages': []})
        (captions/'fonts').mkdir(); (captions/'fonts/test.ttf').write_bytes(b'test-font')
        return project, plan_path, captions

    def approve(self, ctx):
        decisions = load(ctx['root']/'decisions.json')
        decisions.update(status='reviewed')
        for check in decisions['checks'].values():
            check.update(status='reviewed', evidence='测试已核实')
        save(ctx['root']/'decisions.json', decisions)

    def audio_report(self, ctx, verdict='pass'):
        wav = ctx['audio_root']/'review.wav'
        wav.parent.mkdir(parents=True, exist_ok=True); wav.write_bytes(b'test-pcm')
        report = {'status': 'complete', 'scope': 'final', 'revision': 'r',
                  'media_sha256': file_hash(wav), 'reviewed_cuts': 1,
                  'results': [{'job': {'signature': 'test-job', 'start_s': 0, 'end_s': 2,
                       'items': [{'id': 'cut-001'}]},
                       'result': {'checks': [{'id': 'cut-001', 'prompt_residue': 'pass',
                           'nonformal_speech': 'pass', 'word_clipping': verdict, 'breath_cut': 'pass',
                           'evidence': '测试字音异常'}], 'events': []}}]}
        save(ctx['audio_root']/'report.json', report)

    def test_missing_pending_and_changed_captions_reject_before_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project, plan, captions = self.fixture(root)
            ctx = review.inputs(project, plan, captions)
            with self.assertRaisesRegex(ValueError, 'missing'):
                review.require(ctx)
            result = review.prepare(root, project, plan, captions)
            self.assertEqual(result['status'], 'decision_required')
            self.approve(ctx); self.assertEqual(review.require(ctx)['status'], 'ready_for_render')
            decisions = load(ctx['root']/'decisions.json'); decisions['pending_changes'] = ['修换气']
            save(ctx['root']/'decisions.json', decisions)
            with self.assertRaisesRegex(ValueError, 'must be applied'):
                review.require(ctx)
            decisions['pending_changes'] = []; save(ctx['root']/'decisions.json', decisions)
            (captions/'captions.ass').write_text('changed', encoding='utf-8')
            with patch('render_timeline.subprocess.run') as encode, self.assertRaisesRegex(ValueError, 'missing'):
                from render_timeline import main
                main(['--source', str(ctx['source']), '--plan', str(plan), '--captions-ass',
                      str(captions/'captions.ass'), '--out', str(root/'final.mp4'), '--ffmpeg', 'ffmpeg'])
            encode.assert_not_called()

    def test_caption_changes_reuse_audio_without_provider_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project, plan, captions = self.fixture(root)
            ctx = review.inputs(project, plan, captions); self.audio_report(ctx)
            (captions/'captions.ass').write_text('new captions', encoding='utf-8')
            updated = review.inputs(project, plan, captions)
            self.assertNotEqual(updated['signature'], ctx['signature'])
            self.assertEqual(updated['audio_root'], ctx['audio_root'])
            with patch('render_timeline.render_audio') as render, patch('audio_review.run') as provider:
                result = review.prepare(root, project, plan, captions, execute=True, audio=True)
            render.assert_not_called(); provider.assert_not_called()
            self.approve(updated)
            self.assertEqual(review.require(updated)['status'], 'ready_for_render')
            changed = load(plan); changed['segments'][0]['source_in_s'] += .02
            save(plan, changed)
            self.assertNotEqual(review.inputs(project, plan, captions)['audio_root'], updated['audio_root'])

    def test_incomplete_audio_and_unresolved_issues_block_and_uncertainty_stays_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project, plan, captions = self.fixture(root)
            ctx = review.inputs(project, plan, captions); self.audio_report(ctx, 'issue')
            review.prepare(root, project, plan, captions, audio=True); self.approve(ctx)
            with self.assertRaisesRegex(ValueError, 'Unresolved'):
                review.require(ctx)
            decisions = load(ctx['root']/'decisions.json')
            decisions['audio_dispositions'][0].update(decision='uncertain_review_version', evidence='原声结论冲突')
            save(ctx['root']/'decisions.json', decisions)
            result = review.require(ctx)
            self.assertTrue(result['unconfirmed_audio'])
            self.assertEqual(result['quality_acceptance'], 'review_required')
            self.assertEqual(result['human_listening'], 'not_checked')
            report = load(ctx['audio_root']/'report.json'); report['reviewed_cuts'] = 0
            save(ctx['audio_root']/'report.json', report)
            with self.assertRaisesRegex(ValueError, 'incomplete/stale'):
                review.require(ctx)

    def test_missing_source_review_and_unreliable_retained_word_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project, plan, captions = self.fixture(root)
            ctx = review.inputs(project, plan, captions)
            doc_path = project/'work/asr/transcript.source.json'
            doc = load(doc_path); doc['words'][0]['timing_status'] = 'uniform_provider_timing'
            save(doc_path, doc)
            roles_path = project/'work/index/recording-review.json'
            roles = load(roles_path); roles['transcript_hash'] = transcript_hash(doc); save(roles_path, roles)
            with self.assertRaisesRegex(ValueError, 'timing is unreliable'):
                review.source_checks(ctx)
            (project/'work/index/recording-review.json').unlink()
            with self.assertRaisesRegex(ValueError, 'are required'):
                review.source_checks(ctx)

    def test_delivery_snapshot_preserves_review_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project, plan, captions = self.fixture(root)
            ctx = review.inputs(project, plan, captions)
            review.prepare(root, project, plan, captions); self.approve(ctx)
            binary = root/'ffmpeg'; binary.write_bytes(b'test-tool')
            save(root/'video-production-deps/tools.json', {'ffmpeg': str(binary), 'ffprobe': str(binary)})
            with patch('delivery.normalize'), patch('delivery.subprocess.run', side_effect=RuntimeError('stop before encoding')):
                with self.assertRaisesRegex(RuntimeError, 'stop before encoding'):
                    delivery.main(['--workspace-root', str(root), '--plan', str(plan),
                        '--captions-dir', str(captions), '--out-dir', str(project/'output')])
            snapshot = next((project/'output').glob('*/project'))
            snap_ctx = review.inputs(project, snapshot/'edit-plan.json', snapshot/'captions')
            self.assertEqual(snap_ctx['signature'], ctx['signature'])
            self.assertEqual(review.require_render(snapshot/'edit-plan.json', snapshot/'captions/captions.ass')['status'], 'ready_for_render')
            sample = copy.deepcopy(ctx['plan']); sample['revision'] = 'sample'
            sample['segments'] = sample['segments'][:1]; sample['duration_s'] = 1; sample['duration_frames'] = 50
            sample_path = snapshot/'sample.json'; save(sample_path, sample)
            self.assertEqual(review.require_render(sample_path, snapshot/'captions/captions.ass', snapshot/'edit-plan.json')['status'], 'ready_for_render')
            sample['segments'][0]['source_in_s'] += .02; save(sample_path, sample)
            with self.assertRaisesRegex(ValueError, 'differs'):
                review.require_render(sample_path, snapshot/'captions/captions.ass', snapshot/'edit-plan.json')


if __name__ == '__main__':
    unittest.main()
