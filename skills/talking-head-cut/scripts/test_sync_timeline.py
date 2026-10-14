"""Small real-FFmpeg checks; set VIDEO_TEST_FFMPEG to the approved executable."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import wave

import numpy as np

import sync_timeline as sync


def write_wav(path, samples, sr):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, sr, 0, 'NONE', ''))
        stream.writeframes(np.asarray(samples, dtype='<i2').tobytes())


def make_plan(source, ranges, sr):
    segments, cursor = [], 0
    for index, (start, end) in enumerate(ranges):
        size = round(end * sr) - round(start * sr)
        segments.append({'id': str(index), 'source_in_s': start, 'source_out_s': end,
                         'final_in_s': cursor / sr, 'final_out_s': (cursor + size) / sr})
        cursor += size
    return {'revision': 'test', 'source': {'path': str(source)}, 'sample_rate': sr,
            'duration_s': cursor / sr, 'segments': segments}


class TimelineSemanticsTests(unittest.TestCase):
    def test_short_and_ambiguous_timelines_are_not_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = make_plan(root / 'unused.wav', [(0, .5)], 48000)
            result = sync.run(plan, root / 'unused.mp4', 'unused-ffmpeg', root)
            self.assertEqual(result['status'], 'not_checked')
            self.assertEqual(result['reason'], 'too_short_for_four_independent_windows')
            plan['segments'][0]['final_in_s'] = .1
            path = root / 'plan.json'
            path.write_text(json.dumps(plan), encoding='utf-8')
            with redirect_stdout(io.StringIO()):
                code = sync.main(['--plan', str(path), '--media', 'unused.mp4',
                                  '--ffmpeg', 'unused-ffmpeg', '--out-dir', str(root)])
            self.assertEqual(code, 0)
            result = json.loads((root / 'sync.json').read_text(encoding='utf-8'))
            self.assertEqual(result['status'], 'not_checked')
            self.assertEqual(result['listening'], 'not_checked')


@unittest.skipUnless(os.environ.get('VIDEO_TEST_FFMPEG'), 'Set VIDEO_TEST_FFMPEG for real media tests')
class SyncMediaTests(unittest.TestCase):
    def setUp(self):
        self.ffmpeg = os.environ['VIDEO_TEST_FFMPEG']

    def test_reference_crosses_two_cuts_in_source_order_and_sample_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sr = 48000
            samples = np.random.default_rng(12).integers(-6000, 6000, sr * 7, dtype=np.int16)
            source = root / '原声.wav'
            write_wav(source, samples, sr)
            ranges = [(4.213, 4.714), (2.133, 2.834), (5.133, 5.734)]
            plan = make_plan(source, ranges, sr)
            _, segments, _ = sync._timeline(plan)
            expected = np.concatenate([samples[round(a * sr):round(b * sr)] for a, b in ranges])
            start, end = round(.4 * sr), round(1.5 * sr)
            ref = root / 'reference.wav'
            slices = sync._reference_window(segments, source, start, end, sr, self.ffmpeg,
                                           ref, root / 'piece.wav')
            _, actual = sync.speech_checks.pcm(ref)
            np.testing.assert_array_equal(actual, expected[start:end])
            self.assertEqual(len(slices), 3)

    def test_matching_delayed_and_silent_media_and_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sr = 16000
            noise = np.random.default_rng(8).normal(0, 5000, sr * 8)
            samples = np.convolve(noise, np.ones(5) / 5, mode='same').astype('<i2')
            source = root / 'source.wav'
            write_wav(source, samples, sr)
            # The first correlation window (.5-1s) itself crosses the .75s cut.
            ranges = [(0, .75), (3, 5.25), (1, 2), (6, 8)]
            plan = make_plan(source.name, ranges, sr)
            plan_path = root / 'plan.json'
            plan_path.write_text(json.dumps(plan), encoding='utf-8')
            reference = np.concatenate([samples[round(a * sr):round(b * sr)] for a, b in ranges])
            delay = round(.05 * sr)
            for name, audio, expected in [
                ('matched', reference, 'pass'),
                ('delayed', np.r_[np.zeros(delay, dtype='<i2'), reference[:-delay]], 'fail'),
                ('timestamp-delayed', reference, 'fail'),
                ('silent', np.zeros_like(reference), 'not_checked'),
                ('truncated', reference[:sr], 'not_checked'),
            ]:
                with self.subTest(name=name):
                    wav, media = root / f'{name}.wav', root / f'{name}.mp4'
                    write_wav(wav, audio, sr)
                    offset = ['-itsoffset', '0.05'] if name == 'timestamp-delayed' else []
                    sync._ffmpeg(self.ffmpeg, ['-f', 'lavfi', '-i', 'color=s=32x32:r=10',
                                              *offset, '-i', wav, '-map', '0:v', '-map', '1:a',
                                              '-c:v', 'libx264', '-preset', 'ultrafast',
                                              '-c:a', 'aac', '-b:a', '128k', '-shortest', media])
                    out_dir = root / name
                    with redirect_stdout(io.StringIO()):
                        code = sync.main(['--plan', str(plan_path), '--media', str(media),
                                          '--ffmpeg', self.ffmpeg, '--out-dir', str(out_dir)])
                    result = json.loads((out_dir / 'sync.json').read_text(encoding='utf-8'))
                    self.assertEqual(result['status'], expected, result)
                    self.assertEqual(result['listening'], 'not_checked')
                    self.assertEqual(code, int(expected == 'fail'))
                    self.assertEqual(len(result['windows']), 4)
                    self.assertEqual([p.name for p in out_dir.iterdir()], ['sync.json'])
                    if name in ('delayed', 'timestamp-delayed'):
                        self.assertTrue(all(abs(w['lag_ms'] - 50) < 2 for w in result['windows']), result)
            write_wav(source, np.zeros_like(samples), sr)
            result = sync.run(plan_path, root / 'matched.mp4', self.ffmpeg, root / 'silent-reference')
            self.assertEqual(result['status'], 'not_checked')
            self.assertTrue(all(w['reason'] == 'no_voiced_reference_candidate' for w in result['windows']))


if __name__ == '__main__':
    unittest.main()
