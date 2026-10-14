"""Bind talking-head content review and PCM listening to inputs before video encoding."""
import argparse
import json
from pathlib import Path
import sys

from bailian_media import load, save, fingerprint, file_hash, config
from managed_job import lock

HERE = Path(__file__).resolve().parent
TALKING = HERE.parents[1] / 'talking-head-cut/scripts'
sys.path.insert(0, str(TALKING))


def project_for(*paths):
    for path in paths:
        if path:
            for parent in Path(path).resolve().parents:
                metadata = parent/'production.json'
                if metadata.is_file() and load(metadata).get('route') == 'talking-head':
                    return parent
    return None


def inputs(project, plan_path, captions_dir):
    project, plan_path, captions_dir = map(lambda p: Path(p).resolve(), (project, plan_path, captions_dir))
    plan = load(plan_path)
    captions = load(captions_dir/'captions.json')
    if captions.get('revision') != plan['revision']:
        raise ValueError('Pre-render captions and timeline revisions differ')
    source = Path(plan['source'].get('path') or plan['source'].get('source_path')).resolve()
    stat = source.stat()
    dependencies = {}
    for name in ('asr/transcript.source.json', 'index/selection.json', 'index/recording-review.json', 'compiled/mapped-words.json',
                 'batch/source-audio.json', 'batch/mixed-speech.json'):
        path = project/'work'/name
        if path.exists():
            dependencies[name] = file_hash(path)
    summary = project/'work/batch/source-audio.json'
    if summary.exists():
        dependencies['source_audio_report'] = file_hash(load(summary)['report_path'])
    timeline = fingerprint([plan, str(source), stat.st_size, stat.st_mtime_ns])
    binding = {'timeline': timeline, 'dependencies': dependencies,
               'captions': file_hash(captions_dir/'captions.json'), 'ass': file_hash(captions_dir/'captions.ass'),
               'fonts': {p.name: file_hash(p) for p in sorted((captions_dir/'fonts').glob('*')) if p.is_file()}}
    resolved = captions_dir/'authoring.resolved.json'
    if resolved.exists():
        binding['caption_authorship'] = file_hash(resolved)
    signature = fingerprint(binding)[:20]
    root = project/'work/pre-render'/signature
    return {'project': project, 'plan_path': plan_path, 'captions_dir': captions_dir, 'plan': plan,
            'source': source, 'captions': captions, 'binding': binding, 'signature': signature, 'root': root,
            'audio_root': project/'work/pre-render/audio'/timeline[:20]}


def source_checks(ctx):
    from check_timeline import validate
    from recording_review import validate_review
    from source_audio_gate import require_source_review
    from mixed_speech import retained_candidates
    plan, work = ctx['plan'], ctx['project']/'work'
    result = validate(plan)
    if result['status'] != 'pass':
        raise ValueError('Pre-render timeline invalid: ' + '; '.join(result['errors']))
    paths = [work/'asr/transcript.source.json', work/'index/selection.json', work/'index/recording-review.json']
    review = load(paths[2]) if paths[2].exists() else {}
    if not all(p.exists() for p in paths):
        raise ValueError('Talking-head source transcript, selection and recording review are required before rendering')
    doc, selection = map(load, paths[:2])
    validate_review(doc, selection, review)
    mapped = load(work/'compiled/mapped-words.json')
    if mapped.get('revision') != plan['revision'] or mapped.get('source_revision') != doc['revision']:
        raise ValueError('Pre-render word mapping and timeline revisions differ')
    retained = {w['id'] for w in mapped['words']}
    if any(w['id'] in retained and w.get('timing_status') == 'uniform_provider_timing' for w in doc['words']):
        raise ValueError('Retained source word timing is unreliable; resolve before video rendering')
    summary = work/'batch/source-audio.json'
    prepared = work/'batch/prepare-result.json'
    requested = prepared.exists() and load(prepared).get('audio_review_status') not in (None, 'not_requested')
    if requested and not summary.exists():
        raise ValueError('Requested source listening report missing; resume its existing ledger')
    if summary.exists():
        report = load(load(summary)['report_path'])
        if Path(report['source']).resolve() != ctx['source']:
            raise ValueError('Source listening report belongs to another source')
        require_source_review(plan, report, review)
    mixed = work/'batch/mixed-speech.json'
    if mixed.exists():
        data = load(mixed)
        if data['source_revision'] != load(paths[0])['revision']:
            raise ValueError('Source voice candidates stale; refresh before review')
        if any(r['status'] == 'review_required' for r in retained_candidates(plan, data, review)):
            raise ValueError('Retained local source voice candidates need evidence before rendering')


def audio_required(ctx, requested=False):
    return (requested or (ctx['project']/'work/batch/source-audio.json').exists()
            or (ctx['project']/'qc/audio-review/budget.json').exists())


