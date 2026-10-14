"""Local audio -> Bailian Paraformer word timestamps. Dry run unless --execute.

The provider state contains signed URLs: keep it local and exclude it from exports.
"""
import argparse
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import quote, urlparse
from urllib.request import Request, build_opener
import uuid
from bailian_media import ROOT, Client, NoRedirect, ApiRejected, api_base, config, load, save, fingerprint, file_hash
from prepare_asr_audio import prepare, MIME_TYPES
from export_subtitles import export


def oss_url(url):
    u = urlparse(url)
    if u.scheme != 'https' or not (u.hostname or '').endswith('.aliyuncs.com') or u.username or u.password or u.port:
        raise ValueError('Unexpected OSS host')
    return url


def upload(audio, client, model):
    mime = MIME_TYPES[audio.suffix.lower()]
    data = client.call('GET', '/uploads?action=getPolicy&model=' + quote(model, safe=''))['data']
    if audio.stat().st_size > float(data.get('max_file_size_mb', 256)) * 1024 * 1024:
        raise ValueError('Audio exceeds provider upload policy size; split with source offsets')
    key = data['upload_dir'].rstrip('/') + '/' + audio.name
    fields = {'OSSAccessKeyId': data['oss_access_key_id'], 'Signature': data['signature'],
              'policy': data['policy'], 'x-oss-object-acl': data['x_oss_object_acl'],
              'x-oss-forbid-overwrite': data['x_oss_forbid_overwrite'], 'key': key,
              'success_action_status': '200'}
    boundary = 'Bailian' + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{audio.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
    parts += [audio.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()]
    try:
        req = Request(oss_url(data['upload_host']), data=b''.join(parts),
                      headers={'Content-Type': 'multipart/form-data; boundary=' + boundary}, method='POST')
        with build_opener(NoRedirect()).open(req, timeout=180) as res:
            if res.status != 200:
                raise RuntimeError('Upload failed')
    except Exception:
        raise RuntimeError('Temporary audio upload failed; credentials and signed URL omitted') from None
    return 'oss://' + key


def result_json(url):
    try:
        with build_opener(NoRedirect()).open(Request(oss_url(url)), timeout=120) as res:
            raw = res.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError('ASR result too large')
        return json.loads(raw)
    except Exception:
        raise RuntimeError('ASR result download failed; resume saved task without resubmitting') from None


def normalize(raw, offset=0, revision='source-001'):
    if not math.isfinite(offset) or offset < 0:
        raise ValueError('Invalid source offset')
    words, sentences, findings = [], [], []
    seen_channels = set()
    for transcript in raw.get('transcripts', []):
        channel = transcript['channel_id']
        if channel in seen_channels:
            raise ValueError('Duplicate channel')
        seen_channels.add(channel)
        previous = -1
        for si, sentence in enumerate(transcript.get('sentences', [])):
            sid = f'c{channel}_s{si:05d}'
            ids = []
            spoken = lambda value: bool(value.strip(' \t\r\n，。！？；：、,.!?;:\"“”‘’「」『』（）()【】[]…'))
            if spoken(sentence.get('text', '')) and not sentence.get('words'):
                raise ValueError('Missing word timestamps; never interpolate sentence timing')
            for wi, word in enumerate(sentence.get('words', [])):
                text = word.get('text', '')
                if not spoken(text):
                    continue
                a, b = word.get('begin_time'), word.get('end_time')
                if not all(type(t) in (int, float) and math.isfinite(t) for t in [a, b]) or not 0 <= a < b:
                    raise ValueError('Invalid or missing word timestamp')
                wid = f'{sid}_w{wi:04d}'
                ids.append(wid)
                if a < previous:
                    findings.append({'word_id': wid, 'reason': 'overlap_or_nonmonotonic', 'status': 'review_required'})
                previous = b
                words.append({'id': wid, 'utterance_id': sid, 'channel_id': channel,
                              'speaker_id': word.get('speaker_id', sentence.get('speaker_id')),
                              'text': text, 'corrected_text': text, 'punctuation': word.get('punctuation', ''),
                              'provider_begin_ms': a, 'provider_end_ms': b,
                              'start': a / 1000 + offset, 'end': b / 1000 + offset,
                              'source_start_s': a / 1000 + offset, 'source_end_s': b / 1000 + offset})
            if ids:
                subset = words[-len(ids):]
                sentences.append({'id': sid, 'provider_sentence_id': sentence.get('sentence_id'),
                                  'text': sentence.get('text', ''), 'word_ids': ids,
                                  'start': subset[0]['start'], 'end': max(w['end'] for w in subset),
                                  'speaker_id': sentence.get('speaker_id')})
            elif spoken(sentence.get('text', '')):
                raise ValueError('Nonempty sentence has no usable timed words')
    if not words:
        raise ValueError('No timed speech; inspect audio/provider result')
    return {'revision': revision, 'provider': 'bailian-paraformer', 'source_offset_s': offset,
            'timestamp_unit': 'seconds', 'words': words, 'utterances': sentences,
            'findings': findings, 'quality_status': 'review_required'}


def stamp(seconds):
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f'{h:02}:{m:02}:{s:02},{ms:03}'


def write_outputs(raw, out, offset=0, model='paraformer-v2'):
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    doc = normalize(raw, offset)
    doc['model'] = model
    if model != 'paraformer-v2':
        doc['provider'] = 'bailian-' + model
    save(out / 'transcript.source.json', doc)
    index = {w['id']: w for w in doc['words']}
    lines, srt = ['# 原声转写（待校对；时间为原片秒）', ''], []
    for i, s in enumerate(doc['utterances'], 1):
        lines += [f"## {s['id']} · speaker {s['speaker_id']} · {s['start']:.3f}–{s['end']:.3f}", s['text'], '']
        lines += [f"- `{wid}` [{index[wid]['start']:.3f}, {index[wid]['end']:.3f}) {index[wid]['text']}" for wid in s['word_ids']]
        lines += ['']
        srt.append(f"{i}\n{stamp(s['start'])} --> {stamp(s['end'])}\n{s['text']}\n")
    (out / 'transcript.readable.md').write_text('\n'.join(lines), encoding='utf-8')
    (out / 'transcript.source.srt').write_text('\n'.join(srt), encoding='utf-8')
    export(doc, out)
    return {'status': 'transcribed_review_required', 'words': len(doc['words']), 'out': str(out)}


def run(media, out, cfg, execute=False, ffmpeg=None, max_polls=40, poll_seconds=15,
        client=None, uploader=upload, fetch=result_json, sleep=time.sleep,
        audio_format='wav', diarization=True, speaker_count=None):
    media, out = Path(media).resolve(), Path(out).resolve()
    if not media.is_file():
        raise ValueError('Input media missing')
    base = api_base(cfg)
    model = cfg.get('BAILIAN_ASR_MODEL', 'paraformer-v2')
    if model not in ('paraformer-v2', 'fun-asr'):
        raise ValueError('This word-timestamp adapter supports paraformer-v2 and fun-asr only')
    if audio_format not in ('wav', 'mp3'):
        raise ValueError('audio_format must be wav or mp3')
    if speaker_count is not None and (not diarization or type(speaker_count) is not int or not 2 <= speaker_count <= 100):
        raise ValueError('speaker_count requires diarization and an integer from 2 to 100; omit for automatic count')
    speaker_options = {'diarization_enabled': True} if diarization else {}
    if speaker_count is not None:
        speaker_options['speaker_count'] = speaker_count
    if not execute:
        return {'mode': 'dry_run', 'model': model, 'endpoint': base + '/services/audio/asr/transcription',
                'audio': 'mono 16k ' + audio_format, 'upload': 'temporary OSS via same Key',
                'word_timestamps': True, 'diarization_enabled': diarization, 'speaker_count': speaker_count,
                'key_configured': bool(cfg.get('DASHSCOPE_API_KEY', '').strip())}
    if max_polls < 1 or poll_seconds < 0:
        raise ValueError('Invalid polling bounds')
    client = client or Client(cfg)
    out.mkdir(parents=True, exist_ok=True)
    lock = out / '.asr.lock'
    with lock.open('x') as handle:
        handle.write(str(os.getpid()))
    try:
        sig = fingerprint({'source_sha256': file_hash(media), 'base': base, 'model': model,
                           'audio': 'mono16k' if audio_format == 'wav' else 'mono16k-mp3-128k', **speaker_options})
        state_path = out / 'asr.state.json'
        state = load(state_path) if state_path.exists() else None
        if state and state['request_hash'] != sig:
            raise ValueError('Input/model changed; use a new output directory')
        if state is None:
            audio = out / ('analysis.' + audio_format)
            prepare(media, audio, ffmpeg)
            uri = uploader(audio, client, model)
            parameters = {'channel_id': [0], 'language_hints': ['zh'], **speaker_options}
            if model == 'paraformer-v2':
                parameters.update(disfluency_removal_enabled=False,timestamp_alignment_enabled=True,language_hints=['zh','en'])
            body = {'model': model, 'input': {'file_urls': [uri]}, 'parameters': parameters}
            state = {'request_hash': sig, 'status': 'submission_unknown', 'model': model}
            save(state_path, state)
            try:
                response = client.call('POST', '/services/audio/asr/transcription', body, True, True)
            except ApiRejected:
                state['status'] = 'rejected'; save(state_path, state); raise
            if response.get('output', {}).get('task_id'):
                state.update(status='pending', task_id=response['output']['task_id'])
            save(state_path, state)
        if state['status'] in ['submission_unknown', 'rejected', 'failed']:
            raise RuntimeError('ASR submission failed or uncertain; do not automatically pay again')
        raw_path = out / 'transcript.provider.json'
        if not raw_path.exists():
            for poll in range(max_polls):
                result = client.call('GET', '/tasks/' + quote(state['task_id'], safe=''))
                status = result.get('output', {}).get('task_status')
                if status == 'SUCCEEDED':
                    entries = result['output'].get('results', [])
                    if len(entries) != 1 or entries[0].get('subtask_status') != 'SUCCEEDED':
                        state['status'] = 'failed'; save(state_path, state)
                        raise RuntimeError('ASR subtask failed or response shape unexpected')
                    raw = fetch(entries[0]['transcription_url'])
                    save(raw_path, raw)
                    state['status'] = 'downloaded'; save(state_path, state)
                    break
                if status in ['FAILED', 'CANCELED', 'UNKNOWN']:
                    state['status'] = 'failed'; save(state_path, state)
                    raise RuntimeError('ASR task failed; no automatic resubmission')
                if poll + 1 < max_polls:
                    sleep(poll_seconds)
            else:
                raise RuntimeError('ASR pending; resume the same output directory')
        return write_outputs(load(raw_path), out, model=model)
    finally:
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--media', type=Path)
    p.add_argument('--normalize', type=Path, help='Offline normalization of a provider JSON result')
    p.add_argument('--source-offset', type=float, default=0)
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--env', type=Path, default=ROOT / '.env')
    p.add_argument('--ffmpeg', default=None, help='Defaults to the resolved ffmpeg (see check_env.py/tools.json)')
    p.add_argument('--execute', action='store_true')
    p.add_argument('--audio-format', choices=['wav', 'mp3'], default='wav', help='Prepared upload format; WAV avoids lossy re-encoding')
    p.add_argument('--no-diarization', action='store_true', help='Disable speaker labels; also resumes legacy non-diarized tasks')
    p.add_argument('--speaker-count', type=int, help='Optional 2–100 hint; omit for automatic speaker count')
    p.add_argument('--max-polls', type=int, default=40)
    p.add_argument('--poll-seconds', type=float, default=15)
    a = p.parse_args()
    try:
        if a.normalize:
            result = write_outputs(load(a.normalize), a.out, a.source_offset)
        elif a.media and not a.source_offset:
            result = run(a.media, a.out, config(a.env), a.execute, a.ffmpeg, a.max_polls, a.poll_seconds,
                         audio_format=a.audio_format, diarization=not a.no_diarization, speaker_count=a.speaker_count)
        else:
            raise ValueError('Supply --media; source offsets are for --normalize of known excerpts')
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        message = str(e) if isinstance(e, (ValueError, RuntimeError)) else type(e).__name__ + ': check local input/state'
        print(json.dumps({'status': 'error', 'message': message}, ensure_ascii=False))
        raise SystemExit(1)
