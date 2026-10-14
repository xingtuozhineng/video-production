"""Check four timeline PCM windows against final media; never a listening check."""
import argparse
import json
import math
from pathlib import Path
import subprocess
import tempfile
import wave

import speech_checks

CHECK_RATE = 16000
WINDOW_S = .5
SEARCH_MS = 100
TOLERANCE_MS = 20


def _timeline(plan):
    """Use the renderer's source sample bounds and reject ambiguous mappings."""
    sr = plan.get('sample_rate', 48000)
    if type(sr) is not int or sr <= 0:
        raise ValueError('sample_rate must be a positive integer')
    duration = plan['duration_s']
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('duration_s must be positive and finite')
    segments, cursor = [], 0
    for index, segment in enumerate(plan['segments']):
        values = [segment[k] for k in ('source_in_s', 'source_out_s', 'final_in_s', 'final_out_s')]
        if not all(type(v) in (float, int) and math.isfinite(v) and v >= 0 for v in values):
            raise ValueError('source/final seconds must be finite and nonnegative')
        si, so, fi, fo = [round(v * sr) for v in values]
        if fi != cursor or fo <= fi or so - si != fo - fi:
            raise ValueError('Only continuous, original-speed sample timelines are supported')
        for key, expected in (('final_in_sample', fi), ('final_out_sample', fo)):
            if key in segment and segment[key] != expected:
                raise ValueError(f'{key} disagrees with timeline seconds')
        segments.append({'id': segment.get('id', str(index)), 'si': si, 'so': so, 'fi': fi, 'fo': fo})
        cursor = fo
    if not segments or cursor > round(duration * sr):
        raise ValueError('segments are empty or extend beyond duration_s')
    # The renderer may pad the last video frame; do not select that silent tail.
    return sr, segments, cursor / sr


def _ffmpeg(ffmpeg, args):
    result = subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin',
                             '-y', *map(str, args)], capture_output=True, text=True,
                            encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError(f'ffmpeg ({result.returncode}): {result.stderr.strip()[-1200:]}')


def _decode(ffmpeg, media, start, end, sr, out):
    # Integer-second seek plus sample trim matches render_timeline's audio graph.
    seek = start // sr
    # Explicit -ss 0 can discard MP4/AAC priming metadata and shift the first
    # window by one codec frame. Start naturally when the anchor is zero.
    seek_args = ['-ss', seek] if seek else []
    _ffmpeg(ffmpeg, [*seek_args, '-t', math.ceil(end / sr) - seek + 1, '-i', media,
                    '-map', '0:a:0', '-vn', '-af',
                    f'aresample={sr},atrim=start_sample={start-seek*sr}:'
                    f'end_sample={end-seek*sr},asetpts=N/SR/TB',
                    '-ac', '1', '-ar', sr, '-c:a', 'pcm_s16le', out])
    with wave.open(str(out), 'rb') as stream:
        data = stream.readframes(stream.getnframes())
    if len(data) != (end - start) * 2:
        raise ValueError('decoded_window_too_short')
    return data


def _reference_window(segments, source, start, end, sr, ffmpeg, out, scratch):
    """Assemble every intersecting retained slice, including reordered source cuts."""
    slices = []
    with wave.open(str(out), 'wb') as stream:
        stream.setparams((1, 2, sr, 0, 'NONE', ''))
        for segment in segments:
            lo, hi = max(start, segment['fi']), min(end, segment['fo'])
            if hi <= lo:
                continue
            si = segment['si'] + lo - segment['fi']
            so = si + hi - lo
            stream.writeframes(_decode(ffmpeg, source, si, so, sr, scratch))
            slices.append({'segment_id': segment['id'], 'source_in_s': si / sr,
                           'source_out_s': so / sr, 'final_in_s': lo / sr, 'final_out_s': hi / sr})
    return slices


def _resample(ffmpeg, source, out):
    _ffmpeg(ffmpeg, ['-i', source, '-ac', '1', '-ar', CHECK_RATE, '-c:a', 'pcm_s16le', out])


