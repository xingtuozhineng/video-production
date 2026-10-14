"""Export an editable Windows Jianying draft, retaining complete source media.

No rendering, paid calls, UI control or modification of existing drafts.
Use --install PACKAGE --draft-root DIRECTORY to copy and rebase a generated package.
"""
import argparse
import configparser
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from bailian_media import load, save

WRITER_COMMIT = 'c3318066d964744e2bfc66f75c71745fe8cea52a'


def micros(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('Time must be finite and nonnegative')
    return math.floor(value * 1000000 + .5)


def normalize(plan, captions=None, layers=None):
    """Convert the existing original-speed edit plan to independent editable clips."""
    revision = plan['revision']
    fps = plan['fps']['num'] / plan['fps']['den']
    if not revision or not math.isfinite(fps) or fps < 1:
        raise ValueError('Jianying export requires a positive FPS and a revision')
    if type(plan['duration_frames']) is not int or plan['duration_frames'] <= 0:
        raise ValueError('Invalid duration_frames')
    duration = micros(plan['duration_frames'] / fps)
    clips, warnings, ids = [], [], set()
    draft_fps = math.floor(fps + .5)
    if fps != draft_fps:
        warnings.append(f'原时间轴/MP4保持{plan["fps"]["num"]}/{plan["fps"]["den"]}fps；'
                        f'剪映编辑网格为{draft_fps}fps，片段和字幕按原时间轴精确微秒保存，未变速；应用内帧量化须另验。')
    previous = 0
    for s in plan.get('segments', []):
        if s['id'] in ids:
            raise ValueError('Duplicate segment ID')
        ids.add(s['id'])
        a, b, start, end = [micros(s[k]) for k in
                            ('source_in_s', 'source_out_s', 'final_in_s', 'final_out_s')]
        if (start != previous or end <= start or b <= a or
                abs((b-a) - (end-start)) > 1 or b > micros(plan['source']['duration_s'])):
            raise ValueError('Only continuous original-speed source segments are supported')
        previous = end
        clips.append(dict(kind='video', track='原片与同期声', id=s['id'],
                          path=plan['source'].get('path') or plan['source'].get('source_path'),
                          start_us=start, duration_us=end-start,
                          source_us=a, volume=1.0))
    if clips and not 0 <= duration-previous < math.ceil(1000000/fps):
        raise ValueError('Edit duration mismatch (only sub-frame tail padding is allowed)')
    if plan.get('timing_mode') == 'sample_audio_cumulative_video':
        warnings.append('采样级气口按微秒写入；剪映打开后的帧量化与声画同步仍须实测。')
    if captions is not None:
        if captions['revision'] != revision or captions['fps'] != plan['fps']:
            raise ValueError('Stale captions revision or mismatched FPS')
        rows = captions.get('captions', captions.get('lines'))
        if rows is None:
            raise ValueError('Expected caption_pages captions or rolling_captions lines')
        if 'captions' not in captions:
            warnings.append('滚动字幕按说话起止转换为独立文本，滚动动画与前句叠行未自动还原。')
        for c in rows:
            start, end = [micros(c[k]/fps) for k in ('start_frame', 'end_frame')]
            clips.append(dict(kind='text', track='字幕', text='\n'.join(c['lines']),
                              start_us=start, duration_us=end-start, y=-.72))
        warnings.append('字幕保留可编辑文字、分页和起止时间；重点短语样式、字体和逐字动画未自动还原。')
    if layers is not None:
        if layers['revision'] != revision:
            raise ValueError('Stale layers revision')
        for item in layers['clips']:
            allowed = {'kind', 'track', 'path', 'text', 'start_s', 'end_s', 'source_in_s',
                       'volume', 'fade_in_s', 'fade_out_s', 'x', 'y', 'scale', 'size', 'color', 'bold'}
            if set(item) - allowed:
                raise ValueError('Unsupported layer fields: ' + ', '.join(sorted(set(item)-allowed)))
            c = {k: v for k, v in item.items() if k not in
                 ('start_s', 'end_s', 'source_in_s', 'fade_in_s', 'fade_out_s')}
            c['start_us'] = micros(item['start_s'])
            c['duration_us'] = micros(item['end_s']) - c['start_us']
            c['source_us'] = micros(item.get('source_in_s', 0))
            c['fade_in_us'] = micros(item.get('fade_in_s', 0))
            c['fade_out_us'] = micros(item.get('fade_out_s', 0))
            c.setdefault('volume', 0 if c['kind'] == 'video' else 1)
            clips.append(c)
    if not clips:
        raise ValueError('Nothing to export')
    tracks = {}
    for c in clips:
        if c['kind'] not in ('video', 'audio', 'text') or not c['track']:
            raise ValueError('Invalid clip kind or track name')
        if c['duration_us'] <= 0 or c['start_us'] + c['duration_us'] > duration:
            raise ValueError('Clip outside final timeline')
        if c['kind'] == 'text' and not c.get('text', '').strip():
            raise ValueError('Empty text clip')
        if c['kind'] != 'text' and not c.get('path'):
            raise ValueError('Missing media path')
        if not math.isfinite(c.get('volume', 1)) or not 0 <= c.get('volume', 1) <= 10:
            raise ValueError('Volume must be between 0 and 10')
        if c.get('fade_in_us', 0) + c.get('fade_out_us', 0) > c['duration_us']:
            raise ValueError('Audio fades exceed clip duration')
        if c['kind'] != 'audio' and (c.get('fade_in_us') or c.get('fade_out_us')):
            raise ValueError('Fade options are only supported on separate audio clips')
        track = tracks.setdefault(c['track'], [])
        if track and track[0]['kind'] != c['kind']:
            raise ValueError('Mixed media types on one track')
        track.append(c)
    for track in tracks.values():
        track.sort(key=lambda c: c['start_us'])
        if any(a['start_us']+a['duration_us'] > b['start_us'] for a,b in zip(track,track[1:])):
            raise ValueError('Overlapping clips on the same track; use separate track names')
    # Place text above images/videos even when captions were supplied before extra layers.
    tracks = dict(sorted(tracks.items(), key=lambda pair: pair[1][0]['kind'] == 'text'))
    return dict(revision=revision, fps=draft_fps, timeline_fps=plan['fps'],
                duration_us=duration, tracks=tracks, warnings=warnings)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def rebase(project, destination, name):
    """Rewrite only our known material references, never edit an existing user draft."""
    manifest = load(project / 'package.json')
    mapping = {a['draft_path']: str(destination / a['relative_path']) for a in manifest['assets']}
    content = load(project / 'draft_content.json')
    for group in ('videos', 'audios'):
        for material in content['materials'].get(group, []):
            material['path'] = mapping[material['path']]
    content['id'] = str(uuid.uuid4()).upper()
    content['name'] = name
    save(project / 'draft_content.json', content)
    meta = load(project / 'draft_meta_info.json')
    meta.update(draft_id=content['id'], draft_name=name, draft_fold_path=str(destination),
                draft_root_path=str(destination.parent), tm_duration=content['duration'],
                tm_draft_create=int(time.time()*1000000), tm_draft_modified=int(time.time()*1000000))
    save(project / 'draft_meta_info.json', meta)
    for asset in manifest['assets']:
        asset['draft_path'] = mapping[asset['draft_path']]
    save(project / 'package.json', manifest)


def export_project(plan, out, captions=None, layers=None, base_dir=None, name=None):
    timeline = normalize(plan, captions, layers)
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError('Output already exists; choose a new folder')
    width, height = plan['width'], plan['height']
    if any(type(v) is not int or v <= 0 for v in (width, height)):
        raise ValueError('Provide positive integer width and height in the edit plan')
    if plan.get('output_orientation') == 'portrait' and width >= height:
        raise ValueError('Talking-head draft canvas must be portrait (height > width)')
    base_dir = Path(base_dir or '.').resolve()
    sources = {}
    for track in timeline['tracks'].values():
        for c in track:
            if c['kind'] != 'text':
                path = (base_dir / c['path']).resolve()
                if not path.is_file():
                    raise FileNotFoundError(path)
                c['path'] = str(path)
                sources[str(path)] = None
    try:
        import pyJianYingDraft as draft
    except ImportError as exc:
        raise RuntimeError('Install scripts/requirements-jianying.txt in the active Python environment') from exc
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.jianying-', dir=out.parent) as temp:
        stage = Path(temp) / 'project'
        script = draft.DraftFolder(temp).create_draft('project', width, height, timeline['fps'])
        (stage / 'assets').mkdir()
        assets = []
        for i, path in enumerate(sources):
            relative = 'assets/' + f'{i+1:03}_' + Path(path).name
            target = stage / relative
            shutil.copy2(path, target)  # Full media retains the handles needed to restore pauses.
            assets.append(dict(relative_path=relative, draft_path=str(target), sha256=sha256(target)))
            sources[path] = str(target)
        materials = {}
        for track_name, clips in timeline['tracks'].items():
            kind = clips[0]['kind']
            track_ref = script.append_track(draft.TrackSpec(getattr(draft.TrackType, kind), track_name))
            for c in clips:
                trange = draft.Timerange(c['start_us'], c['duration_us'])
                settings = draft.ClipSettings(transform_x=c.get('x', 0), transform_y=c.get('y', 0),
                                               scale_x=c.get('scale', 1), scale_y=c.get('scale', 1))
                if kind == 'text':
                    style = draft.TextStyle(size=c.get('size', 8), bold=c.get('bold', False),
                                            color=tuple(c.get('color', [1,1,1])), align=1)
                    segment = draft.TextSegment(c['text'], trange, style=style, clip_settings=settings)
                else:
                    key = (kind, c['path'])
                    if key not in materials:
                        cls = draft.VideoMaterial if kind == 'video' else draft.AudioMaterial
                        materials[key] = cls(sources[c['path']])
                    mat = materials[key]
                    source_range = draft.Timerange(c.get('source_us', 0), c['duration_us'])
                    kwargs = dict(source_timerange=source_range, volume=c.get('volume', 1), speed=1.0)
                    if kind == 'video':
                        segment = draft.VideoSegment(mat, trange, clip_settings=settings, **kwargs)
                    else:
                        segment = draft.AudioSegment(mat, trange, **kwargs)
                        if c.get('fade_in_us') or c.get('fade_out_us'):
                            segment.add_fade(c.get('fade_in_us', 0), c.get('fade_out_us', 0))
                script.add_segment(segment, track_ref)
        script.duration = timeline['duration_us']
        script.save()
        save(stage / 'package.json', dict(schema_version=1, writer_commit=WRITER_COMMIT,
                                         revision=plan['revision'], assets=assets))
        save(stage / 'edit-plan.json', plan)
        if captions is not None:
            save(stage / 'captions.final.json', captions)
        if layers is not None:
            save(stage / 'jianying-layers.json', layers)
        report = dict(status='generated_not_app_verified', revision=plan['revision'],
                      writer_commit=WRITER_COMMIT, tracks={k:len(v) for k,v in timeline['tracks'].items()},
                      duration_us=timeline['duration_us'], full_source_media=True,
                      app_open='not_checked', manual_edit='not_checked', playback='not_checked',
                      warnings=timeline['warnings'])
        save(stage / 'export-report.json', report)
        rebase(stage, out, name or out.name)
        stage.rename(out)
    return report


def install_project(package, draft_root):
    """Install a portable package under a fresh name without touching the root index."""
    package, draft_root = Path(package).resolve(), Path(draft_root).resolve()
    if not draft_root.is_dir():
        raise FileNotFoundError('Select an existing Jianying draft root in its Settings')
    out = draft_root / package.name
    if out.exists():
        raise FileExistsError('Draft already exists; use a new package name')
    manifest = load(package / 'package.json')
    if manifest['schema_version'] != 1:
        raise ValueError('Unsupported package schema')
    for asset in manifest['assets']:
        path = (package / asset['relative_path']).resolve()
        if not path.is_relative_to(package) or sha256(path) != asset['sha256']:
            raise ValueError('Missing, changed or invalid packaged asset')
    with tempfile.TemporaryDirectory(prefix='.jianying-', dir=draft_root) as temp:
        stage = Path(temp) / 'project'
        shutil.copytree(package, stage)
        rebase(stage, out, package.name)
        stage.rename(out)
    return out


def find_draft_root(user_data=None):
    """Read the current Windows Jianying setting instead of assuming its default root."""
    if user_data is None:
        local = os.environ.get('LOCALAPPDATA')
        if not local:
            raise ValueError('Cannot detect Windows Jianying; specify --draft-root')
        user_data = Path(local) / 'JianyingPro' / 'User Data'
    user_data = Path(user_data)
    settings = configparser.ConfigParser(interpolation=None)
    settings.read(user_data / 'Config' / 'globalSetting', encoding='utf-8-sig')
    custom = settings.get('General', 'currentCustomDraftPath', fallback='').strip()
    # Qt INI stores Windows path separators as doubled backslashes.
    path = Path(custom.replace('\\\\', '\\')) if custom else user_data / 'Projects' / 'com.lveditor.draft'
    if not path.is_dir():
        raise FileNotFoundError('Configured draft root is missing; check Jianying Settings or pass --draft-root: ' + str(path))
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', type=Path)
    mode.add_argument('--install', type=Path, metavar='PACKAGE')
    p.add_argument('--captions', type=Path)
    p.add_argument('--layers', type=Path)
    p.add_argument('--out', type=Path)
    p.add_argument('--draft-root', type=Path)
    p.add_argument('--name')
    a = p.parse_args()
    if a.install:
        root = a.draft_root if a.draft_root else find_draft_root()
        result = {'installed': str(install_project(a.install, root)), 'app_open': 'not_checked'}
    else:
        if not a.out:
            p.error('--plan requires --out')
        result = export_project(load(a.plan), a.out, load(a.captions) if a.captions else None,
                                load(a.layers) if a.layers else None, a.plan.resolve().parent, a.name)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
