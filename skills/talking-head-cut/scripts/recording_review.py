"""Validate authored roles, content purpose and takes; never guess from speaker IDs."""
import hashlib
import json
import math


def transcript_hash(transcript):
    return hashlib.sha256(json.dumps(transcript, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def utterance_groups(words):
    groups = []
    for i, word in enumerate(words, 1):
        speaker = word.get('effective_speaker_id', word.get('speaker_id'))
        key = (word.get('utterance_id'), word.get('channel_id', 0), speaker)
        if not groups or groups[-1]['key'] != key:
            groups.append({'key': key, 'first': i, 'last': i, 'words': []})
        groups[-1]['last'] = i
        groups[-1]['words'].append(word)
    return groups


def draft_review(transcript):
    speakers = dict.fromkeys((w.get('channel_id', 0), w.get('effective_speaker_id', w.get('speaker_id')))
                             for w in transcript['words'])
    return {'source_revision': transcript['revision'], 'transcript_hash': transcript_hash(transcript),
            'status': 'not_checked', 'mode': 'unknown', 'evidence': '',
            'content_review': {'status': 'not_checked', 'evidence': ''},
            'speaker_roles': [{'channel_id': channel, 'speaker_id': speaker,
                               'role': 'unknown', 'evidence': ''} for channel, speaker in speakers],
            'spans': [],
            'excluded_audio': []}


def validate_review(transcript, selection, review):
    """Reuse confirmed speaker roles, apply sparse overrides and check selected takes."""
    def has_text(value):
        return isinstance(value, str) and bool(value.strip())

    if not review or review.get('status') != 'reviewed':
        raise ValueError('Recording roles/takes not reviewed; complete recording-review.json before select')
    if (review.get('source_revision') != transcript['revision']
            or review.get('transcript_hash') != transcript_hash(transcript)):
        raise ValueError('Stale recording review; source transcript changed')
    if review.get('mode') not in ('solo', 'guided_audible', 'guided_inaudible', 'mixed') or not has_text(review.get('evidence')):
        raise ValueError('Recording review requires a mode and source audio/video evidence')
    content_review = review.get('content_review')
    if (not isinstance(content_review, dict) or content_review.get('status') != 'reviewed'
            or not has_text(content_review.get('evidence'))):
        raise ValueError('Content-purpose review not completed; confirm formal speech versus rehearsal/production '
                         'in recording-review.json.content_review before select (existing transcript may be reused)')
    roles = ('performer', 'prompt', 'production', 'unknown')
    speakers = {}
    observed = {(w.get('channel_id', 0), w.get('effective_speaker_id', w.get('speaker_id')))
                for w in transcript['words']}
    for item in review.get('speaker_roles', []):
        key = (item.get('channel_id', 0), item.get('speaker_id'))
        if key not in observed or key in speakers or item.get('role') not in roles:
            raise ValueError('Speaker roles must uniquely reference observed channel/speaker IDs')
        if item['role'] != 'unknown' and not has_text(item.get('evidence')):
            raise ValueError('Confirmed speaker roles require source evidence')
        speakers[key] = item
    owners = {}
    for g in utterance_groups(transcript['words']):
        # Unannotated units retain original selection semantics. Semantic repeat
        # groups are authored only where needed; these IDs do not detect repetition.
        _, channel, speaker = g['key']
        base = speakers.get((channel, speaker), {'role': 'unknown'})
        for i in range(g['first'], g['last'] + 1):
            owners[i] = {**base, 'group': f'unit-{g["first"]}', 'take': 'source'}
    overridden = set()
    for span in review.get('spans', []):
        a, b = span.get('first'), span.get('last')
        if (type(a) is not int or type(b) is not int or not 1 <= a <= b <= len(transcript['words'])
                or overridden.intersection(range(a, b + 1))):
            raise ValueError('Role overrides require valid, nonoverlapping source word ranges')
        role = span.get('role')
        if role not in roles:
            raise ValueError('Unknown role in recording review')
        if role != 'unknown' and not has_text(span.get('evidence')):
            raise ValueError('Confirmed roles require source evidence')
        if any(span.get(k) not in (None, '') for k in ('group', 'take')) and any(not has_text(span.get(k)) for k in ('group', 'take')):
            raise ValueError('Repeat annotations require both semantic group and take IDs')
        override = {k: v for k, v in span.items() if k not in ('group', 'take') or v not in (None, '')}
        for i in range(a, b + 1):
            owners[i] = {**owners[i], **override}
        overridden.update(range(a, b + 1))
    takes, errors = {}, []
    if not selection.get('ranges'):
        raise ValueError('No retained ranges')
    for n, item in enumerate(selection['ranges'], 1):
        a, b = item.get('first'), item.get('last')
        if type(a) is not int or type(b) is not int or not 1 <= a <= b <= len(owners):
            errors.append(f'range {n}: invalid source word indexes')
            continue
        for i in range(a, b + 1):
            span = owners[i]
            if span['role'] != 'performer':
                errors.append(f'range {n}: word {i} has role {span["role"]}; only confirmed performer words may be retained')
                break
            group, take = span['group'], span['take']
            if group in takes and takes[group] != take:
                errors.append(f'range {n}: mixed takes for group {group}; choose one complete performer take')
                break
            takes[group] = take
    for region in review.get('excluded_audio', []):
        a, b = region.get('start_s'), region.get('end_s')
        if (not all(type(t) in (int, float) and math.isfinite(t) for t in (a, b))
                or not 0 <= a < b or region.get('role') not in ('prompt', 'production', 'unknown')
                or not has_text(region.get('evidence'))):
            errors.append('Excluded audio requires valid source times, role and evidence')
    if errors:
        raise ValueError('; '.join(errors))
    return takes


def protect_audio(start, end, first_word, last_word, review):
    """Trim handles around reviewed excluded audio; reject any overlap inside a take."""
    core_start, core_end = first_word['source_start_s'], last_word['source_end_s']
    for region in review.get('excluded_audio', []):
        a, b = region['start_s'], region['end_s']
        if a < core_end and b > core_start:
            raise ValueError('Selected range crosses excluded audio; split/reselect or correct source timing after review')
        if b <= core_start and b > start:
            start = b
        if a >= core_end and a < end:
            end = a
    return start, end
