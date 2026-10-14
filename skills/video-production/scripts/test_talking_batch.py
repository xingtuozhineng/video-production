import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from talking_batch import Batch, load, save, source_packet, levels, representative_windows, audio_summary, digest
import video_production as cli


class TalkingBatchTests(unittest.TestCase):
    def test_missing_pre_render_decision_never_launches_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = self.fixture(Path(tmp))
            with patch.object(batch, 'review', return_value={'status': 'decision_required'}) as review, \
                    patch.object(batch, 'command') as command, patch.object(batch, 'review_audio') as audio:
                self.assertEqual(batch.deliver(True, True)['status'], 'decision_required')
            review.assert_called_once(); command.assert_not_called(); audio.assert_not_called()

    def test_final_listening_remains_required_after_pre_render_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = self.fixture(Path(tmp)); save(batch.project/'output/handoff.json', {'outputs': {}})
            with patch.object(batch, 'review', return_value={'status': 'ready_for_render', 'audio_review_requested': True}), \
                    patch.object(batch, 'command'), patch.object(batch, 'review_audio', return_value={
                        'status': 'complete', 'results': []}) as audio:
                result = batch.deliver(True, False)
            audio.assert_called_once_with('final', {'outputs': {}})
            self.assertEqual(result['audio_status'], 'complete')

    def test_deliver_consumes_rebased_caption_draft_without_editing_compiled_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch=self.fixture(Path(tmp)); pause={'groups':[]}; override=batch.work/'caption-rebased.json'
            save(batch.work/'pause-authoring.json',pause)
            original={'pauses':digest(pause),'revision':'r','draft':'original.json'}
            save(batch.work/'batch/compiled-input.json',original)
            save(batch.work/'compiled/edit-plan.json',{'revision':'r'})
            save(batch.work/'caption-edits.json',{'review':'reviewed'})
            save(batch.project/'output/handoff.json',{'outputs':{}})
            with patch.object(batch,'require_selection'),patch.object(batch,'command') as command, \
                    patch('talking_batch.prepare_pre_render', return_value={'status':'ready_for_render'}):
                batch.deliver(False,False,override)
            self.assertIn(override,command.call_args_list[0].args)
            self.assertEqual(load(batch.work/'batch/compiled-input.json'),original)

    def test_source_boundary_edits_rebase_verified_review_once_and_preserve_raw(self):
        from recording_review import transcript_hash
        with tempfile.TemporaryDirectory() as tmp:
            batch=self.fixture(Path(tmp)); doc=load(batch.transcript)
            doc['words'][0].update(start=0.,end=.2); save(batch.transcript,doc)
            review={'source_revision':'source','transcript_hash':transcript_hash(doc),
                    'status':'reviewed','evidence':'已核对角色'}
            save(batch.work/'index/recording-review.json',review)
            save(batch.work/'asr/source-boundary-edits.json',{'source_revision':'source','corrections':[
                 {'word_id':'w0','old_range_s':[0.,.2],'new_range_s':[0.,.25],'evidence':'真实短窗偏移核实'}]})
            revised=batch.source_document()
            self.assertEqual(batch.source_document(),revised)
            self.assertEqual(revised['words'][0]['end'],.2)
            self.assertEqual(revised['words'][0]['source_end_s'],.25)
            self.assertEqual(load(batch.work/'index/recording-review.json'),
                             {**review,'source_revision':revised['revision'],'transcript_hash':transcript_hash(revised)})
            self.assertEqual(load(batch.work/'index/selection.json')['source_revision'],revised['revision'])

    def test_mixed_candidate_review_reuses_only_identical_evidence_with_direct_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch=self.fixture(Path(tmp))
            candidate={'first':1,'last':1,'source_start_s':0.,'source_end_s':.2,
                       'text':'保持','word_rms_dbfs':[-40.],'event_id':'old'}
            save(batch.work/'batch/mixed-speech.json',{'source_revision':'source','candidates':[candidate]})
            save(batch.work/'index/recording-review.json',{'audio_observations':[
                 {'event_id':'old','decision':'retain','evidence':'原片声画已核实'}]})
            doc={**load(batch.transcript),'revision':'new','source_parent_revision':'source'}
            regenerated={'source_revision':'new','candidates':[{**candidate,'event_id':'new'},
                         {**candidate,'event_id':'changed','source_end_s':.25}]}
            with patch('talking_batch.levels',return_value=[]),patch('talking_batch.mixed_candidates',return_value=regenerated):
                batch.refresh_mixed(doc)
            observation=load(batch.work/'index/recording-review.json')['audio_observations'][0]
            self.assertEqual(observation['event_id'],'new')
            self.assertEqual(observation['rebased_from_event_id'],'old')
            self.assertNotIn('changed',[observation['event_id']])
            with self.assertRaisesRegex(ValueError,'stale'):
                batch.refresh_mixed({**doc,'revision':'other','source_parent_revision':'unrelated'})

    def fixture(self, root):
        source = root/'source.mp4'; source.write_bytes(b'source')
        project = root/'projects/talking-head/test'
        save(project/'production.json', {'route': 'talking-head'})
        save(project/'input/source-manifest.json', {'sources': [{'path': str(source)}]})
        save(root/'video-production-deps/tools.json', {'ffmpeg': 'ffmpeg', 'ffprobe': 'ffprobe'})
        words = [{'id': f'w{i}', 'source_start_s': i*.3, 'source_end_s': i*.3+.2, 'text': t,
                  'speaker_id': 0, 'channel_id': 0, 'utterance_id': 'u'}
                 for i,t in enumerate(['保持', '否定', '和金额', '十万元'])]
        save(project/'work/asr/transcript.source.json', {'revision': 'source', 'words': words})
        save(project/'work/edit/words.selected.json', {'revision': 'selected', 'words': words})
        save(project/'work/index/selection.json', {'source_revision': 'source', 'ranges': [{'first': 1, 'last': 4, 'reason': '正文'}]})
        save(project/'work/index/recording-review.json', {'revision': 'source'})
        return Batch(root, project)

    def test_complete_packet_preserves_all_indexed_text_even_long_utterances(self):
        doc = {'revision': 'r', 'words': [{'id': str(i), 'text': '否定', 'source_start_s': i*.1,
                 'source_end_s': (i+1)*.1, 'utterance_id': 'u', 'speaker_id': 1} for i in range(500)]}
        with patch('talking_batch.levels', return_value=[{'rms_dbfs': -20, 'peak_dbfs': -2}]):
            packet, rows = source_packet(doc, Path('audio.wav'))
        self.assertEqual(len(rows), 1)
        for i in range(1, 501):
            self.assertIn(f'{i}:否定', packet)
        self.assertNotIn('truncated', packet)

    def test_pcm_measurements_handle_empty_and_silent_windows_without_role_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp)/'audio.wav'
            with wave.open(str(wav), 'wb') as out:
                out.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                out.writeframes(b'\0\0'*16000)
            result = levels(wav, [(0, .5), (2, 3), (.5, .4)])
        self.assertEqual(result[0]['rms_dbfs'], -200)
        self.assertIsNone(result[1]['peak_dbfs'])
        self.assertIsNone(result[2]['rms_dbfs'])

    def test_selection_changes_are_rejected_before_compiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = self.fixture(Path(tmp))
            save(batch.work/'batch/selection-input.json', {'fingerprint': batch.selection_fingerprint()})
            batch.require_selection()
            review = load(batch.work/'index/recording-review.json'); review['changed'] = True
            save(batch.work/'index/recording-review.json', review)
            with self.assertRaisesRegex(ValueError, 'Selection inputs changed'):
                batch.compile()

    def test_prepare_dry_run_does_not_submit_or_generate_editorial_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = self.fixture(Path(tmp))
            with patch.object(batch, 'command') as command, patch.object(batch, 'review_audio') as audio:
                self.assertEqual(batch.prepare(False, True)['status'], 'dry_run')
                command.assert_not_called(); audio.assert_not_called()

    def test_audio_summary_preserves_observations_and_coverage_not_transport(self):
        report = {'status': 'complete', 'required_cuts': 3, 'reviewed_cuts': 3, 'results': [
            {'job': {'id': 'x', 'media': 'final', 'start_s': 2, 'end_s': 4},
             'result': {'issues': [{'text': '尾字不全'}]}, 'transport': 'not needed'}]}
        summary = audio_summary(report)
        self.assertEqual(summary['observations'][0]['result'], report['results'][0]['result'])
        self.assertEqual(summary['reviewed_cuts'], 3)
        self.assertNotIn('transport', json.dumps(summary))

    def test_changed_pauses_and_unreviewed_captions_stop_before_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = self.fixture(Path(tmp))
            save(batch.work/'batch/selection-input.json', {'fingerprint': batch.selection_fingerprint()})
            save(batch.work/'pause-authoring.json', {'groups': []})
            save(batch.work/'batch/compiled-input.json', {'pauses': 'stale', 'revision': 'r'})
            with patch.object(batch, 'command') as command, self.assertRaisesRegex(ValueError, 'Pause decisions changed'):
                batch.deliver(True, True)
            command.assert_not_called()

    def test_long_batch_stages_background_once_and_forward_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = self.fixture(Path(tmp))
            argv = ['talking-batch', '--workspace-root', str(batch.workspace), '--project-dir', str(batch.project),
                    '--stage', 'prepare', '--execute', '--audio-review']
            with patch('managed_job.start', return_value={'job_id': 'x'}) as start:
                cli.main(argv)
                command = start.call_args.args[1]
                self.assertIn('--workspace-root', command)
                self.assertIn('--audio-review', command)
                self.assertNotIn('--foreground', command)
                self.assertEqual(start.call_count, 1)

    def test_select_compile_packet_chain_reuses_edits_and_keeps_source_specs(self):
        from recording_review import draft_review
        with tempfile.TemporaryDirectory() as tmp:
            batch = self.fixture(Path(tmp))
            doc = load(batch.transcript)
            review = draft_review(doc)
            review.update(status='reviewed', mode='solo', evidence='原片身份已核对')
            review['content_review'] = {'status': 'reviewed', 'evidence': '正文无拍摄交流'}
            review['speaker_roles'][0].update(role='performer', evidence='原片视听证据')
            save(batch.work/'index/recording-review.json', review)
            audio = batch.work/'asr/analysis.wav'
            with wave.open(str(audio), 'wb') as wav:
                wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0\0'*32000)
            meta = {'streams': [{'codec_type': 'video', 'width': 3840, 'height': 2160,
                                 'avg_frame_rate': '60000/1001', 'side_data_list': [{'rotation': 90}]},
                                {'codec_type': 'audio'}], 'format': {'duration': '2'}}
            with patch('authoring.subprocess.run') as probe:
                probe.return_value.stdout = json.dumps(meta)
                selected = batch.selection()
            save(batch.work/'pause-authoring.json', {'revision': selected['revision'], 'groups': [
                {'indexes': list(range(1, selected['boundaries']+1)), 'category': 'within_sentence', 'reason': '同一句语法衔接'}]})
            result = batch.compile()
            plan = load(batch.work/'compiled/edit-plan.json')
            self.assertEqual(plan['fps'], {'num': 60000, 'den': 1001})
            self.assertEqual((plan['width'], plan['height']), (2160, 3840))
            draft = load(result['draft']); draft['review'] = 'reviewed'
            save(result['draft'], draft)
            self.assertEqual(batch.compile()['revision'], result['revision'])
            self.assertEqual(load(result['draft'])['review'], 'reviewed')
            packet = Path(result['packet']).read_text(encoding='utf-8')
            self.assertIn('4:十万元', packet)
            with patch.object(batch, 'command') as command, self.assertRaisesRegex(ValueError, 'complete caption packet'):
                batch.deliver(False, False)
            command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