def run(plan, media, ffmpeg, out_dir):
    """Return and save out_dir/sync.json. plan accepts a dict or a JSON path.

    Relative source paths use the plan file's directory (cwd for dicts). Only a
    confident out-of-tolerance lag is fail; unavailable evidence is not_checked.
    Temporary PCM files are removed even on decode failure. No model is called.
    """
    report = {'status': 'not_checked', 'listening': 'not_checked', 'windows': [],
              'method': 'timeline_pcm_cross_correlation', 'required_windows': 4,
              'window_s': WINDOW_S, 'search_ms': SEARCH_MS, 'tolerance_ms': TOLERANCE_MS,
              'positive_lag': 'encoded audio is late relative to timeline PCM',
              'media': str(media)}
    try:
        import numpy as np
        base = Path.cwd()
        if not isinstance(plan, dict):
            plan_path = Path(plan).resolve()
            base = plan_path.parent
            plan = json.loads(plan_path.read_text(encoding='utf-8-sig'))
        report['revision'] = plan.get('revision')
        sr, segments, duration = _timeline(plan)
        source = Path(plan['source']['path'])
        if not source.is_absolute():
            source = base / source
        report['source'] = str(source)
        pad = SEARCH_MS / 1000
        span = WINDOW_S + 2 * pad
        if duration < 4 * span:
            report['reason'] = 'too_short_for_four_independent_windows'
        else:
            with tempfile.TemporaryDirectory(prefix='timeline-sync-') as directory:
                temp = Path(directory)
                reference, encoded = temp / 'reference.wav', temp / 'encoded.wav'
                native, piece = temp / 'native.wav', temp / 'piece.wav'
                for quarter in range(4):
                    lo, hi = quarter * duration / 4, (quarter + 1) * duration / 4 - span
                    row = {'region': quarter + 1, 'status': 'not_checked',
                           'reason': 'no_voiced_reference_candidate', 'candidates': []}
                    # Selection depends only on reference energy, never on correlation.
                    for start_s in dict.fromkeys(((lo + hi) / 2, lo, hi)):
                        start = round(start_s * sr)
                        end = start + round(span * sr)
                        slices = _reference_window(segments, source, start, end, sr, ffmpeg, native, piece)
                        _resample(ffmpeg, native, reference)
                        _, x = speech_checks.pcm(reference)
                        offset = round(pad * CHECK_RATE)
                        amplitude = float(np.std(x[offset:offset + round(WINDOW_S * CHECK_RATE)]))
                        row['candidates'].append({'final_in_s': start / sr, 'reference_std': amplitude})
                        if not math.isfinite(amplitude) or amplitude < 20:
                            continue
                        row.update(final_in_s=start / sr, final_out_s=end / sr,
                                   time_s=start / sr + pad, source_slices=slices)
                        try:
                            _decode(ffmpeg, media, start, end, sr, native)
                            _resample(ffmpeg, native, encoded)
                            result = speech_checks.sync(reference, encoded, [pad], window_s=WINDOW_S,
                                                        search_ms=SEARCH_MS, max_lag_ms=TOLERANCE_MS)
                            check = dict(result['windows'][0])
                            check.pop('time_s', None)  # report only absolute final timeline seconds
                            row.pop('reason', None)
                            row.update(check)
                            if row['status'] == 'not_checked':
                                row.setdefault('reason', 'insufficient_correlation_or_search_boundary')
                        except (OSError, ValueError, RuntimeError, wave.Error) as error:
                            row['reason'] = str(error)
                        break
                    report['windows'].append(row)
    except (OSError, ValueError, TypeError, KeyError, ImportError, RuntimeError, wave.Error) as error:
        report['reason'] = str(error)
    statuses = [row['status'] for row in report['windows']]
    if 'fail' in statuses:
        report['status'] = 'fail'
    elif len(statuses) == 4 and all(status == 'pass' for status in statuses):
        report['status'] = 'pass'
    out = Path(out_dir) / 'sync.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--media', type=Path, required=True)
    parser.add_argument('--ffmpeg', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    report = run(args.plan, args.media, args.ffmpeg, args.out_dir)
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report['status'] == 'fail' else 0


if __name__ == '__main__':
    raise SystemExit(main())
