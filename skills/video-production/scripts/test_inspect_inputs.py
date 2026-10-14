import copy
from pathlib import Path
import tempfile
import unittest
import zipfile

from inspect_inputs import inspect, document_rows


class InspectInputsTests(unittest.TestCase):
    def test_selection_review_exposes_omitted_neighbors_and_source_seconds(self):
        words = self.words()
        selection = {'source_revision': 'r', 'ranges': [{'first': 2, 'last': 4, 'reason': '保留句子'}]}
        row = inspect('selection', selection, words=words)['rows'][0]
        self.assertEqual(row['text'], '123')
        self.assertEqual(row['before'], '0')
        self.assertEqual(row['after'], '45')
        self.assertEqual(row['start_s'], .2)
        self.assertEqual(row['first_word'], 2)
        with self.assertRaisesRegex(ValueError, 'revision'):
            inspect('selection', selection, words={**words, 'revision': 'stale'})

    def words(self):
        return {'revision': 'r', 'words': [
            {'id': str(i), 'instance_id': 'a' if i < 3 else 'b', 'utterance_id': 'u',
             'channel_id': 0, 'speaker_id': 1, 'text': str(i),
             'source_start_s': i * .2, 'source_end_s': (i + 1) * .2,
             'final_start_s': i * .2, 'final_end_s': (i + 1) * .2} for i in range(6)]}

    def test_word_range_keeps_original_one_based_indexes_and_is_read_only(self):
        doc = self.words(); original = copy.deepcopy(doc)
        result = inspect('words', doc, span=(3, 4), limit=1)
        self.assertEqual([r['index'] for r in result['rows']], [3])
        self.assertEqual(result['matched'], 2)
        self.assertEqual(result['next_offset'], 1)
        self.assertEqual(doc, original)

    def test_utterances_do_not_merge_same_ids_across_cut_instances(self):
        result = inspect('utterances', self.words())
        self.assertEqual([(r['first'], r['last']) for r in result['rows']], [(1, 3), (4, 6)])

    def test_blocks_find_text_across_asr_utterances_without_crossing_instances(self):
        doc = self.words()
        doc['words'][0].update(text='内容', utterance_id='u1')
        doc['words'][1].update(text='团队', utterance_id='u2')
        result = inspect('blocks', doc, text='内容团队')
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['rows'][0]['first'], 1)
        self.assertEqual(result['rows'][0]['last'], 3)
        self.assertEqual(inspect('utterances', doc, text='内容团队')['matched'], 0)

    def test_literal_text_and_final_time_filters(self):
        result = inspect('words', self.words(), text='3', window=(.6, .81))
        self.assertEqual([r['index'] for r in result['rows']], [4])

    def test_time_filter_is_half_open_and_keeps_point_events(self):
        doc = self.words()
        doc['words'][3]['final_start_s'] = .6
        doc['words'][2]['final_end_s'] = .6
        self.assertEqual([r['index'] for r in inspect('words', doc, window=(.6, .8))['rows']], [4])
        doc['words'][3]['final_end_s'] = .6
        self.assertEqual([r['index'] for r in inspect('words', doc, window=(.6, .8))['rows']], [4])

    def test_pause_query_keeps_keys_and_unreviewed_semantics(self):
        doc = {'revision': 'r', 'boundaries': [{'left_key': 'a:0', 'right_key': 'a:1',
            'left_context': '一个判断', 'right_context': '下一句', 'old_gap_ms': 400,
            'kind': 'internal', 'category': None, 'reason': '', 'review': 'not_checked'}]}
        row = inspect('pauses', doc)['rows'][0]
        self.assertEqual(row['left_key'], 'a:0')
        self.assertIsNone(row['category'])

    def test_join_context_uses_final_instances_not_old_pause_keys(self):
        doc = self.words()
        plan = {'revision': 'r', 'segments': [
            {'id': 'a', 'source_in_s': 0, 'source_out_s': .6, 'final_in_s': 0, 'final_out_s': .6},
            {'id': 'b', 'source_in_s': .6, 'source_out_s': 1.2, 'final_in_s': .6, 'final_out_s': 1.2}]}
        row = inspect('joins', plan, words=doc)['rows'][0]
        self.assertEqual(row['left_word'], '2')
        self.assertEqual(row['right_word'], '3')
        self.assertAlmostEqual(row['gap_ms'], 0)
        with self.assertRaisesRegex(ValueError, 'revision'):
            inspect('joins', plan, words={**doc, 'revision': 'old'})

    def test_docx_reads_paragraphs_tables_and_xml_entities_in_order(self):
        xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               '<w:body><w:p><w:r><w:t>标题&amp;说明</w:t></w:r></w:p>'
               '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格文案</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
               '</w:body></w:document>')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'script.docx'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('word/document.xml', xml)
            self.assertEqual([r['text'] for r in document_rows(path)], ['标题&说明', '表格文案'])

    def test_text_truncation_and_pagination_are_explicit(self):
        doc = self.words(); doc['words'][0]['text'] = '长内容' * 100
        result = inspect('words', doc, limit=2, max_chars=10)
        self.assertTrue(result['rows'][0]['text_truncated'])
        self.assertEqual(result['rows'][0]['text_chars'], 300)
        self.assertEqual(result['next_offset'], 2)

    def test_rejects_invalid_bounds(self):
        for kwargs in [{'limit': 0}, {'offset': -1}, {'span': (0, 3)}, {'window': (2, 1)}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                inspect('words', self.words(), **kwargs)


if __name__ == '__main__':
    unittest.main()
