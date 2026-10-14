"""Unified CLI and machine-readable contract for the video-production skill family."""
import argparse
import json
import subprocess
import sys
from pathlib import Path
from hardware import ENCODERS


HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
TALKING = SKILL.parent / 'talking-head-cut' / 'scripts'
ROUTES = ('talking-head', 'hook-video', 'ppt-screencast', 'transcription', 'storyboard',
          'asset', 'existing-edit', 'validation')


def add_path(parser, flag, help_text, required=True, action=None):
    kwargs = {'help': help_text, 'required': required}
    if action:
        kwargs.update(action=action, default=[])
    else:
        kwargs['type'] = Path
    parser.add_argument(flag, **kwargs)


def command(subparsers, name, help_text, script, outputs):
    parser = subparsers.add_parser(name, help=help_text, description=help_text)
    parser.set_defaults(_script=script, _outputs=outputs, _command=name)
    return parser


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='video-production-cli 1')
    sub = parser.add_subparsers(dest='command', required=True)

    p = command(sub, 'prepare', 'Install shared dependencies before production work.',
                HERE / 'prepare_workspace.py', ['video-production-deps/**', 'video-production-deps/tools.json'])
    add_path(p, '--workspace-root', 'Workspace containing raw media, projects and shared dependencies.')
    p.add_argument('--dry-run', action='store_true', help='Print actions without writing or installing.')

    p = command(sub, 'check', 'Check an already prepared workspace; never installs.',
                HERE / 'check_env.py', ['video-production-deps/tools.json'])
    add_path(p, '--workspace-root', 'Prepared video workspace.')
    p.add_argument('--deep', action='store_true', help='Verify bundled font hashes.')
    p.add_argument('--network', action='store_true', help='Check configured API TLS reachability.')
    add_path(p, '--env', 'Optional Skill .env path.', required=False)

    p = command(sub, 'init', 'Create a classified project directory and source manifest.',
                HERE / 'init_project.py', ['projects/<route>/<date>-<name>/**', 'production.json'])
    add_path(p, '--workspace-root', 'Video workspace root.')
    p.add_argument('--route', required=True, choices=ROUTES, help='Production route and project category.')
    p.add_argument('--name', required=True, help='Stable ASCII project slug or title.')
    p.add_argument('--date', help='YYYYMMDD; defaults to today.')
    add_path(p, '--source', 'Source media path; repeatable and recorded without copying.',
             required=False, action='append')

    p = command(sub, 'inspect', 'Query scripts, words, utterances, pauses or final joins without helper code.',
                HERE / 'inspect_inputs.py', ['bounded JSON on stdout; optional --out report'])
    p.add_argument('--kind', required=True, choices=('script', 'words', 'utterances', 'blocks', 'pauses', 'joins', 'selection'))
    add_path(p, '--input', 'Script DOCX/TXT/MD or canonical transcript/decisions/plan JSON.')
    add_path(p, '--words', 'Same-revision mapped words, required for joins.', required=False)
    p.add_argument('--range', help='Inclusive original indexes FIRST:LAST.')
    p.add_argument('--time', help='START:END seconds.')
    p.add_argument('--text', help='Literal text filter.')
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--limit', type=int, default=40)
    p.add_argument('--max-text-chars', type=int, default=200)
    add_path(p, '--out', 'Optional bounded query report.', required=False)

    p = command(sub, 'talking-batch', 'Run deterministic talking-head stages and return complete decision packets.',
                HERE / 'talking_batch.py', ['<project-dir>/work/batch/state.json', 'complete stage review packets', 'output/handoff.json'])
    for flag in ('workspace-root', 'project-dir'):
        add_path(p, '--' + flag, flag)
    p.add_argument('--stage', required=True, choices=('prepare', 'select', 'compile', 'review', 'deliver', 'evidence'))
    p.add_argument('--ranges', help='Additional evidence windows in source seconds START:END,...')
    add_path(p, '--env', 'Optional Skill .env path.', required=False)
    p.add_argument('--execute', action='store_true', help='Authorize ASR and requested listening calls; prepare defaults to dry-run.')
    p.add_argument('--audio-review', action='store_true', help='Include source, pre-render PCM and actual final Qwen-Omni review within the shared 0.2 CNY ledger.')
    add_path(p, '--caption-draft', 'Reviewed same-revision rebased caption draft for review/deliver; otherwise use the compiled draft.', required=False)
    p.add_argument('--foreground', action='store_true', help='Maintenance verification; prepare/review/deliver normally run in the background.')

    p = command(sub, 'pre-render-review', 'Review current talking-head captions, boundaries and real PCM audio before video encoding.',
                HERE/'pre_render_review.py', ['<project-dir>/work/pre-render/<input-signature>/review.md', 'decisions.json'])
    for flag in ('workspace-root', 'project-dir', 'plan', 'captions-dir'):
        add_path(p, '--'+flag, flag)
    add_path(p, '--env', 'Optional Skill .env path.', required=False)
    p.add_argument('--execute', action='store_true', help='Execute requested PCM timeline listening within the existing ledger.')
    p.add_argument('--audio-review', action='store_true', help='Same 0.2 CNY ledger for source, PCM preview and actual final MP4.')
    p.add_argument('--foreground', action='store_true', help='Maintenance verification only.')

    p = command(sub, 'screencast-check', 'Check page targets, camera and emphasis intervals.',
                SKILL.parent / 'ppt-screencast' / 'scripts' / 'check_screencast_plan.py', ['<out>/plan-check.json'])
    add_path(p, '--plan', 'Canonical screencast plan.')
    add_path(p, '--out', 'Optional geometry check report.', required=False)

    p = command(sub, 'screencast-build', 'Compile authored targets/timing into camera keys; preserve narration timing.',
                SKILL.parent / 'ppt-screencast/scripts/build_screencast.py', ['screencast-plan.json'])
    inputs = p.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--author', type=Path, help='Legacy authored boxes and final-frame cues.')
    inputs.add_argument('--content', type=Path, help='Page boxes plus scene/cue sentence IDs; no frame arithmetic.')
    for flag in ('workspace-root', 'script', 'timing', 'audio', 'author-out'):
        add_path(p, '--' + flag, flag + '; needed for semantic content binding', required=False)
    add_path(p, '--out', 'Compiled canonical plan.')
    p = command(sub, 'screencast-deliver', 'Render, mux and QC one frozen screencast plan; MP4 only.',
                SKILL.parent / 'ppt-screencast/scripts/deliver_screencast.py', ['<out-dir>/handoff.json', '<out-dir>/<signature>/final.mp4'])
    for flag in ('workspace-root', 'plan', 'audio', 'out-dir'):
        add_path(p, '--' + flag, flag)
    add_path(p, '--font', 'Optional local font file; defaults to bundled Noto Sans SC.', required=False)
    add_path(p, '--review', 'Passed review JSON tied to the preview signature.', required=True)
    p.add_argument('--foreground', action='store_true', help='Local verification only; normal work returns a background job.')

    p = command(sub, 'screencast-preview', 'Render review stills only; do not start full video rendering.',
                SKILL.parent / 'ppt-screencast/scripts/deliver_screencast.py', ['<out-dir>/<signature>/preflight.json', '<out-dir>/<signature>/review-draft.json'])
    for flag in ('workspace-root', 'plan', 'audio', 'out-dir'):
        add_path(p, '--' + flag, flag)
    add_path(p, '--font', 'Optional bundled font override.', required=False)
    p.add_argument('--frames', help='Optional CSV frames for partial inspection; partial preview cannot authorize delivery.')
    p.add_argument('--foreground', action='store_true', help='Local verification only.')
    p.set_defaults(preview_only=True)

    p = command(sub, 'voice-create', 'Create one reusable cloned voice from authorized local audio; dry-run unless --execute.',
                HERE / 'bailian_voice.py', ['<out> voice record', '<out-stem>.state.json enrollment checkpoint'])
    p.set_defaults(_action='create')
    for flag in ('workspace-root', 'audio', 'out'):
        add_path(p, '--' + flag, flag)
    add_path(p, '--env', 'Optional Skill .env path.', required=False)
    p.add_argument('--preferred-name', default='video', help='1-16 ASCII letters, numbers or underscores.')
    p.add_argument('--start-s', type=float, default=0)
    p.add_argument('--sample-seconds', type=float, default=15)
    p.add_argument('--consent', action='store_true', help='Reference is owned, authorized or synthetic.')
    p.add_argument('--execute', action='store_true', help='Submit one paid voice enrollment; no automatic retries.')

    p = command(sub, 'voice-list', 'Query account voice identities without creating another voice.',
                HERE / 'bailian_voice.py', ['paged JSON on stdout'])
    p.set_defaults(_action='list')
    add_path(p, '--env', 'Optional Skill .env path.', required=False)
    p.add_argument('--page-index', type=int, default=0)
    p.add_argument('--page-size', type=int, default=20)
    p.add_argument('--execute', action='store_true', help='Query provider; dry-run by default.')

    p = command(sub, 'tts', 'Synthesize exact narration with shared Qwen TTS; dry-run unless --execute.',
                HERE / 'bailian_tts.py', ['<out-dir>/voiceover.wav', '<out-dir>/script-sentences.json', '<out-dir>/tts-manifest.json'])
    for flag in ('workspace-root', 'script', 'out-dir'):
        add_path(p, '--' + flag, flag)
    add_path(p, '--env', 'Optional Skill .env path.', required=False)
    p.add_argument('--model', help='Qwen3-TTS-Flash model ID; defaults to config or qwen3-tts-flash.')
    add_path(p, '--voice-record', 'Reusable cloned voice record; selects its bound VC model and voice.', required=False)
    p.add_argument('--voice', help='Defaults to config or Cherry.')
    p.add_argument('--language-type', help='Defaults to config or Chinese.')
    p.add_argument('--gap-s', type=float, default=0, help='Optional silence between TTS chunks; no pause compression.')
    p.add_argument('--execute', action='store_true', help='Authorize actual provider calls/resume.')
    p.add_argument('--foreground', action='store_true', help='Local verification only; paid execution normally returns a background job.')

    p = command(sub, 'align', 'Align exact script sentences to measured ASR words; no project helper code.',
                HERE / 'align_script.py', ['timing.json'])
    for flag in ('workspace-root', 'script', 'transcript', 'out'):
        add_path(p, '--' + flag, flag)
    add_path(p, '--json-detail', 'Optional alignment findings for bounded review.', required=False)
    p = command(sub, 'tighten', 'Explicitly tighten pauses and remap word timestamps; PCM WAV fast path.',
                HERE / 'compress_pauses.py', ['tightened audio', 'mapped transcript', 'pause analysis'])
    for flag in ('workspace-root', 'media', 'transcript', 'out', 'out-transcript'):
        add_path(p, '--' + flag, flag)
    add_path(p, '--analysis', 'Optional measurement report.', required=False)
    p.add_argument('--max-pause-s', type=float, required=True, help='Maximum pause threshold; choose for this route.')
    p.add_argument('--keep-pause-s', type=float, required=True, help='Retained pause duration; no inherited hook default.')
    p.add_argument('--no-second-pass', action='store_true')

    p = command(sub, 'transcribe', 'Create or resume a cached word-level Bailian transcript.',
                HERE / 'bailian_asr.py', ['<out>/transcript.source.json', '<out>/*.srt', '<out>/*-manifest.json'])
    add_path(p, '--workspace-root', 'Workspace used to resolve shared FFmpeg.')
    add_path(p, '--media', 'Source media file.')
    add_path(p, '--out', 'Transcription output directory.')
    add_path(p, '--env', 'Optional Skill .env path.', required=False)
    p.add_argument('--execute', action='store_true', help='Authorize actual provider submission/resume.')
    p.add_argument('--audio-format', choices=('wav', 'mp3'), default='wav')
    p.add_argument('--no-diarization', action='store_true')
    p.add_argument('--speaker-count', type=int)

    p = command(sub, 'compile', 'Compile arbitrary editorial selections into one validated timeline.',
                TALKING / 'compile_timeline.py', ['edit-plan.json', 'mapped-words.json', 'pause-report.json', 'timeline-check.json'])
    add_path(p, '--selection', 'Content-authored selection-plan.json.')
    add_path(p, '--transcript', 'Word-level transcript JSON.')
    add_path(p, '--decisions', 'Explicit semantic pause decisions JSON.')
    add_path(p, '--out-dir', 'Timeline output directory.')
    p.add_argument('--sample-rate', type=int, default=48000)

    p = command(sub, 'captions', 'Compile semantic caption authorship with exact word coverage.',
                TALKING / 'caption_pages.py', ['<out>.json', '<out>.srt'])
    add_path(p, '--words', 'mapped-words.json from compile.')
    add_path(p, '--editorial', 'Agent-authored caption-editorial.json.')
    add_path(p, '--out', 'Output captions JSON; SRT is written beside it.')

    p = command(sub, 'render', 'Render a validated timeline with shared FFmpeg.',
                TALKING / 'render_timeline.py', ['<out>', '<out-dir>/render.log'])
    add_path(p, '--workspace-root', 'Prepared workspace used to resolve FFmpeg.')
    add_path(p, '--source', 'Original source media.')
    add_path(p, '--plan', 'Validated edit-plan.json.')
    add_path(p, '--out', 'Final MP4 path.')
    add_path(p, '--captions-ass', 'Optional ASS subtitle file.', required=False)
    p.add_argument('--audio-only', action='store_true', help='Render only exact timeline PCM to WAV before expensive video rendering.')
    p.add_argument('--width', type=int, help='Output width override; defaults to the edit plan.')
    p.add_argument('--height', type=int, help='Output height override; defaults to the edit plan.')
    p.add_argument('--encoder', choices=('auto', 'libx264', *ENCODERS), default='auto')
    p.add_argument('--preset', default='medium'); p.add_argument('--crf', type=int, default=18)

    p = command(sub, 'qc', 'Run Windows-safe deterministic technical delivery QC.',
                TALKING / 'qc_delivery.py', ['<out>/qc.json'])
    add_path(p, '--workspace-root', 'Prepared workspace used to resolve FFmpeg and ffprobe.')
    add_path(p, '--media', 'Rendered delivery file.')
    add_path(p, '--plan', 'The exact edit-plan.json used for rendering.')
    add_path(p, '--out', 'QC JSON output path.')
    add_path(p, '--reference-audio', 'Optional same-revision narration for bounded decoded-audio identity checks.', required=False)

    p = command(sub, 'sample', 'Extract short diagnostic frame ranges without full-source decoding.',
                HERE / 'sample_frames.py', ['diagnostic video with requested source ranges'])
    for flag in ('workspace-root', 'input', 'output'):
        add_path(p, '--' + flag, flag)
    p.add_argument('--ranges', required=True, help='Inclusive original frame ranges START:END,...')
    p.add_argument('--fps', help='Optional source FPS as NUM/DEN; otherwise probed.')
    p.add_argument('--scale', help='Optional output size W:H.')

    p = command(sub, 'hardware', 'Inventory GPUs and test actual encoder initialization.',
                HERE / 'hardware.py', ['video-production-deps/hardware.json'])
    add_path(p, '--workspace-root', 'Prepared workspace.')
    p.add_argument('--refresh', action='store_true')

    def author(name, description):
        child = command(sub, name, description, TALKING / 'authoring.py', ['--out files'])
        child.set_defaults(_action=name)
        return child
    p = author('index', 'Create a compact word TSV and editable selection range template.')
    add_path(p, '--transcript', 'Cached transcript.source.json.')
    add_path(p, '--out', 'Index directory.')
    p = author('select', 'Turn authored word ranges into a probed selection and pause candidates.')
    for flag in ['workspace-root', 'transcript', 'selection', 'source', 'out']:
        add_path(p, '--' + flag, flag)
    add_path(p, '--review', 'Reviewed recording-review.json: performer/prompt roles, repeated takes and excluded audio.')
    p.add_argument('--fps', type=int, help='Output FPS override; defaults to the exact source frame rate.')
    p.add_argument('--width', type=int, help='Portrait output width override; defaults to the source display short edge.')
    p.add_argument('--height', type=int, help='Portrait output height override; defaults to the source display long edge.')
    p = author('pause-author', 'Apply compact semantic groups to all pause candidates with revision/coverage checks.')
    for flag in ('decisions', 'draft', 'out'):
        add_path(p, '--' + flag, flag)
    p = author('pause-rebase', 'Reuse unchanged word-neighbor semantic decisions; return only changed boundaries for review.')
    for flag in ('old-decisions','decisions','old-words','words','out'):
        add_path(p,'--'+flag,flag)
    p = command(sub, 'pause-prepare', 'Prepare all pause decisions from selected words.',
                TALKING / 'semantic_pacing.py', ['pause-decisions.json'])
    p.set_defaults(_prepare=True)
    add_path(p, '--words', 'words.selected.json.')
    add_path(p, '--out', 'Output directory.')
    p = author('caption-draft', 'Generate editable word-range pages; never match retyped text.')
    add_path(p, '--words', 'mapped-words.json.')
    add_path(p, '--plan', 'edit-plan.json.')
    add_path(p, '--out', 'New caption-authoring.json file.')
    p = author('caption-build', 'Validate all pages together; emit captions JSON, SRT, ASS and fonts.')
    for flag in ['words', 'plan', 'draft', 'out']:
        add_path(p, '--' + flag, flag)
    add_path(p, '--font', 'Optional real font file; defaults to bundled Source Han Sans.', required=False)
    add_path(p, '--edits', 'Optional sparse corrections, highlights and exceptional page replacements.', required=False)
    p = author('caption-rebase', 'Migrate reviewed pages/corrections by stable source word IDs after deletion-only edits.')
    for flag in ('old-words','draft','words','out'):
        add_path(p,'--'+flag,flag)
    p = command(sub, 'sync-check', 'Compare decoded delivery audio against bounded canonical timeline PCM windows.',
                TALKING / 'sync_timeline.py', ['<out-dir>/sync.json'])
    for flag in ('workspace-root', 'plan', 'media', 'out-dir'):
        add_path(p, '--' + flag, flag)
    p = command(sub, 'audio-review', 'Qwen-Omni source/final audio review with a shared CNY 0.2 per-video ledger.',
                HERE / 'audio_review.py', ['<project-dir>/qc/audio-review/budget.json', '<project-dir>/qc/audio-review/latest.json'])
    for flag in ('workspace-root', 'project-dir', 'source'):
        add_path(p, '--' + flag, flag)
    for flag in ('media', 'plan', 'env'):
        add_path(p, '--' + flag, flag, required=False)
    p.add_argument('--stage', choices=('source', 'final', 'both'), default='both')
    p.add_argument('--focus', help='At most two final-media START:END windows, shared budget and cumulative limit.')
    p.add_argument('--source-focus', help='At most four source START:END targets with context audio and actual mouth frames; stage source only.')
    p.add_argument('--recheck-invalid', action='store_true', help='Explicit bounded re-listening for known-cost malformed cut replies; preserve original ledger and never retry unknown outcomes.')
    p.add_argument('--execute', action='store_true', help='Authorize paid audio review within the shared CNY 0.2 cap; default dry-run.')
    p.add_argument('--foreground', action='store_true', help='Local testing only; paid runs default to background.')
    p = command(sub, 'export', 'Export editable Jianying draft from the same timeline.',
                HERE / 'export_jianying.py', ['--out draft directory'])
    add_path(p, '--plan', 'edit-plan.json with width/height.')
    add_path(p, '--captions', 'captions.json.', required=False)
    add_path(p, '--layers', 'Optional independent tracks.', required=False)
    add_path(p, '--out', 'New draft directory.')
    p = command(sub, 'deliver', 'Start a background render/QC/draft job; reuse verified checkpoints on resume.',
                HERE / 'delivery.py', ['<out-dir>/handoff.json', '<out-dir>/<signature>/**'])
    for flag in ['workspace-root', 'plan', 'captions-dir', 'out-dir']:
        add_path(p, '--' + flag, flag)
    p.add_argument('--encoder', choices=('auto', 'libx264', *ENCODERS), default='auto')
    p.add_argument('--preset', default='medium'); p.add_argument('--crf', type=int, default=18)
    p.add_argument('--foreground', action='store_true', help='For local checks; normal Agent work uses background jobs.')
    for name in ['status', 'stop', 'resume']:
        p = command(sub, 'job-' + name, 'Read/control the existing local delivery job.',
                    HERE / 'managed_job.py', ['job state JSON'])
        p.set_defaults(_action=name)
        add_path(p, '--job-dir', 'The job_dir returned by deliver.')
    p = command(sub, 'job-watch', 'Wait in a tool/background job; emit terminal state without model polling.',
                HERE / 'managed_job.py', ['terminal job state JSON'])
    p.set_defaults(_action='watch')
    add_path(p, '--job-dir', 'The job_dir returned by deliver.')
    p.add_argument('--job-id', required=True, help='Pin the exact job_id returned by deliver.')
    p.add_argument('--timeout', type=float, default=3600, help='Tool-side timeout in seconds; never cancels the job.')

    p = sub.add_parser('contract', help='Print the machine-readable CLI contract derived from argparse.')
    p.add_argument('--pretty', action='store_true', help='Indent JSON output.')
    p.add_argument('--command', dest='only_command', help='Return just one command to keep model context small.')
    p.set_defaults(_command='contract')
    return parser


