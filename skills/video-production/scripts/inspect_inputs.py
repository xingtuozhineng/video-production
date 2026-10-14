"""Bounded, read-only views of script documents and canonical talking-head data."""
import argparse
import json
import math
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET


def document_rows(path):
    if path.suffix.lower() == '.docx':
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read('word/document.xml'))
        ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        lines = [''.join(node.text or '' for node in p.iter(ns + 't')) for p in root.iter(ns + 'p')]
    elif path.suffix.lower() in ('.txt', '.md'):
        lines = path.read_text(encoding='utf-8-sig').splitlines()
    else:
        raise ValueError('Script input must be DOCX, UTF-8 TXT or Markdown')
    return [{'index': i, 'text': text} for i, text in enumerate(lines, 1)]


def word_rows(doc):
    rows = []
    for i, word in enumerate(doc['words'], 1):
        rows.append({'index': i, 'id': word['id'], 'instance_id': word.get('instance_id'),
                     'utterance_id': word.get('utterance_id'), 'channel_id': word.get('channel_id', 0),
                     'speaker_id': word.get('effective_speaker_id', word.get('speaker_id')),
                     'start_s': word.get('final_start_s', word.get('source_start_s')),
                     'end_s': word.get('final_end_s', word.get('source_end_s')),
                     'text': word.get('corrected_text', word.get('text', '')) + word.get('punctuation', '')})
    return rows


def utterance_rows(doc, blocks=False):
    groups = []
    for word in word_rows(doc):
        fields = ('instance_id', 'channel_id', 'speaker_id') if blocks else ('instance_id', 'utterance_id', 'channel_id', 'speaker_id')
        key = tuple(word[k] for k in fields)
        adjacent = not blocks or groups and word['start_s'] - groups[-1]['end_s'] <= 1.2
        if groups and groups[-1]['key'] == key and adjacent:
            row = groups[-1]
            row.update(last=word['index'], end_s=word['end_s'], text=row['text'] + word['text'])
        else:
            groups.append({**word, 'key': key, 'first': word['index'], 'last': word['index']})
    return [{k: v for k, v in row.items() if k not in ('key', 'id', 'index')} for row in groups]


def join_rows(plan, words):
    if not words or words['revision'] != plan['revision']:
        raise ValueError('Joins require mapped words with the same revision as the plan')
    groups = {}
    for word in words['words']:
        groups.setdefault(word['instance_id'], []).append(word)
    rows = []
    for i, (left, right) in enumerate(zip(plan['segments'], plan['segments'][1:]), 1):
        a, b = groups.get(left['id'], []), groups.get(right['id'], [])
        text = lambda ws: ''.join(w.get('corrected_text', w.get('text', '')) + w.get('punctuation', '') for w in ws)
        rows.append({'index': i, 'left_segment': left['id'], 'right_segment': right['id'],
                     'final_s': left['final_out_s'], 'source_left_end_s': left['source_out_s'],
                     'source_right_start_s': right['source_in_s'],
                     'left_word': a[-1]['id'] if a else None, 'right_word': b[0]['id'] if b else None,
                     'gap_ms': round((b[0]['final_start_s'] - a[-1]['final_end_s']) * 1000, 5) if a and b else None,
                     'left_text': text(a[-10:]), 'right_text': text(b[:10]),
                     'listening': 'not_checked'})
    return rows


def selection_rows(selection, transcript):
    if not transcript or selection.get('source_revision') != transcript.get('revision'):
        raise ValueError('Selection review requires its source transcript revision')
    words = transcript['words']
    text = lambda ws: ''.join(w.get('corrected_text', w.get('text', '')) for w in ws)
    rows = []
    for i, item in enumerate(selection['ranges'], 1):
        a, b = item['first'], item['last']
        if type(a) is not int or type(b) is not int or not 1 <= a <= b <= len(words):
            raise ValueError(f'Invalid selection range {i}')
        rows.append({'index': i, 'first_word': a, 'last_word': b,
                     'start_s': words[a-1]['source_start_s'], 'end_s': words[b-1]['source_end_s'],
                     'first_text': text(words[a-1:min(a+2,b)]), 'last_text': text(words[max(a-1,b-3):b]),
                     'before': text(words[max(0,a-4):a-1]), 'after': text(words[b:b+3]),
                     'text': text(words[a-1:b]), 'reason': item.get('reason', '')})
    return rows


