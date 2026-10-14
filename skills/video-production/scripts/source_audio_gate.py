"""Surface retained source-listening observations; require an explicit local decision."""
import hashlib
import json


def event_key(source_hash, job_id, event):
    evidence = [source_hash, job_id, event.get('media_time_s'), event.get('kind'), event.get('heard')]
    return hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def retained_observations(plan, report, review):
    """Model timestamps are candidates, not boundaries or automatic deletion instructions."""
    decisions = {r['event_id']: r for r in review.get('audio_observations', []) if isinstance(r, dict) and 'event_id' in r}
    result = []
    for row in report.get('results', []):
        job = row['job']
        if job.get('media') != 'source':
            continue
        for event in row.get('result', {}).get('events', []):
            if event.get('kind') not in ('prompt_residue', 'nonformal_speech'):
                continue
            at = event.get('media_time_s')
            if not isinstance(at, (float, int)):
                continue
            segments = [s['id'] for s in plan['segments'] if s['source_in_s'] <= at < s['source_out_s']]
            if not segments:
                continue
            key = event_key(report['source_sha256'], job['id'], event)
            decision = decisions.get(key, {})
            reviewed = decision.get('decision') == 'retain' and bool(str(decision.get('evidence', '')).strip())
            result.append({'event_id': key, 'source_s': at, 'kind': event['kind'], 'heard': event.get('heard'),
                           'audio_evidence': event.get('evidence'), 'selected_segments': segments,
                           'status': 'reviewed_retained' if reviewed else 'review_required',
                           'decision_evidence': decision.get('evidence'),
                           'note': '模型时码只定位候选；核对源片。若确为领读/交流，修改现有spans/excluded_audio和selection；'
                                   '若为正常正文，写recording-review.json.audio_observations中的event_id/decision=retain/evidence。'})
    return result


def require_source_review(plan, report, review):
    if report.get('stage') != 'source' or report.get('status') != 'complete' or report.get('scope')=='source_focused_recheck':
        raise ValueError('Source audio review is incomplete; resume the same audio-review ledger before compiling')
    pending = [r for r in retained_observations(plan, report, review) if r['status'] == 'review_required']
    if pending:
        raise ValueError(json.dumps({'error': 'Retained source audio observations need source-evidence decisions before render',
                                     'candidates': pending}, ensure_ascii=False))
