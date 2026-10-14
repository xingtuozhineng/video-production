import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import audio_review as review

CFG = {'DASHSCOPE_BASE_URL':'https://dashscope.aliyuncs.com/api/v1','DASHSCOPE_API_KEY':'fixture'}


def response(job, verdict='pass'):
    return {'finish_reason':'stop','request_id':'offline', 'usage':{'prompt_tokens':1000,'completion_tokens':100},
            'text':json.dumps({'checks':[dict(id=i['id'],evidence='声音连续',**{k:verdict for k in review.CHECKS})
                                        for i in job['items']], 'events':[]})}


class Client:
    def __init__(self, fail=False): self.calls=[]; self.fail=fail
    def call(self,job,audio):
        self.calls.append(job)
        if self.fail: raise RuntimeError('transport unavailable')
        return response(job)


class AudioReviewTests(unittest.TestCase):
    def test_missing_cut_ids_are_not_invented_and_explicit_recheck_retains_paid_lineage(self):
        class MissingCutClient(Client):
            def call(self,job,audio):
                answer=super().call(job,audio)
                if job['kind']=='cuts' and 'recheck_of' not in job:
                    value=json.loads(answer['text']); value['checks'].pop()
                    answer['text']=json.dumps(value)
                return answer
        with tempfile.TemporaryDirectory() as tmp,patch.object(review,'probe_duration',return_value=10),patch.object(review,'extract',return_value='fixture'):
            root=Path(tmp); source,media,plan=self.fixture(root)
            data=json.loads(plan.read_text()); data['segments']=[{'final_out_s':i} for i in range(1,7)]+[{'final_out_s':10}]
            plan.write_text(json.dumps(data)); client=MissingCutClient()
            first=review.run(source,root,CFG,'ffmpeg','ffprobe',media,plan,stage='final',execute=True,client=client)
            self.assertEqual(first['status'],'incomplete'); count=len(client.calls)
            review.run(source,root,CFG,'ffmpeg','ffprobe',media,plan,stage='final',execute=True,client=client)
            self.assertEqual(len(client.calls),count)
            fixed=review.run(source,root,CFG,'ffmpeg','ffprobe',media,plan,stage='final',execute=True,client=client,recheck_invalid=True)
            self.assertEqual(fixed['status'],'complete'); self.assertEqual(fixed['reviewed_cuts'],6)
            self.assertEqual(len(client.calls)-count,2)
            ledger=review.load(root/'qc/audio-review/budget.json')
            parent=next(iter(ledger['rechecks']))
            self.assertEqual(ledger['requests'][parent]['status'],'invalid_response')
            self.assertEqual(fixed['cost_from_usage_cny'],round(sum(r['cost_cny'] for r in ledger['requests'].values()),8))
            self.assertTrue(all(j['recheck_of']==parent and len(j['items'])==3 for j in client.calls[-2:]))
            review.run(source,root,CFG,'ffmpeg','ffprobe',media,plan,stage='final',execute=True,client=client)
            self.assertEqual(len(client.calls),count+2)

    def test_invalid_recheck_refuses_unknown_usage_and_does_not_submit(self):
        root=Path('fixture'); job={'signature':'bad','kind':'cuts','items':[{'id':'cut-001'}]}
        for response_value,cost in [(dict(finish_reason='length',usage={}),.001),(dict(finish_reason='stop',usage=None),None)]:
            ledger={'requests':{'bad':{'status':'invalid_response','cost_cny':cost}}}
            with patch.object(Path,'exists',return_value=True),patch.object(review,'load',return_value=response_value):
                with self.assertRaisesRegex(ValueError,'unknown'):
                    review.recovery_schedule([job],ledger,root,lambda j:None,True)

    def test_source_focus_has_real_target_frames_context_and_bounded_cost(self):
        jobs=review.jobs(625,stage='source',source_focus_ranges=[(55.43,55.89)])
        job=jobs[0]
        self.assertEqual(job['kind'],'source_focus')
        self.assertAlmostEqual(job['start_s'],52.43)
        self.assertAlmostEqual(job['end_s'],58.89)
        self.assertEqual(job['frame_times_s'],[55.522,55.66,55.798])
        self.assertIn('3.000至3.460',review.prompt(job))
        self.assertLess(review.reserve_cny(job),.012)
        with self.assertRaises(ValueError): review.jobs(625,stage='final',source_focus_ranges=[(1,2)])
        with self.assertRaises(ValueError): review.jobs(625,stage='source',source_focus_ranges=[(1,20)])

    def test_client_requests_json_mode_and_keeps_audio_and_frames_in_one_call(self):
        from unittest.mock import MagicMock
        job=review.jobs(625,stage='source',source_focus_ranges=[(55.43,55.89)])[0]
        job['_images']=['jpeg-fixture']*3
        stream=MagicMock()
        stream.__enter__.return_value=[b'data: [DONE]\n']
        opener=MagicMock(); opener.open.return_value=stream
        with patch.object(review,'build_opener',return_value=opener):
            review.OmniClient(CFG).call(job,'audio-fixture')
        body=json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(body['response_format'],{'type':'json_object'})
        self.assertEqual(body['reasoning_effort'],'none')
        self.assertEqual(body['temperature'],0)
        self.assertEqual([c['type'] for c in body['messages'][1]['content']],
                         ['input_audio','image_url','image_url','image_url','text'])

    def test_transient_windows_save_conflict_retries_only_local_write(self):
        with patch.object(review,'_save',side_effect=[PermissionError('reader'),None]) as write, \
                patch.object(review.time,'sleep'):
            review.save(Path('fixture.json'),{})
            self.assertEqual(write.call_count,2)

    def fixture(self, root):
        source=root/'raw.mp4'; source.write_bytes(b'source')
        media=root/'final.mp4'; media.write_bytes(b'final')
        plan=root/'plan.json'
        plan.write_text(json.dumps({'revision':'r','duration_s':10,'source':{'path':str(source)},
            'segments':[{'final_out_s':4},{'final_out_s':7},{'final_out_s':10}]}))
        return source,media,plan

    def test_source_and_final_coverage_and_relative_cut_times(self):
        plan={'duration_s':138.74,'segments':[{'final_out_s':i*2} for i in range(1,58)]}
        jobs=review.jobs(430.56,138.74,plan)
        self.assertEqual([j['items'][0]['id'] for j in jobs if j['kind']=='source'],
                         [f'source-{i:03}' for i in range(1,6)])
        cuts=[i['id'] for j in jobs if j['kind']=='cuts' for i in j['items']]
        self.assertEqual(cuts,[f'cut-{i:03}' for i in range(1,57)])
        self.assertTrue(all(0<=j['start_s']<j['end_s'] for j in jobs))
        self.assertLess(sum(review.reserve_cny(j) for j in jobs),.2)
        self.assertEqual(jobs[0]['start_s'],0)
        self.assertEqual([j for j in jobs if j['kind']=='source'][-1]['end_s'],430.56)

    def test_schema_failure_never_counts_as_pass(self):
        job=review.jobs(1,stage='source')[0]
        good=response(job)
        for bad in ({**good,'finish_reason':'length'}, {**good,'text':'{}'},
                    {**good,'text':good['text'].replace('pass','n/a')}):
            with self.assertRaises(ValueError): review.parse_result(bad,job)
        self.assertEqual(review.parse_result(good,job)['checks'][0]['word_clipping'],'pass')

    def test_rounded_window_edge_is_valid_but_explicitly_flagged(self):
        job={'start_s':77.26,'end_s':92.345,'items':[{'id':'cut-036'}]}
        data=response(job); body=json.loads(data['text'])
        body['events']=[{'at_s':15.085,'kind':'word_clipping'}]
        data['text']=json.dumps(body)
        parsed=review.parse_result(data,job)
        self.assertTrue(parsed['events'][0]['window_edge_observation'])

    def test_observed_chinese_nonformal_label_is_normalized_without_changing_evidence(self):
        job=review.jobs(90,stage='source')[0]
        data=response(job); body=json.loads(data['text'])
        body['events']=[{'at_s':41.72,'kind':'非正式试词/拍摄交流','heard':'那你说','evidence':'拍摄交流'}]
        data['text']=json.dumps(body,ensure_ascii=False)
        event=review.parse_result(data,job)['events'][0]
        self.assertEqual(event['kind'],'nonformal_speech')
        self.assertEqual(event['raw_kind'],'非正式试词/拍摄交流')
        self.assertEqual(event['heard'],'那你说')

    def test_missing_opening_quote_recovery_preserves_the_received_evidence(self):
        job=review.jobs(90,stage='source')[0]
        data=response(job); body=json.loads(data['text'])
        body['events']=[{'at_s':1.2,'kind':'word_clipping','heard':'老板','evidence':'“老板”二字音量骤降。'}]
        data['text']=json.dumps(body,ensure_ascii=False).replace('"evidence": "“老板”', '"evidence": “老板”')
        original=data['text']
        parsed=review.parse_result(data,job)
        self.assertEqual(parsed['events'][0]['evidence'],'“老板”二字音量骤降。')
        self.assertEqual(parsed['local_parse_repairs'][0]['kind'],'missing_opening_ascii_quote')
        self.assertEqual(data['text'],original)
        with self.assertRaises(ValueError):
            review.parse_result({**data,'text':original[:-2]},job)

    def test_typographic_text_key_recovery_keeps_all_cut_ids_and_evidence(self):
        job={'start_s':0,'end_s':10,'items':[{'id':f'cut-{n:03}'} for n in range(1,7)]}
        data=response(job)
        data['text']=data['text'].replace('"evidence": "', '"evidence：')
        parsed=review.parse_result(data,job)
        self.assertEqual([r['id'] for r in parsed['checks']],[i['id'] for i in job['items']])
        self.assertTrue(all(r['evidence']=='声音连续' for r in parsed['checks']))
        self.assertEqual(parsed['local_parse_repairs'][0]['count'],6)

    def test_valid_json_swallowed_evidence_key_is_recovered_without_inventing_a_verdict(self):
        job={'start_s':0,'end_s':10,'items':[{'id':'cut-058'}]}
        data=response(job); body=json.loads(data['text'])
        row=body['checks'][0]; row.pop('evidence')
        evidence='“诶”为自然语气助词，非试音，上下文连贯。'
        row['evidence：'+evidence+'},{']='id'
        data['text']=json.dumps(body,ensure_ascii=False); original=data['text']
        parsed=review.parse_result(data,job)
        self.assertEqual(parsed['checks'][0]['evidence'],evidence)
        self.assertTrue(all(parsed['checks'][0][k]=='pass' for k in review.CHECKS))
        self.assertEqual(parsed['local_parse_repairs'],[{'kind':'evidence_swallowed_into_key','id':'cut-058'}])
        self.assertEqual(data['text'],original)
        row.pop('breath_cut'); data['text']=json.dumps(body,ensure_ascii=False)
        with self.assertRaisesRegex(ValueError,'Missing verdict'): review.parse_result(data,job)

    def test_swallowed_evidence_recovery_refuses_other_or_missing_text(self):
        job={'start_s':0,'end_s':10,'items':[{'id':'cut-058'}]}
        for key,value in [('evidence：},{','id'),('evidence：声音连续','id'),('evidence：声音连续},{','other')]:
            data=response(job); body=json.loads(data['text'])
            row=body['checks'][0]; row.pop('evidence'); row[key]=value
            data['text']=json.dumps(body,ensure_ascii=False)
            with self.assertRaisesRegex(ValueError,'Missing verdict'): review.parse_result(data,job)

    def test_focus_is_bounded_and_has_no_implicit_full_rerun(self):
        plan={'duration_s':100,'segments':[{'final_out_s':20},{'final_out_s':60},{'final_out_s':100}]}
        jobs=review.jobs(200,100,plan,'final',[(55,70)])
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['items'],[{'id':'focus-001'}])
        self.assertEqual(jobs[0]['cut_contexts'],[{'id':'cut-002','at_s':60}])
        with self.assertRaises(ValueError): review.jobs(200,100,plan,'final',[(0,95)])
        with self.assertRaises(ValueError): review.jobs(200,stage='source',focus_ranges=[(0,10)])

    def test_endpoint_and_cost_no_free_quota_assumption(self):
        self.assertEqual(review.endpoint(CFG),'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions')
        with self.assertRaises(ValueError): review.endpoint({**CFG,'DASHSCOPE_BASE_URL':'https://evil.test/api/v1'})
        with self.assertRaisesRegex(ValueError,'Beijing'): review.endpoint({**CFG,'DASHSCOPE_BASE_URL':'https://dashscope-intl.aliyuncs.com/api/v1'})
        self.assertEqual(review.usage_cost({'prompt_tokens':100000,'completion_tokens':10000}),.107)
        self.assertIsNone(review.usage_cost(None))

    def test_dry_run_then_cached_replay_has_no_second_paid_call(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(review,'probe_duration',return_value=10), \
                patch.object(review,'extract',return_value='fixture'):
            root=Path(tmp); source,media,plan=self.fixture(root); client=Client()
            dry=review.run(source,root,CFG,'ffmpeg','ffprobe',media,plan,execute=False,client=client)
            self.assertTrue(dry['fits_budget']); self.assertFalse((root/'qc').exists()); self.assertEqual(client.calls,[])
            first=review.run(source,root,CFG,'ffmpeg','ffprobe',media,plan,execute=True,client=client)
            self.assertEqual(first['status'],'complete'); self.assertEqual(first['reviewed_cuts'],2)
            self.assertEqual(first['human_listening'],'not_checked')
            count=len(client.calls)
            second=review.run(source,root,CFG,'ffmpeg','ffprobe',media,plan,execute=True,client=client)
            self.assertEqual(len(client.calls),count)
            self.assertEqual(first['cost_from_usage_cny'],second['cost_from_usage_cny'])
            self.assertEqual(second['new_paid_requests_this_run'],0)
            self.assertEqual(second['cached_requests_this_run'],count)

    def test_unknown_outcome_retains_reservation_and_stops_resume(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(review,'probe_duration',return_value=10), \
                patch.object(review,'extract',return_value='fixture'):
            root=Path(tmp); source,_,_=self.fixture(root); client=Client(fail=True)
            first=review.run(source,root,CFG,'ffmpeg','ffprobe',stage='source',execute=True,client=client)
            self.assertEqual(first['status'],'incomplete'); self.assertGreater(first['budget_occupied_cny'],0)
            self.assertEqual(first['cost_from_usage_cny'],0)
            second=review.run(source,root,CFG,'ffmpeg','ffprobe',stage='source',execute=True,client=client)
            self.assertEqual(len(client.calls),1)
            self.assertEqual(first['budget_occupied_cny'],second['budget_occupied_cny'])

    def test_budget_applies_across_revisions_and_all_stages(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(review,'probe_duration',return_value=10), \
                patch.object(review,'extract',return_value='fixture'):
            root=Path(tmp); source,media,plan=self.fixture(root); client=Client()
            ledger={'source_sha256':review.file_hash(source),'budget_cny':.2,'requests':{
                'prior':{'status':'outcome_unknown','reserved_cny':.2,'cost_cny':None}}}
            review.save(root/'qc/audio-review/budget.json',ledger)
            report=review.run(source,root,CFG,'ffmpeg','ffprobe',media,plan,execute=True,client=client)
            self.assertEqual(report['stop_reason'],'budget_exhausted')
            self.assertEqual(report['reviewed_cuts'],0); self.assertEqual(client.calls,[])

    def test_missing_usage_reserves_budget_and_cannot_silently_retry(self):
        class NoUsage(Client):
            def call(self,job,audio):
                self.calls.append(job)
                return {**response(job),'usage':None}
        with tempfile.TemporaryDirectory() as tmp, patch.object(review,'probe_duration',return_value=10), \
                patch.object(review,'extract',return_value='fixture'):
            root=Path(tmp); source,_,_=self.fixture(root); client=NoUsage()
            first=review.run(source,root,CFG,'ffmpeg','ffprobe',stage='source',execute=True,client=client)
            self.assertEqual(first['status'],'incomplete'); self.assertGreater(first['budget_occupied_cny'],0)
            review.run(source,root,CFG,'ffmpeg','ffprobe',stage='source',execute=True,client=client)
            self.assertEqual(len(client.calls),1)

    def test_focus_limit_is_shared_across_revisions(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(review,'probe_duration',return_value=10), \
                patch.object(review,'extract',return_value='fixture'):
            root=Path(tmp); source,media,plan=self.fixture(root); client=Client()
            review.run(source,root,CFG,'ffmpeg','ffprobe',media,plan,'both',True,client)
            for window in ([(1,5)],[(5,9)],[(2,6)],[(3,7)],[(.5,4.5)],[(1.5,5.5)],[(2.5,6.5)],[(3.5,7.5)]):
                result=review.run(source,root,CFG,'ffmpeg','ffprobe',media,plan,'final',True,client,window)
                self.assertEqual(result['status'],'complete')
                self.assertEqual(result['scope'],'focused_recheck')
                self.assertEqual(result['reviewed_cuts'],0)
                self.assertEqual(result['total_internal_cuts'],2)
            count=len(client.calls)
            result=review.run(source,root,CFG,'ffmpeg','ffprobe',media,plan,'final',True,client,[(4,8)])
            self.assertEqual(result['stop_reason'],'focus_review_limit_reached')
            self.assertEqual(len(client.calls),count)

    def test_wide_focus_does_not_request_an_unbounded_list_of_verdicts(self):
        plan={'duration_s':100,'segments':[{'final_out_s':i} for i in range(1,101)]}
        job=review.jobs(200,100,plan,'final',[(10,90)])[0]
        self.assertEqual(len(job['items']),1)
        self.assertEqual(len(job['cut_contexts']),81)
        self.assertIn('整个窗口',review.prompt(job))
        self.assertLess(review.reserve_cny(job),.02)

    def test_source_focus_has_four_conflict_rechecks_but_keeps_a_shared_hard_limit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(review,'probe_duration',return_value=10), \
                patch.object(review,'extract',return_value='fixture'), \
                patch.object(review.subprocess,'run',side_effect=lambda argv,**kw: Path(argv[-1]).write_bytes(b'jpeg')):
            root=Path(tmp); source,_,_=self.fixture(root); client=Client()
            for i in range(12):
                a=1+i*.4
                result=review.run(source,root,CFG,'ffmpeg','ffprobe',stage='source',execute=True,
                                  client=client,source_focus_ranges=[(a,a+.1)])
                self.assertEqual(result['status'],'complete')
            result=review.run(source,root,CFG,'ffmpeg','ffprobe',stage='source',execute=True,
                              client=client,source_focus_ranges=[(7,7.1)])
            self.assertEqual(result['stop_reason'],'source_focus_review_limit_reached')
            self.assertEqual(len(client.calls),12)
            self.assertLess(result['budget_occupied_cny'],.2)

    def test_saved_response_recovery_is_local_and_maps_explicit_chinese_category(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(review,'probe_duration',return_value=10), \
                patch.object(review,'extract',return_value='fixture'):
            root=Path(tmp); source,_,_=self.fixture(root); client=Client()
            report=review.run(source,root,CFG,'ffmpeg','ffprobe',stage='source',execute=True,client=client)
            ledger_path=root/'qc/audio-review/budget.json'; ledger=review.load(ledger_path)
            signature,row=next(iter(ledger['requests'].items()))
            row['status']='invalid_response'; review.save(ledger_path,ledger)
            response_path=root/'qc/audio-review/responses'/f'{signature}.json'
            saved=review.load(response_path); result=json.loads(saved['text'])
            result['events']=[{'kind':'画外提示/另一声音','at_s':1,'heard':'开头一','evidence':'另一声音提示'}]
            saved['text']=json.dumps(result,ensure_ascii=False); review.save(response_path,saved)
            recovered=review.run(source,root,CFG,'ffmpeg','ffprobe',stage='source',execute=True,client=client)
            self.assertEqual(len(client.calls),1)
            self.assertEqual(recovered['status'],'complete')
            self.assertEqual(recovered['results'][0]['result']['events'][0]['kind'],'prompt_residue')


if __name__=='__main__': unittest.main()
