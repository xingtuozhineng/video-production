import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from authoring import caption_draft, compile_authored, load, select, author_pauses, merge_caption_edits, rebase_caption, rebase_pauses
from recording_review import draft_review


class AuthoringTests(unittest.TestCase):
    def test_pause_rebase_reuses_only_identical_source_neighbors_and_context(self):
        old,_,new=self.rebase_fixture()
        row={'left_key':'old:w1','right_key':'old:w3','left_context':'1','right_context':'3',
             'category':'sentence','reason':'完整句末','target_ms':60}
        previous={'revision':'old','boundaries':[row]}
        current={'revision':'new','boundaries':[{**row,'left_key':'new:w1','right_key':'new:w3'},
                 {**row,'left_context':'变化正文'}]}
        result=rebase_pauses(previous,current,old,new)
        self.assertEqual(result['groups'],[{'indexes':[1],'category':'sentence','reason':'完整句末','target_ms':60}])
        self.assertEqual([r['index'] for r in result['pending']],[2])
        self.assertEqual(previous['boundaries'][0]['left_key'],'old:w1')

    def rebase_fixture(self):
        old={'revision':'old','source_revision':'source','words':[dict(id=f'w{i}',text=str(i),
             source_start_s=i,source_end_s=i+.5) for i in range(1,6)]}
        draft={'revision':'old','review':'reviewed','corrections':[{'word':4,'text':'SOP','reason':'原声术语'}],
               'pages':[{'lines':[[1,3],[4,5]],'emphasis':[{'first':4,'last':5,'role':'focus','reason':'完整结论'}]}]}
        new={**old,'revision':'new','words':[w for w in old['words'] if w['id']!='w2']}
        return old,draft,new

    def test_rebase_keeps_reviewed_lines_corrections_and_emphasis_without_retyping(self):
        old,draft,new=self.rebase_fixture(); result=rebase_caption(old,draft,new)
        self.assertEqual(result['pages'][0]['lines'],[[1,2],[3,4]])
        self.assertEqual(result['corrections'][0]['word'],3)
        self.assertEqual(result['pages'][0]['emphasis'][0]['first'],3)
        self.assertEqual(result['rebase']['removed_word_ids'],['w2'])
        self.assertEqual(result['review'],'reviewed')
        self.assertEqual(draft['pages'][0]['lines'],[[1,3],[4,5]])

    def test_rebase_rejects_new_takes_changed_source_and_reordering(self):
        for kind in ('new_word','source','order','timing'):
            old,draft,new=self.rebase_fixture(); new=copy.deepcopy(new)
            if kind=='new_word': new['words'][0]['id']='new'
            elif kind=='source': new['source_revision']='changed'
            elif kind=='order': new['words'].reverse()
            else: new['words'][0]['source_start_s']+=.01
            with self.assertRaises(ValueError): rebase_caption(old,draft,new)

    def test_rebase_does_not_silently_change_a_partly_deleted_emphasis(self):
        old,draft,new=self.rebase_fixture(); draft['pages'][0]['emphasis'][0].update(first=1,last=3)
        with self.assertRaisesRegex(ValueError,'Partly deleted emphasis'):
            rebase_caption(old,draft,new)

    def test_rebase_accepts_only_explicit_same_source_timing_correction_lineage(self):
        old,draft,new=self.rebase_fixture(); new=copy.deepcopy(new)
        new.update(source_revision='corrected',source_parent_revision='source')
        new['words'][0]['source_end_s']=1.6
        new['source_timing_corrections']=[{'word_id':'w1','old_range_s':[1,1.5],
            'new_range_s':[1,1.6],'evidence':'真实源音短窗逐词复识别，源偏移核实'}]
        self.assertEqual(rebase_caption(old,draft,new)['pages'][0]['lines'],[[1,2],[3,4]])
        new['source_timing_corrections'][0]['evidence']=''
        with self.assertRaisesRegex(ValueError,'verified lineage'): rebase_caption(old,draft,new)

    def select_fixture(self, video, **overrides):
        transcript = {'revision': 'source', 'words': [{'id': 'w1', 'speaker_id': 7,
            'utterance_id': 'u1', 'source_start_s': .1, 'source_end_s': .5, 'text': '原声'}]}
        selection = {'source_revision': 'source', 'ranges': [
            {'first': 1, 'last': 1, 'reason': '保留完整原声'}]}
        review = draft_review(transcript)
        review.update(status='reviewed', mode='solo', evidence='已核对原片主角')
        review['content_review'] = {'status': 'reviewed', 'evidence': '测试正文无试词或拍摄交流'}
        review['speaker_roles'][0].update(role='performer', evidence='主角原声确认')
        with tempfile.TemporaryDirectory() as tmp, patch('authoring.subprocess.run') as probe:
            root = Path(tmp); source = root / 'raw.mp4'; source.write_bytes(b'fixture')
            probe.return_value.stdout = json.dumps({'streams': [dict(codec_type='video', **video),
                {'codec_type': 'audio'}], 'format': {'duration': '1'}})
            select(transcript, selection, source, 'ffprobe', root / 'edit', review=review, **overrides)
            return load(root / 'edit/selection-plan.json')

    def test_default_specs_preserve_source_resolution_grade_and_exact_fps_in_portrait(self):
        for width, height, rotation, rate, expected in [
            (3840, 2160, 0, '50/1', (2160, 3840)),
            (3840, 2160, 90, '50/1', (2160, 3840)),
            (3840, 2160, -90, '60000/1001', (2160, 3840)),
            (3840, 2160, 180, '30000/1001', (2160, 3840)),
            (720, 1280, 0, '25/1', (720, 1280)),
            (1080, 1080, 0, '30/1', (1080, 1920))]:
            with self.subTest(rotation=rotation, rate=rate, size=expected):
                plan = self.select_fixture({'width': width, 'height': height, 'avg_frame_rate': rate,
                                           'side_data_list': [{'rotation': rotation}]})
                self.assertEqual((plan['width'], plan['height']), expected)
                self.assertEqual(plan['output_orientation'], 'portrait')
                self.assertLess(plan['width'], plan['height'])
                self.assertEqual(plan['fps'], plan['source']['fps'])
                self.assertEqual(plan['fps'], dict(zip(('num', 'den'), map(int, rate.split('/')))))
                displayed = (height, width) if abs(rotation) % 180 == 90 else (width, height)
                self.assertEqual((plan['source']['display_width'], plan['source']['display_height']), displayed)
                self.assertEqual((plan['source']['width'], plan['source']['height']), (width, height))

    def test_explicit_output_override_keeps_source_specs_separate(self):
        plan = self.select_fixture({'width': 3840, 'height': 2160, 'avg_frame_rate': '50/1',
                                   'side_data_list': [{'rotation': 90}]}, width=1080, height=1920, fps=30)
        self.assertEqual((plan['width'], plan['height']), (1080, 1920))
        self.assertEqual(plan['fps'], {'num': 30, 'den': 1})
        self.assertEqual(plan['source']['fps'], {'num': 50, 'den': 1})
        self.assertEqual((plan['source']['display_width'], plan['source']['display_height']), (2160, 3840))

    def test_landscape_or_square_output_override_is_rejected(self):
        for width, height in ((1920, 1080), (1080, 1080)):
            with self.subTest(size=(width, height)), self.assertRaisesRegex(ValueError, 'must be portrait'):
                self.select_fixture({'width': 3840, 'height': 2160, 'avg_frame_rate': '50/1'},
                                    width=width, height=height)

    def fixture(self):
        words = {'revision': 'r', 'words': [
            {'id': str(i), 'instance_id': 'k', 'text': text, 'final_start_s': i * .2,
             'final_end_s': (i + 1) * .2} for i, text in enumerate(['不是', '软件', '不是', '工具', '。'])]}
        plan = {'revision': 'r', 'fps': {'num': 30, 'den': 1}, 'duration_frames': 30}
        return words, plan

    def test_generated_draft_round_trips_without_retyping_words(self):
        words, plan = self.fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'draft.json'
            caption_draft(words, plan, path, 4)
            result, errors = compile_authored(words, plan, load(path))
        self.assertEqual(errors, [])
        self.assertEqual(''.join(''.join(c['lines']) for c in result['captions']), '不是软件不是工具。')

    def test_repeated_phrase_has_unambiguous_emphasis_by_word_range(self):
        words, plan = self.fixture()
        draft = {'revision': 'r', 'pages': [{'lines': [[1, 5]], 'emphasis': [
            {'first': 3, 'last': 3, 'role': 'focus', 'reason': '强调第二个否定'}]}]}
        result, errors = compile_authored(words, plan, draft)
        self.assertFalse(errors)
        self.assertEqual(result['captions'][0]['emphasis'][0]['start_char'], 4)

    def test_all_page_errors_and_missing_words_reported_together(self):
        words, plan = self.fixture()
        draft = {'revision': 'r', 'pages': [{'lines': [[0, 2]]}, {'lines': [[3, 99]]}]}
        result, errors = compile_authored(words, plan, draft)
        self.assertIsNone(result)
        self.assertEqual([x['page'] for x in errors if 'page' in x], [1, 2])
        self.assertEqual(errors[-1]['missing'], [1, 2, 3, 4, 5])

    def test_display_correction_keeps_word_coverage_and_source_immutable(self):
        words, plan = self.fixture(); original = copy.deepcopy(words)
        draft = {'revision': 'r', 'corrections': [{'word': 2, 'text': '系统', 'reason': 'ASR校字'}],
                 'pages': [{'lines': [[1, 5]]}]}
        result, errors = compile_authored(words, plan, draft)
        self.assertFalse(errors)
        self.assertIn('系统', result['captions'][0]['lines'][0])
        self.assertEqual(words, original)

    def test_stale_revision_rejected(self):
        words, plan = self.fixture()
        self.assertTrue(compile_authored(words, plan, {'revision': 'old'})[1])

    def test_pause_groups_cover_candidates_without_reimplementing_timing(self):
        original = {'revision': 'r', 'boundaries': [{'left_key': 'a', 'right_key': 'b'}, {'left_key': 'b', 'right_key': 'c'}]}
        draft = {'revision': 'r', 'groups': [
            {'indexes': [1], 'category': 'sentence', 'reason': '完整判断结束'},
            {'indexes': [2], 'category': 'within_sentence', 'reason': '待核字音', 'preserve': True}]}
        result = author_pauses(original, draft)
        self.assertEqual(result['boundaries'][0]['category'], 'sentence')
        self.assertTrue(result['boundaries'][1]['preserve'])
        self.assertNotIn('category', original['boundaries'][0])
        for bad in ({**draft, 'revision': 'old'}, {**draft, 'groups': draft['groups'][:1]},
                    {**draft, 'groups': draft['groups'] + draft['groups'][:1]}):
            with self.assertRaises(ValueError): author_pauses(original, bad)

    def test_sparse_caption_edits_keep_unmodified_pages_and_source_words(self):
        words, plan = self.fixture()
        draft = {'revision': 'r', 'corrections': [], 'pages': [
            {'lines': [[1, 2]], 'emphasis': []}, {'lines': [[3, 5]], 'emphasis': []}]}
        edited = merge_caption_edits(draft, {'revision': 'r',
            'corrections': [{'word': 2, 'text': '系统', 'reason': '校字'}],
            'highlights': [{'first': 3, 'last': 4, 'role': 'focus', 'reason': '核心否定'}]})
        result, errors = compile_authored(words, plan, edited)
        self.assertFalse(errors)
        self.assertEqual(result['captions'][0]['lines'], ['不是系统'])
        self.assertEqual(result['captions'][1]['emphasis'][0]['text'], '不是工具')
        self.assertEqual(draft['corrections'], [])

    def test_default_caption_pages_do_not_split_overlapping_word_pairs(self):
        words, plan = self.fixture()
        words['words'][1]['final_start_s'] = .18
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'draft.json'
            caption_draft(words, plan, path, 2)
            result, errors = compile_authored(words, plan, load(path))
            self.assertFalse(errors)
            self.assertIn('不是软件', ''.join(result['captions'][0]['lines']))
            self.assertTrue(path.with_suffix('.review.tsv').is_file())


if __name__ == '__main__':
    unittest.main()
