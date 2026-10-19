"""Shared Qwen narration executor: Flash or enrolled VC, exact text and resumable audio."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
from urllib.request import Request, build_opener
from urllib.parse import urlsplit, urlunsplit
import wave

from bailian_media import ApiRejected, Client, NoRedirect, ROOT, api_base, config, file_hash, fingerprint, load, save
from bailian_asr import oss_url
from managed_job import lock

ENDPOINT = '/services/aigc/multimodal-generation/generation'
CHUNK_CHARS = 500
SAMPLE_RATE = 24000


def sentences_from_text(text):
    # Keep punctuation and whitespace; concatenation must reconstruct the input.
    pieces = re.split(r'(?<=[；;。！？!?\n])', text)
    result, prefix = [], ''
    for piece in pieces:
        if not piece:
            continue
        if not piece.strip():
            if result:
                result[-1]['text_zh'] += piece
            else:
                prefix += piece
        else:
            result.append({'id': f's{len(result) + 1:04d}', 'text_zh': prefix + piece})
            prefix = ''
    return result


def read_script(path):
    if path.suffix.lower() == '.json':
        raw = load(path)
        sentences = raw['sentences']
    else:
        sentences = sentences_from_text(path.read_text('utf-8-sig'))
    if not isinstance(sentences, list) or not sentences:
        raise ValueError('Script needs nonempty sentences or UTF-8 text')
    seen = set()
    for sentence in sentences:
        if not isinstance(sentence, dict):
            raise ValueError('Each sentence must contain id and text_zh')
        sid, text = sentence.get('id'), sentence.get('text_zh')
        if not isinstance(sid, str) or not sid.strip() or sid in seen:
            raise ValueError('Sentence IDs must be nonempty and unique')
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f'{sid}: text_zh must contain narration')
        seen.add(sid)
    return sentences


def chunk_text(text, limit=CHUNK_CHARS):
    chunks = []
    while text:
        end = min(limit, len(text))
        if end < len(text):
            boundaries = list(re.finditer(r'[，,；;。！？!?\s]', text[:end]))
            if boundaries and boundaries[-1].end() >= end // 2:
                end = boundaries[-1].end()
        piece, text = text[:end], text[end:]
        if piece.strip():
            chunks.append(piece)
        elif chunks:
            chunks[-1] += piece
        else:
            raise ValueError('Script starts with excessive whitespace; provide narration text')
    if any(len(chunk) > limit for chunk in chunks):
        raise ValueError('Excessive whitespace exceeds the TTS chunk limit')
    return chunks


def download_audio(url, path):
    try:
        # Qwen VC can return an HTTP OSS URL. Fetch the same official object over TLS only.
        parts = urlsplit(url)
        if parts.scheme == 'http':
            url = urlunsplit(parts._replace(scheme='https'))
        with build_opener(NoRedirect()).open(Request(oss_url(url)), timeout=120) as response:
            data = response.read(64 * 1024 * 1024 + 1)
        if not data or len(data) > 64 * 1024 * 1024:
            raise ValueError('Invalid audio size')
        temporary = path.with_suffix('.download')
        temporary.write_bytes(data)
        temporary.replace(path)
    except Exception:
        raise RuntimeError('TTS audio download failed; resume saved response, not paid synthesis') from None


def normalize_audio(ffmpeg, source, target):
    result = subprocess.run([ffmpeg, '-hide_banner', '-v', 'error', '-nostdin', '-y', '-i', str(source),
                             '-vn', '-ar', str(SAMPLE_RATE), '-ac', '1', '-c:a', 'pcm_s16le', str(target)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError('TTS audio normalization failed; retain response and resume')


def wav_frames(path):
    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, SAMPLE_RATE, 'NONE'):
            raise ValueError('Expected mono PCM16 24kHz audio')
        frames = audio.getnframes()
        if not frames:
            raise ValueError('TTS returned empty audio')
        # Verify that data is not truncated, without loading long narration into memory.
        remaining = frames
        while remaining:
            block = audio.readframes(min(remaining, SAMPLE_RATE))
            if not block or len(block) % 2:
                raise ValueError('Truncated TTS audio')
            remaining -= len(block) // 2
        return frames


def join_audio(parts, gap_s, output):
    gap_frames = round(gap_s * SAMPLE_RATE)
    temporary = output.with_suffix('.tmp.wav')
    offsets, cursor = [], 0
    with wave.open(str(temporary), 'wb') as joined:
        joined.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
        for index, part in enumerate(parts):
            count = wav_frames(part)
            if index and gap_frames:
                joined.writeframesraw(b'\0\0' * gap_frames)
                cursor += gap_frames
            offsets.append({'index': index, 'start_s': cursor / SAMPLE_RATE, 'end_s': (cursor + count) / SAMPLE_RATE})
            with wave.open(str(part), 'rb') as source:
                while block := source.readframes(SAMPLE_RATE):
                    joined.writeframesraw(block)
            cursor += count
    wav_frames(temporary)
    temporary.replace(output)
    return offsets


def run(script, out_dir, cfg, execute=False, model=None, voice=None, language_type=None, gap_s=0, ffmpeg=None, client=None, voice_record=None):
    if not math.isfinite(gap_s) or not 0 <= gap_s <= 5:
        raise ValueError('gap_s must be between 0 and 5 seconds')
    gap_s = int(gap_s) if gap_s == int(gap_s) else gap_s
    if script.resolve() in {out_dir.resolve() / name for name in ('voiceover.wav', 'script-sentences.json', 'tts-manifest.json')}:
        raise ValueError('TTS output must not overwrite the source script')
    configured_base = api_base(cfg) if cfg.get('DASHSCOPE_BASE_URL') else ''
    # Qwen3/Cherry uses the regional public speech endpoint. Select it before
    # submitting, without changing the shared ASR config or retrying another host.
    if not voice_record and configured_base:
        host = urlsplit(configured_base).hostname
        for suffix, speech_base in (
            ('.cn-beijing.maas.aliyuncs.com', 'https://dashscope.aliyuncs.com/api/v1'),
            ('.ap-southeast-1.maas.aliyuncs.com', 'https://dashscope-intl.aliyuncs.com/api/v1'),
        ):
            if host.endswith(suffix):
                cfg = {**cfg, 'DASHSCOPE_BASE_URL': speech_base}
                break
    sentences = read_script(script)
    text = ''.join(sentence['text_zh'] for sentence in sentences)
    profile = None
    if voice_record:
        if voice_record.resolve() in {out_dir.resolve() / name for name in ('voiceover.wav', 'script-sentences.json', 'tts-manifest.json')}:
            raise ValueError('TTS output must not overwrite the voice record')
        from bailian_voice import load_voice_record
        profile = load_voice_record(voice_record, cfg)
        if (model and model != profile['model']) or (voice and voice != profile['voice']):
            raise ValueError('Explicit model/voice must match the cloned voice record')
        model, voice = profile['model'], profile['voice']
    chunks = chunk_text(text, 200 if profile else CHUNK_CHARS)
    if ''.join(chunks) != text:
        raise ValueError('TTS chunks do not reconstruct source text')
    options = {'model': model or os.environ.get('BAILIAN_TTS_MODEL') or cfg.get('BAILIAN_TTS_MODEL') or 'qwen3-tts-flash',
               'voice': voice or os.environ.get('BAILIAN_TTS_VOICE') or cfg.get('BAILIAN_TTS_VOICE') or 'Cherry',
               'language_type': language_type or os.environ.get('BAILIAN_TTS_LANGUAGE') or cfg.get('BAILIAN_TTS_LANGUAGE') or 'Chinese'}
    if not profile and not re.fullmatch(r'qwen3-tts-flash(?:-\d{4}-\d{2}-\d{2})?', options['model']):
        raise ValueError('Use --voice-record for cloned VC synthesis; otherwise this adapter supports non-realtime Qwen3-TTS-Flash only')
    base = api_base(cfg) if cfg.get('DASHSCOPE_BASE_URL') else ''
    signature = fingerprint({'schema': 1, 'text': text, 'options': options, 'base': base, 'gap_s': gap_s})
    # Older CLI runs fingerprinted 0.0 while direct calls used 0. They are the same audio.
    legacy_signature = fingerprint({'schema': 1, 'text': text, 'options': options, 'base': base, 'gap_s': float(gap_s)})
    signatures = [signature, legacy_signature]
    if configured_base != base:
        signatures.extend(fingerprint({'schema': 1, 'text': text, 'options': options,
                                       'base': configured_base, 'gap_s': gap}) for gap in (gap_s, float(gap_s)))
    script_revision = 'script-' + fingerprint(sentences)[:20]
    plan = {'signature': signature, **options, 'base_url': base, 'endpoint': ENDPOINT,
            'text_chars': len(text), 'sentences': len(sentences),
            'chunks': len(chunks), 'chunk_chars': [len(chunk) for chunk in chunks], 'gap_s': gap_s,
            'out_dir': str(out_dir), 'key_configured': bool(cfg.get('DASHSCOPE_API_KEY', '').strip()), 'script_revision': script_revision}
    if profile:
        plan['voice_record'] = str(voice_record)
    if not execute:
        return {'mode': 'dry_run', **plan}
    api_base(cfg)
    if not ffmpeg:
        raise ValueError('Prepared workspace FFmpeg is required')
    client = client or Client(cfg)
    with lock(out_dir / '.tts-lock'):
        final, manifest_path = out_dir / 'voiceover.wav', out_dir / 'tts-manifest.json'
        save(out_dir / 'script-sentences.json', {'revision': script_revision, 'sentences': sentences})
        if manifest_path.is_file():
            manifest = load(manifest_path)
            if manifest.get('signature') in signatures and final.is_file() and file_hash(final) == manifest.get('audio_sha256'):
                wav_frames(final)
                manifest.update(plan)
                save(manifest_path, manifest)
                return {**manifest, 'reused': True}
        cache = out_dir / '.tts-cache' / signature
        cache = next((out_dir / '.tts-cache' / sig for sig in signatures
                      if (out_dir / '.tts-cache' / sig).exists()), cache)
        cache.mkdir(parents=True, exist_ok=True)
        details, parts = [], []
        for index, chunk in enumerate(chunks):
            state_path, part = cache / f'{index:04d}.json', cache / f'{index:04d}.wav'
            state = load(state_path) if state_path.is_file() else {}
            if state.get('status') == 'complete' and part.is_file() and file_hash(part) == state.get('audio_sha256'):
                wav_frames(part)
                details.append({**state['detail'], 'reused': True})
                parts.append(part)
                continue
            if state.get('status') in ('submitting', 'uncertain', 'rejected'):
                raise RuntimeError(f'Chunk {index} has an unresolved prior request; inspect provider outcome before retrying')
            if not state:
                save(state_path, {'status': 'submitting'})
                try:
                    response = client.call('POST', ENDPOINT, {'model': options['model'],
                                           'input': {'text': chunk, 'voice': options['voice'], 'language_type': options['language_type']}})
                    # Save the received response before interpreting/downloading it. A local adapter
                    # failure must not lose a paid result or be mistaken for an uncertain API call.
                    save(cache / f'{index:04d}.response.json', response)
                    state = {'status': 'received', 'audio_url': response.get('output', {}).get('audio', {}).get('url'), 'request_id': response.get('request_id'),
                             'usage': response.get('usage', {})}
                    save(state_path, state)
                except ApiRejected as exc:
                    save(state_path, {'status': 'rejected', 'error': str(exc)})
                    raise RuntimeError(f'Chunk {index} rejected: {exc}; fix configuration before an authorized retry') from None
                except Exception:
                    save(state_path, {'status': 'uncertain'})
                    raise RuntimeError(f'Chunk {index} synthesis failed or outcome uncertain; do not resubmit blindly') from None
            raw = cache / f'{index:04d}.audio'
            if not raw.is_file() or not raw.stat().st_size:
                if not state.get('audio_url'):
                    raise RuntimeError(f'Chunk {index} response has no audio URL; inspect saved response, do not resynthesize')
                download_audio(state['audio_url'], raw)
            normalize_audio(ffmpeg, raw, part)
            count = wav_frames(part)
            detail = {'index': index, 'chars': len(chunk), 'duration_s': count / SAMPLE_RATE,
                      'request_id': state.get('request_id'), 'usage': state.get('usage', {}), 'reused': False}
            state.update(status='complete', audio_sha256=file_hash(part), detail=detail)
            save(state_path, state)
            details.append(detail)
            parts.append(part)
        offsets = join_audio(parts, gap_s, final)
        manifest = {**plan, 'status': 'completed', 'audio': str(final), 'audio_sha256': file_hash(final),
                    'duration_s': wav_frames(final) / SAMPLE_RATE, 'sample_rate': SAMPLE_RATE,
                    'chunks_detail': [{**detail, **offset} for detail, offset in zip(details, offsets)],
                    'script': str(out_dir / 'script-sentences.json'), 'word_timestamps': 'not_provided'}
        save(manifest_path, manifest)
        return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--script', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--env', type=Path, default=ROOT / '.env')
    parser.add_argument('--voice-record', type=Path)
    for name in ('model', 'voice', 'language-type'):
        parser.add_argument('--' + name)
    parser.add_argument('--gap-s', type=float, default=0)
    parser.add_argument('--ffmpeg')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = run(args.script, args.out_dir, config(args.env), args.execute,
                     args.model, args.voice, args.language_type, args.gap_s, args.ffmpeg, voice_record=args.voice_record)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, wave.Error):
        # Downloads can contain signed URLs. Never echo raw transport exceptions.
        import sys
        exc = sys.exception()
        error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else 'Local script/audio I/O failed'
        print(json.dumps({'status': 'fail', 'error': error}, ensure_ascii=False))
        return 1
    summary = {key: value for key, value in result.items() if key != 'chunks_detail'}
    if args.execute:
        summary['manifest'] = str(args.out_dir / 'tts-manifest.json')
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