def audio_proof(ctx):
    path = ctx['audio_root']/'report.json'
    wav = ctx['audio_root']/'review.wav'
    if not path.exists() or not wav.exists():
        raise ValueError('PCM timeline listening not completed; run pre-render-review --execute --audio-review')
    report = load(path)
    count = len(ctx['plan']['segments'])-1
    actual = {i['id'] for r in report.get('results', []) for i in r['job']['items'] if i['id'].startswith('cut-')}
    expected = {f'cut-{i:03}' for i in range(1, count+1)}
    if (report.get('status') != 'complete' or report.get('scope') != 'final'
            or report.get('revision') != ctx['plan']['revision'] or report.get('media_sha256') != file_hash(wav)
            or report.get('reviewed_cuts') != count or actual != expected):
        raise ValueError('PCM listening incomplete/stale; preserve and resume the same 0.2 CNY ledger')
    return report


def audio_risks(report):
    risks = []
    for row in report.get('results', []):
        for check in row['result']['checks']:
            for kind in ('prompt_residue', 'nonformal_speech', 'word_clipping', 'breath_cut'):
                if check[kind] != 'pass':
                    risks.append({'id': fingerprint([row['job']['signature'], check['id'], kind])[:16],
                                  'kind': kind, 'verdict': check[kind], 'evidence': check['evidence'],
                                  'window_s': [row['job']['start_s'], row['job']['end_s']],
                                  'events': row['result'].get('events', [])})
    return risks


def require(ctx):
    source_checks(ctx)
    path = ctx['root']/'decisions.json'
    if not path.exists():
        raise ValueError('Pre-render review missing; run pre-render-review before video encoding')
    decisions = load(path)
    if decisions.get('signature') != ctx['signature'] or decisions.get('status') != 'reviewed':
        raise ValueError('Pre-render review unreviewed/stale; review the current packet before video encoding')
    if decisions.get('pending_changes'):
        raise ValueError('Known selection/caption/breath changes must be applied before video encoding')
    checks = decisions.get('checks', {})
    for key in ('selection_boundaries', 'captions'):
        if checks.get(key, {}).get('status') != 'reviewed' or not str(checks[key].get('evidence', '')).strip():
            raise ValueError(f'Pre-render {key} review incomplete')
    pending = []
    if audio_required(ctx, decisions.get('audio_requested', False)):
        report = audio_proof(ctx)
        if decisions.get('audio_report_sha256') != file_hash(ctx['audio_root']/'report.json'):
            raise ValueError('PCM findings changed; review the current audio evidence')
        if checks.get('audio', {}).get('status') != 'reviewed' or not str(checks['audio'].get('evidence', '')).strip():
            raise ValueError('PCM listening findings not reviewed before video encoding')
        dispositions = {d.get('id'): d for d in decisions.get('audio_dispositions', [])}
        for risk in audio_risks(report):
            d = dispositions.get(risk['id'], {})
            if d.get('decision') not in ('sampling_edge', 'verified_source_delivery', 'uncertain_review_version') or not str(d.get('evidence', '')).strip():
                raise ValueError('Unresolved PCM voice/cut findings; resolve known changes before encoding')
            if d['decision'] == 'uncertain_review_version':
                pending.append(risk['id'])
    return {'status': 'ready_for_render', 'signature': ctx['signature'], 'decisions': str(path),
            'audio_review_requested': audio_required(ctx, decisions.get('audio_requested', False)),
            'unconfirmed_audio': pending, 'human_listening': 'not_checked', 'quality_acceptance': 'review_required'}


