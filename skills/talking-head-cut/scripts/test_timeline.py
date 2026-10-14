import copy
import unittest

from check_timeline import validate
from map_words import remap


def plan(source_fps=50, output_fps=30):
    return {
        'revision': 'r',
        'source': {'duration_s': 10, 'fps': {'num': source_fps, 'den': 1}},
        'fps': {'num': output_fps, 'den': 1},
        'duration_s': 2,
        'duration_frames': 60,
        'segments': [{
            'id': 'k1', 'source_in_s': 7.2, 'source_out_s': 9.2,
            'final_in_s': 0, 'final_out_s': 2,
            'final_in_frame': 0, 'final_out_frame': 60,
            'source_in_frame': round(7.2 * source_fps),
            'source_out_frame': round(9.2 * source_fps),
            'source_frame_count': round(2 * source_fps),
        }],
    }


class TimelineTests(unittest.TestCase):
    def test_word_mapping_retains_verified_source_boundary_lineage(self):
        correction={'word_id':'w','old_range_s':[8,8.1],'new_range_s':[8,8.2],'evidence':'源短窗偏移核实'}
        transcript={'revision':'corrected','source_parent_revision':'original',
                    'source_timing_corrections':[correction],
                    'words':[{'id':'w','text':'群','source_start_s':8,'source_end_s':8.2}]}
        result=remap(transcript,plan())
        self.assertEqual(result['source_revision'],'corrected')
        self.assertEqual(result['source_parent_revision'],'original')
        self.assertEqual(result['source_timing_corrections'],[correction])
        self.assertAlmostEqual(result['words'][0]['final_end_s'],1.)

    def test_distinct_source_and_output_rates_pass(self):
        report = validate(plan())
        self.assertEqual(report['status'], 'pass')

    def test_output_rate_cannot_be_used_for_source_frame(self):
        broken = plan()
        broken['segments'][0]['source_in_frame'] = round(7.2 * 30)
        broken['segments'][0]['source_out_frame'] = round(9.2 * 30)
        broken['segments'][0]['source_frame_count'] = round(2 * 30)
        report = validate(broken)
        self.assertEqual(report['status'], 'fail')
        self.assertTrue(any('source_in_frame' in error for error in report['errors']))

    def test_missing_source_fps_fails(self):
        broken = copy.deepcopy(plan())
        broken['source'].pop('fps')
        with self.assertRaisesRegex(ValueError, 'source.fps'):
            validate(broken)


if __name__ == '__main__':
    unittest.main(verbosity=2)
