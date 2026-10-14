# 正式脚本契约

Agent 开始制作前先读本页。公开入口是 `scripts/video_production.py`；制作时编辑内容数据，工具缺陷报告具体错误和恢复路径，由明确的维护任务处理。只获取当前命令的契约：

```text
python scripts/video_production.py contract --command caption-build --pretty
```

该 JSON 从同一套 `argparse` parser 自动生成，包含所有命令的参数、类型、必填项、choices、默认值、底层 runner 和产物；本页解释工作流边界，不重复充当参数事实源。

普通口播默认按 [固定流水线](stable-talking-head.md) 的`talking-batch`执行：prepare准备完整原片资料，select返回完整选段/气口表，compile产生字幕审核包，review构建最终字幕并集中审查/预听，deliver核对当前审核后连续渲染、QC、剪映导出和请求过的实际成片听审。prepare/review执行与deliver默认返回后台任务ID，沿用`job-watch/job-status/job-stop/job-resume`。审核数据沿用现有JSON，不另建第二套时间轴。以下原子命令仍保留，供已有工程或明确的局部修正使用。

`pre-render-review`供已有工程使用，生成最终字幕/接缝审核包及可选真实PCM听审。当前输入的决定文件审核完成、已知修改全部应用后才放行；时间轴/字幕/字体/角色证据变化使旧审核失效，同时间轴音频预听可复用。口播项目内原子`render/deliver`也检查同一门禁，实际MP4的后置QC与听审仍保留。命令和决定格式见 [渲染前集中审核](stable-talking-head.md#4-渲染前集中审核)。

`index`生成含说话人的词/句索引及`recording-review.json`。`select --review`需要speaker_roles身份核对和`content_review`发言用途核对；试词、拍摄交流、未知用途等局部例外用spans，漏识别声音用excluded_audio，不要求逐句填表。缺失/过期、用途未核对、保留非正式发言/领读/未知角色或混用已标注take时拒绝选段。旧表只补用途核对，不重新转写；详见 [拍摄角色与发言用途](../../talking-head-cut/references/recording-roles.md)。

`inspect` 提供Word／文本脚本、源／成片词表、语句、气口和最终接缝的只读分页查询，格式与调用时机见 [输入查询](input-inspection.md)。`job-watch` 在工具进程内等待指定 job_id 的终态；通过宿主后台工具收结果，不以模型轮询保活。watch 超时不取消原任务，失败与取消返回非零。

`audio-review`提供 [Qwen-Omni音频复核](audio-review.md)：默认无网络预检，`--execute`在单项目同原片累计0.2元预算内批量检查原片或成片，付费执行默认后台job；使用已有`job-watch`等待。它独立于`sync-check`的PCM同步测量。

## 安装阶段：prepare_workspace.py

```text
python scripts/video_production.py prepare --workspace-root <workspace-root>
```

- 参数：`--workspace-root` 是存放原片、项目和共享依赖的工作区；`--dry-run` 只打印命令。
- 输出：命令进度和最终环境检查 JSON。
- 产物：`video-production-deps/venv/`、`ffmpeg/`、`node/`、`npm-cache/`、`tools.json`。
- 时机：下载 Skill 后执行一次；不是每条视频任务的一部分。

## 任务开始：check_env.py

```text
python scripts/video_production.py check --workspace-root <workspace-root> --deep
```

- 参数：工作区、可选 `.env`、是否检查网络。
- 输出：各依赖及路线的 `ok/problems/routes` JSON；非零退出表示环境未就绪。
- 产物：刷新 `video-production-deps/tools.json`。
- 边界：任务阶段只检查，不带 `--install`；缺依赖时停止制作并回到安装阶段。

## 公共配音：bailian_tts.py

声音克隆由公共 `bailian_voice.py` 的 `voice-create` 和 `voice-list` 提供；所有子 Skill 复用，不各自写接口。参考录音、模型绑定、记录复用及授权边界见 [公共声音克隆](voice-cloning.md)。创建后将记录传给 `tts --voice-record`，普通配音默认值保持不变。

`tts` 从本次文本或句子JSON生成配音，不需要项目自写脚本；参数、配置和缓存恢复见 [公共 TTS](tts.md)。不带 `--execute` 为无网络／无写入预检；授权执行后返回后台job，复用 `job-watch` 等终态。产物为 `voiceover.wav`、可直接交给整句对齐的 `script-sentences.json` 及 `tts-manifest.json`。

执行器负责长文分段、音频格式统一、样本拼接和请求状态恢复，不包含 ASR、气口压缩或视频渲染。读取清单再进入这些后续步骤；遇到公共工具缺陷走维护任务，不在视频生产会话改代码。

## PPT 运镜计划：check_screencast_plan.py

```text
python scripts/video_production.py screencast-check --plan <work/screencast-plan.json> --out <qc/plan-check.json>
```

逐帧检查目标／标注／鼠标可见性、镜头稳定、场景覆盖、引用和字幕安全区。数据格式与组件见 [运镜计划](../../ppt-screencast/references/motion-plan.md)。结果只表示计划几何通过；语义、连续动作、声音和剪映动效仍需独立验证。

`screencast-build --content` 消费页面与句子ID、实测timing和配音，公共绑定器负责场景／强调时码与字幕分页；旧帧级author接口保留。`screencast-preview` 只渲染审核静帧和数据包；`screencast-deliver --review` 在当前快照完整预检及逐页审核通过后才渲染、混流和技术QC。未审、过期或未解决内容问题不能开始长渲染。三者参数由contract提供，预检／交付为后台job；技术ready仍不自动代表完整听审。

## 配音后处理与抽查

`align` 接收原句JSON和词级转写，输出实测句子timing；已知他/她同音转写差异有回归，不要求每个项目写debug_align。`tighten` 必须明确提供max-pause-s/keep-pause-s，PCM16 WAV走线性样本快速路径，其他媒体保留文件式FFmpeg过滤图兼容；处理后重新消费映射词表。PPT不默认套用钩子气口策略。

`sample` 复用源帧范围输入seek的公共短窗抽样器，不从长视频开头解码全部帧，不新写select/filter脚本。`qc --reference-audio` 可追加五个一秒解码窗口的旁白一致性核对；PPT交付器自动传入同一快照配音，不再写check_audio_identity。

## 通用时间轴：compile_timeline.py

```text
python scripts/video_production.py compile --selection <selection-plan.json> --transcript <transcript.json> --decisions <pause-decisions.json> --out-dir <work/edit>
```

- 参数：Agent依照实际内容写出的保留片段、词级转写、逐处语义气口决定；可选采样率。
- 输出：一行状态 JSON；无论视频时长、段数和内容如何都使用同一编译器。
- 产物：`edit-plan.json`、`mapped-words.json`、`pause-report.json`、`timeline-check.json`。
- 失败：词跨切口或时间基冲突时拒绝输出；Agent修改选择/气口决定，不另写时间轴修补脚本。

## 固定字幕格式：caption_pages.py

已审核设计的同源局部修剪，可用以下正式入口迁移，输出必须是新文件，不覆盖历史证据：

```text
vp pause-rebase --old-words <旧words.selected.json> --words <新words.selected.json> --old-decisions <旧已标注pause-decisions.json> --decisions <新pause-decisions.json> --out <work/pause-rebased.json>
vp caption-rebase --old-words <旧mapped-words.json> --words <新mapped-words.json> --draft <旧已审核字幕草稿.json> --out <work/caption-rebased.json>
```

源词ID和文字必须保持、顺序只允许删除；时码差异必须有直接同源修正谱系及证据。气口只复用左右源词与完整上下文相同的决定，返回pending需补充判断；字幕保留分页、校字及完整重点，报告变化页，不替代新revision的覆盖、时码、布局验证。

```text
python scripts/video_production.py captions --words <mapped-words.json> --editorial <caption-editorial.json> --out <work/captions/captions.json>
```

- 参数：编译器产生的 mapped words；Agent填写的 editorial JSON。每页提供有序 `word_keys`、1–2 行、`takeaway`，强调短语提供连续 word keys、`role` 与 `reason`。
- 输出：页数和警告 JSON。
- 产物：`captions.json` 与同名 `.srt`。
- 失败：漏词、重复词、未知 key、跨页保护短语或重叠页一律拒绝；不要绕过覆盖检查手写 SRT。

## 固定渲染器：render_timeline.py

```text
python scripts/video_production.py render --workspace-root <workspace-root> --source <raw.mp4> --plan <edit-plan.json> --captions-ass <captions.ass> --out <output/final.mp4> --encoder auto
```

- 参数：任意长度原片、已验证时间轴、可选 ASS、尺寸、编码器和质量参数。
- 输出：实际采用的 `libx264` 或 `h264_nvenc` 状态 JSON。
- 产物：`final.mp4`、旁边的 `render.log`。
- 行为：`auto`核对`video-production-deps/hardware.json`，实测NVENC/QSV/AMF/VideoToolbox并选择可用编码器；设备失败才回退CPU一次并记录。音频与视频分别拼接，保留采样时间轴与累计帧边界；过滤图通过文件传递，避开Windows命令长度上限。
- 输入先 seek 到最早保留源时刻之前的整秒，并限制到最后保留时刻之后；图内源时码减去同一偏移，最终时间轴不变。短样不再从长原片开头顺序解码；`render-result.json.input_window` 记录实际窗口。
- `--audio-only --out <review.wav>`只生成同时间轴48kHz PCM，不编码视频、不烧字幕，用于已知声音疑点的渲染前检查；`listening=not_checked`，需真实听审，最终MP4仍独立验收。

## 固定技术 QC：qc_delivery.py

```text
python scripts/video_production.py qc --workspace-root <workspace-root> --media <output/final.mp4> --plan <edit-plan.json> --out <qc/qc.json>
```

- 参数：成片、同 revision 时间轴及共享 FFmpeg 路径。
- 输出：`pass/fail` 状态 JSON和退出码。
- 产物：UTF-8 `qc.json`，包含元数据、全片解码、时长差、帧率、尺寸、帧数和连续PTS检查；人工内容和听审保持 `not_checked`。
- 逐帧时间戳读取显式启用自动解码线程（`ffprobe -threads 0`），仍检查每个解码帧，不改为抽样或包时间戳替代。
- Windows：所有子进程输出均显式使用 UTF-8 并替换不可解码字节；不要使用 Bash heredoc 或 PowerShell 默认带 BOM 的中间文件。
