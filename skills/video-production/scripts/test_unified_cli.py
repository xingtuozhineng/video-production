import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import video_production as cli


class UnifiedCliTests(unittest.TestCase):
    def test_pre_render_review_forwards_required_workspace_to_runner(self):
        args = cli.build_parser().parse_args(['pre-render-review', '--workspace-root', 'workspace',
            '--project-dir', 'project', '--plan', 'plan.json', '--captions-dir', 'captions'])
        forwarded = cli.forwarded(args)
        self.assertEqual(forwarded[forwarded.index('--workspace-root') + 1], 'workspace')

    def test_contract_is_derived_from_every_public_parser(self):
        parser = cli.build_parser()
        contract = cli.parser_contract(parser)
        self.assertEqual(set(contract['commands']),
                         {'prepare', 'check', 'init', 'tts', 'voice-create', 'voice-list', 'align', 'tighten', 'sample', 'screencast-preview', 'transcribe', 'compile', 'captions', 'render', 'qc',
                          'hardware', 'index', 'select', 'pause-prepare', 'pause-author', 'pause-rebase', 'caption-draft', 'caption-build', 'caption-rebase', 'sync-check', 'audio-review', 'talking-batch',
                          'export', 'deliver', 'pre-render-review', 'job-status', 'job-stop', 'job-resume', 'job-watch', 'inspect', 'screencast-check',
                          'screencast-build', 'screencast-deliver'})
        render = contract['commands']['render']
        encoder = next(a for a in render['arguments'] if a['dest'] == 'encoder')
        self.assertEqual(encoder['choices'], ['auto', 'libx264', 'h264_nvenc', 'h264_qsv', 'h264_amf', 'h264_videotoolbox'])
        self.assertEqual(encoder['default'], 'auto')
        self.assertTrue(next(a for a in render['arguments'] if a['dest'] == 'source')['required'])
        self.assertIn('<out-dir>/render.log', render['outputs'])

    def test_contract_json_is_serializable(self):
        text = json.dumps(cli.parser_contract(cli.build_parser()), ensure_ascii=False)
        self.assertEqual(json.loads(text)['schema_version'], 1)

    def test_check_never_forwards_install(self):
        args = cli.build_parser().parse_args(['check', '--workspace-root', '.'])
        forwarded = cli.forwarded(args)
        self.assertEqual(forwarded[:2], ['--project-dir', '.'])
        self.assertIn('--json', forwarded)
        self.assertIn('--write-tools', forwarded)
        self.assertNotIn('--install', forwarded)

    def test_render_resolves_shared_ffmpeg_without_exposing_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            deps = root / 'video-production-deps'; deps.mkdir()
            (deps / 'tools.json').write_text(json.dumps({'ffmpeg': 'shared-ffmpeg'}), encoding='utf-8')
            args = cli.build_parser().parse_args([
                'render', '--workspace-root', str(root), '--source', 'raw.mp4',
                '--plan', 'edit-plan.json', '--out', 'final.mp4'])
            forwarded = cli.forwarded(args)
            self.assertEqual(forwarded[-2:], ['--ffmpeg', 'shared-ffmpeg'])
            self.assertNotIn('--workspace-root', forwarded)
            self.assertNotIn('--width', forwarded)
            self.assertNotIn('--height', forwarded)

    def test_select_does_not_forward_fixed_output_specs(self):
        with patch('video_production.tools', return_value={'ffprobe': 'shared-probe'}):
            args = cli.build_parser().parse_args(['select', '--workspace-root', '.', '--source', 'raw.mp4',
                '--transcript', 'words.json', '--selection', 'selection.json', '--review', 'review.json', '--out', 'edit'])
            forwarded = cli.forwarded(args)
        for flag in ('--fps', '--width', '--height'):
            self.assertNotIn(flag, forwarded)

    def test_audio_review_is_dry_by_default_and_paid_run_is_backgrounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'video-production-deps').mkdir()
            (root/'video-production-deps/tools.json').write_text(json.dumps({'ffmpeg':'shared-ffmpeg','ffprobe':'shared-probe'}))
            argv=['audio-review','--workspace-root',str(root),'--project-dir',str(root/'project'),
                  '--source','raw.mp4','--stage','source']
            with patch('video_production.subprocess.run') as run, patch('managed_job.start') as start:
                run.return_value.returncode=0
                cli.main(argv); start.assert_not_called()
                self.assertNotIn('--execute',run.call_args.args[0])
                start.return_value={'job_id':'audio-review'}
                cli.main(argv+['--execute']); start.assert_called_once()
                self.assertEqual(start.call_args.args[0],root/'project/qc/audio-review/.job')
                self.assertIn('shared-probe',start.call_args.args[1])

    def test_caption_build_resolves_python_from_plan_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'video-production-deps').mkdir()
            runtime = root / 'python.exe'; runtime.touch()
            (root / 'video-production-deps/tools.json').write_text(json.dumps({'python': str(runtime)}))
            with patch('video_production.subprocess.run') as run:
                run.return_value.returncode = 0
                cli.main(['caption-build', '--words', str(root/'work/words.json'), '--plan', str(root/'work/plan.json'),
                          '--draft', str(root/'work/draft.json'), '--out', str(root/'work/captions')])
                self.assertEqual(run.call_args.args[0][0], str(runtime))

    def test_dispatch_uses_argv_list_not_shell(self):
        with patch('video_production.subprocess.run') as run:
            run.return_value.returncode = 0
            result = cli.main(['init', '--workspace-root', '.', '--route', 'validation',
                               '--name', 'contract-smoke'])
        self.assertEqual(result, 0)
        self.assertIsInstance(run.call_args.args[0], list)

    def test_tts_resolves_shared_media_tool_and_backgrounds_only_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'video-production-deps').mkdir()
            (root / 'video-production-deps/tools.json').write_text(json.dumps({'ffmpeg': 'shared-ffmpeg'}), encoding='utf-8')
            argv = ['tts', '--workspace-root', str(root), '--script', 'copy.txt', '--out-dir', str(root / 'voice')]
            self.assertEqual(cli.forwarded(cli.build_parser().parse_args(argv))[-2:], ['--ffmpeg', 'shared-ffmpeg'])
            with patch('video_production.subprocess.run') as run, patch('managed_job.start') as start:
                run.return_value.returncode = 0
                start.return_value = {'job_id': 'offline-job'}
                cli.main(argv)
                start.assert_not_called()
                cli.main(argv + ['--execute'])
                start.assert_called_once()
                self.assertEqual(start.call_args.args[0], root / 'voice/.job')

    def test_semantic_build_and_preview_forward_their_public_contracts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'video-production-deps').mkdir()
            (root / 'video-production-deps/tools.json').write_text(json.dumps({'ffprobe': 'shared-probe'}), encoding='utf-8')
            args = cli.build_parser().parse_args(['screencast-build', '--workspace-root', str(root),
                '--content', 'content.json', '--script', 'script.json', '--timing', 'timing.json', '--audio', 'voice.wav', '--out', 'plan.json'])
            self.assertEqual(cli.forwarded(args)[-2:], ['--ffprobe', 'shared-probe'])
            argv = ['screencast-preview', '--workspace-root', str(root), '--plan', 'plan.json', '--audio', 'voice.wav', '--out-dir', str(root/'output')]
            with patch('managed_job.start', return_value={'job_id': 'preview'}) as start:
                cli.main(argv)
            command = start.call_args.args[1]
            self.assertIn('--preview-only', command)
            self.assertIn('--workspace-root', command)
            self.assertNotIn('--review', command)

    def test_voice_public_commands_forward_shared_tools_and_action(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'video-production-deps').mkdir()
            (root / 'video-production-deps/tools.json').write_text(json.dumps({'ffmpeg': 'shared-ffmpeg'}), encoding='utf-8')
            args = cli.build_parser().parse_args(['voice-create', '--workspace-root', str(root),
                '--audio', 'sample.wav', '--out', 'voice.json', '--consent', '--execute'])
            forwarded = cli.forwarded(args)
            self.assertEqual(forwarded[0], 'create')
            self.assertEqual(forwarded[-2:], ['--ffmpeg', 'shared-ffmpeg'])
            self.assertNotIn('--workspace-root', forwarded)
            args = cli.build_parser().parse_args(['voice-list', '--execute'])
            self.assertEqual(cli.forwarded(args)[0], 'list')


if __name__ == '__main__':
    unittest.main(verbosity=2)
