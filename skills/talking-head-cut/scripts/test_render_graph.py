"""Static guards; real media behavior is tested in test_render_media.py."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from render_timeline import graph, audio_graph, main as render


def fixture():
    return {'fps': {'num': 30, 'den': 1}, 'duration_frames': 61, 'sample_rate': 48000, 'segments': [
        {'id': 'a', 'source_in_s': .1, 'source_out_s': 1.115, 'final_in_s': 0, 'final_out_s': 1.015},
        {'id': 'b', 'source_in_s': 2.1, 'source_out_s': 3.106, 'final_in_s': 1.015, 'final_out_s': 2.021}]}


class RenderGraphTests(unittest.TestCase):
    def test_audio_preview_uses_identical_av_sample_trims_and_final_tail(self):
        for fps in ({'num':30,'den':1},{'num':60000,'den':1001}):
            plan=fixture(); plan['fps']=fps
            av=graph(plan,1080,1920,input_start_s=0).split(';')
            audio=[r for r in av if r.startswith('[0:a]') or r.startswith('[as') or ':v=0:a=1,' in r]
            self.assertEqual(audio_graph(plan,0),';'.join(audio))
            self.assertNotIn('[0:v]',audio_graph(plan,0))

    def test_portrait_plan_cannot_be_rendered_with_a_landscape_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = dict(fixture(), width=1080, height=1920, output_orientation='portrait')
            (root / 'plan.json').write_text(json.dumps(plan), encoding='utf-8')
            with patch('render_timeline.validate', return_value={'status': 'pass'}), \
                 self.assertRaisesRegex(ValueError, 'must be portrait'):
                render(['--source', str(root / 'raw.mp4'), '--plan', str(root / 'plan.json'),
                        '--out', str(root / 'out.mp4'), '--ffmpeg', 'ffmpeg',
                        '--width', '1920', '--height', '1080'])
            self.assertFalse((root / 'out.mp4').exists())

    def test_audio_keeps_sample_lengths_and_pads_only_final_tail(self):
        text = graph(fixture(), 1080, 1920)
        self.assertIn('atrim=start_sample=4800:end_sample=53520', text)
        self.assertIn('atrim=start_sample=100800:end_sample=149088', text)
        self.assertEqual(text.count('apad'), 1)
        self.assertIn('trim=end_frame=30', text)
        self.assertIn('trim=end_frame=31', text)

    def test_tail_reaches_declared_ceil_frame_without_moving_cuts(self):
        plan = fixture(); plan['duration_frames'] = 62
        text = graph(plan, 1080, 1920)
        self.assertIn('trim=end_frame=62', text)
        self.assertIn('atrim=end_sample=99200', text)

    def test_subframe_video_segment_rejected(self):
        plan = fixture(); plan['segments'][0]['final_out_s'] = .001
        with self.assertRaisesRegex(ValueError, 'below one frame'):
            graph(plan, 1080, 1920)

    def test_seek_offset_changes_only_source_not_final_time(self):
        plan = fixture()
        for segment in plan['segments']:
            segment['source_in_s'] += 600
            segment['source_out_s'] += 600
        text = graph(plan, 1080, 1920, input_start_s=600)
        self.assertIn('trim=start=0.100000000:end=1.115000000', text)
        self.assertIn('atrim=start_sample=4800:end_sample=53520', text)
        self.assertIn('trim=end_frame=30', text)


if __name__ == '__main__':
    unittest.main()
