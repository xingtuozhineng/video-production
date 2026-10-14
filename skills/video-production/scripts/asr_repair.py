"""Re-recognize demonstrably uniform provider word timing before editorial indexing."""
import copy
import json
import math
from pathlib import Path
import subprocess
from bailian_media import load, save, fingerprint, config
from inspect_inputs import utterance_rows
import bailian_asr


def correct_boundaries(doc, edits):
    """Apply evidenced boundary corrections; raw provider response/IDs/text stay intact."""
    if edits.get('source_revision') != doc['revision']:
        raise ValueError('Stale source boundary edits')
    result=copy.deepcopy(doc); ws=result['words']; positions={w['id']:i for i,w in enumerate(ws)}; used=set()
    for fix in edits.get('corrections',[]):
        wid=fix.get('word_id')
        if wid not in positions or wid in used or not str(fix.get('evidence','')).strip():
            raise ValueError('Boundary edits need unique existing IDs and source evidence')
        i=positions[wid]; w=ws[i]; a,b=fix.get('new_range_s',[None,None])
        if fix.get('old_range_s') != [w['source_start_s'],w['source_end_s']]:
            raise ValueError('Boundary edit does not match original source times')
        if not all(type(v) in (float,int) and math.isfinite(v) for v in (a,b)) or not 0<=a<b:
            raise ValueError('Invalid corrected source boundary')
        if (i and a<ws[i-1]['source_end_s']) or (i+1<len(ws) and b>ws[i+1]['source_start_s']):
            raise ValueError('Boundary correction crosses an adjacent source word; re-recognize the whole window')
        w.update(source_start_s=a,source_end_s=b); used.add(wid)
    if not used:
        raise ValueError('Source boundary edits are empty')
    result.update(revision='source-boundary-'+fingerprint(edits)[:16],source_parent_revision=doc['revision'],
                  source_timing_corrections=copy.deepcopy(edits['corrections']))
    return result


def uniform_windows(doc):
    rows=utterance_rows(doc); found=[]
    for i,row in enumerate(rows):
        ws=doc['words'][row['first']-1:row['last']]
        lengths=[w['source_end_s']-w['source_start_s'] for w in ws]
        if (len(ws)<12 or row['end_s']-row['start_s']<2 or max(lengths)-min(lengths)>.0021
                or any(abs(b['source_start_s']-a['source_end_s'])>.0011 for a,b in zip(ws,ws[1:]))):
            continue
        left,right=rows[max(0,i-1)],rows[min(len(rows)-1,i+1)]
        entry={'first':left['first'],'last':right['last'],'start_s':left['start_s'],
               'end_s':right['end_s'],'suspect_first':row['first'],'suspect_last':row['last']}
        if found and entry['first']<=found[-1]['last']:
            found[-1]['last']=max(found[-1]['last'],entry['last']); found[-1]['end_s']=max(found[-1]['end_s'],entry['end_s'])
            found[-1]['suspect_last']=entry['suspect_last']
        else: found.append(entry)
    return found


def splice(doc, replacements):
    result=copy.deepcopy(doc); words=[]; cursor=0; lineage=[]
    for window, local, directory in sorted(replacements,key=lambda r:r[0]['first']):
        a,b=window['first']-1,window['last']
        if a<cursor: raise ValueError('Overlapping ASR replacement windows')
        prefix='repair-'+fingerprint(window)[:8]
        corrected=[]
        for original in local['words']:
            word=copy.deepcopy(original)
            word['id']=prefix+':'+word['id']; word['utterance_id']=prefix+':'+word['utterance_id']
            word['effective_speaker_id']=prefix+':'+str(word.get('speaker_id'))
            for name in ('start','end','source_start_s','source_end_s'):
                if name in word: word[name]+=window['start_s']
            word['asr_window_offset_s']=window['start_s']
            corrected.append(word)
        if not corrected: raise ValueError('Repair produced no timed words')
        words.extend(doc['words'][cursor:a]); words.extend(corrected); cursor=b
        lineage.append({'source_window':window,'provider_dir':str(directory),'old_word_ids':[w['id'] for w in doc['words'][a:b]],
                        'new_word_ids':[w['id'] for w in corrected],'mapping':'window-level replacement; not fabricated word-to-word timing'})
    words.extend(doc['words'][cursor:]); result['words']=words
    result['revision']='source-repaired-'+fingerprint([doc['revision'],lineage,words])[:16]
    result['previous_revision']=doc['revision']; result['asr_repairs']=lineage
    result['utterances']=[{'id':r['utterance_id'],'speaker_id':r['speaker_id'],'text':r['text'],
                          'start':r['start_s'],'end':r['end_s'],
                          'word_ids':[w['id'] for w in words[r['first']-1:r['last']]]} for r in utterance_rows(result)]
    if any(a['source_end_s']>b['source_start_s']+.001 for a,b in zip(words,words[1:])):
        raise ValueError('Re-recognized boundaries overlap unchanged source words; review the saved window')
    return result


def repair(doc, audio, root, ffmpeg, cfg, execute=False):
    windows=uniform_windows(doc)
    if not windows: return doc,{'status':'not_needed','windows':[]}
    if len(windows)>3 or any(w['end_s']-w['start_s']>60 for w in windows):
        raise ValueError('Uniform timing affects too much audio for bounded automatic repair; inspect source transcript')
    if not execute: return doc,{'status':'repair_required','windows':windows}
    replacements=[]
    for window in windows:
        folder=root/fingerprint([doc['revision'],window])[:16]; folder.mkdir(parents=True,exist_ok=True)
        clip=folder/'source-window.wav'
        if not clip.exists():
            subprocess.run([str(ffmpeg),'-v','error','-nostdin','-y','-ss',str(window['start_s']),'-i',str(audio),
                            '-t',str(window['end_s']-window['start_s']),'-c:a','pcm_s16le',str(clip)],capture_output=True,check=True)
        repair_cfg={**cfg,'BAILIAN_ASR_MODEL':'fun-asr'}
        bailian_asr.run(clip,folder/'fun-asr',repair_cfg,execute=True,ffmpeg=ffmpeg)
        local=load(folder/'fun-asr/transcript.source.json')
        # A weak/off-camera subsection can remain unaligned while the actual
        # performer is repaired. Keep the evidence, mark only those words, and
        # let selection reject retained unreliable words instead of failing on
        # footage that will be excluded anyway. Never retry a paid window blindly.
        for unresolved in uniform_windows(local):
            for word in local['words'][unresolved['suspect_first']-1:unresolved['suspect_last']]:
                word['timing_status']='uniform_provider_timing'
        replacements.append((window,local,folder/'fun-asr'))
    result=splice(doc,replacements)
    unresolved=[w['id'] for w in result['words'] if w.get('timing_status')=='uniform_provider_timing']
    report={'status':'repaired_with_unresolved' if unresolved else 'repaired','model':'fun-asr','windows':windows,'revision':result['revision'],
            'unresolved_word_ids':unresolved,
            'audio_seconds':sum(w['end_s']-w['start_s'] for w in windows),
            'note':'Raw provider transcripts preserved; replacement speaker labels are scoped to the window and require role review'}
    save(root/'repair-report.json',report); save(root/'transcript.repaired.json',result)
    return result,report


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('transcript','audio','out','ffmpeg','env'): p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--execute',action='store_true'); a=p.parse_args()
    _,report=repair(load(a.transcript),a.audio,a.out,a.ffmpeg,config(a.env),a.execute)
    print(json.dumps(report,ensure_ascii=False))
