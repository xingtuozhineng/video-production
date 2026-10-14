import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from asr_repair import uniform_windows, splice, repair, correct_boundaries


class AsrRepairTests(unittest.TestCase):
    def test_boundary_correction_preserves_provider_times_ids_text_and_source(self):
        doc={'revision':'r','words':[dict(id='w',text='群',speaker_id=1,
             start=1.,end=1.2,source_start_s=1.,source_end_s=1.2),
             dict(id='next',text='那',source_start_s=1.5,source_end_s=1.7)]}
        before=copy.deepcopy(doc)
        edits={'source_revision':'r','corrections':[{'word_id':'w','old_range_s':[1.,1.2],
               'new_range_s':[1.,1.3],'evidence':'真实短窗识别；逐采样核实源偏移'}]}
        revised=correct_boundaries(doc,edits)
        self.assertEqual(doc,before)
        self.assertEqual(revised['words'][0],{**doc['words'][0],'source_end_s':1.3})
        self.assertEqual(revised['source_parent_revision'],'r')
        self.assertEqual(revised['revision'],correct_boundaries(doc,edits)['revision'])
        for field,value in [('evidence',''),('old_range_s',[1.,1.1]),('new_range_s',[1.,1.6])]:
            invalid=copy.deepcopy(edits); invalid['corrections'][0][field]=value
            with self.assertRaises(ValueError): correct_boundaries(doc,invalid)

    def fixture(self):
        return {'revision':'r','words':[{'id':str(i),'utterance_id':'u','speaker_id':0,'text':'字',
                'source_start_s':i*.2,'source_end_s':(i+1)*.2,'start':i*.2,'end':(i+1)*.2} for i in range(15)]}

    def test_uniform_provider_words_are_detected_but_variable_timing_is_not(self):
        doc=self.fixture(); self.assertEqual(len(uniform_windows(doc)),1)
        doc['words'][7]['source_end_s']-=.03
        self.assertEqual(uniform_windows(doc),[])

    def test_splice_preserves_raw_input_and_scopes_window_speakers(self):
        doc=self.fixture(); before=copy.deepcopy(doc)
        local={'words':[{'id':'local','utterance_id':'new','speaker_id':1,'text':'正文',
                         'source_start_s':.3,'source_end_s':1.,'start':.3,'end':1.}]}
        window={'first':1,'last':15,'start_s':0,'end_s':3}
        fixed=splice(doc,[(window,local,Path('window'))])
        self.assertEqual(doc,before)
        self.assertEqual(fixed['words'][0]['speaker_id'],1)
        self.assertTrue(fixed['words'][0]['effective_speaker_id'].startswith('repair-'))
        self.assertEqual(fixed['asr_repairs'][0]['old_word_ids'],[str(i) for i in range(15)])

    def test_window_offset_is_applied_once_without_fake_per_word_alignment(self):
        doc=self.fixture()
        for w in doc['words']:
            for field in ('start','end','source_start_s','source_end_s'): w[field]+=10
        local={'words':[{'id':'local','utterance_id':'new','speaker_id':0,'text':'正文',
                         'start':.3,'end':1.,'source_start_s':.3,'source_end_s':1.}]}
        fixed=splice(doc,[({'first':1,'last':15,'start_s':10,'end_s':13},local,Path('window'))])
        self.assertEqual(fixed['words'][0]['source_start_s'],10.3)
        self.assertEqual(fixed['words'][0]['source_end_s'],11.)

    def test_dry_run_does_not_submit_and_unchanged_timing_does_not_retry(self):
        doc=self.fixture()
        with tempfile.TemporaryDirectory() as tmp, patch('asr_repair.bailian_asr.run') as run:
            _,report=repair(doc,Path('audio'),Path(tmp),'ffmpeg',{},False)
            self.assertEqual(report['status'],'repair_required'); run.assert_not_called()
            with patch('asr_repair.subprocess.run'),patch('asr_repair.load',return_value=doc):
                fixed,report=repair(doc,Path('audio'),Path(tmp),'ffmpeg',{},True)
                self.assertEqual(report['status'],'repaired_with_unresolved')
                self.assertTrue(all(w['timing_status']=='uniform_provider_timing' for w in fixed['words']))
            self.assertEqual(run.call_count,1)
