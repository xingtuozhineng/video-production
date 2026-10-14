"""Fixed talking-head delivery: render, QC, editable draft, resumable checkpoints."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import copy
import math

from managed_job import lock, read, write
from export_jianying import normalize
import pre_render_review

HERE = Path(__file__).resolve().parent
TALKING = HERE.parents[1] / 'talking-head-cut/scripts'


def stamp(path):
    path = Path(path).resolve()
    st = path.stat()
    return {'path': str(path), 'bytes': st.st_size, 'modified_ns': st.st_mtime_ns}


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checkpoint_valid(entry):
    try:
        return bool(entry) and all(stamp(item['path']) == item for item in entry['files'])
    except (OSError, KeyError):
        return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for name in ['workspace-root', 'plan', 'captions-dir', 'out-dir']:
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--encoder', default='auto')
    p.add_argument('--preset', default='medium')
    p.add_argument('--crf', type=int, default=18)
    a = p.parse_args(argv)
    plan, captions = read(a.plan), read(a.captions_dir / 'captions.json')
    source = Path(plan['source'].get('path') or plan['source'].get('source_path')).resolve()
    if not all(type(plan.get(k)) is int and plan[k] > 0 for k in ['width', 'height']):
        raise ValueError('Plan requires positive width/height; use select before compile')
    if plan['width'] >= plan['height']:
        raise ValueError('Talking-head delivery must be portrait; rebuild the plan and captions with select')
    normalize(plan, captions)  # Validate export compatibility BEFORE expensive rendering.
    project = pre_render_review.project_for(a.plan, a.captions_dir/'captions.ass')
    pre_review = pre_render_review.require(pre_render_review.inputs(project, a.plan, a.captions_dir)) if project else None
    if importlib.util.find_spec('pyJianYingDraft') is None:
        raise RuntimeError('Draft writer missing from workspace Python; run prepare')
    tools = read(a.workspace_root / 'video-production-deps/tools.json')
    fonts = sorted((a.captions_dir / 'fonts').glob('*'))
    if not fonts or not (a.captions_dir / 'captions.ass').is_file():
        raise ValueError('Run caption-build to provide ASS and fonts before delivery')
    inputs = {'plan': file_hash(a.plan), 'captions': file_hash(a.captions_dir / 'captions.json'),
              'ass': file_hash(a.captions_dir / 'captions.ass'), 'source': stamp(source),
              'fonts': {f.name: file_hash(f) for f in fonts},
              'ffmpeg': stamp(tools['ffmpeg']), 'ffprobe': stamp(tools['ffprobe']),
              'encoder': a.encoder, 'preset': a.preset, 'crf': a.crf,
              'code': {f.name: file_hash(f) for f in [Path(__file__), HERE / 'hardware.py',
                       HERE / 'export_jianying.py', TALKING / 'render_timeline.py', TALKING / 'qc_delivery.py',
                       TALKING / 'sync_timeline.py', TALKING / 'speech_checks.py', HERE/'pre_render_review.py']}}
    signature = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:20]
    out = a.out_dir.resolve() / signature
    out.mkdir(parents=True, exist_ok=True)
    state_file = out / 'delivery.json'
    with lock(a.out_dir / '.delivery-lock'):
        state = read(state_file) if state_file.exists() else {'signature': signature, 'revision': plan['revision'], 'stages': {}}
        snapshot = out / 'project'; snapshot.mkdir(exist_ok=True)
        write(snapshot / 'edit-plan.json', plan)
        write(snapshot / 'inputs.json', inputs)
        if pre_review:
            write(snapshot/'pre-render-review.json', {**pre_review, 'decision_record': read(pre_review['decisions'])})
        (snapshot / 'captions').mkdir(exist_ok=True)
        for name in ['captions.json', 'captions.ass', 'captions.srt']:
            shutil.copy2(a.captions_dir / name, snapshot / 'captions' / name)
        resolved = a.captions_dir / 'authoring.resolved.json'
        if resolved.is_file():
            shutil.copy2(resolved, snapshot / 'captions' / resolved.name)
        shutil.copytree(a.captions_dir / 'fonts', snapshot / 'captions/fonts', dirs_exist_ok=True)
        plan_path = snapshot / 'edit-plan.json'
        def stage(name, command, outputs, force=False):
            if not force and checkpoint_valid(state['stages'].get(name)):
                print(json.dumps({'stage': name, 'state': 'reused'}), flush=True)
                return False
            state.update(state='running', stage=name)
            write(state_file, state)
            print(json.dumps({'stage': name, 'state': 'running'}), flush=True)
            result = subprocess.run([str(x) for x in command])
            if result.returncode:
                state.update(state='failed', stage=name, exit_code=result.returncode)
                write(state_file, state)
                raise RuntimeError(f'{name} failed; inspect stage log/report and resume the same job')
            state['stages'][name] = {'completed_at': time.time(), 'files': [stamp(f) for f in outputs]}
            write(state_file, state)
            return True
        render = [sys.executable, TALKING / 'render_timeline.py', '--source', source,
                  '--ffmpeg', tools['ffmpeg'], '--encoder', a.encoder, '--preset', a.preset, '--crf', a.crf,
                  '--hardware-report', a.workspace_root / 'video-production-deps/hardware.json',
                  '--width', plan['width'], '--height', plan['height'],
                  '--captions-ass', snapshot / 'captions/captions.ass']
        sample = copy.deepcopy(plan)
        sample['segments'] = []
        fps = plan['fps']['num'] / plan['fps']['den']
        sfps = plan['source']['fps']['num'] / plan['source']['fps']['den']
        for original in plan['segments']:
            seg = copy.deepcopy(original)
            if seg['final_in_s'] >= 3:
                break
            if seg['final_out_s'] > 3:
                removed = seg['final_out_s'] - 3
                seg['source_out_s'] -= removed
                seg['final_out_s'] = 3
            seg['source_out_frame'] = round(seg['source_out_s'] * sfps)
            seg['source_frame_count'] = seg['source_out_frame'] - seg['source_in_frame']
            seg['final_out_frame'] = round(seg['final_out_s'] * fps)
            seg['duration_frames'] = seg['final_out_frame'] - seg['final_in_frame']
            if seg['duration_frames'] > 0:
                sample['segments'].append(seg)
        sample['audio_duration_s'] = sample['segments'][-1]['final_out_s']
        sample['duration_frames'] = math.ceil(sample['audio_duration_s'] * fps - 1e-7)
        sample['duration_s'] = sample['duration_frames'] / fps
        sample['audio_samples'] = round(sample['audio_duration_s'] * plan.get('sample_rate', 48000))
        preflight = out / 'preflight'; preflight.mkdir(exist_ok=True)
        write(preflight / 'plan.json', sample)
        stage('preflight', [*render, '--plan', preflight / 'plan.json', '--out', preflight / 'sample.mp4',
                            '--review-sample-parent', plan_path],
              [preflight / 'sample.mp4'])
        rendered = stage('render', [*render, '--plan', plan_path, '--out', out / 'final.mp4'], [out / 'final.mp4'])
        stage('qc', [sys.executable, TALKING / 'qc_delivery.py', '--media', out / 'final.mp4',
                     '--plan', plan_path, '--out', out / 'qc.json', '--ffmpeg', tools['ffmpeg'],
                     '--ffprobe', tools['ffprobe']], [out / 'qc.json'], force=rendered)
        stage('sync', [sys.executable, TALKING / 'sync_timeline.py', '--media', out / 'final.mp4',
                       '--plan', plan_path, '--out-dir', out / 'sync', '--ffmpeg', tools['ffmpeg']],
              [out / 'sync/sync.json'], force=rendered)
        review = out / 'review'; review.mkdir(exist_ok=True)
        for index, fraction in enumerate((.05, .33, .66, .95), 1):
            second = min(plan['duration_s'] - 1 / fps, plan['duration_s'] * fraction)
            stage(f'review-frame-{index}', [tools['ffmpeg'], '-v', 'error', '-nostdin', '-ss', second,
                  '-i', out / 'final.mp4', '-frames:v', 1, '-vf',
                  'scale=360:640:force_original_aspect_ratio=decrease,pad=360:640:(ow-iw)/2:(oh-ih)/2',
                  '-y', review / f'frame{index}.jpg'], [review / f'frame{index}.jpg'], force=rendered)
        stage('review-sheet', [tools['ffmpeg'], '-v', 'error', '-nostdin', '-framerate', 1,
                              '-i', review / 'frame%d.jpg', '-vf', 'tile=2x2', '-frames:v', 1,
                              '-y', review / 'contact-sheet.jpg'], [review / 'contact-sheet.jpg'], force=rendered)
        draft = out / '剪映工程'
        stage('jianying', [sys.executable, HERE / 'export_jianying.py', '--plan', plan_path,
                          '--captions', snapshot / 'captions/captions.json', '--out', draft],
              [draft / 'draft_content.json', draft / 'package.json', draft / 'export-report.json'])
        shutil.copy2(snapshot / 'captions/captions.srt', out / 'final.srt')
        state.update(state='completed', stage='done', human_review='not_checked',
                     overall='review_required', outputs={'mp4': str(out / 'final.mp4'),
                     'srt': str(out / 'final.srt'), 'jianying': str(draft), 'qc': str(out / 'qc.json')})
        state['outputs'].update(sync=str(out / 'sync/sync.json'), review_sheet=str(review / 'contact-sheet.jpg'))
        if pre_review:
            state['outputs']['pre_render_review'] = str(snapshot/'pre-render-review.json')
        write(state_file, state)
        # A compact, file-based handoff survives a cold model cache or a new context.
        write(a.out_dir / 'handoff.json', {'revision': plan['revision'], 'state': state['state'],
                                         'overall': state['overall'], 'outputs': state['outputs'],
                                         'project': str(snapshot), 'summary': {
                                             'width': plan['width'], 'height': plan['height'], 'fps': plan['fps'],
                                             'duration_s': plan['duration_s'], 'segments': len(plan['segments']),
                                             'caption_pages': len(captions['captions']),
                                             'caption_warnings': captions.get('warnings', []),
                                             'technical_qc': read(out / 'qc.json')['status'],
                                             'audio_sync': read(out / 'sync/sync.json')['status'],
                                             'encoder': read(out / 'render-result.json')['encoder']},
                                         'remaining': ['成片抽帧视看', '听审', '剪映打开和编辑保存']})
        production_path = a.out_dir.parent / 'production.json'
        if production_path.is_file():
            production = read(production_path)
            production.update(export_format='both', timeline_revision=plan['revision'],
                              outputs={**state['outputs'], 'revision': plan['revision']},
                              qc_summary={'overall': 'review_required', 'technical': 'pass',
                                          'audio_sync': read(out / 'sync/sync.json')['status'],
                                          'listening': 'not_checked', 'jianying_app': 'not_checked'})
            write(production_path, production)
        print(json.dumps(read(a.out_dir / 'handoff.json'), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