def prepare(workspace, project, plan_path, captions_dir, execute=False, audio=False, env=None):
    ctx = inputs(project, plan_path, captions_dir)
    source_checks(ctx)
    requested = audio_required(ctx, audio)
    if execute and requested:
        from render_timeline import render_audio
        import audio_review
        tools = load(Path(workspace)/'video-production-deps/tools.json')
        wav = ctx['audio_root']/'review.wav'
        if not wav.exists():
            render_audio(ctx['source'], ctx['plan'], wav, Path(tools['ffmpeg']))
        report_path = ctx['audio_root']/'report.json'
        if not report_path.exists() or load(report_path).get('status') != 'complete':
            report = audio_review.run(ctx['source'], Path(project), config(env or HERE.parent/'.env'),
                                     tools['ffmpeg'], tools['ffprobe'], wav, Path(plan_path), 'final', True)
            save(report_path, report)
    try:
        report = audio_proof(ctx) if requested else None
    except ValueError:
        report = None
    risks = audio_risks(report) if report else []
    ctx['root'].mkdir(parents=True, exist_ok=True)
    decisions_path = ctx['root']/'decisions.json'
    if not decisions_path.exists():
        decisions = {'signature': ctx['signature'], 'revision': ctx['plan']['revision'], 'status': 'not_checked',
                     'audio_requested': requested, 'pending_changes': [], 'audio_report_sha256': None,
                     'checks': {k: {'status': 'not_checked', 'evidence': ''} for k in ('selection_boundaries', 'captions', 'audio')},
                     'audio_dispositions': [{'id': r['id'], 'decision': 'needs_review', 'evidence': ''} for r in risks]}
        save(decisions_path, decisions)
    decisions = load(decisions_path)
    report_hash = file_hash(ctx['audio_root']/'report.json') if report else None
    if decisions.get('audio_report_sha256') != report_hash or requested and not decisions.get('audio_requested'):
        decisions.update(status='not_checked', audio_requested=requested, audio_report_sha256=report_hash,
                         audio_dispositions=[{'id': r['id'], 'decision': 'needs_review', 'evidence': ''} for r in risks])
        decisions['checks']['audio'] = {'status': 'not_checked', 'evidence': ''}
        save(decisions_path, decisions)
    body = ['# 渲染前集中审核', f'revision: {ctx["plan"]["revision"]}',
            '把会改选段、气口或字幕的问题在这里处理；正常项沿用已确认决定，不重新读取整片源JSON。',
            f'决定文件：{decisions_path}', '## 字幕校字、分行与短卡（全部最终页）']
    for i, c in enumerate(ctx['captions']['captions'], 1):
        duration = (c['end_frame']-c['start_frame']) * ctx['plan']['fps']['den']/ctx['plan']['fps']['num']
        body.append(f'{i}\t{duration:.3f}s\t'+' / '.join(c['lines'])+ ('\tshort_card' if duration < .4 else ''))
    body += ['## 声音与切口', f'PCM听审：{report["status"] if report else ("not_completed" if requested else "not_requested")}',
             *[json.dumps(r, ensure_ascii=False) for r in risks],
             '模型issue不能自动删词。真实问题先修改时间轴并重建字幕；采样边缘/正常源片发音需写依据；不确定项保留待审，不能改写为pass。']
    words_path = Path(project)/'work/compiled/mapped-words.json'
    if words_path.exists():
        from inspect_inputs import join_rows
        body += ['## 最终接缝与首尾词', *[json.dumps(r, ensure_ascii=False) for r in join_rows(ctx['plan'], load(words_path))]]
    packet = ctx['root']/'review.md'
    packet.write_text('\n'.join(body)+'\n', encoding='utf-8')
    result = {'status': 'decision_required', 'decision': 'pre_render_review', 'signature': ctx['signature'],
              'packet': str(packet), 'decisions': str(decisions_path), 'audio_report': str(ctx['audio_root']/'report.json') if report else None}
    try:
        result.update(require(ctx))
    except ValueError as exc:
        result['remaining'] = str(exc)
    save(ctx['root']/'status.json', result)
    return result


def require_render(plan_path, captions_ass, sample_parent=None, source=None, width=None, height=None):
    project = project_for(plan_path, captions_ass)
    if not project:
        return None  # Standalone renderer/media fixtures and other routes keep their existing contract.
    if not captions_ass:
        raise ValueError('Talking-head final render needs reviewed captions')
    ctx = inputs(project, sample_parent or plan_path, Path(captions_ass).parent)
    if source is not None and Path(source).resolve() != ctx['source']:
        raise ValueError('Render source differs from the reviewed source')
    if any(value is not None and value != ctx['plan'][key] for key, value in (('width', width), ('height', height))):
        raise ValueError('Render dimensions differ from the reviewed timeline')
    if sample_parent:
        sample, parent = map(load, (plan_path, sample_parent))
        if (not sample['segments'] or len(sample['segments']) > len(parent['segments'])
                or sample['duration_s'] > 40 or sample['duration_s'] > parent['duration_s']+.001):
            raise ValueError('Calibration preview must be a bounded sample of its reviewed parent')
        if any(sample.get(key) != parent.get(key) for key in ('source', 'fps', 'width', 'height', 'sample_rate')) or any(
                s['id'] != p['id'] or s['source_in_s'] != p['source_in_s']
                or s['final_in_s'] != p['final_in_s'] or s['source_out_s'] > p['source_out_s']+.001
                or s['source_out_s'] != p['source_out_s'] and i != len(sample['segments'])-1
                for i, (s, p) in enumerate(zip(sample['segments'], parent['segments']))):
            raise ValueError('Calibration sample differs from the reviewed timeline')
    return require(ctx)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('workspace-root', 'project-dir', 'plan', 'captions-dir'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--env', type=Path)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--audio-review', action='store_true')
    args = parser.parse_args(argv)
    with lock(args.project_dir/'work/pre-render/.lock'):
        result = prepare(args.workspace_root, args.project_dir, args.plan, args.captions_dir,
                         args.execute, args.audio_review, args.env)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
