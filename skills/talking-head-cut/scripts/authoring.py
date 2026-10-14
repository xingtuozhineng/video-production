"""Deterministic authoring adapters: word ranges in, validated production files out."""
import argparse
import copy
import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path
import shutil
import subprocess

from map_words import remap
from semantic_pacing import prepare, TARGETS
from caption_pages import compile_pages, key, word_text, write_srt, frame
from recording_review import draft_review, utterance_groups, validate_review, protect_audio


FONT = Path(__file__).resolve().parents[2] / 'video-production/assets/fonts/source-han-sans/SourceHanSansSC-Light.otf'


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def index(transcript, out):
    words = transcript['words']
    if not words or len({w['id'] for w in words}) != len(words):
        raise ValueError('Transcript must have nonempty, unique word IDs')
    out.mkdir(parents=True, exist_ok=True)
    clean = lambda text: str(text).replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')
    rows = ['index\tid\tstart_s\tend_s\ttext\tspeaker_id\teffective_speaker_id\tutterance_id\tchannel_id']
    rows += [f"{i}\t{w['id']}\t{w['source_start_s']}\t{w['source_end_s']}\t{clean(word_text(w))}"
             f"\t{w.get('speaker_id', '')}\t{w.get('effective_speaker_id', '')}\t{w.get('utterance_id', '')}\t{w.get('channel_id', 0)}"
             for i, w in enumerate(words, 1)]
    (out / 'words.tsv').write_text('\n'.join(rows) + '\n', encoding='utf-8')
    utterances = ['first\tlast\tutterance_id\tchannel_id\teffective_speaker_id\tstart_s\tend_s\ttext']
    for g in utterance_groups(words):
        ws = g['words']
        utterances.append('\t'.join(clean(v) for v in [g['first'], g['last'], *g['key'],
            ws[0]['source_start_s'], ws[-1]['source_end_s'], ''.join(word_text(w) for w in ws)]))
    (out / 'utterances.tsv').write_text('\n'.join(utterances) + '\n', encoding='utf-8')
    review_path = out / 'recording-review.json'
    if not review_path.exists():
        save(review_path, draft_review(transcript))
    draft = out / 'selection.json'
    if not draft.exists():
        save(draft, {'source_revision': transcript['revision'], 'review': 'not_checked',
                     'ranges': [{'first': 1, 'last': len(words), 'reason': '保留全部；根据词索引选择有效内容'}]})
    return {'words': len(words), 'index': str(out / 'words.tsv'), 'selection': str(draft),
            'utterances': str(out / 'utterances.tsv'), 'recording_review': str(review_path)}


