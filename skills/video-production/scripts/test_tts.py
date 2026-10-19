import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import wave

import bailian_tts as tts

CFG = {'DASHSCOPE_BASE_URL': 'https://dashscope.aliyuncs.com/api/v1', 'DASHSCOPE_API_KEY': 'offline-fixture'}
RESPONSE = {'request_id': 'test-request', 'output': {'audio': {'url': 'https://result.oss.aliyuncs.com/audio.wav?signature=private'}},
            'usage': {'characters': 10}}


class Client:
    def __init__(self, replies=None):
        self.calls = []
        self.replies = iter(replies) if replies is not None else None

    def call(self, *args):
        self.calls.append(args)
        reply = next(self.replies) if self.replies else RESPONSE
        if isinstance(reply, Exception):
            raise reply
        return reply


def audio_fixture(url, path):
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x01\x00' * 2400)


class TtsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='video-tts-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'copy.txt'
        self.script.write_text('这是原文，保留条件和数字。', encoding='utf-8')
        self.out = self.root / 'voice'
        self.env_patch = patch.dict(os.environ, {'BAILIAN_TTS_MODEL': '', 'BAILIAN_TTS_VOICE': '', 'BAILIAN_TTS_LANGUAGE': ''})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def run_tts(self, client, **kwargs):
        with patch.object(tts, 'download_audio', side_effect=audio_fixture), \
                patch.object(tts, 'normalize_audio', side_effect=lambda _, source, target: shutil.copyfile(source, target)):
            return tts.run(self.script, self.out, CFG, True, ffmpeg='offline-ffmpeg', client=client, **kwargs)

    def test_plain_text_sentences_and_chunks_reconstruct_exact_text(self):
        text = '\n第一句，条件不能删！\n' + '很长的原文' * 260 + '\n最后一句。\n'
        self.script.write_text(text, encoding='utf-8')
        sentences = tts.read_script(self.script)
        self.assertEqual(''.join(s['text_zh'] for s in sentences), text)
        self.assertEqual(sentences[0]['text_zh'], '\n第一句，条件不能删！\n')
        chunks = tts.chunk_text(text)
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(0 < len(c) <= 500 for c in chunks))

    def test_json_ids_preserved_and_invalid_ids_rejected(self):
        script = self.root / 'script.json'
        sentences = [{'id': 'argument-1', 'text_zh': '不是所有公司，只有符合条件的公司。'}]
        script.write_text(json.dumps({'sentences': sentences}, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(tts.read_script(script), sentences)
        script.write_text(json.dumps({'sentences': sentences * 2}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'unique'):
            tts.read_script(script)

    def test_dry_run_never_calls_provider_or_writes_outputs(self):
        with patch.object(tts, 'Client') as client:
            result = tts.run(self.script, self.out, {}, False)
            self.assertEqual(result['mode'], 'dry_run')
            self.assertFalse(result['key_configured'])
            client.assert_not_called()
        self.assertFalse(self.out.exists())
        with self.assertRaisesRegex(ValueError, 'empty'):
            tts.run(self.script, self.out, {**CFG, 'DASHSCOPE_API_KEY': ''}, True, ffmpeg='ffmpeg')
        self.assertFalse(self.out.exists())

    def test_request_shape_and_completed_audio_are_reused_without_paid_calls(self):
        first = Client()
        result = self.run_tts(first)
        body = first.calls[0][2]
        self.assertEqual(body, {'model': 'qwen3-tts-flash', 'input': {
            'text': self.script.read_text('utf-8'), 'voice': 'Cherry', 'language_type': 'Chinese'}})
        self.assertEqual(result['duration_s'], .1)
        self.assertEqual(result['word_timestamps'], 'not_provided')
        self.assertNotIn('signature=private', json.dumps(result))
        second = Client()
        self.assertTrue(self.run_tts(second)['reused'])
        self.assertEqual(second.calls, [])

    def test_changed_voice_or_source_cannot_reuse_wrong_audio(self):
        self.run_tts(Client())
        voice = Client()
        a = self.run_tts(voice, voice='OtherVoice')
        self.assertEqual(len(voice.calls), 1)
        self.script.write_text('另一份文案。', encoding='utf-8')
        changed = Client()
        b = self.run_tts(changed, voice='OtherVoice')
        self.assertEqual(len(changed.calls), 1)
        self.assertNotEqual(a['signature'], b['signature'])

    def test_resegmentation_updates_script_without_repaying_for_same_audio(self):
        self.script = self.root / 'source.json'
        self.script.write_text(json.dumps({'sentences': [{'id': 'old', 'text_zh': '原文，条件不变。'}]}), encoding='utf-8')
        first = self.run_tts(Client())
        self.script.write_text(json.dumps({'sentences': [{'id': 'new1', 'text_zh': '原文，'},
                                                        {'id': 'new2', 'text_zh': '条件不变。'}]}), encoding='utf-8')
        resumed = Client()
        second = self.run_tts(resumed)
        self.assertEqual(resumed.calls, [])
        self.assertEqual(first['signature'], second['signature'])
        self.assertNotEqual(first['script_revision'], second['script_revision'])
        self.assertEqual(second['sentences'], 2)
        self.assertEqual(json.loads((self.out / 'script-sentences.json').read_text('utf-8'))['sentences'][0]['id'], 'new1')

    def test_completed_chunk_resumes_download_failure_without_resynthesis(self):
        self.script.write_text('内容' * 375, encoding='utf-8')
        client = Client()
        calls = 0

        def fail_second(url, path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('offline download failed')
            audio_fixture(url, path)

        with patch.object(tts, 'download_audio', side_effect=fail_second), \
                patch.object(tts, 'normalize_audio', side_effect=lambda _, source, target: shutil.copyfile(source, target)):
            with self.assertRaises(RuntimeError):
                tts.run(self.script, self.out, CFG, True, ffmpeg='offline', client=client)
        self.assertEqual(len(client.calls), 2)
        resumed = Client()
        result = self.run_tts(resumed)
        self.assertEqual(resumed.calls, [])
        self.assertTrue(result['chunks_detail'][0]['reused'])
        self.assertAlmostEqual(result['duration_s'], .2)

    def test_uncertain_submission_is_not_automatically_retried(self):
        self.script.write_text('内容' * 375, encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'uncertain'):
            self.run_tts(Client([RESPONSE, TimeoutError('sensitive URL must not escape')]))
        resumed = Client()
        with self.assertRaisesRegex(RuntimeError, 'unresolved'):
            self.run_tts(resumed)
        self.assertEqual(resumed.calls, [])

    def test_rejected_submission_preserves_safe_error_and_does_not_loop(self):
        with self.assertRaisesRegex(RuntimeError, 'HTTP 400'):
            self.run_tts(Client([tts.ApiRejected('API rejected request (HTTP 400)')]))
        resumed = Client()
        with self.assertRaisesRegex(RuntimeError, 'unresolved'):
            self.run_tts(resumed)
        self.assertEqual(resumed.calls, [])

    def test_configured_voice_and_cli_override_are_used(self):
        cfg = {**CFG, 'BAILIAN_TTS_VOICE': 'ConfiguredVoice', 'BAILIAN_TTS_LANGUAGE': 'Auto'}
        result = tts.run(self.script, self.out, cfg)
        self.assertEqual(result['voice'], 'ConfiguredVoice')
        self.assertEqual(result['language_type'], 'Auto')
        result = tts.run(self.script, self.out, cfg, voice='ExplicitVoice')
        self.assertEqual(result['voice'], 'ExplicitVoice')

    def test_workspace_tts_uses_regional_speech_host_without_changing_other_services(self):
        cfg = {**CFG, 'DASHSCOPE_BASE_URL': 'https://workspace.cn-beijing.maas.aliyuncs.com/api/v1'}
        client = Client()
        with patch.object(tts, 'Client', return_value=client) as factory, \
                patch.object(tts, 'download_audio', side_effect=audio_fixture), \
                patch.object(tts, 'normalize_audio', side_effect=lambda _, source, target: shutil.copyfile(source, target)):
            result = tts.run(self.script, self.out, cfg, True, ffmpeg='offline')
        self.assertEqual(factory.call_args.args[0]['DASHSCOPE_BASE_URL'], CFG['DASHSCOPE_BASE_URL'])
        self.assertEqual(cfg['DASHSCOPE_BASE_URL'], 'https://workspace.cn-beijing.maas.aliyuncs.com/api/v1')
        self.assertEqual(client.calls[0][:2], ('POST', '/services/aigc/multimodal-generation/generation'))
        self.assertEqual((result['model'], result['voice']), ('qwen3-tts-flash', 'Cherry'))
        self.assertEqual(result['base_url'], CFG['DASHSCOPE_BASE_URL'])
        self.assertEqual(result['endpoint'], client.calls[0][1])
        # Moving the same effective URL from the shared field to the TTS field is not new audio.
        resumed = Client()
        self.assertTrue(self.run_tts(resumed)['reused'])
        self.assertEqual(resumed.calls, [])

    def test_speech_host_selection_is_regional_and_validates_before_calling(self):
        for base, expected in (
            ('https://workspace.ap-southeast-1.maas.aliyuncs.com/api/v1', 'https://dashscope-intl.aliyuncs.com/api/v1'),
            ('https://dashscope.aliyuncs.com/api/v1', 'https://dashscope.aliyuncs.com/api/v1'),
            ('https://dashscope-intl.aliyuncs.com/api/v1', 'https://dashscope-intl.aliyuncs.com/api/v1'),
        ):
            with self.subTest(base=base):
                result = tts.run(self.script, self.out, {**CFG, 'DASHSCOPE_BASE_URL': base})
                self.assertEqual(result['base_url'], expected)
        with patch.object(tts, 'Client') as factory:
            with self.assertRaisesRegex(ValueError, 'official Bailian HTTPS'):
                tts.run(self.script, self.out, {**CFG, 'DASHSCOPE_BASE_URL': 'http://untrusted.example/api/v1'},
                        True, ffmpeg='offline')
            factory.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_other_protocols_are_rejected_instead_of_switching_model_or_voice(self):
        for model in ('qwen-audio-3.1-tts-flash', 'qwen3-tts-flash-realtime'):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, 'Qwen3-TTS-Flash'):
                tts.run(self.script, self.out, CFG, model=model)
        snapshot = tts.run(self.script, self.out, CFG, model='qwen3-tts-flash-2025-11-27')
        self.assertEqual(snapshot['voice'], 'Cherry')
        self.assertFalse(self.out.exists())

    def test_workspace_cache_migration_reuses_paid_audio_and_saved_chunks(self):
        from bailian_media import fingerprint, load, save
        first = self.run_tts(Client())
        workspace_base = 'https://workspace.cn-beijing.maas.aliyuncs.com/api/v1'
        old_signature = fingerprint({'schema': 1, 'text': self.script.read_text('utf-8'),
            'options': {key: first[key] for key in ('model', 'voice', 'language_type')},
            'base': workspace_base, 'gap_s': 0})
        cache = self.out / '.tts-cache'
        (cache / first['signature']).rename(cache / old_signature)
        manifest = load(self.out / 'tts-manifest.json')
        manifest['signature'] = old_signature
        save(self.out / 'tts-manifest.json', manifest)
        cfg = {**CFG, 'DASHSCOPE_BASE_URL': workspace_base}
        resumed = Client()
        result = tts.run(self.script, self.out, cfg, True, ffmpeg='offline', client=resumed)
        self.assertTrue(result['reused'])
        (self.out / 'voiceover.wav').unlink()
        result = tts.run(self.script, self.out, cfg, True, ffmpeg='offline', client=resumed)
        self.assertEqual(tts.wav_frames(Path(result['audio'])), 2400)
        self.assertEqual(resumed.calls, [])

    def test_regional_host_change_does_not_bypass_unresolved_workspace_request(self):
        from bailian_media import fingerprint, save
        workspace_base = 'https://workspace.cn-beijing.maas.aliyuncs.com/api/v1'
        cfg = {**CFG, 'DASHSCOPE_BASE_URL': workspace_base}
        old_signature = fingerprint({'schema': 1, 'text': self.script.read_text('utf-8'),
            'options': {'model': 'qwen3-tts-flash', 'voice': 'Cherry', 'language_type': 'Chinese'},
            'base': workspace_base, 'gap_s': 0.0})
        state_path = self.out / '.tts-cache' / old_signature / '0000.json'
        for status in ('uncertain', 'submitting', 'rejected'):
            save(state_path, {'status': status})
            resumed = Client()
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, 'unresolved'):
                tts.run(self.script, self.out, cfg, True, ffmpeg='offline', client=resumed)
            self.assertEqual(resumed.calls, [])

    def test_corrupt_final_rebuilds_from_valid_cached_chunks(self):
        self.run_tts(Client())
        (self.out / 'voiceover.wav').write_bytes(b'corrupt')
        resumed = Client()
        result = self.run_tts(resumed)
        self.assertEqual(resumed.calls, [])
        self.assertEqual(tts.wav_frames(Path(result['audio'])), 2400)

    def test_input_is_not_overwritten_and_gap_is_validated(self):
        source = self.root / 'script-sentences.json'
        source.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            tts.run(source, self.root, CFG)
        for gap in (-1, float('nan'), 6):
            with self.subTest(gap=gap), self.assertRaisesRegex(ValueError, 'gap_s'):
                tts.run(self.script, self.out, CFG, gap_s=gap)

    def test_pcm_join_has_exact_offsets_gap_and_original_samples(self):
        parts = [self.root / f'{i}.wav' for i in range(2)]
        for part in parts:
            audio_fixture('', part)
        output = self.root / 'joined.wav'
        offsets = tts.join_audio(parts, .05, output)
        self.assertEqual(tts.wav_frames(output), 6000)
        self.assertAlmostEqual(offsets[1]['start_s'], .15)
        with wave.open(str(output), 'rb') as audio:
            self.assertEqual(audio.readframes(6000), b'\x01\x00' * 2400 + b'\0\0' * 1200 + b'\x01\x00' * 2400)

    def test_shared_ffmpeg_normalizes_real_audio_without_network(self):
        media_tools = os.environ.get('VIDEO_PRODUCTION_TOOLS_JSON')
        if not media_tools:
            self.skipTest('Set VIDEO_PRODUCTION_TOOLS_JSON for local media integration')
        ffmpeg = json.loads(Path(media_tools).read_text('utf-8'))['ffmpeg']
        with patch.object(tts, 'download_audio', side_effect=audio_fixture):
            result = tts.run(self.script, self.out, CFG, True, ffmpeg=ffmpeg, client=Client())
        self.assertEqual(tts.wav_frames(Path(result['audio'])), 2400)

    def test_cloned_voice_selects_bound_model_and_reuses_audio(self):
        from bailian_voice import CLONE_MODEL, provider_scope
        profile = self.root / 'voice-record.json'
        profile.write_text(json.dumps({'schema_version': 1, 'status': 'ready', 'model': CLONE_MODEL,
            'voice': 'qwen-tts-vc-fixture-123', 'provider_scope': provider_scope(CFG)}), encoding='utf-8')
        client = Client()
        result = self.run_tts(client, voice_record=profile)
        self.assertEqual(client.calls[0][2]['model'], CLONE_MODEL)
        self.assertEqual(client.calls[0][2]['input']['voice'], 'qwen-tts-vc-fixture-123')
        resumed = Client()
        self.assertTrue(self.run_tts(resumed, voice_record=profile)['reused'])
        self.assertEqual(resumed.calls, [])
        with self.assertRaisesRegex(ValueError, 'must match'):
            tts.run(self.script, self.out, CFG, model='qwen3-tts-flash', voice_record=profile)
        self.assertEqual(result['voice_record'], str(profile))
        with self.assertRaisesRegex(ValueError, 'overwrite the voice record'):
            tts.run(self.script, self.root, CFG, voice_record=self.root / 'tts-manifest.json')

    def test_vc_requires_profile_and_uses_shorter_chunks(self):
        from bailian_voice import CLONE_MODEL, provider_scope
        with self.assertRaisesRegex(ValueError, 'voice-record'):
            tts.run(self.script, self.out, CFG, model=CLONE_MODEL, voice='qwen-tts-vc-fixture')
        profile = self.root / 'voice-record.json'
        profile.write_text(json.dumps({'schema_version': 1, 'status': 'ready', 'model': CLONE_MODEL,
            'voice': 'qwen-tts-vc-fixture-123', 'provider_scope': provider_scope(CFG)}), encoding='utf-8')
        self.script.write_text('新的旁白内容' * 100, encoding='utf-8')
        result = tts.run(self.script, self.out, CFG, voice_record=profile)
        self.assertTrue(all(count <= 200 for count in result['chunk_chars']))

    def test_http_official_audio_url_upgrades_to_https_and_unknown_host_is_rejected(self):
        from urllib.parse import urlsplit
        import io
        opener = unittest.mock.MagicMock()
        opener.open.return_value.__enter__.return_value = io.BytesIO(b'fixture')
        with patch.object(tts, 'build_opener', return_value=opener):
            tts.download_audio('http://result.oss.aliyuncs.com/voice.wav?signature=private', self.root / 'download.audio')
            self.assertEqual(urlsplit(opener.open.call_args.args[0].full_url).scheme, 'https')
            with self.assertRaises(RuntimeError):
                tts.download_audio('http://untrusted.example/voice.wav', self.root / 'other.audio')
            self.assertEqual(opener.open.call_count, 1)

    def test_unexpected_received_response_is_saved_and_never_repaid(self):
        client = Client([{'request_id': 'received-id', 'output': {}, 'usage': {'characters': 20}}])
        with self.assertRaisesRegex(RuntimeError, 'no audio URL'):
            self.run_tts(client)
        response_files = list((self.out / '.tts-cache').rglob('*.response.json'))
        self.assertEqual(json.loads(response_files[0].read_text('utf-8'))['request_id'], 'received-id')
        resumed = Client()
        with self.assertRaisesRegex(RuntimeError, 'no audio URL'):
            self.run_tts(resumed)
        self.assertEqual(resumed.calls, [])

    def test_integer_and_float_gaps_reuse_legacy_cache_without_paid_calls(self):
        from bailian_media import fingerprint, save, load
        first = self.run_tts(Client())
        legacy = fingerprint({'schema': 1, 'text': self.script.read_text('utf-8'),
            'options': {key: first[key] for key in ('model', 'voice', 'language_type')},
            'base': CFG['DASHSCOPE_BASE_URL'], 'gap_s': 0.0})
        cache = self.out / '.tts-cache'
        (cache / first['signature']).rename(cache / legacy)
        manifest = load(self.out / 'tts-manifest.json')
        manifest.update(signature=legacy, gap_s=0.0)
        save(self.out / 'tts-manifest.json', manifest)
        resumed = Client()
        self.assertTrue(self.run_tts(resumed, gap_s=0.0)['reused'])
        self.assertEqual(resumed.calls, [])
        # Final output missing: recover the old completed chunk cache, still no new POST.
        (self.out / 'voiceover.wav').unlink()
        self.run_tts(resumed)
        self.assertEqual(resumed.calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
