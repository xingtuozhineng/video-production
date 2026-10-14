"""Render any validated original-speed edit plan in one FFmpeg graph."""
import argparse
import json
import subprocess
from pathlib import Path
import os
import sys
import tempfile
import shutil
import math

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'video-production/scripts'))
from managed_job import lock, contain_worker, write as write_state
from hardware import detect, ENCODERS

from check_timeline import validate


def choose_encoder(ffmpeg, requested, report_path=None):
    if requested != 'auto':
        return requested
    report = detect(Path(ffmpeg), report_path)
    if not report['ready']:
        raise ValueError('No tested encoder is usable; inspect hardware report')
    return report['selected_encoder']


def input_window(plan):
    # Whole-second anchor keeps audio sample offsets exact. FFmpeg accurate input seek
    # discards preroll; both trim grids below remain relative to this same anchor.
    start = math.floor(min(s['source_in_s'] for s in plan['segments']))
    end = math.ceil(max(s['source_out_s'] for s in plan['segments'])) + 1
    return start, end - start


def graph(plan, width, height, captions=False, input_start_s=0):
    """Separate audio/sample and video/frame concatenation; pad only the final tail.

    Pairwise AV concat waits for the longer stream at every seam. Padding every audio
    segment to the video frame length instead changes the canonical word/sample mapping.
    Independent concatenation preserves both grids without cumulative audio edits.
    """
    segments = plan['segments']
    count = len(segments)
    fps = plan['fps']['num'] / plan['fps']['den']
    fps_expr = f"{plan['fps']['num']}/{plan['fps']['den']}"
    sr = plan.get('sample_rate', 48000)
    rows = [f'[0:v]split={count}' + ''.join(f'[vs{i}]' for i in range(count)),
            f'[0:a]aresample={sr},asplit={count}' + ''.join(f'[as{i}]' for i in range(count))]
    for i, segment in enumerate(segments):
        a, b = segment['source_in_s'] - input_start_s, segment['source_out_s'] - input_start_s
        frames = round(segment['final_out_s'] * fps) - round(segment['final_in_s'] * fps)
        if frames <= 0:
            raise ValueError('Video segment below one frame')
        chain = (f'trim=start={a:.9f}:end={b:.9f},setpts=PTS-STARTPTS,fps={fps_expr},'
                 f'tpad=stop_mode=clone:stop=2,trim=end_frame={frames},setpts=N/({fps_expr}*TB)')
        # Source sample bounds are computed once; no per-segment silence or duration change.
        audio = (f"atrim=start_sample={round(segment['source_in_s'] * sr) - input_start_s * sr}:"
                 f"end_sample={round(segment['source_out_s'] * sr) - input_start_s * sr},asetpts=N/SR/TB")
        rows.append(f'[vs{i}]{chain},scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,'
                    f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]')
        rows.append(f'[as{i}]{audio}[a{i}]')
    rows.append(''.join(f'[v{i}]' for i in range(count)) +
                f"concat=n={count}:v=1:a=0,tpad=stop_mode=clone:stop=1,trim=end_frame={plan['duration_frames']},setpts=N/({fps_expr}*TB)[vcat]")
    rows.append(''.join(f'[a{i}]' for i in range(count)) +
                f"concat=n={count}:v=0:a=1,apad,atrim=end_sample={round(plan['duration_frames'] / fps * sr)}[aout]")
    rows.append('[vcat]ass=captions.ass:fontsdir=fonts[vout]' if captions else '[vcat]null[vout]')
    return ';'.join(rows)


def audio_graph(plan, input_start_s=0):
    """The same sample trims, concatenation and final tail as the AV renderer."""
    sr=plan.get('sample_rate',48000); count=len(plan['segments'])
    rows=[f'[0:a]aresample={sr},asplit={count}'+''.join(f'[as{i}]' for i in range(count))]
    for i,segment in enumerate(plan['segments']):
        rows.append(f"[as{i}]atrim=start_sample={round(segment['source_in_s']*sr)-input_start_s*sr}:"
                    f"end_sample={round(segment['source_out_s']*sr)-input_start_s*sr},asetpts=N/SR/TB[a{i}]")
    fps=plan['fps']['num']/plan['fps']['den']
    samples=round(plan['duration_frames']/fps*sr)
    rows.append(''.join(f'[a{i}]' for i in range(count))+f'concat=n={count}:v=0:a=1,apad,atrim=end_sample={samples}[aout]')
    return ';'.join(rows)