def select(transcript, selection, source, ffprobe, out, fps=None, width=None, height=None, review=None):
    if selection.get('source_revision') != transcript['revision']:
        raise ValueError('Stale selection source_revision')
    validate_review(transcript, selection, review)
    words = transcript['words']
    result = subprocess.run([str(ffprobe), '-v', 'error', '-show_streams', '-show_format',
                             '-of', 'json', str(source)], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', check=True)
    meta = json.loads(result.stdout)
    video = next(s for s in meta['streams'] if s['codec_type'] == 'video')
    if not any(s['codec_type'] == 'audio' for s in meta['streams']):
        raise ValueError('Talking-head source needs an audio stream')
    source_rate = Fraction(video['avg_frame_rate'])
    fps = source_rate if fps is None else Fraction(fps)
    duration = float(meta['format']['duration'])
    if any(r['end_s'] > duration for r in review.get('excluded_audio', [])):
        raise ValueError('Excluded audio exceeds source duration')
    vw, vh = video['width'], video['height']
    rotation = next((s.get('rotation', 0) for s in video.get('side_data_list', []) if 'rotation' in s), 0)
    if abs(round(rotation)) % 180 == 90:
        vw, vh = vh, vw
    if (width is None) != (height is None):
        raise ValueError('Provide both width and height, or neither')
    if width is None:
        width, height = min(vw, vh), max(vw, vh)
        if width == height:
            height = math.ceil(width * 16 / 9 / 2) * 2
    if fps <= 0 or width <= 0 or height <= 0 or width % 2 or height % 2:
        raise ValueError('Positive FPS and positive even dimensions required')
    if width >= height:
        raise ValueError('Talking-head output must be portrait (height > width)')
    errors, used, segments, cursor = [], set(), [], 0
    for i, item in enumerate(selection.get('ranges', []), 1):
        a, b = item.get('first'), item.get('last')
        if type(a) is not int or type(b) is not int or not 1 <= a <= b <= len(words) or not item.get('reason'):
            errors.append({'range': i, 'error': 'first/last must name an inclusive word-index range; reason required'})
            continue
        if used.intersection(range(a, b + 1)):
            errors.append({'range': i, 'error': 'Duplicate retained words'})
            continue
        used.update(range(a, b + 1))
        start, end = words[a - 1]['source_start_s'], words[b - 1]['source_end_s']
        # Preserve up to 40ms handles without accidentally retaining adjacent omitted words.
        start = max(0, start - .04, words[a - 2]['source_end_s'] if a > 1 else 0)
        end = min(duration, end + .04, words[b]['source_start_s'] if b < len(words) else duration)
        try:
            start, end = protect_audio(start, end, words[a - 1], words[b - 1], review)
        except ValueError as exc:
            errors.append({'range': i, 'error': str(exc)})
            continue
        sa, sb = round(start * 48000), round(end * 48000)
        if sb <= sa:
            errors.append({'range': i, 'error': 'Empty or overlapping source timing; inspect original audio'})
            continue
        fa, fb = cursor / 48000, (cursor + sb - sa) / 48000
        segments.append({'id': f'k{i:04}', 'source_in_s': sa / 48000, 'source_out_s': sb / 48000,
                         'final_in_s': fa, 'final_out_s': fb, 'reason': item['reason']})
        cursor += sb - sa
    if not segments:
        errors.append({'error': 'No retained ranges'})
    if errors:
        save(out / 'selection-errors.json', errors)
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    stat = source.stat()
    source_info = {'path': str(source.resolve()), 'duration_s': duration,
                   'fps': {'num': source_rate.numerator, 'den': source_rate.denominator},
                   'width': video['width'], 'height': video['height'], 'rotation': rotation,
                   'display_width': vw, 'display_height': vh,
                   'size': stat.st_size, 'modified_ns': stat.st_mtime_ns}
    output_rate = {'num': fps.numerator, 'den': fps.denominator}
    revision = 'selection-' + digest([selection, source_info, transcript, output_rate, width, height, review])
    frames = math.ceil(cursor / 48000 * fps - 1e-7)
    plan = {'revision': revision, 'source': source_info, 'width': width, 'height': height,
            'output_orientation': 'portrait',
            'fps': output_rate, 'duration_frames': frames, 'duration_s': float(frames / fps),
            'sample_rate': 48000, 'audio_samples': cursor, 'audio_duration_s': cursor / 48000,
            'timing_mode': 'sample_audio_cumulative_video', 'segments': segments,
            'recording_review': copy.deepcopy(review)}
    mapped = remap(transcript, plan)
    save(out / 'selection-plan.json', plan)
    save(out / 'words.selected.json', mapped)
    decisions_path = out / 'pause-decisions.json'
    old_decisions = load(decisions_path) if decisions_path.exists() else None
    if not old_decisions or old_decisions.get('revision') != revision:
        if old_decisions:
            save(out / ('pause-decisions.previous-' + digest(old_decisions) + '.json'), old_decisions)
        save(decisions_path, prepare(mapped))
    return {'revision': revision, 'segments': len(segments), 'words': len(mapped['words'])}


def author_pauses(decisions, draft):
    if draft.get('revision') != decisions['revision']:
        raise ValueError('Stale pause authorship revision')
    result = copy.deepcopy(decisions)
    used = set()
    for group in draft.get('groups', []):
        if group.get('category') not in TARGETS or not group.get('reason'):
            raise ValueError('Each pause group needs a category and reason')
        for i in group['indexes']:
            if type(i) is not int or not 1 <= i <= len(result['boundaries']) or i in used:
                raise ValueError('Pause indexes must be valid, unique and one-based')
            used.add(i)
            row = result['boundaries'][i-1]
            for field in ('target_ms', 'preserve'):
                row.pop(field, None)
            row.update({k: group[k] for k in ('category', 'reason', 'target_ms', 'preserve') if k in group})
            if 'target_ms' in row and (type(row['target_ms']) not in (int, float)
                                      or not math.isfinite(row['target_ms']) or not 0 < row['target_ms'] <= 1000):
                raise ValueError('Pause target_ms must be finite and within (0,1000]')
            if 'preserve' in row and type(row['preserve']) is not bool:
                raise ValueError('Pause preserve must be boolean')
    if used != set(range(1, len(result['boundaries']) + 1)):
        raise ValueError('Pause groups must cover every candidate exactly once')
    return result


def caption_draft(words, plan, out, max_chars=16):
    if words['revision'] != plan['revision']:
        raise ValueError('Stale mapped words')
    ws = words['words']
    fps = plan['fps']['num'] / plan['fps']['den']
    pages, lines, first, length = [], [], 1, 0
    for i, w in enumerate(ws, 1):
        length += len(word_text(w))
        next_word = ws[i] if i < len(ws) else None
        # Keep quantized overlapping/zero-duration word pairs on the same page.
        safe_break = (next_word is None or frame(w['final_end_s'], fps) <= frame(next_word['final_start_s'], fps))
        punctuation = (word_text(w) + w.get('punctuation', '')).endswith(('。', '！', '？', '；', '!', '?'))
        pause = next_word is not None and next_word['final_start_s'] - w['final_end_s'] >= .25
        limit = next_word is not None and length + len(word_text(next_word)) > max_chars
        if safe_break and (limit or punctuation or pause or next_word is None):
            lines.append([first, i])
            first, length = i + 1, 0
            if len(lines) == 2 or punctuation or pause or next_word is None:
                pages.append({'lines': lines, 'emphasis': []})
                lines = []
    if out.exists():
        raise FileExistsError('Caption draft already exists; edit it or choose a new output')
    draft = {'revision': words['revision'], 'review': 'not_checked', 'corrections': [], 'pages': pages}
    save(out, draft)
    rows = ['index\tkey\ttext'] + [f'{i}\t{key(w)}\t{word_text(w)}' for i, w in enumerate(words['words'], 1)]
    out.with_suffix('.tsv').write_text('\n'.join(rows) + '\n', encoding='utf-8')
    review = out.with_suffix('.review.tsv')
    rows = ['page\tfirst\tlast\tstart_s\tduration_s\ttext\tflags']
    for i, page in enumerate(pages, 1):
        a, b = page['lines'][0][0], page['lines'][-1][1]
        texts = [''.join(word_text(w) for w in ws[x-1:y]) for x,y in page['lines']]
        duration = ws[b-1]['final_end_s'] - ws[a-1]['final_start_s']
        flags = (['short_card'] if duration < .4 else []) + (['long_line'] if any(len(t) > max_chars for t in texts) else [])
        rows.append(f"{i}\t{a}\t{b}\t{ws[a-1]['final_start_s']:.3f}\t{duration:.3f}\t{' / '.join(texts)}\t{','.join(flags)}")
    review.write_text('\n'.join(rows) + '\n', encoding='utf-8')
    return {'pages': len(pages), 'draft': str(out), 'index': str(out.with_suffix('.tsv')), 'review': str(review)}


def merge_caption_edits(draft, edits):
    if edits.get('revision') != draft['revision']:
        raise ValueError('Stale caption edits revision')
    merged = copy.deepcopy(draft)
    merged['corrections'] = merged.get('corrections', []) + edits.get('corrections', [])
    used = set()
    for change in edits.get('pages', []):
        i = change['page']
        if type(i) is not int or not 1 <= i <= len(merged['pages']) or i in used:
            raise ValueError('Caption edits require unique valid page indexes')
        used.add(i)
        merged['pages'][i-1].update({k:v for k,v in change.items() if k in ('lines', 'emphasis', 'takeaway')})
    for highlight in edits.get('highlights', []):
        matches = [p for p in merged['pages'] if p['lines'][0][0] <= highlight['first'] <= highlight['last'] <= p['lines'][-1][1]]
        if len(matches) != 1:
            raise ValueError(f'Highlight must fit one page: {highlight}')
        matches[0].setdefault('emphasis', []).append(highlight)
    merged['review'] = edits.get('review', draft.get('review', 'not_checked'))
    return merged


def rebase_word_indexes(old_words, words):
    """Validate unchanged text/IDs or explicit timing lineage; never infer new takes."""
    parent = words.get('source_parent_revision') == old_words.get('source_revision')
    same = old_words.get('source_revision') == words.get('source_revision')
    if not old_words.get('source_revision') or not (same or parent):
        raise ValueError('Caption rebase needs a reviewed draft and the same source_revision')
    old = old_words['words']; new = words['words']
    old_by_id = {w['id']: (i,w) for i,w in enumerate(old,1)}
    if len(old_by_id) != len(old) or len({w['id'] for w in new}) != len(new):
        raise ValueError('Caption rebase requires unique stable source word IDs')
    positions = []; fixes = {r['word_id']:r for r in words.get('source_timing_corrections',[]) if 'word_id' in r}
    for w in new:
        if w['id'] not in old_by_id:
            raise ValueError('New words/takes need fresh caption review')
        i, previous = old_by_id[w['id']]; positions.append(i)
        if any(w.get(k) != previous.get(k) for k in ('text','corrected_text')):
            raise ValueError('Source word content changed; review captions again')
        before=[previous['source_start_s'],previous['source_end_s']]
        after=[w['source_start_s'],w['source_end_s']]
        if before != after:
            fix=fixes.get(w['id'],{})
            if not (parent and fix.get('old_range_s')==before and fix.get('new_range_s')==after
                    and str(fix.get('evidence','')).strip()):
                raise ValueError('Source timing changed without verified lineage; review captions again')
    if positions != sorted(set(positions)):
        raise ValueError('Reordered content needs fresh caption review')
    return {old_index: new_index for new_index,old_index in enumerate(positions,1)}


def rebase_pauses(old_decisions, decisions, old_words, words):
    rebase_word_indexes(old_words,words)
    key=lambda r:(r['left_key'].partition(':')[2],r['right_key'].partition(':')[2],r['left_context'],r['right_context'])
    lookup={key(r):r for r in old_decisions['boundaries']}; groups=[]; pending=[]
    for i,row in enumerate(decisions['boundaries'],1):
        previous=lookup.get(key(row))
        if previous and previous.get('category') and previous.get('reason'):
            groups.append({'indexes':[i],**{k:previous[k] for k in ('category','reason','preserve','target_ms') if k in previous}})
        else:
            pending.append({'index':i,**row})
    return {'revision':decisions['revision'],'groups':groups,'pending':pending,
            'rebase':{'from_revision':old_decisions['revision'],'reused_boundaries':len(groups)}}


def rebase_caption(old_words, old_draft, words):
    """Reuse reviewed semantic pages only for a deletion-only, same-source edit."""
    if old_draft.get('review') != 'reviewed' or old_draft.get('revision') != old_words['revision']:
        raise ValueError('Caption rebase needs a reviewed matching draft')
    mapped=rebase_word_indexes(old_words,words); old=old_words['words']
    def span(a,b):
        indexes = [mapped[i] for i in range(a,b+1) if i in mapped]
        return [indexes[0],indexes[-1]] if indexes else None
    pages = []; changed = []
    for n,page in enumerate(old_draft['pages'],1):
        lines = [value for a,b in page['lines'] if (value:=span(a,b))]
        if not lines:
            changed.append(n); continue
        current = copy.deepcopy(page); current['lines'] = lines; current['emphasis'] = []
        if any(i not in mapped for a,b in page['lines'] for i in range(a,b+1)):
            changed.append(n)
        for emphasis in page.get('emphasis',[]):
            a,b = emphasis['first'],emphasis['last']; value = span(a,b)
            if value and any(i not in mapped for i in range(a,b+1)):
                raise ValueError('Partly deleted emphasis needs explicit semantic review')
            if value:
                current['emphasis'].append({**emphasis,'first':value[0],'last':value[1]})
        current.pop('takeaway',None)  # Recomputed from remaining words, not stale text.
        pages.append(current)
    corrections = [{**fix,'word':mapped[fix['word']]} for fix in old_draft.get('corrections',[]) if fix['word'] in mapped]
    return {'revision':words['revision'],'review':'reviewed','pages':pages,'corrections':corrections,
            'rebase':{'from_revision':old_words['revision'],'changed_pages':changed,
                      'removed_word_ids':[w['id'] for i,w in enumerate(old,1) if i not in mapped],
                      'method':'stable source IDs/text; deletion-only or evidenced direct timing lineage; timing/layout validation still required'}}


def compile_authored(words, plan, draft):
    """Collect page errors together. Authors never reconstruct text to look up IDs."""
    errors, pages, used = [], [], []
    words = copy.deepcopy(words)
    ws = words['words']
    if words['revision'] != plan['revision'] or draft.get('revision') != plan['revision']:
        return None, [{'error': 'Stale revision; recreate caption draft for this plan'}]
    corrected = set()
    for fix in draft.get('corrections', []):
        i = fix.get('word')
        if type(i) is not int or not 1 <= i <= len(ws) or i in corrected or not isinstance(fix.get('text'), str) or not fix.get('reason'):
            errors.append({'correction': fix, 'error': 'Unique word index, text and reason required'})
        else:
            ws[i - 1]['corrected_text'] = fix['text']
            corrected.add(i)
    for n, page in enumerate(draft.get('pages', []), 1):
        try:
            lines, ids, indexes = [], [], []
            for span in page['lines']:
                a, b = span
                if type(a) is not int or type(b) is not int or not 1 <= a <= b <= len(ws):
                    raise ValueError('Line range must be [first,last], inclusive indexes from caption TSV')
                indexes.extend(range(a, b + 1))
                line = [key(w) for w in ws[a - 1:b]]
                lines.append(line)
                ids.extend(line)
            used.extend(indexes)
            emphasis = []
            for span in page.get('emphasis', []):
                a, b = span['first'], span['last']
                if type(a) is not int or type(b) is not int or not 1 <= a <= b <= len(ws):
                    raise ValueError('Invalid emphasis indexes')
                emphasis.append({'word_keys': [key(w) for w in ws[a - 1:b]],
                                 'role': span['role'], 'reason': span['reason']})
            authored = {'lines': lines, 'emphasis': emphasis,
                        'takeaway': page.get('takeaway') or ''.join(word_text(ws[i - 1]) for i in indexes)}
            base = {'revision': plan['revision'], 'fps': plan['fps'], 'duration_frames': plan['duration_frames']}
            compile_pages({**words, 'words': [ws[i - 1] for i in indexes]}, {**base, 'pages': [authored]})
            pages.append(authored)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append({'page': n, 'error': str(exc)})
    if used != list(range(1, len(ws) + 1)):
        from collections import Counter
        counts = Counter(used)
        errors.append({'error': 'Word coverage/order mismatch',
                       'missing': [i for i in range(1, len(ws) + 1) if not counts[i]],
                       'duplicates': [i for i, count in counts.items() if count > 1]})
    if errors:
        return None, errors
    # Report all overlapping boundaries with page numbers, not a generic failure.
    fps = plan['fps']['num'] / plan['fps']['den']
    lookup = {key(w): w for w in ws}
    for i, (left, right) in enumerate(zip(pages, pages[1:]), 1):
        end = frame(lookup[left['lines'][-1][-1]]['final_end_s'], fps)
        start = frame(lookup[right['lines'][0][0]]['final_start_s'], fps)
        if end > start:
            errors.append({'page': i, 'next_page': i+1, 'error': 'Overlapping pages', 'overlap_frames': end-start})
    if errors:
        return None, errors
    try:
        return compile_pages(words, {**base, 'pages': pages}), []
    except ValueError as exc:
        return None, [{'error': str(exc)}]


def write_ass(captions, plan, target, font):
    from fontTools.ttLib import TTFont
    with TTFont(font) as face:
        family = face['name'].getDebugName(1)
        cmap = face.getBestCmap()
        missing = sorted({ch for c in captions['captions'] for line in c['lines'] for ch in line
                          if not ch.isspace() and ord(ch) not in cmap})
    if missing:
        raise ValueError('Font lacks characters: ' + ''.join(missing))
    if not family or any(c in family for c in ',\n\r'):
        raise ValueError('Invalid font family')
    fonts = target.parent / 'fonts'
    fonts.mkdir(exist_ok=True)
    shutil.copy2(font, fonts / font.name)
    if (font.parent / 'LICENSE.txt').exists():
        shutil.copy2(font.parent / 'LICENSE.txt', fonts / 'LICENSE.txt')
    width, height = plan['width'], plan['height']
    size = max(16, round(min(width, height) * .052))
    fps = plan['fps']['num'] / plan['fps']['den']
    def stamp(frame):
        cs = round(frame / fps * 100)
        return f'{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}'
    def escape(s):
        return s.replace('\\', '＼').replace('{', '｛').replace('}', '｝').replace('\n', ' ')
    rows = ['[Script Info]', 'ScriptType: v4.00+', f'PlayResX: {width}', f'PlayResY: {height}',
            'WrapStyle: 2', '[V4+ Styles]',
            'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
            f'Style: Default,{family},{size},&H00FFFFFF,&H00FFFFFF,&H00202020,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,{width // 20},{width // 20},{height // 9},1',
            '[Events]', 'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    for c in captions['captions']:
        starts = {s['start_char']: s for s in c['emphasis']}
        ends = {s['end_char'] for s in c['emphasis']}
        text, offset = '', 0
        for ln, line in enumerate(c['lines']):
            if ln:
                text += '\\N'
            for ch in line:
                if offset in ends:
                    text += '{\\r}'
                if offset in starts:
                    color = '&H00C8D250&' if starts[offset]['role'] == 'structure' else '&H00FFFFFF&'
                    text += '{\\b1\\c' + color + '}'
                text += escape(ch)
                offset += 1
        rows.append(f"Dialogue: 0,{stamp(c['start_frame'])},{stamp(c['end_frame'])},Default,,0,0,0,,{text}")
    target.write_text('\n'.join(rows) + '\n', encoding='utf-8')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['index', 'select', 'pause-author', 'pause-rebase', 'caption-draft', 'caption-build', 'caption-rebase'])
    for name in ['transcript', 'selection', 'review', 'source', 'ffprobe', 'words', 'plan', 'draft', 'out', 'decisions', 'edits', 'old-words', 'old-decisions']:
        p.add_argument('--' + name, type=Path)
    p.add_argument('--font', type=Path, default=FONT)
    p.add_argument('--fps', type=int, help='Output FPS override; defaults to the exact source frame rate.')
    p.add_argument('--width', type=int); p.add_argument('--height', type=int)
    a = p.parse_args(argv)
    if a.action == 'index':
        result = index(load(a.transcript), a.out)
    elif a.action == 'select':
        result = select(load(a.transcript), load(a.selection), a.source, a.ffprobe, a.out, a.fps, a.width, a.height,
                        load(a.review) if a.review else None)
    elif a.action == 'caption-draft':
        result = caption_draft(load(a.words), load(a.plan), a.out)
    elif a.action == 'pause-author':
        authored = author_pauses(load(a.decisions), load(a.draft))
        save(a.out, authored)
        result = {'status': 'pass', 'boundaries': len(authored['boundaries']), 'out': str(a.out)}
    elif a.action == 'caption-rebase':
        if a.out.exists():
            raise FileExistsError('Caption rebase uses a new output; existing drafts remain unchanged')
        draft = rebase_caption(load(a.old_words),load(a.draft),load(a.words))
        save(a.out,draft)
        result = {'status':'pass','out':str(a.out),'pages':len(draft['pages']),**draft['rebase']}
    elif a.action == 'pause-rebase':
        if a.out.exists():
            raise FileExistsError('Pause rebase uses a new output')
        draft=rebase_pauses(load(a.old_decisions),load(a.decisions),load(a.old_words),load(a.words))
        save(a.out,draft)
        result={'status':'decision_required' if draft['pending'] else 'pass','out':str(a.out),
                'pending':draft['pending'],**draft['rebase']}
    else:
        plan = load(a.plan)
        draft = load(a.draft)
        if a.edits:
            draft = merge_caption_edits(draft, load(a.edits))
        result, errors = compile_authored(load(a.words), plan, draft)
        save(a.out / 'validation.json', {'status': 'fail' if errors else 'pass', 'errors': errors})
        if errors:
            print(json.dumps({'status': 'fail', 'errors': errors}, ensure_ascii=False)); return 1
        a.out.mkdir(parents=True, exist_ok=True)
        write_ass(result, plan, a.out / 'captions.ass', a.font)
        save(a.out / 'captions.json', result)
        save(a.out / 'authoring.resolved.json', draft)
        write_srt(result, a.out / 'captions.srt')
        result = {'status': 'pass', 'pages': len(result['captions']), 'warnings': result['warnings'], 'out': str(a.out)}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