def type_name(action):
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        return 'boolean'
    return {Path: 'path', int: 'integer', float: 'number', str: 'string'}.get(action.type, 'string')


def action_contract(action):
    item = {'flags': list(action.option_strings), 'dest': action.dest, 'type': type_name(action),
            'required': bool(action.required), 'help': action.help or ''}
    if action.default not in (None, argparse.SUPPRESS, [], False):
        item['default'] = action.default
    if action.choices is not None:
        item['choices'] = list(action.choices)
    if isinstance(action, argparse._AppendAction):
        item['repeatable'] = True
    return item


def parser_contract(parser):
    result = {'schema_version': 1, 'program': 'video-production', 'commands': {}}
    sub_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for name, child in sub_action.choices.items():
        if name == 'contract':
            continue
        defaults = child._defaults
        result['commands'][name] = {
            'description': child.description or '',
            'arguments': [action_contract(a) for a in child._actions if a.option_strings and a.dest != 'help'],
            'outputs': list(defaults.get('_outputs', [])),
            'runner': str(defaults.get('_script', '')),
        }
    return result


def tools(workspace):
    path = workspace.resolve() / 'video-production-deps' / 'tools.json'
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f'workspace is not prepared; run prepare first ({path}: {exc})') from exc


def forwarded(args):
    command = args._command
    values = vars(args)
    out = []
    skip = {'command', '_command', '_script', '_outputs', '_action', '_prepare', 'foreground'}
    if values.get('_action'):
        out.append(values['_action'])
    if values.get('_prepare'):
        out.append('--prepare')
    for dest, value in values.items():
        if dest in skip or value is None or value is False or value == []:
            continue
        if dest == 'workspace_root':
            if command == 'check':
                flag = '--project-dir'
            elif command in ('prepare', 'init', 'deliver', 'pre-render-review', 'screencast-deliver', 'screencast-preview', 'hardware', 'talking-batch'):
                flag = '--workspace-root'
            else:
                continue
        else:
            flag = '--' + dest.replace('_', '-')
        if value is True:
            out.append(flag)
        elif isinstance(value, list):
            for item in value:
                out += [flag, str(item)]
        else:
            out += [flag, str(value)]
    if command == 'check':
        out += ['--json', '--write-tools']
    if command in ('transcribe', 'tts', 'tighten', 'voice-create'):
        out += ['--ffmpeg', tools(args.workspace_root)['ffmpeg']]
    if command == 'screencast-build' and args.workspace_root:
        out += ['--ffprobe', tools(args.workspace_root)['ffprobe']]
    if command == 'render':
        out += ['--hardware-report', str(args.workspace_root.resolve() / 'video-production-deps/hardware.json'),
                '--ffmpeg', tools(args.workspace_root)['ffmpeg']]
    if command == 'select':
        out += ['--ffprobe', tools(args.workspace_root)['ffprobe']]
    if command == 'sync-check':
        out += ['--ffmpeg', tools(args.workspace_root)['ffmpeg']]
    if command == 'audio-review':
        resolved = tools(args.workspace_root)
        out += ['--ffmpeg', resolved['ffmpeg'], '--ffprobe', resolved['ffprobe']]
    if command == 'qc':
        resolved = tools(args.workspace_root)
        out += ['--ffmpeg', resolved['ffmpeg'], '--ffprobe', resolved['ffprobe']]
    if command == 'sample':
        resolved = tools(args.workspace_root)
        out += ['--ffmpeg', resolved['ffmpeg'], '--ffprobe', resolved['ffprobe']]
    return out


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args._command == 'contract':
        value = parser_contract(parser)
        if args.only_command:
            if args.only_command not in value['commands']:
                parser.error('Unknown contract command')
            value['commands'] = {args.only_command: value['commands'][args.only_command]}
        print(json.dumps(value, ensure_ascii=False, indent=2 if args.pretty else None))
        return 0
    runtime = sys.executable
    workspace = getattr(args, 'workspace_root', None)
    if workspace is None:
        # Caption/compile commands have no workspace flag; infer it from their actual input path.
        for field in ('plan', 'draft', 'decisions', 'words', 'transcript'):
            path = getattr(args, field, None)
            if path:
                workspace = next((p for p in path.resolve().parents if (p / 'video-production-deps/tools.json').is_file()), None)
                if workspace:
                    break
    if (workspace and args._command not in ('prepare', 'init')
            and (workspace / 'video-production-deps/tools.json').is_file()):
        configured = tools(workspace).get('python')
        if configured and Path(configured).is_file():
            runtime = configured
    command_line = [runtime, args._script, *forwarded(args)]
    if (args._command == 'talking-batch' and not args.foreground
            and (args.stage == 'deliver' or (args.stage in ('prepare', 'review') and args.execute))):
        from managed_job import start
        print(json.dumps(start(args.project_dir / 'work/batch/.job', command_line), ensure_ascii=False))
        return 0
    if args._command == 'audio-review' and args.execute and not args.foreground:
        from managed_job import start
        print(json.dumps(start(args.project_dir / 'qc/audio-review/.job', command_line), ensure_ascii=False))
        return 0
    if args._command == 'pre-render-review' and args.execute and not args.foreground:
        from managed_job import start
        print(json.dumps(start(args.project_dir/'work/pre-render/.job', command_line), ensure_ascii=False))
        return 0
    if ((args._command in ('deliver', 'screencast-deliver', 'screencast-preview')
         or (args._command == 'tts' and args.execute)) and not args.foreground):
        from managed_job import start
        print(json.dumps(start(args.out_dir / '.job', command_line), ensure_ascii=False))
        return 0
    return subprocess.run([str(x) for x in command_line]).returncode


if __name__ == '__main__':
    raise SystemExit(main())