def render_audio(source, plan, out, ffmpeg):
    if out.suffix.lower()!='.wav' or source==out:
        raise ValueError('Audio-only preview needs a distinct WAV output')
    out.parent.mkdir(parents=True,exist_ok=True)
    start,duration=input_window(plan)
    with lock(out.parent/('.'+out.name+'.lock')), tempfile.TemporaryDirectory(prefix='.audio-preview-',dir=out.parent) as directory:
        temp=Path(directory); (temp/'graph.txt').write_text(audio_graph(plan,start),encoding='utf-8')
        command=[ffmpeg,'-v','error','-nostdin','-ss',str(start),'-t',str(duration),'-i',source,
                 '-/filter_complex','graph.txt','-map','[aout]','-vn','-c:a','pcm_s16le','-y',temp/'preview.wav']
        subprocess.run(list(map(str,command)),cwd=temp,capture_output=True,check=True)
        os.replace(temp/'preview.wav',out)
    report={'status':'pass','revision':plan['revision'],'output':str(out),'kind':'timeline_pcm_preview',
            'sample_rate':plan.get('sample_rate',48000),'listening':'not_checked','video_rendered':False}
    write_state(out.with_suffix('.json'),report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--ffmpeg', type=Path, required=True)
    parser.add_argument('--captions-ass', type=Path)
    parser.add_argument('--audio-only', action='store_true', help='PCM WAV from identical sample cuts; no video encoder.')
    parser.add_argument('--review-sample-parent', type=Path, help='Internal bounded calibration sample of a reviewed parent plan.')
    parser.add_argument('--width', type=int); parser.add_argument('--height', type=int)
    parser.add_argument('--encoder', choices=('auto', 'libx264', *ENCODERS), default='auto')
    parser.add_argument('--hardware-report', type=Path)
    parser.add_argument('--preset', default='medium'); parser.add_argument('--crf', type=int, default=18)
    args = parser.parse_args(argv)
    args.source = args.source.resolve()
    args.plan = args.plan.resolve()
    args.out = args.out.resolve()
    args.ffmpeg = args.ffmpeg.resolve()
    if args.captions_ass:
        args.captions_ass = args.captions_ass.resolve()
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    report = validate(plan)
    if report['status'] != 'pass':
        raise SystemExit('invalid timeline: ' + '; '.join(report['errors']))
    if args.audio_only:
        print(json.dumps(render_audio(args.source,plan,args.out,args.ffmpeg),ensure_ascii=False))
        return 0
    from pre_render_review import require_render
    require_render(args.plan, args.captions_ass, args.review_sample_parent, args.source, args.width, args.height)
    if (args.width is None) != (args.height is None):
        raise ValueError('Provide both width and height, or neither')
    if args.width is None:
        args.width, args.height = plan.get('width'), plan.get('height')
    if not all(type(x) is int and x > 0 and x % 2 == 0 for x in (args.width, args.height)):
        raise ValueError('Positive even plan dimensions required; rerun select or provide width and height')
    if plan.get('output_orientation') == 'portrait' and args.width >= args.height:
        raise ValueError('Talking-head output must be portrait (height > width)')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.source == args.out:
        raise ValueError('Output must not overwrite source media')
    job_handle = contain_worker()
    with lock(args.out.parent / ('.' + args.out.name + '.lock')):
        with tempfile.TemporaryDirectory(prefix='.render-', dir=args.out.parent) as directory:
            work = Path(directory)
            if args.captions_ass:
                (work / 'captions.ass').write_bytes(args.captions_ass.read_bytes())
                font_dir = args.captions_ass.parent / 'fonts'
                if not font_dir.is_dir():
                    raise ValueError('Caption fonts directory missing; run caption-build')
                shutil.copytree(font_dir, work / 'fonts')
            input_start, input_duration = input_window(plan)
            (work / 'graph.txt').write_text(graph(plan, args.width, args.height, bool(args.captions_ass), input_start), encoding='utf-8')
            encoder = choose_encoder(args.ffmpeg, args.encoder, args.hardware_report)
            command = [args.ffmpeg, '-v', 'warning', '-nostdin', '-ss', str(input_start),
                       '-t', str(input_duration), '-i', args.source,
                       '-/filter_complex', 'graph.txt', '-progress', str(args.out.parent / 'render-progress.txt'),
                       '-map', '[vout]', '-map', '[aout]', '-c:v', encoder]
            options = {'libx264': ['-preset', args.preset, '-crf', args.crf],
                       'h264_nvenc': ['-preset', 'p5', '-cq', args.crf],
                       'h264_qsv': ['-preset', 'medium', '-global_quality', args.crf],
                       'h264_amf': ['-quality', 'quality', '-rc', 'cqp', '-qp_i', args.crf, '-qp_p', args.crf],
                       'h264_videotoolbox': ['-b:v', '10M']}
            command += options[encoder]
            command += ['-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', '-y', work / 'encoded.mp4']
            with (args.out.parent / 'render.log').open('wb') as log:
                result = subprocess.run([str(x) for x in command], cwd=work, stdout=log, stderr=log)
            fallback = None
            if result.returncode and args.encoder == 'auto' and encoder != 'libx264':
                error = (args.out.parent / 'render.log').read_text(encoding='utf-8', errors='replace')
                # Retry only encoder/device failures, not broken graphs, fonts or bad source media.
                markers = ('Error while opening encoder', 'Error initializing an internal MFX session',
                           'Cannot load nvcuda', 'Failed to create  hardware device context')
                if any(marker in error for marker in markers):
                    fallback = {'from': encoder, 'reason': error[-3000:]}
                    start = command.index('-c:v')
                    end = command.index('-pix_fmt')
                    command[start:end] = ['-c:v', 'libx264', *options['libx264']]
                    encoder = 'libx264'
                    with (args.out.parent / 'render.log').open('ab') as log:
                        log.write(b'\nAUTO FALLBACK: hardware encoder failed; retrying libx264 once\n')
                        result = subprocess.run([str(x) for x in command], cwd=work, stdout=log, stderr=log)
                    if args.hardware_report and args.hardware_report.exists():
                        hardware = json.loads(args.hardware_report.read_text(encoding='utf-8'))
                        hardware['encoders'][fallback['from']].update(usable=False, runtime_failure=fallback['reason'])
                        hardware['selected_encoder'] = 'libx264'
                        write_state(args.hardware_report, hardware)
            if result.returncode:
                raise SystemExit(f'ffmpeg failed ({result.returncode}); see {args.out.parent / "render.log"}')
            os.replace(work / 'encoded.mp4', args.out)
    report = {'status': 'pass', 'encoder': encoder, 'output': str(args.out), 'fallback': fallback,
              'input_window': {'start_s': input_start, 'duration_s': input_duration}}
    write_state(args.out.parent / 'render-result.json', report)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
