"""Local word-level contrast candidates. Never infer identity or delete speech."""
import hashlib
import json
import math
from inspect_inputs import utterance_rows


def candidates(transcript, measurements):
    words=transcript['words']
    if len(words)!=len(measurements):
        raise ValueError('Word measurements must match the complete transcript')
    result=[]
    for utterance in utterance_rows(transcript):
        a,b=utterance['first']-1,utterance['last']
        values=[m['rms_dbfs'] for m in measurements[a:b] if m['rms_dbfs'] is not None]
        if len(values)<6:
            continue
        reference=sorted(values)[math.floor((len(values)-1)*.75)]
        threshold=min(-30,reference-14)
        run=[]
        def flush():
            if not run: return
            x,y=run[0],run[-1]
            wa,wb=words[x],words[y]
            content=''.join(w.get('corrected_text',w.get('text','')) for w in words[x:y+1])
            if wb['source_end_s']-wa['source_start_s']<.3 or len(content.strip())<2:
                return
            identity=[transcript['revision'],[w['id'] for w in words[x:y+1]],wa['source_start_s'],wb['source_end_s']]
            event_id='level-'+hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()[:16]
            result.append({'event_id':event_id,'kind':'mixed_level_candidate','first':x+1,'last':y+1,
                'source_start_s':wa['source_start_s'],'source_end_s':wb['source_end_s'],
                'reference_rms_dbfs':reference,'word_rms_dbfs':[m['rms_dbfs'] for m in measurements[x:y+1]],
                'text':content,'before':''.join(w.get('text','') for w in words[max(a,x-8):x]),
                'after':''.join(w.get('text','') for w in words[y+1:min(b,y+9)]),
                'note':'同一ASR句内的局部电平落差，只是混声/弱字音/时码异常线索；结合前后声音和口型核对，不能自动删除。'})
        for i in range(a,b):
            level=measurements[i]['rms_dbfs']
            low=level is not None and level<=threshold and words[i]['source_end_s']-words[i]['source_start_s']>=.06
            if not low or (run and words[i]['source_start_s']-words[run[-1]]['source_end_s']>.2):
                flush(); run=[]
            if low: run.append(i)
        flush()
    return {'source_revision':transcript['revision'],'method':'within-utterance word RMS contrast >=14dB; no speaker classification',
            'candidates':result}


def retained_candidates(plan, report, review):
    decisions={r.get('event_id'):r for r in review.get('audio_observations',[]) if isinstance(r,dict)}
    result=[]
    for item in report['candidates']:
        if not any(min(s['source_out_s'],item['source_end_s'])-max(s['source_in_s'],item['source_start_s'])>1e-6 for s in plan['segments']):
            continue
        decision=decisions.get(item['event_id'],{})
        reviewed=decision.get('decision')=='retain' and bool(str(decision.get('evidence','')).strip())
        result.append({**item,'status':'reviewed_retained' if reviewed else 'review_required',
                       'decision_evidence':decision.get('evidence')})
    return result
