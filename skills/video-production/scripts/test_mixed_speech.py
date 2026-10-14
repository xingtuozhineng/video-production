import unittest
from mixed_speech import candidates, retained_candidates


class MixedSpeechTests(unittest.TestCase):
    def fixture(self):
        doc={'revision':'r','words':[{'id':str(i),'text':t,'utterance_id':'u','speaker_id':0,
             'source_start_s':i*.2,'source_end_s':(i+1)*.2} for i,t in enumerate('商学院有产业有各种产业园')]}
        levels=[{'rms_dbfs':-20} for w in doc['words']]
        for i in (3,4,5): levels[i]['rms_dbfs']=-45
        return doc,levels

    def test_mixed_utterance_exposes_quiet_phrase_not_whole_speaker(self):
        doc,levels=self.fixture(); report=candidates(doc,levels)
        self.assertEqual(len(report['candidates']),1)
        item=report['candidates'][0]
        self.assertEqual((item['first'],item['last'],item['text']),(4,6,'有产业'))
        self.assertNotIn('role',item)

    def test_uniform_quiet_voice_and_single_weak_consonant_are_not_mixed_voice_proof(self):
        doc,levels=self.fixture()
        self.assertEqual(candidates(doc,[{'rms_dbfs':-45} for w in doc['words']])['candidates'],[])
        levels=[{'rms_dbfs':-20} for w in doc['words']]; levels[4]['rms_dbfs']=-45
        self.assertEqual(candidates(doc,levels)['candidates'],[])

    def test_only_retained_candidate_needs_explicit_decision(self):
        doc,levels=self.fixture(); report=candidates(doc,levels)
        plan={'segments':[{'source_in_s':0,'source_out_s':.6},{'source_in_s':1.2,'source_out_s':2.4}]}
        self.assertEqual(retained_candidates(plan,report,{}),[])
        plan={'segments':[{'source_in_s':0,'source_out_s':2.4}]}
        item=retained_candidates(plan,report,{})[0]
        review={'audio_observations':[{'event_id':item['event_id'],'decision':'retain','evidence':'回源嘴型与前后原声一致，是主角轻声强调'}]}
        self.assertEqual(retained_candidates(plan,report,review)[0]['status'],'reviewed_retained')
