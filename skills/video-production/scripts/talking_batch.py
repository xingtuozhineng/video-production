"""Batch mechanical talking-head work; return complete, indexed decision packets."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import wave

HERE = Path(__file__).resolve().parent
TALKING = HERE.parents[1] / 'talking-head-cut/scripts'
sys.path.insert(0, str(TALKING))
from authoring import load, save, digest, index, select, author_pauses, caption_draft
from compile_timeline import compile_timeline
from inspect_inputs import utterance_rows, selection_rows, join_rows
from speech_checks import asr_candidates
from managed_job import lock
from source_audio_gate import retained_observations, require_source_review
from mixed_speech import candidates as mixed_candidates, retained_candidates as retained_mixed
from asr_repair import repair as repair_asr, correct_boundaries
from recording_review import transcript_hash
from pre_render_review import prepare as prepare_pre_render


def text(words):
    return ''.join(w.get('corrected_text', w.get('text', '')) + w.get('punctuation', '') for w in words)


def indexed(words, first=1):
    return ' '.join(f'{i}:{text([w])}' for i, w in enumerate(words, first))


def levels(audio, ranges):
    """Measure a decoded PCM once for a whole batch; loudness never assigns roles."""
    import numpy as np
    with wave.open(str(audio), 'rb') as wav:
        if wav.getsampwidth() != 2:
            raise ValueError('Batch analysis requires PCM16')
        rate = wav.getframerate()
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(float)
        data = data.reshape(-1, wav.getnchannels()) / 32768
    result = []
    for a, b in ranges:
        clip = data[max(0, round(a * rate)):max(0, round(b * rate))]
        result.append({'rms_dbfs': round(20 * math.log10(max(1e-10, float(np.sqrt(np.mean(clip**2))))), 1),
                       'peak_dbfs': round(20 * math.log10(max(1e-10, float(np.max(abs(clip))))), 1)}
                      if clip.size else {'rms_dbfs': None, 'peak_dbfs': None})
    return result


def source_packet(doc, audio):
    rows = utterance_rows(doc)
    measures = levels(audio, [(r['start_s'], r['end_s']) for r in rows])
    lines = ['# 原片完整审核表', f'revision: {doc["revision"]}',
             '每行包含完整原文及每个源词索引；音量、相似度只作线索，不自动判断角色或删除。',
             '句号\t源词范围\t源秒\tchannel/speaker\tRMS/peak dBFS\t时码状态\t相似近邻句号\t索引:原文']
    for n, (r, measure) in enumerate(zip(rows, measures), 1):
        candidates = [str(i + 1) for i in range(max(0, n - 7), n - 1)
                      if min(len(rows[i]['text']), len(r['text'])) >= 4
                      and SequenceMatcher(None, rows[i]['text'], r['text'], autojunk=False).ratio() >= .65]
        timing='UNRELIABLE_UNIFORM' if any(w.get('timing_status')=='uniform_provider_timing' for w in doc['words'][r['first']-1:r['last']]) else 'normal'
        lines.append(f'{n}\t{r["first"]}:{r["last"]}\t{r["start_s"]:.3f}:{r["end_s"]:.3f}'
                     f'\t{r["channel_id"]}/{r["speaker_id"]}\t{measure["rms_dbfs"]}/{measure["peak_dbfs"]}'
                     f'\t{timing}\t{",".join(candidates)}\t{indexed(doc["words"][r["first"]-1:r["last"]], r["first"])}')
    anomalies = asr_candidates(doc)
    lines += ['\n## 时码异常候选', json.dumps(anomalies, ensure_ascii=False)]
    return '\n'.join(lines) + '\n', rows


def representative_windows(rows):
    result = []
    speakers = sorted({(str(r['channel_id']), str(r['speaker_id'])) for r in rows})
    for channel, speaker in speakers:
        group = [r for r in rows if (str(r['channel_id']), str(r['speaker_id'])) == (channel, speaker)
                 and r['end_s'] - r['start_s'] >= .6]
        if speaker.startswith('repair-') and group:
            indexes=[max(range(len(group)),key=lambda i:group[i]['end_s']-group[i]['start_s'])]
        else:
            indexes=sorted({0,len(group)//2,len(group)-1}) if group else []
        for i in indexes:
            r = group[i]
            result.append((r['start_s'], min(r['end_s'], r['start_s'] + 4), f'c{channel}-s{speaker}'))
    return result


def evidence(source, windows, out, ffmpeg):
    from PIL import Image, ImageDraw
    out.mkdir(parents=True, exist_ok=True)
    images, entries = [], []
    for n, (a, b, label) in enumerate(windows):
        if not all(math.isfinite(v) for v in (a, b)) or not 0 <= a < b:
            raise ValueError('Evidence requires finite nonempty source ranges')
        for j, fraction in enumerate((.2, .5, .8)):
            second = a + (b - a) * fraction
            target = out / f'{n:03}-{j}.jpg'
            subprocess.run([str(ffmpeg), '-v', 'error', '-y', '-ss', str(second), '-i', str(source),
                            '-frames:v', '1', '-vf', 'scale=180:320:force_original_aspect_ratio=decrease,pad=180:320:(ow-iw)/2:(oh-ih)/2',
                            str(target)], check=True, capture_output=True)
            with Image.open(target) as im:
                tile = Image.new('RGB', (180, 348), 'white'); tile.paste(im, (0, 28))
            ImageDraw.Draw(tile).text((3, 3), f'{label} {second:.3f}s', fill='black')
            images.append(tile); entries.append({'source_s': second, 'path': str(target), 'label': label})
    sheets = []
    for page in range(0, len(images), 18):
        tiles = images[page:page+18]
        columns = 3 if len(tiles) >= 6 else 1
        sheet = Image.new('RGB', (180*columns, 348*math.ceil(len(tiles)/columns)), 'white')
        for i, im in enumerate(tiles):
            sheet.paste(im, ((i % columns)*180, (i//columns)*348))
        target = out / f'contact-{page//18+1}.jpg'; sheet.save(target); sheets.append(str(target))
    result = {'frames': entries, 'sheets': sheets, 'visual_review': 'not_checked'}
    save(out / 'manifest.json', result)
    return result


def audio_summary(report):
    issues = []
    for row in report.get('results', []):
        issues.append({'job': row['job']['id'], 'source_or_final': row['job']['media'],
                       'start_s': row['job']['start_s'], 'end_s': row['job']['end_s'], 'result': row.get('result')})
    # Keep every observation, but omit transport data, prompts, hashes and request bookkeeping.
    return {**{k: report.get(k) for k in ('status', 'stage', 'scope', 'source_sha256', 'revision', 'verdicts', 'completed_requests',
              'required_requests', 'reviewed_cuts', 'required_cuts', 'cost_from_usage_cny', 'report_path')},
            'observations': issues, 'human_listening': 'not_checked'}


def write_packet(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8')
    return str(path)


class Batch:
    def __init__(self, workspace, project, env=None):
        self.workspace, self.project = workspace.resolve(), project.resolve()
        self.production = load(self.project / 'production.json')
        manifest = load(self.project / 'input/source-manifest.json')
        if len(manifest['sources']) != 1:
            raise ValueError('Batch talking-head requires one original source')
        self.source = Path(manifest['sources'][0]['path']).resolve()
        self.tools = load(self.workspace / 'video-production-deps/tools.json')
        self.env = env or HERE.parent / '.env'
        self.work = self.project / 'work'
        self.transcript = self.work / 'asr/transcript.source.json'
        self.state_path = self.work / 'batch/state.json'

    def command(self, name, script, *arguments):
        log = self.work / 'batch/logs' / (name + '.log')
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a', encoding='utf-8') as stream:
            result = subprocess.run([self.tools.get('python', sys.executable), str(script), *map(str, arguments)],
                                    stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            if name == 'caption-build' and (self.work/'captions/validation.json').exists():
                validation = load(self.work/'captions/validation.json')
                if validation.get('status') == 'fail' and validation.get('errors'):
                    raise ValueError(json.dumps(validation, ensure_ascii=False))
            raise ValueError(f'{name} failed ({result.returncode}); read {log}')

    def review_audio(self, stage, handoff=None):
        import audio_review
        return audio_review.run(self.source, self.project, audio_review.config(self.env),
             self.tools['ffmpeg'], self.tools['ffprobe'],
             Path(handoff['outputs']['mp4']) if handoff else None,
             self.work / 'compiled/edit-plan.json' if handoff else None, stage, True)

    def selection_fingerprint(self):
        return digest([load(self.transcript), load(self.work/'index/selection.json'),
                       load(self.work/'index/recording-review.json'),
                       {'path': str(self.source), 'size': self.source.stat().st_size,
                        'mtime': self.source.stat().st_mtime_ns}])

    def source_document(self):
        doc=load(self.transcript); path=self.work/'asr/source-boundary-edits.json'
        if not path.exists():
            return doc
        edits=load(path)
        if doc.get('source_parent_revision')==edits.get('source_revision') and doc.get('source_timing_corrections')==edits.get('corrections'):
            return doc
        revised=correct_boundaries(doc,edits)
        review_path=self.work/'index/recording-review.json'
        if review_path.exists():
            review=load(review_path)
            if review.get('source_revision')==doc['revision'] and review.get('transcript_hash')==transcript_hash(doc):
                review.update(source_revision=revised['revision'],transcript_hash=transcript_hash(revised))
                save(review_path,review)
        selection_path=self.work/'index/selection.json'
        if selection_path.exists():
            selection=load(selection_path)
            if selection.get('source_revision')==doc['revision']:
                selection['source_revision']=revised['revision']; save(selection_path,selection)
        save(self.transcript,revised)
        return revised

    def refresh_mixed(self,doc):
        path=self.work/'batch/mixed-speech.json'
        if not path.exists() or load(path)['source_revision']==doc['revision']:
            return
        old=load(path)
        if old['source_revision']!=doc.get('source_parent_revision'):
            raise ValueError('Mixed-speech candidates stale; rerun prepare')
        new=mixed_candidates(doc,levels(self.work/'asr/analysis.wav',[(w['source_start_s'],w['source_end_s']) for w in doc['words']]))
        key=lambda r:(r['first'],r['last'],r['source_start_s'],r['source_end_s'],r['text'],tuple(r['word_rms_dbfs']))
        before={key(r):r['event_id'] for r in old['candidates']}
        mapping={before[key(r)]:r['event_id'] for r in new['candidates'] if key(r) in before}
        review=load(self.work/'index/recording-review.json')
        for observation in review.get('audio_observations',[]):
            if observation.get('event_id') in mapping:
                previous=observation['event_id']; observation['event_id']=mapping[previous]
                observation['rebased_from_event_id']=previous
        save(path,new); save(self.work/'index/recording-review.json',review)

    def source_review(self):
        summary_path = self.work/'batch/source-audio.json'
        if not summary_path.exists():
            prepared = self.work/'batch/prepare-result.json'
            if prepared.exists() and load(prepared).get('audio_review_status') not in (None, 'not_requested'):
                raise ValueError('Requested source listening report missing; resume the same prepare/audio-review stage')
            return None
        # Read the authoritative report again after a locally recovered provider response.
        report = load(load(summary_path)['report_path'])
        if Path(report['source']).resolve() != self.source:
            raise ValueError('Source listening report belongs to another source')
        return report

    def prepare(self, execute, audio):
        if not execute:
            return {'status': 'dry_run', 'next': 'Use --execute for ASR; --audio-review also authorizes the shared 0.2 CNY listening budget'}
        args = ['--media', self.source, '--out', self.work/'asr', '--ffmpeg', self.tools['ffmpeg'], '--env', self.env, '--execute']
        def recognize_and_repair():
            from bailian_media import config
            self.command('transcribe', HERE/'bailian_asr.py', *args)
            original=load(self.transcript)
            doc,repair_report=repair_asr(original,self.work/'asr/analysis.wav',self.work/'asr/repairs',
                                        self.tools['ffmpeg'],config(self.env),True)
            if repair_report['status'].startswith('repaired'):
                save(self.work/'asr/transcript.original.json',original)
                save(self.transcript,doc)
            return repair_report
        with ThreadPoolExecutor(max_workers=2) as pool:
            recognition = pool.submit(recognize_and_repair)
            listening = pool.submit(self.review_audio, 'source') if audio else None
            repair_report=recognition.result()
            report = listening.result() if listening else None
        doc = self.source_document()
        result = index(doc, self.work/'index')
        packet, rows = source_packet(doc, self.work/'asr/analysis.wav')
        mixed=mixed_candidates(doc,levels(self.work/'asr/analysis.wav',
                                [(w['source_start_s'],w['source_end_s']) for w in doc['words']]))
        save(self.work/'batch/mixed-speech.json',mixed)
        packet += '\n## 句内局部声能/时码候选（不是自动删除表）\n'
        packet += '\n'.join(json.dumps(row,ensure_ascii=False) for row in mixed['candidates'])+'\n'
        repair_summary={k:v for k,v in repair_report.items() if k!='unresolved_word_ids'}
        repair_summary['unresolved_words']=len(repair_report.get('unresolved_word_ids',[]))
        packet += '\n## 局部时码修复\n'+json.dumps(repair_summary,ensure_ascii=False)+'\n'
        frames = evidence(self.source, representative_windows(rows), self.work/'batch/source-frames', self.tools['ffmpeg'])
        if report:
            save(self.work/'batch/source-audio.json', audio_summary(report))
            packet += '\n## 原片音频模型观察\n' + json.dumps(audio_summary(report), ensure_ascii=False) + '\n'
        packet += '\n## 代表画面\n' + json.dumps(frames['sheets'], ensure_ascii=False) + '\n'
        return {**result, 'status': 'decision_required', 'decision': 'roles_and_selection',
                'packet': write_packet(self.work/'batch/source-review.md', packet), 'sheets': frames['sheets'],
                'audio_review_status': report.get('status') if report else 'not_requested',
                'asr_repair':repair_summary,'mixed_speech_candidates':len(mixed['candidates'])}

    def selection(self):
        doc=self.source_document(); self.refresh_mixed(doc)
        index(doc,self.work/'index')
        selection = load(self.work/'index/selection.json')
        result = select(doc, selection, self.source, self.tools['ffprobe'], self.work/'edit',
                        review=load(self.work/'index/recording-review.json'))
        save(self.work/'batch/selection-input.json', {'fingerprint': self.selection_fingerprint()})
        rows = selection_rows(selection, doc)
        decisions = load(self.work/'edit/pause-decisions.json')
        ranges = [(r['source_left_end_s'], r['source_right_start_s']) for r in decisions['boundaries']]
        measures = levels(self.work/'asr/analysis.wav', ranges)
        body = ['# 保留范围和全部气口审核', f'revision: {result["revision"]}',
                '确认保留范围的全文、边缘和段内混声后，批量填写 pause-authoring.json；电平不等于纯静音或身份。',
                '\n## 保留范围（完整原文，前后为被排除相邻词）']
        report = self.source_review()
        conflicts = retained_observations(load(self.work/'edit/selection-plan.json'), report,
                                         load(self.work/'index/recording-review.json')) if report else []
        mixed_path=self.work/'batch/mixed-speech.json'
        if mixed_path.exists():
            mixed=load(mixed_path)
            if mixed['source_revision']!=doc['revision']:
                raise ValueError('Mixed-speech candidates stale; rerun prepare for the current transcript')
            conflicts += retained_mixed(load(self.work/'edit/selection-plan.json'),mixed,load(self.work/'index/recording-review.json'))
        if report or conflicts:
            body[3:3] = ['\n## 先处理：原片听审与保留区间的交叉疑点',
                         '听审覆盖状态：' + (report['status'] if report else 'not_requested'),
                         *[json.dumps(row, ensure_ascii=False) for row in conflicts],
                         '未解决候选或原片听审不完整时，compile/deliver会拦截。普通已排除片段不重复审。']
        for row in rows:
            body.append(json.dumps(row, ensure_ascii=False))
        body += ['\n## 全部气口', 'index\tkind\t原间隔ms\t源左尾:右首\tRMS/peak dBFS\t左文 → 右文']
        for n, (row, measure) in enumerate(zip(decisions['boundaries'], measures), 1):
            body.append(f'{n}\t{row["kind"]}\t{row["old_gap_ms"]}\t{row["source_left_end_s"]}:{row["source_right_start_s"]}'
                        f'\t{measure["rms_dbfs"]}/{measure["peak_dbfs"]}\t{row["left_context"]} → {row["right_context"]}')
        return {**result, 'status': 'decision_required', 'decision': 'selection_review_and_pauses',
                'packet': write_packet(self.work/'batch/selection-review.md', '\n'.join(body)+'\n'),
                'pause_draft': str(self.work/'pause-authoring.json'), 'boundaries': len(decisions['boundaries']),
                'source_audio_status': report['status'] if report else 'not_requested',
                'blocking_source_observations': [r for r in conflicts if r['status'] == 'review_required']}

    def compile(self):
        self.require_selection()
        authored = author_pauses(load(self.work/'edit/pause-decisions.json'), load(self.work/'pause-authoring.json'))
        result = compile_timeline(load(self.work/'edit/selection-plan.json'), load(self.transcript), authored)
        for name, doc in zip(('edit-plan.json', 'mapped-words.json', 'pause-report.json', 'timeline-check.json'), result):
            save(self.work/'compiled'/name, doc)
        save(self.work/'edit/pause-decisions.json', authored)
        plan, words = result[:2]
        draft = self.work/'caption-drafts'/plan['revision']/'draft.json'
        if not draft.exists():
            caption_draft(words, plan, draft)
        save(self.work/'batch/compiled-input.json', {'pauses': digest(load(self.work/'pause-authoring.json')),
                                                  'revision': plan['revision'], 'draft': str(draft)})
        body = ['# 完整字幕审核表', f'revision: {plan["revision"]}',
                '每页词索引为成片索引；完整正文保留，批量提交 caption-edits.json。正常页无需改写。',
                draft.with_suffix('.review.tsv').read_text(encoding='utf-8'), '\n## 每页精确词索引']
        for n, page in enumerate(load(draft)['pages'], 1):
            a, b = page['lines'][0][0], page['lines'][-1][1]
            body.append(f'{n}\t' + indexed(words['words'][a-1:b], a))
        body += ['\n## 编译后接缝（与同一时间轴对应）']
        body += [json.dumps(r, ensure_ascii=False) for r in join_rows(plan, words)]
        return {'status': 'decision_required', 'decision': 'captions', 'revision': plan['revision'],
                'packet': write_packet(self.work/'batch/caption-review.md', '\n'.join(body)+'\n'),
                'draft': str(draft), 'edits': str(self.work/'caption-edits.json'),
                'segments': len(plan['segments']), 'duration_s': plan['duration_s']}

    def require_selection(self):
        if load(self.work/'batch/selection-input.json')['fingerprint'] != self.selection_fingerprint():
            raise ValueError('Selection inputs changed; rerun talking-batch --stage select before compile/deliver')
        unreliable=[{'id':w['id'],'text':w.get('text'),'source_start_s':w['source_start_s'],'source_end_s':w['source_end_s']}
                    for w in load(self.work/'edit/words.selected.json')['words'] if w.get('timing_status')=='uniform_provider_timing']
        if unreliable:
            raise ValueError(json.dumps({'error':'Selected words still have uniform/unreliable provider timing; choose an intact reliable take or re-align the actual source, never override by claiming reviewed',
                                         'words':unreliable},ensure_ascii=False))
        report = self.source_review()
        if report:
            require_source_review(load(self.work/'edit/selection-plan.json'), report,
                                  load(self.work/'index/recording-review.json'))
        mixed_path=self.work/'batch/mixed-speech.json'
        if mixed_path.exists():
            mixed=load(mixed_path)
            if mixed['source_revision']!=load(self.transcript)['revision']:
                raise ValueError('Mixed-speech candidates stale; rerun prepare')
            pending=[r for r in retained_mixed(load(self.work/'edit/selection-plan.json'),mixed,
                     load(self.work/'index/recording-review.json')) if r['status']=='review_required']
            if pending:
                raise ValueError(json.dumps({'error':'Retained local voice/timing candidates need source verification before render',
                                            'candidates':pending},ensure_ascii=False))

    def review(self, execute, audio, caption_draft=None):
        self.require_selection()
        compiled = load(self.work/'batch/compiled-input.json')
        if compiled['pauses'] != digest(load(self.work/'pause-authoring.json')):
            raise ValueError('Pause decisions changed; rerun talking-batch --stage compile')
        if compiled['revision'] != load(self.work/'compiled/edit-plan.json')['revision']:
            raise ValueError('Compiled timeline changed; rerun talking-batch --stage compile')
        if audio and not execute:
            raise ValueError('--audio-review requires --execute; no render started')
        edits = self.work/'caption-edits.json'
        if not edits.exists() or load(edits).get('review') != 'reviewed':
            raise ValueError('Read the complete caption packet and save caption-edits.json with review=reviewed (empty edits allowed)')
        self.command('caption-build', TALKING/'authoring.py', 'caption-build', '--words', self.work/'compiled/mapped-words.json',
                     '--plan', self.work/'compiled/edit-plan.json', '--draft', caption_draft or compiled['draft'], '--edits', edits,
                     '--out', self.work/'captions')
        return prepare_pre_render(self.workspace, self.project, self.work/'compiled/edit-plan.json',
                                  self.work/'captions', execute, audio, self.env)

    def deliver(self, execute, audio, caption_draft=None):
        reviewed = self.review(execute, audio, caption_draft)
        if reviewed['status'] != 'ready_for_render':
            return reviewed  # No video job is launched with a missing/stale review or unresolved changes.
        self.command('deliver', HERE/'delivery.py', '--workspace-root', self.workspace,
                     '--plan', self.work/'compiled/edit-plan.json', '--captions-dir', self.work/'captions', '--out-dir', self.project/'output')
        handoff = load(self.project/'output/handoff.json')
        report = self.review_audio('final', handoff) if audio or reviewed.get('audio_review_requested') else None
        if report:
            save(self.work/'batch/final-audio.json', audio_summary(report))
        return {'status': 'review_required', 'handoff': str(self.project/'output/handoff.json'),
                'audio_review': str(self.work/'batch/final-audio.json') if report else None,
                'audio_status': report.get('status') if report else 'not_requested',
                'human_listening': 'not_checked'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('workspace-root', 'project-dir'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--stage', choices=('prepare', 'select', 'compile', 'review', 'deliver', 'evidence'), required=True)
    parser.add_argument('--ranges', help='Source seconds START:END,... for an additional evidence batch')
    parser.add_argument('--env', type=Path)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--audio-review', action='store_true')
    parser.add_argument('--caption-draft', type=Path, help='Reviewed rebased caption draft for review/deliver; ordinary runs use the compiled draft')
    args = parser.parse_args(argv)
    batch = Batch(args.workspace_root, args.project_dir, args.env)
    started = time.time()
    try:
        with lock(batch.work/'batch/.lock'):
            if args.stage in ('review', 'deliver'):
                result = getattr(batch, args.stage)(args.execute, args.audio_review, args.caption_draft)
            elif args.stage == 'prepare':
                result = getattr(batch, args.stage)(args.execute, args.audio_review)
            elif args.stage == 'evidence':
                windows = [(*map(float, pair.split(':')), f'window-{i}') for i, pair in enumerate((args.ranges or '').split(','), 1)]
                result = evidence(batch.source, windows, batch.work/'batch/evidence'/digest(windows), batch.tools['ffmpeg'])
            else:
                result = getattr(batch, 'selection' if args.stage == 'select' else args.stage)()
            result.update(stage=args.stage, started_at=started, finished_at=time.time(), elapsed_seconds=round(time.time()-started, 3))
            save(batch.work/'batch'/f'{args.stage}-result.json', result)
            save(batch.state_path, result)
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        result = {'status': 'fail', 'stage': args.stage, 'error': str(exc), 'elapsed_seconds': round(time.time()-started, 3)}
        save(batch.work/'batch'/f'{args.stage}-error.json', result)
        print(json.dumps(result, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