def inspect(kind, doc, *, words=None, span=None, window=None, text=None, offset=0, limit=40, max_chars=200):
    if not 1 <= limit <= 200 or offset < 0 or not 1 <= max_chars <= 4000:
        raise ValueError('limit 1..200, nonnegative offset, max-text-chars 1..4000 required')
    if span and (span[0] < 1 or span[1] < span[0]):
        raise ValueError('Range must be positive inclusive FIRST:LAST indexes')
    if window and (not all(math.isfinite(v) for v in window) or not 0 <= window[0] < window[1]):
        raise ValueError('Time window must be finite START:END seconds')
    if window and kind == 'script':
        raise ValueError('Script paragraphs have no media time; use paragraph range or text')
    if kind == 'script':
        rows = doc
    elif kind == 'words':
        rows = word_rows(doc)
    elif kind in ('utterances', 'blocks'):
        rows = utterance_rows(doc, blocks=kind == 'blocks')
    elif kind == 'pauses':
        rows = [{'index': i, **row} for i, row in enumerate(doc['boundaries'], 1)]
    elif kind == 'joins':
        rows = join_rows(doc, words)
    elif kind == 'selection':
        rows = selection_rows(doc, words)
    else:
        raise ValueError('Unknown inspection kind')
    matched = []
    for row in rows:
        if span and (row.get('last', row.get('index')) < span[0] or row.get('first', row.get('index')) > span[1]):
            continue
        if window:
            start = row.get('start_s', row.get('final_s', row.get('source_left_end_s')))
            end = row.get('end_s', start)
            overlap = (start is not None and end is not None and
                       (start < window[1] and end > window[0] if end > start
                        else window[0] <= start < window[1]))
            if not overlap:
                continue
        if text and text.casefold() not in ' '.join(v for v in row.values() if isinstance(v, str)).casefold():
            continue
        matched.append(row)
    result = []
    for row in matched[offset:offset + limit]:
        item = dict(row)
        for field in ('text', 'left_context', 'right_context', 'left_text', 'right_text', 'reason'):
            value = item.get(field)
            if isinstance(value, str) and len(value) > max_chars:
                item[field + '_chars'] = len(value)
                item[field + '_truncated'] = True
                item[field] = value[:max_chars]
        result.append(item)
    return {'kind': kind, 'revision': doc.get('revision') if isinstance(doc, dict) else None,
            'total': len(rows), 'matched': len(matched), 'offset': offset, 'rows': result,
            'next_offset': offset + len(result) if offset + len(result) < len(matched) else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', required=True, choices=('script', 'words', 'utterances', 'blocks', 'pauses', 'joins', 'selection'))
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--words', type=Path)
    parser.add_argument('--range', dest='span', help='Inclusive original word/paragraph/boundary FIRST:LAST indexes')
    parser.add_argument('--time', dest='window', help='START:END seconds; final time for mapped words')
    parser.add_argument('--text', help='Literal substring; not fuzzy alignment or content selection')
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--limit', type=int, default=40)
    parser.add_argument('--max-text-chars', type=int, default=200)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    try:
        load = lambda p: json.loads(p.read_text(encoding='utf-8-sig'))
        doc = document_rows(args.input) if args.kind == 'script' else load(args.input)
        span = tuple(map(int, args.span.split(':'))) if args.span else None
        window = tuple(map(float, args.window.split(':'))) if args.window else None
        if any(value is not None and len(value) != 2 for value in (span, window)):
            raise ValueError('Range/time requires exactly two values separated by colon')
        result = inspect(args.kind, doc, words=load(args.words) if args.words else None,
                         span=span, window=window, text=args.text, offset=args.offset,
                         limit=args.limit, max_chars=args.max_text_chars)
        if args.out:
            if args.out.resolve() in {p.resolve() for p in (args.input, args.words) if p}:
                raise ValueError('Inspection output must not overwrite an input')
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(json.dumps({'status': 'fail', 'error': str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
