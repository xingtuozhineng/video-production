"""Run deterministic Windows-safe technical QC for a rendered delivery."""
import argparse
import json
import subprocess
from pathlib import Path
from fractions import Fraction


def run(command, check=False):
    return subprocess.run([str(x) for x in command], capture_output=True, text=True,
                          encoding='utf-8', errors='replace', check=check)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--media', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--ffmpeg', type=Path, required=True)
    parser.add_argument('--ffprobe', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--reference-audio', type=Path)
    args = parser.parse_args(argv)
    args.media = args.media.resolve(); args.plan = args.plan.resolve(); args.out = args.out.resolve()
    args.ffmpeg = args.ffmpeg.resolve(); args.ffprobe = args.ffprobe.resolve()
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    probe = run([args.ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', args.media])
    findings = []
    try:
        metadata = json.loads(probe.stdout)
    except json.JSONDecodeError:
        metadata = {}
        findings.append({'check': 'ffprobe_json', 'status': 'fail', 'detail': probe.stderr})
    decode = run([args.ffmpeg, '-v', 'error', '-nostdin', '-i', args.media, '-map', '0:v:0', '-map', '0:a:0?',
                  '-f', 'null', '-'])
    findings.append({'check': 'full_decode', 'status': 'pass' if decode.returncode == 0 and not decode.stderr.strip() else 'fail',
                     'detail': decode.stderr[-4000:]})
    video = next((s for s in metadata.get('streams', []) if s.get('codec_type') == 'video'), {})
    expected_fps = Fraction(plan['fps']['num'], plan['fps']['den'])
    actual_fps = Fraction(video.get('r_frame_rate', '0/1'))
    findings.append({'check': 'fps', 'status': 'pass' if actual_fps == expected_fps else 'fail',
                     'actual': str(actual_fps), 'expected': str(expected_fps)})
    if 'width' in plan and 'height' in plan:
        findings.append({'check': 'dimensions', 'status': 'pass' if
                         (video.get('width'), video.get('height')) == (plan['width'], plan['height']) else 'fail'})
    if plan.get('output_orientation') == 'portrait':
        width, height = video.get('width', 0), video.get('height', 0)
        rotation = next((s['rotation'] for s in video.get('side_data_list', []) if 'rotation' in s), 0)
        if abs(round(rotation)) % 180 == 90:
            width, height = height, width
        findings.append({'check': 'portrait', 'status': 'pass' if 0 < width < height else 'fail',
                         'display_width': width, 'display_height': height})
    frame_probe = run([args.ffprobe, '-v', 'error', '-threads', '0', '-select_streams', 'v:0', '-show_entries',
                       'frame=best_effort_timestamp_time', '-of', 'json', args.media])
    try:
        frames = json.loads(frame_probe.stdout)['frames']
        max_error = max(abs(float(f['best_effort_timestamp_time']) - i / float(expected_fps)) for i, f in enumerate(frames))
        continuous = len(frames) == plan['duration_frames'] and max_error <= .00001
        findings.append({'check': 'frame_timeline', 'status': 'pass' if continuous else 'fail',
                         'frames': len(frames), 'expected_frames': plan['duration_frames'], 'max_pts_error_s': max_error})
    except (KeyError, ValueError):
        findings.append({'check': 'frame_timeline', 'status': 'fail', 'detail': frame_probe.stderr[-1000:]})
    duration = float((metadata.get('format') or {}).get('duration') or 0)
    expected = float(plan.get('audio_duration_s') or plan.get('duration_s') or 0)
    delta = abs(duration - expected)
    findings.append({'check': 'duration', 'status': 'pass' if delta <= .05 else 'fail',
                     'actual_s': duration, 'expected_s': expected, 'delta_s': delta})
    if args.reference_audio:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'video-production/scripts'))
        from audio_identity import verify_audio
        findings.append(verify_audio(args.ffmpeg, args.media, args.reference_audio, duration))
    report = {'status': 'pass' if all(x['status'] == 'pass' for x in findings) else 'fail',
              'media': str(args.media.resolve()), 'findings': findings, 'metadata': metadata,
              'content_review': 'not_checked', 'natural_listening': 'not_checked'}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'out': str(args.out)}, ensure_ascii=False))
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
