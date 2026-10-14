import copy
import unittest
from source_audio_gate import retained_observations, require_source_review


class SourceAudioGateTests(unittest.TestCase):
    def fixture(self):
        plan = {'segments': [{'id': 'a', 'source_in_s': 130.7, 'source_out_s': 134.4}]}
        report = {'stage': 'source', 'status': 'complete', 'source_sha256': 'source', 'results': [
            {'job': {'id': 'source-002', 'media': 'source'}, 'result': {'events': [
                {'kind': 'prompt_residue', 'media_time_s': 131.7, 'heard': '讲完就说一句话', 'evidence': '另一声音提示'}]}}]}
        return plan, report

    def test_known_retained_prompt_blocks_even_if_asr_role_says_performer(self):
        plan, report = self.fixture()
        with self.assertRaisesRegex(ValueError, 'before render'):
            require_source_review(plan, report, {'speaker_roles': [{'role': 'performer'}]})

    def test_removing_prompt_resolves_without_discarding_performer_repetition(self):
        plan, report = self.fixture()
        plan['segments'][0]['source_in_s'] = 132.1
        self.assertEqual(retained_observations(plan, report, {}), [])
        require_source_review(plan, report, {})

    def test_false_positive_can_be_retained_only_with_explicit_evidence(self):
        plan, report = self.fixture()
        item = retained_observations(plan, report, {})[0]
        review = {'audio_observations': [{'event_id': item['event_id'], 'decision': 'retain', 'evidence': '原片连续嘴型和原声为同一人，正常引用'}]}
        require_source_review(plan, report, review)
        review['audio_observations'][0]['evidence'] = ''
        with self.assertRaises(ValueError): require_source_review(plan, report, review)

    def test_changed_audio_observation_or_source_does_not_reuse_old_decision(self):
        plan, report = self.fixture()
        key = retained_observations(plan, report, {})[0]['event_id']
        review = {'audio_observations': [{'event_id': key, 'decision': 'retain', 'evidence': '核对'}]}
        changed = copy.deepcopy(report); changed['source_sha256'] = 'other'
        with self.assertRaises(ValueError): require_source_review(plan, changed, review)

    def test_incomplete_coverage_cannot_pass_with_no_events(self):
        plan, report = self.fixture(); report.update(status='incomplete', results=[])
        with self.assertRaisesRegex(ValueError, 'incomplete'): require_source_review(plan, report, {})

    def test_focused_source_windows_do_not_replace_full_source_coverage(self):
        plan,report=self.fixture(); report.update(scope='source_focused_recheck',results=[])
        with self.assertRaisesRegex(ValueError,'incomplete'): require_source_review(plan,report,{})


if __name__ == '__main__':
    unittest.main()
