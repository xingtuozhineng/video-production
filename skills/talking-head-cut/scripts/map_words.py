"""Map source words through an applied, original-speed edit plan. Never clamp cut words."""
import argparse
import math
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'video-production' / 'scripts'))
from bailian_media import load, save


def remap(transcript, plan):
    fps = plan['fps']['num'] / plan['fps']['den']
    if not math.isfinite(fps) or fps <= 0 or not plan.get('revision') or not plan['segments']:
        raise ValueError('Invalid applied timeline')
    ids, words, retained = set(), [], set()
    # Cut edges live on the audio sample grid, so their seconds value can differ from a
    # word's declared time by less than one sample. A word that only touches an edge is
    # not clipped by it; half a sample is the tolerance for both the overlap and the
    # containment test.
    eps = 0.5 / plan.get('sample_rate', 48000)
    source_words = transcript['words']
    if len({w['id'] for w in source_words}) != len(source_words):
        raise ValueError('Duplicate source word IDs; merge excerpt namespaces first')
    final_end = 0
    for seg in plan['segments']:
        sid = seg['id']
        a, b, fa, fb = [seg[k] for k in ['source_in_s', 'source_out_s', 'final_in_s', 'final_out_s']]
        if sid in ids or not all(math.isfinite(v) for v in [a,b,fa,fb]):
            raise ValueError('Invalid or duplicate segment')
        ids.add(sid)
        if not 0 <= a < b <= plan['source']['duration_s'] or abs(fa-final_end) > 1e-6 or abs((fb-fa)-(b-a)) > 1e-6:
            raise ValueError('Only continuous original-speed applied segments are supported')
        final_end = fb
        for w in source_words:
            wa, wb = w['source_start_s'], w['source_end_s']
            point = wa == wb and w.get('timing_status') == 'point_only_review_required'
            if not (0 <= wa < wb <= plan['source']['duration_s'] or point and 0 <= wa <= plan['source']['duration_s']):
                raise ValueError('Invalid source word time')
            if wa < b - eps and wb > a + eps or point and a + eps < wa < b - eps:
                if wa < a - eps or wb > b + eps:
                    raise ValueError('Word crosses cut boundary; review source instead of clipping: ' + w['id'])
                words.append({**w, 'instance_id': sid, 'final_start_s': fa+wa-a, 'final_end_s': fa+wb-a})
                retained.add(w['id'])
    if plan.get('timing_mode') == 'sample_audio_cumulative_video':
        sr = plan['sample_rate']
        if abs(final_end-plan['audio_samples']/sr) > 1/sr or not -1e-7 <= plan['duration_frames']/fps-final_end < 1/fps:
            raise ValueError('Sample audio duration or video tail padding mismatch')
    elif abs(final_end * fps - plan['duration_frames']) > 1e-4:
        raise ValueError('Applied duration does not match final frame count')
    return {'revision': plan['revision'], 'source_revision': transcript['revision'], 'words': words,
            **{k:transcript[k] for k in ('source_parent_revision','source_timing_corrections') if k in transcript},
            'excluded_word_ids': [w['id'] for w in source_words if w['id'] not in retained]}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--transcript', type=Path, required=True)
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    save(a.out, remap(load(a.transcript), load(a.plan)))
