"""Offline provider contracts; never submits a real paid request."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import bailian_asr as asr
import prepare_asr_audio

CFG = {'DASHSCOPE_BASE_URL': 'https://example.cn-beijing.maas.aliyuncs.com/api/v1'}
RAW = {'transcripts': [{'channel_id': 0, 'sentences': [{'sentence_id': 1, 'text': '千万不要，三五十万。',
    'words': [{'text': '千万', 'begin_time': 123, 'end_time': 578},
              {'text': '不要', 'begin_time': 590, 'end_time': 1034},
              {'text': '三五十万', 'begin_time': 1200, 'end_time': 2109}]}]}]}
PENDING = {'output': {'task_id': 'asr-123', 'task_status': 'PENDING'}}
DONE = {'output': {'task_status': 'SUCCEEDED', 'results': [
    {'subtask_status': 'SUCCEEDED', 'transcription_url': 'https://result.oss.aliyuncs.com/result.json'}]}}

class Client:
    def __init__(self, replies): self.replies = iter(replies); self.calls = []
    def call(self, *args):
        self.calls.append(args)
        r = next(self.replies)
        if isinstance(r, Exception): raise r
        return r

def extract(cmd, **kwargs): Path(cmd[-1]).write_bytes(b'offline audio fixture')
def upload(*args): return 'oss://temporary/audio.wav'

class Tests(unittest.TestCase):
    def test_punctuation_only_provider_tokens_do_not_create_fake_tiny_speech(self):
        raw=copy.deepcopy(RAW)
        raw['transcripts'][0]['sentences'].append({'sentence_id':2,'text':'”. ',
            'words':[{'text':'”','begin_time':2200,'end_time':2210}]})
        self.assertEqual(len(asr.normalize(raw)['words']),3)

    def test_fun_asr_preserves_word_contract_and_uses_its_supported_parameters(self):
        with tempfile.TemporaryDirectory() as t, patch.object(prepare_asr_audio.subprocess,'run',side_effect=extract):
            p=Path(t)/'input.wav'; p.write_bytes(b'fixture'); client=Client([PENDING,DONE])
            asr.run(p,Path(t)/'out',{**CFG,'BAILIAN_ASR_MODEL':'fun-asr'},True,
                    client=client,uploader=upload,fetch=lambda _:RAW,poll_seconds=0)
            body=client.calls[0][2]
            self.assertEqual(body['model'],'fun-asr')
            self.assertEqual(body['parameters']['language_hints'],['zh'])
            self.assertNotIn('timestamp_alignment_enabled',body['parameters'])
            self.assertEqual(asr.load(Path(t)/'out/transcript.source.json')['model'],'fun-asr')
    def test_millisecond_precision_offset_ids(self):
        d = asr.normalize(RAW, 10)
        self.assertEqual(d['words'][0]['start'], 10.123)
        self.assertEqual(d['words'][-1]['end'], 12.109)
        self.assertEqual(d['words'][0]['provider_begin_ms'], 123)
        self.assertEqual(d['utterances'][0]['word_ids'], [w['id'] for w in d['words']])
    def test_no_fabricated_word_times(self):
        r = copy.deepcopy(RAW); r['transcripts'][0]['sentences'][0].pop('words')
        with self.assertRaisesRegex(ValueError, 'Missing word'): asr.normalize(r)
    def test_bad_time_rejected_overlap_flagged(self):
        r = copy.deepcopy(RAW); r['transcripts'][0]['sentences'][0]['words'][1]['begin_time'] = 500
        self.assertTrue(asr.normalize(r)['findings'])
        r['transcripts'][0]['sentences'][0]['words'][0]['begin_time'] = -1
        with self.assertRaises(ValueError): asr.normalize(r)
    def test_srt_and_readable_preserve_source(self):
        with tempfile.TemporaryDirectory() as t:
            asr.write_outputs(RAW, t)
            self.assertIn('00:00:00,123 --> 00:00:02,109', (Path(t)/'transcript.source.srt').read_text(encoding='utf-8'))
            self.assertIn('c0_s00000_w0000', (Path(t)/'transcript.readable.md').read_text(encoding='utf-8'))
    def test_no_key_no_upload(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'input.mp4'; p.write_bytes(b'x'); out = Path(t)/'out'
            self.assertEqual(asr.run(p, out, CFG)['mode'], 'dry_run')
            with self.assertRaisesRegex(ValueError, 'empty'): asr.run(p, out, CFG, True)
            self.assertFalse(out.exists())
    def test_submit_resume_and_disfluencies(self):
        with tempfile.TemporaryDirectory() as t, patch.object(prepare_asr_audio.subprocess, 'run', side_effect=extract):
            p = Path(t)/'input.mp4'; p.write_bytes(b'x'); out = Path(t)/'out'
            c = Client([PENDING, PENDING])
            with self.assertRaisesRegex(RuntimeError, 'pending'):
                asr.run(p, out, CFG, True, client=c, uploader=upload, max_polls=1)
            request = c.calls[0]
            self.assertFalse(request[2]['parameters']['disfluency_removal_enabled'])
            self.assertTrue(request[2]['parameters']['diarization_enabled'])
            self.assertNotIn('speaker_count', request[2]['parameters'])
            self.assertTrue(request[3]); self.assertTrue(request[4])
            resumed = Client([DONE])
            asr.run(p, out, CFG, True, client=resumed, fetch=lambda _: RAW)
            self.assertEqual(resumed.calls[0][0], 'GET')
            asr.run(p, out, CFG, True, client=resumed)
            self.assertEqual(len(resumed.calls), 1)
    def test_uncertain_submission_never_resubmits(self):
        with tempfile.TemporaryDirectory() as t, patch.object(prepare_asr_audio.subprocess, 'run', side_effect=extract):
            p = Path(t)/'input.mp4'; p.write_bytes(b'x'); c = Client([RuntimeError('timeout')])
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    asr.run(p, Path(t)/'out', CFG, True, client=c, uploader=upload)
            self.assertEqual(len(c.calls), 1)
    def test_download_failure_gets_task_again(self):
        with tempfile.TemporaryDirectory() as t, patch.object(prepare_asr_audio.subprocess, 'run', side_effect=extract):
            p = Path(t)/'input.mp4'; p.write_bytes(b'x'); c = Client([PENDING, DONE, DONE])
            with self.assertRaises(RuntimeError):
                asr.run(p, Path(t)/'out', CFG, True, client=c, uploader=upload,
                        fetch=lambda _: (_ for _ in ()).throw(RuntimeError('expired')))
            asr.run(p, Path(t)/'out', CFG, True, client=c, fetch=lambda _: RAW)
            self.assertEqual([r[0] for r in c.calls], ['POST', 'GET', 'GET'])

    def test_speaker_and_format_changes_do_not_reuse_or_resubmit(self):
        with tempfile.TemporaryDirectory() as t, patch.object(prepare_asr_audio.subprocess, 'run', side_effect=extract):
            p = Path(t)/'input.mp4'; p.write_bytes(b'x'); out = Path(t)/'out'
            c = Client([PENDING, DONE])
            asr.run(p, out, CFG, True, client=c, uploader=upload, fetch=lambda _: RAW, speaker_count=2)
            self.assertEqual(c.calls[0][2]['parameters']['speaker_count'], 2)
            for options in ({'speaker_count': 3}, {'speaker_count': 2, 'audio_format': 'mp3'}):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    asr.run(p, out, CFG, True, client=c, **options)
            self.assertEqual(len(c.calls), 2)

    def test_legacy_cache_and_invalid_speaker_count(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'input.mp4'; p.write_bytes(b'x'); out = Path(t)/'out'; out.mkdir()
            sig = asr.fingerprint({'source_sha256': asr.file_hash(p), 'base': CFG['DASHSCOPE_BASE_URL'], 'model': 'paraformer-v2', 'audio': 'mono16k'})
            asr.save(out/'asr.state.json', {'request_hash': sig, 'status': 'downloaded'})
            asr.save(out/'transcript.provider.json', RAW)
            c = Client([])
            asr.run(p, out, CFG, True, client=c, diarization=False)
            self.assertFalse(c.calls)
            for options in ({'speaker_count': 1}, {'speaker_count': 2, 'diarization': False}):
                with self.assertRaises(ValueError): asr.run(p, out, CFG, **options)

    def test_mp3_request_mime_and_upload_has_no_bearer(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
        policy = {'data': {'max_file_size_mb': 256, 'upload_dir': 'test', 'oss_access_key_id': 'fake',
                          'signature': 'fake', 'policy': 'fake', 'x_oss_object_acl': 'private',
                          'x_oss_forbid_overwrite': 'true', 'upload_host': 'https://test.oss.aliyuncs.com'}}
        with tempfile.TemporaryDirectory() as t, patch.object(asr, 'build_opener') as opener:
            p = Path(t)/'analysis.mp3'; p.write_bytes(b'mp3 fixture')
            opener.return_value.open.return_value = Response()
            self.assertEqual(asr.upload(p, Client([policy]), 'paraformer-v2'), 'oss://test/analysis.mp3')
            req = opener.return_value.open.call_args.args[0]
            self.assertIn(b'Content-Type: audio/mpeg', req.data)
            self.assertNotIn('Authorization', req.headers)

    def test_prepare_mp3_encoding_and_original_preserved(self):
        with tempfile.TemporaryDirectory() as t, patch.object(prepare_asr_audio.subprocess, 'run', side_effect=extract) as ff:
            p = Path(t)/'input.mp4'; p.write_bytes(b'original')
            result = prepare_asr_audio.prepare(p, Path(t)/'analysis.mp3')
            self.assertEqual(result['mime_type'], 'audio/mpeg')
            self.assertIn('libmp3lame', ff.call_args.args[0])
            self.assertEqual(p.read_bytes(), b'original')
            with self.assertRaises(ValueError): prepare_asr_audio.prepare(p, p)

    def test_mp3_full_adapter_roundtrip(self):
        with tempfile.TemporaryDirectory() as t, patch.object(prepare_asr_audio.subprocess, 'run', side_effect=extract):
            p = Path(t)/'recording.mp3'; p.write_bytes(b'original mp3')
            out = Path(t)/'asr'; c = Client([PENDING, DONE]); uploaded = []
            def capture(audio, *args):
                uploaded.append(audio.name)
                return 'oss://temporary/analysis.mp3'
            asr.run(p, out, CFG, True, client=c, uploader=capture, fetch=lambda _: RAW, audio_format='mp3')
            self.assertEqual(uploaded, ['analysis.mp3'])
            self.assertTrue((out/'subtitles.source.srt').is_file())
            asr.run(p, out, CFG, True, client=c, audio_format='mp3')
            self.assertEqual(len(c.calls), 2)

if __name__ == '__main__': unittest.main(verbosity=2)
