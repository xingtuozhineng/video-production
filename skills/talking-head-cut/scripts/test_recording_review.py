"""Roles, repeated takes and ASR-missed prompts exercised through real select()."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from authoring import index, load, select
from recording_review import draft_review, utterance_groups


class RecordingReviewTests(unittest.TestCase):
    def fixture(self, speakers=(0, 1, 1, 1)):
        words = [{'id': f'w{i}', 'utterance_id': f'u{i}', 'speaker_id': sid,
                  'source_start_s': i * 2 + .5, 'source_end_s': i * 2 + 1.5,
                  'text': '同一句话'} for i, sid in enumerate(speakers)]
        transcript = {'revision': 'source', 'words': words}
        review = draft_review(transcript)
        review.update(status='reviewed', mode='guided_audible', evidence='原片开中末领读/复述连续样本已核对')
        review['content_review'] = {'status': 'reviewed', 'evidence': '正文与领读/拍摄交流已核对，例外写入spans'}
        review['spans'] = [{'first': g['first'], 'last': g['last']} for g in utterance_groups(words)]
        for i, span in enumerate(review['spans']):
            span.update(role='prompt' if i == 0 else 'performer', group='sentence-1',
                        take=f'take-{i}', evidence=f'原片第{i+1}段连续嘴型与原声核对')
        selection = {'source_revision': 'source', 'ranges': [
            {'first': 3, 'last': 3, 'reason': '主角第二次复述完整自然'}]}
        return transcript, review, selection

    def run_select(self, transcript, review, selection):
        with tempfile.TemporaryDirectory() as tmp, patch('authoring.subprocess.run') as probe:
            root = Path(tmp); source = root / 'raw.mp4'; source.write_bytes(b'fixture')
            probe.return_value.stdout = json.dumps({'streams': [
                {'codec_type': 'video', 'avg_frame_rate': '30/1', 'width': 360, 'height': 640},
                {'codec_type': 'audio'}], 'format': {'duration': '10'}})
            select(transcript, selection, source, 'ffprobe', root / 'edit', review=review)
            return load(root / 'edit/selection-plan.json'), load(root / 'edit/words.selected.json')

    def test_index_keeps_speakers_and_does_not_infer_roles_or_overwrite_review(self):
        transcript, review, _ = self.fixture()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp); result = index(transcript, out)
            self.assertIn('speaker_id', (out / 'words.tsv').read_text('utf-8').splitlines()[0])
            self.assertIn('effective_speaker_id', (out / 'utterances.tsv').read_text('utf-8').splitlines()[0])
            draft = load(Path(result['recording_review']))
            self.assertEqual(draft['mode'], 'unknown')
            self.assertEqual(draft['spans'], [])
            self.assertEqual(draft['content_review'], {'status': 'not_checked', 'evidence': ''})
            self.assertEqual({s['role'] for s in draft['speaker_roles']}, {'unknown'})
            Path(result['recording_review']).write_text(json.dumps(review), encoding='utf-8')
            index(transcript, out)
            self.assertEqual(load(Path(result['recording_review'])), review)

    def test_solo_without_repeats_keeps_all_content(self):
        transcript, review, selection = self.fixture((7, 7, 7))
        review['mode'] = 'solo'
        for i, span in enumerate(review['spans']):
            span.update(role='performer', group=f'sentence-{i}', take='original')
        selection['ranges'] = [{'first': 1, 'last': 3, 'reason': '独立内容完整保留'}]
        plan, mapped = self.run_select(transcript, review, selection)
        self.assertEqual(len(mapped['words']), 3)
        self.assertEqual(plan['recording_review'], review)

    def test_audible_prompt_and_three_performer_takes_select_only_best_performer(self):
        transcript, review, selection = self.fixture()
        _, mapped = self.run_select(transcript, review, selection)
        self.assertEqual([w['id'] for w in mapped['words']], ['w2'])
        self.assertEqual(set(mapped['excluded_word_ids']), {'w0', 'w1', 'w3'})

    def test_prompt_cannot_be_chosen_because_its_text_is_cleaner(self):
        transcript, review, selection = self.fixture()
        selection['ranges'] = [{'first': 1, 'last': 1, 'reason': '领读更短更流畅'}]
        with self.assertRaisesRegex(ValueError, 'role prompt'):
            self.run_select(transcript, review, selection)

    def test_two_or_three_retakes_cannot_be_retained_or_spliced_as_one_group(self):
        transcript, review, selection = self.fixture()
        for indexes in ((2, 3), (2, 3, 4)):
            with self.subTest(indexes=indexes):
                selection['ranges'] = [{'first': i, 'last': i, 'reason': '各取一点'} for i in indexes]
                with self.assertRaisesRegex(ValueError, 'mixed takes'):
                    self.run_select(transcript, review, selection)

    def test_one_asr_speaker_does_not_skip_the_role_review(self):
        transcript, _, selection = self.fixture((0, 0, 0))
        with patch('authoring.subprocess.run') as probe:
            for review in (None, draft_review(transcript)):
                with self.assertRaisesRegex(ValueError, 'not reviewed'):
                    select(transcript, selection, Path('raw'), 'ffprobe', Path('edit'), review=review)
            probe.assert_not_called()

    def test_unknown_role_is_rejected_even_after_overall_review(self):
        transcript, review, selection = self.fixture()
        review['spans'][2]['role'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'role unknown'):
            self.run_select(transcript, review, selection)

    def test_provider_speaker_mislabel_does_not_override_reviewed_performer_identity(self):
        transcript, review, selection = self.fixture()
        review['spans'][0].update(role='performer', group='different-sentence', take='original',
                                 evidence='同编号含主角尾句；已回源确认主角发音')
        selection['ranges'] = [{'first': 1, 'last': 1, 'reason': '保留已核对主角尾句'}]
        _, mapped = self.run_select(transcript, review, selection)
        self.assertEqual(mapped['words'][0]['speaker_id'], 0)

    def test_quiet_untranscribed_prompt_inside_selected_gap_is_not_silence(self):
        transcript, review, selection = self.fixture((1, 1))
        review['mode'] = 'guided_inaudible'
        for i, span in enumerate(review['spans']):
            span.update(role='performer', group=f'sentence-{i}', take='original')
        review['excluded_audio'] = [{'start_s': 1.8, 'end_s': 2.2, 'role': 'prompt',
                                     'evidence': '低声领读可听见，但没有 ASR 词'}]
        selection['ranges'] = [{'first': 1, 'last': 2, 'reason': '连续正文'}]
        with self.assertRaisesRegex(ValueError, 'crosses excluded audio'):
            self.run_select(transcript, review, selection)
        selection['ranges'] = [{'first': i, 'last': i, 'reason': '按领读两侧拆开'} for i in (1, 2)]
        plan, _ = self.run_select(transcript, review, selection)
        self.assertEqual(len(plan['segments']), 2)

    def test_quiet_prompt_is_not_reintroduced_by_cut_handles(self):
        transcript, review, selection = self.fixture((1,))
        review['mode'] = 'guided_inaudible'
        review['spans'][0].update(role='performer', group='sentence', take='original')
        review['excluded_audio'] = [{'start_s': .3, 'end_s': .49, 'role': 'prompt',
                                     'evidence': '原声领读结束于主角首词之前'}]
        selection['ranges'] = [{'first': 1, 'last': 1, 'reason': '主角完整句'}]
        plan, _ = self.run_select(transcript, review, selection)
        self.assertAlmostEqual(plan['segments'][0]['source_in_s'], .49)

    def test_intentional_reprise_in_a_new_context_and_reordering_are_preserved(self):
        transcript, review, selection = self.fixture((1, 1))
        for span, group in zip(review['spans'], ['opening-claim', 'closing-reprise']):
            span.update(role='performer', group=group, take='original', evidence='开头判断与结尾有意呼应，非重拍')
        selection['ranges'] = [{'first': i, 'last': i, 'reason': '相同文字在不同语义位置'} for i in (2, 1)]
        _, mapped = self.run_select(transcript, review, selection)
        self.assertEqual([w['id'] for w in mapped['words']], ['w1', 'w0'])

    def test_review_fingerprint_and_selected_role_coverage(self):
        transcript, review, selection = self.fixture()
        changed = copy.deepcopy(transcript); changed['words'][2]['text'] = '改过的识别'
        with self.assertRaisesRegex(ValueError, 'Stale recording review'):
            self.run_select(changed, review, selection)
        review['spans'].pop(2)
        with self.assertRaisesRegex(ValueError, 'role unknown'):
            self.run_select(transcript, review, selection)

    def test_confirmed_roles_apply_without_full_transcript_annotation(self):
        transcript, _, selection = self.fixture()
        review = draft_review(transcript)
        review.update(status='reviewed', mode='guided_audible', evidence='相邻示范复述样本已确认')
        review['content_review'] = {'status': 'reviewed', 'evidence': '正式正文与示范已区分'}
        for item in review['speaker_roles']:
            item.update(role='prompt' if item['speaker_id'] == 0 else 'performer', evidence='连续样本核对')
        plan, mapped = self.run_select(transcript, review, selection)
        self.assertEqual([w['id'] for w in mapped['words']], ['w2'])
        self.assertEqual(plan['recording_review']['spans'], [])
        selection['ranges'] = [{'first': 1, 'last': 1, 'reason': '原错误选择'}]
        with self.assertRaisesRegex(ValueError, 'role prompt'):
            self.run_select(transcript, review, selection)

    def test_sparse_unknown_override_blocks_an_exception_to_confirmed_speaker(self):
        transcript, _, selection = self.fixture()
        review = draft_review(transcript)
        review.update(status='reviewed', mode='mixed', evidence='大部分段落身份稳定，局部待核对')
        review['content_review'] = {'status': 'reviewed', 'evidence': '已核对发言用途，存疑例外保持unknown'}
        for item in review['speaker_roles']:
            item.update(role='performer', evidence='核对过的主角编号')
        review['spans'] = [{'first': 3, 'last': 3, 'role': 'unknown'}]
        with self.assertRaisesRegex(ValueError, 'role unknown'):
            self.run_select(transcript, review, selection)

    def test_missing_evidence_or_invalid_take_ids_fail_before_media_probe(self):
        transcript, review, selection = self.fixture()
        for field, value, error in [('evidence', None, 'evidence'), ('take', 2, 'take IDs')]:
            changed = copy.deepcopy(review)
            changed['spans'][2][field] = value
            with self.subTest(field=field), patch('authoring.subprocess.run') as probe:
                with self.assertRaisesRegex(ValueError, error):
                    select(transcript, selection, Path('raw'), 'ffprobe', Path('edit'), review=changed)
                probe.assert_not_called()

    def content_fixture(self, texts):
        transcript, _, _ = self.fixture(tuple(7 for _ in texts))
        for word, text in zip(transcript['words'], texts):
            word['text'] = text
            word['utterance_id'] = 'mixed-utterance'
        review = draft_review(transcript)
        review.update(status='reviewed', mode='solo', evidence='原片连续画面已确认均为同一出镜者')
        review['speaker_roles'][0].update(role='performer', evidence='出镜者的原声与嘴型对应')
        review['content_review'] = {'status': 'reviewed', 'evidence': '结合前后文核对正文与拍摄过程发言'}
        selection = {'source_revision': 'source', 'ranges': [
            {'first': 1, 'last': len(texts), 'reason': '候选正文'}]}
        return transcript, review, selection

    def test_speaker_identity_alone_cannot_replace_content_purpose_review(self):
        transcript, review, selection = self.content_fixture(['这句怎么说，重来一遍'])
        for value in (None, {}, {'status': 'not_checked', 'evidence': '尚未核对'},
                      {'status': 'reviewed', 'evidence': ''}):
            review['content_review'] = value
            with self.subTest(value=value), patch('authoring.subprocess.run') as probe:
                with self.assertRaisesRegex(ValueError, 'Content-purpose review'):
                    select(transcript, selection, Path('raw'), 'ffprobe', Path('edit'), review=review)
                probe.assert_not_called()
        review.pop('content_review')  # Existing role files must be reviewed, not silently approved.
        with self.assertRaisesRegex(ValueError, 'existing transcript may be reused'):
            self.run_select(transcript, review, selection)

    def test_same_performer_rehearsal_and_crew_talk_are_excluded_without_losing_body(self):
        transcript, review, selection = self.content_fixture([
            '今天讲客户交付', '刚才没说好，这句重来', '提词器往下一点', '完整交付需要验收'])
        original = copy.deepcopy(transcript)
        review['spans'] = [{'first': 2, 'last': 3, 'role': 'production',
                            'evidence': '源片2.5至5.5秒为试词和拍摄指令，随后重新面向观众讲述'}]
        with self.assertRaisesRegex(ValueError, 'role production'):
            self.run_select(transcript, review, selection)
        selection['ranges'] = [{'first': i, 'last': i, 'reason': '保留正式表达'} for i in (1, 4)]
        plan, mapped = self.run_select(transcript, review, selection)
        self.assertEqual([w['id'] for w in mapped['words']], ['w0', 'w3'])
        self.assertEqual(set(mapped['excluded_word_ids']), {'w1', 'w2'})
        self.assertEqual(transcript, original)
        self.assertEqual(len(plan['segments']), 2)

    def test_quoted_self_talk_and_quiet_formal_narration_are_not_keyword_filtered(self):
        transcript, review, selection = self.content_fixture([
            '我当时就想，这事靠谱吗', '客户说这句重来，于是我们重做方案', '我想想，这里还有一个条件'])
        review['content_review']['evidence'] = '三句均为面向观众的叙述，引用与思考过程属于正文；末句虽小声仍连续讲述'
        _, mapped = self.run_select(transcript, review, selection)
        self.assertEqual([w['text'] for w in mapped['words']], [w['text'] for w in transcript['words']])
        self.assertEqual(review['spans'], [])

    def test_uncertain_purpose_cannot_enter_selected_ranges(self):
        transcript, review, selection = self.content_fixture(['正式开头', '我想想', '正式结尾'])
        review['spans'] = [{'first': 2, 'last': 2, 'role': 'unknown',
                            'evidence': '缺少上下文，不能确认是试词还是正文'}]
        with self.assertRaisesRegex(ValueError, 'role unknown'):
            self.run_select(transcript, review, selection)
        selection['ranges'] = [{'first': 1, 'last': 1, 'reason': '已确认的独立正式句'}]
        _, mapped = self.run_select(transcript, review, selection)
        self.assertEqual(len(mapped['words']), 1)

    def test_untranscribed_production_talk_inside_body_requires_split(self):
        transcript, review, selection = self.content_fixture(['正式开头', '正式结尾'])
        review['excluded_audio'] = [{'start_s': 1.8, 'end_s': 2.2, 'role': 'production',
                                     'evidence': '源片间隙有未被ASR识别的试词'}]
        with self.assertRaisesRegex(ValueError, 'crosses excluded audio'):
            self.run_select(transcript, review, selection)
        selection['ranges'] = [{'first': i, 'last': i, 'reason': '试词两侧的正式表达'} for i in (1, 2)]
        plan, _ = self.run_select(transcript, review, selection)
        self.assertTrue(all(s['source_out_s'] <= 1.8 or s['source_in_s'] >= 2.2 for s in plan['segments']))


if __name__ == '__main__':
    unittest.main()
