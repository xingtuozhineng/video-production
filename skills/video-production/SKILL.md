---
name: video-production
description: 视频制作与视频/录音转字幕入口。按素材和目标路由到转写、真人口播剪辑、钩子、PPT 运镜讲解或分镜制作流程，并提供公共配音、授权声音克隆和字幕字体资源。
---

# 视频制作主 Skill

识别用户目标后，先建立规范项目目录并通过环境门，再选择一个主流程；保持用户的素材、风格、时长和授权，不把所有视频都当真人口播。空镜的生成、内容规划、插入与时长节奏由后续剪辑师处理，不属于本 Skill。

## 工作区与项目目录

开始任何新任务都先读取 [workspace-layout.md](references/workspace-layout.md) 和 [正式脚本契约](references/script-contracts.md)，所有正式动作通过 `scripts/video_production.py` 统一 CLI 执行，并用其 `init` 子命令建立项目目录。工作区根目录允许用户直接放原始视频或录音；不要为了整理而移动或复制数 GB 原片。项目的 `input/source-manifest.json` 记录这些源文件的绝对路径、大小和修改时间。

项目先按 route 分类到 `projects/<route>/`；每个项目固定使用 `input/`、`brief/`、`work/`、`project/`、`output/`、`qc/`。转写、计划、时间轴和下载素材进入 `work/`，Remotion 与剪映可编辑工程进入 `project/`，最终 MP4/SRT 进入 `output/`，检查结果和审片帧进入 `qc/`。临时探测统一放 `<workspace-root>/scratch/`，不得在工作区根目录新建 `_probe`、`v2` 等临时项目。

工作区只保留一套共享依赖 `<workspace-root>/video-production-deps/`：Python venv、FFmpeg/ffprobe、Node/Remotion 依赖及 `tools.json` 分目录存放，由所有项目复用；项目目录不复制依赖。Skill 源码仓库、共享依赖、项目和原始素材彼此分离。

## 运行前唯一环境门

凡是要执行脚本、CLI、云 API 或渲染的任务，先在工作区根目录建立共享依赖目录，并运行一次。`<workspace-root>` 指包含原始素材、`projects/` 和共享依赖的工作区，不是 Skill 安装目录，也不是单个任务目录：

```text
python "<skill-root>/scripts/video_production.py" check --workspace-root "<workspace-root>"
```

`check_env.py` 是本 Skill 家族的唯一依赖入口。工作区模式会创建并使用 `<workspace-root>/video-production-deps/ffmpeg/bin/ffmpeg(.exe)`、`ffprobe(.exe)`，并把共享路径写入 `<workspace-root>/video-production-deps/tools.json`；它不会用机器其他位置的 FFmpeg 把检查变绿。后续脚本消费这份共享报告，并显式传入其中的 `ffmpeg`/`ffprobe` 路径；脚本没有路径参数时，为该次命令设置 `VIDEO_PRODUCTION_PROJECT_DIR=<workspace-root>`。

任务开始时只检查，不安装。若 JSON 报告的 `auto_fixable` 有内容，停止制作并回到 Skill 下载后的准备阶段执行：

```text
python "<skill-root>/scripts/video_production.py" prepare --workspace-root "<workspace-root>"
```

安装完成前不进入对应制作路线；仍未 ready 时，只报告该路线的 `routes`、`problems` 和缺项。项目模式下不要使用 `--search`，也不要自行写 `Get-ChildItem -Recurse`、`find` 或 `where /R` 搜索整盘；项目依赖目录就是 FFmpeg 的唯一来源。

环境门只解决运行环境；`.env` 的业务空间 Key 和地址仍需人工填写。

需要新旁白时读取 [公共 TTS 执行器](references/tts.md)，只使用 `video_production.py tts`；它接收本次文本／句子JSON，自动分句、分段合成、拼接和缓存恢复。普通配音默认保持 `qwen3-tts-flash / Cherry`，接口失败不自动换模型、换音色或另写兜底执行器；默认配置已有可用音色时直接使用，不为每条视频写 TTS 脚本、查旧项目调用代码或重新询问全部参数。

用户明确要求声音克隆时读取 [公共声音克隆](references/voice-cloning.md)，使用 `voice-create → tts --voice-record`；已有音色直接复用记录。仅创建／查询音色不新建视频项目，记录保存到工作区的 `voice-library/`。其他子 Skill 共用这一入口，不各自实现上传、克隆或配音脚本。

## 决策树

```text
用户要制作视频或把音视频转成字幕
├─ 只要现有视频或录音的文字稿/SRT？ → references/asr.md 的字幕流程
├─ 核心是讲解 PPT／长页，用镜头聚焦与鼠标圈画强调（即使有营销钩子或配音）？ → ppt-screencast 子 Skill
├─ 有原片，核心是保留真人原声表达？
│  ├─ 是：真人讲述/知识分享/口播 → talking-head-cut 子 Skill
│  └─ 否：现有素材混剪/其他剪辑 → route=existing-edit；只复用当前工程已有时间轴，没有工程时先确认剪辑范围
└─ 没有真人原片，只有文案/脚本/主题？
   ├─ 目标是钩子/推广，接受纯排版动画（动态 PPT） → hook-video 子 Skill
   └─ 其他分镜视频 → 本 Skill 的 references/storyboard.md
```

一句“生成一段口播”可能指剪已有真人录像或从文案造数字人口播；先检查附件与原片，只有这个区别影响后续时才简短澄清。声音克隆须有明确授权的参考录音及调用范围，不自动从风格参考取声；不承诺口型克隆或数字人能力。仅改字幕/换某镜头时沿用既有工程做局部修正。

## 路由调用约定

真人口播读 [talking-head-cut/SKILL.md](../talking-head-cut/SKILL.md)。这是子 Skill，直接在当前任务执行，不自动新建任务或派生 Agent。用户日常只需提供 raw＋风格；主 Skill 负责识别，无需用户记住子 Skill 名称。直接调用子 Skill 也保持可用。

钩子视频读 [hook-video/SKILL.md](../hook-video/SKILL.md)，同为子 Skill，同一执行约定。用户只需提供宣传方向（加可选受众/CTA/时长）；路由到钩子视频后执行子流程的"钩子脚本 → 配音与时间戳 → 排版计划 → Remotion 制作 → 验收交付"。零实拍：画面由排版动画与已有配图构成，不伪造真人镜头或数据截图。

PPT 运镜讲解读 [ppt-screencast/SKILL.md](../ppt-screencast/SKILL.md)，同一执行约定。以“信息地图 → 讲解时间轴 → 目标绑定运镜与鼠标圈画 → 验收交付”为主；视觉形式优先于“营销”或“有录音”这些标签，不压成默认 30 秒钩子，也不把配音当真人原片。只有要求匀速长页滚动或逐屏大字时仍按实际形态选择原流程。

路由到真人口播后，执行子流程的“转写 → 角色与发言用途核对 → 主角重拍取舍与保留区间复核 → 气口与时间轴 → 基础字幕＋少量重点 → 渲染前集中审核 → 渲染/QC”。选段前按 [角色与发言用途规则](../talking-head-cut/references/recording-roles.md) 区分领读、正式正文、试词与拍摄交流，完成同一角色表的`content_review`及必要例外后传给`select --review`。确认保留内容后再处理气口；字幕/字体的局部修改复用已有转写与剪辑计划。

写入任务目录根部的 `production.json`：`route`（transcription/talking-head/hook-video/ppt-screencast/storyboard/asset/existing-edit；Skill 验证使用 validation）、`route_reason`、`inputs`、`style`、`project_dir`、`output_dir`、`constraints`、`environment.tools_json`。路径必须落在规范分层中；这是路由记录，不是给用户多加表单。仅转字幕时交付文字稿、词级 JSON、SRT 和说话人摘要；视频制作返回工程、文件和真实质检。

## 主干道：默认按这一条走

1. **建项目**：确认 route 和简短项目名，执行统一 CLI 的 `init --workspace-root ... --route ... --name ... --source ...`；复用已有任务时读取其 `production.json`，不另建 `-v2` 目录。
2. **环境门**：运行统一 CLI 的 `check --workspace-root`，消费共享 `tools.json`；按所选路线判断缺项，安装由准备阶段的 `prepare` 完成。
3. **路由**：确认附件、目标和输出范围，只选择一个 `route`，完善 `production.json`；不为了寻找“更好的工具”新增路线。
4. **建立唯一时间轴**：
   - 仅转写：探测媒体 → 转写 → 词级 JSON/可读稿/SRT → 文字质检。
   - 真人口播：默认读取 [固定口播流水线](references/stable-talking-head.md)，用`talking-batch`的`prepare → select → compile → review → deliver`阶段交接。Agent通读完整审核包，集中编辑角色/选段、气口和字幕数据；review在整片编码前集中检查，程序完成衔接、后台渲染、QC、导出及用户要求的听审。原子命令用于局部修改或异常查询，也通过同一前置审核门。
   - 钩子/分镜：脚本或分镜 → 时间轴计划 → 准备已有素材 → 渲染 → 文件与画面 QC。
   - PPT 运镜：信息地图 → 讲解时间轴 → screencast-plan → screencast-check → 运镜／鼠标渲染 → 连续动作与内容 QC。
5. **交付**：所有输出消费同一 revision；PPT运镜默认只交MP4，其他完整视频按双产物合同生成剪映工程；分别报告文件、技术、内容和实际视听状态。

主干中的每一步都以文件产物作为下一步输入。时间轴、字幕、渲染和 QC 使用 [正式脚本契约](references/script-contracts.md) 的入口。内容输入错误按批量诊断修正；公共工具缺陷报告失败阶段与可恢复路径，由明确的维护任务处理。制作任务保留同一时间轴和既有检查点。

## Agent 执行约束

- 环境门只跑一次；主 Skill 将同一份结果传给子 Skill。子 Skill 不重复探测 PATH、缓存、浏览器、字体或模型，也不重新寻找 FFmpeg。
- 参数不清楚时用统一CLI的`contract --command <当前子命令>`或`--help`查询；本次已确认的命令与固定流水线示例直接复用。优先查看当前JSON/TSV摘要，只有错误诊断不足以解释阻塞时才按符号或行范围读取源码。
- 大型转写只生成一次紧凑索引（如 utterance/word TSV），后续内容选择、气口和字幕都复用索引；不要在每个阶段重新读取完整转写或重新写一套 dump 脚本。
- Word脚本、词范围、气口和最终接缝用统一 CLI `inspect` 查询；有脚本文档、多原片或需要定位上下文时读 [输入查询](references/input-inspection.md)。返回的分页／截断标记必须处理，完整语义不能从截断摘要推断。
- 小范围画面验证使用 `scripts/sample_frames.py --ranges START:END,...`；它按每个时间窗口 seek 后再 concat。不要用全片 `select` 只取少数帧，否则仍会顺序解码整个 HEVC 文件。
- 每个任务只保留一条 canonical 时间轴和一份 revision；脚本输出是下一步的输入，失败时修复该输入或记录阻塞，不通过旁路工程重新建一条时间轴。

## 完整视频的最低交付

PPT运镜讲解是MP4-only例外：`export_format: "mp4"`，由 `screencast-build → screencast-deliver` 完成，默认不生成任何剪映草稿或工程交付；其完成条件见子Skill。其他完整视频制作（真人口播、钩子视频、分镜或整片剪辑）保持 `final.mp4`＋`剪映工程/`，记录 `export_format: "both"`，进入制作时读取 [jianying-export.md](references/jianying-export.md)。仅转写、单素材和局部修改沿用任务范围。

以下双产物检查用于需要剪映的路线；PPT运镜不执行这些工程检查。最终交付前执行：

1. 锁定同一时间轴 revision，渲染 MP4，并将切段、字幕及实际使用的 BGM/配音导出到剪映草稿；原渲染工程可额外交付。
2. 验证 MP4 可解码、规格与时长正确；草稿附完整素材，原片切段可恢复气口，字幕是可改文字，已用 BGM 为独立音轨。无 BGM 的片子无需为了验收添加音乐，在报告记录未使用。
3. 在 `production.json.outputs` 记录 `mp4`、`jianying` 两个实际路径及共同的 `revision`，在 `qc.json` 分别记录文件检查、剪映打开、编辑保存和视听结果。任一产物缺失时继续处理或明确阻塞，不能宣布完整交付；应用内未验收保留 `review_required`。
4. 回复同时给出 MP4 和草稿目录的可访问位置、草稿安装方法及已知样式差异。剪映内后续修改需重新导出 MP4，旧成片不会自动更新。

## 公共能力

- 用户要求音频听审或复核领读/试词/切口声音时，使用 [Qwen-Omni音频复核](references/audio-review.md)：原片在选段前检查，最终字幕构建后预听同时间轴PCM，实际成片在渲染后复核，同一视频共用0.2元预算。程序管理调用、用量与覆盖率，模型意见不自动替代角色确认或人工听审。

- 环境状态统一由上面的 [scripts/check_env.py](scripts/check_env.py) 提供。需要云 API 时才加 `--network`；需要完整字体哈希时才加 `--deep`。项目模式下使用 `video-production-deps/tools.json` 的路径；FFmpeg 脚本调用必须显式传入项目内路径。
- 视频/录音需要字幕或文字稿时读取 [asr.md](references/asr.md)。`.env` 已配置时，在用户要求的转写/剪辑范围内用百炼脚本执行；支持 WAV/MP3 上传、词级时码和说话人分离。含画外提示时先确定主角角色与保留词，再剪媒体和重建字幕。ffmpeg 不在 PATH 时按检查脚本解析出的路径传 `--ffmpeg`。
- 需要字幕或图形文字时读取 [fonts.md](references/fonts.md)。字体与许可证在 `assets/fonts/`，按视觉定调选择真实文件及字重，随工程带上用到的字体许可。
- 实际 API 调用、字体渲染、音画同步和语义质量分别记录证据。没有 Key 时先完成本地可验证工作；不能把模拟服务测试写成真实模型通过。

## 扩展边界

共享安装时将本 Skill 与 `talking-head-cut`、`hook-video`、`ppt-screencast` 放在同一级技能目录，按宿主的安装规则复制或建立目录链接。其他 harness 需要将共享目录纳入技能发现范围；可读取文件不等于会自动发现 Skill。没有自动发现能力时，显式读取本 Skill 的 `SKILL.md`，继续按相对路径读取子流程。

迁移到其他 Agent 时各 Skill 目录一并复制，保留相邻路径。用 `.env.example` 创建本机 `.env` 并填写自己的业务空间 Key 和地址；分享包排除真实 `.env` 和工作区 `video-production-deps/` 中的二进制。复制后以 `check_env.py --project-dir <workspace-root> --json --write-tools` 作为唯一验收入口。更新前备份本机 `.env` 到 Skill 目录之外，更新后重新检查环境。宿主仍需有文件/命令/网络执行能力和推理模型；本 Skill 的 FFmpeg/ffprobe 由工作区共享依赖目录提供。

目前的子 Skill 是真人口播、钩子视频与 PPT 运镜讲解；最后一种提供独立流程、计划检查及运镜组件，不代表全片或剪映动效已验收。新增类型只有在有明确独立流程及验证用例时才增加子 Skill 和路由分支；不要生成空目录或声称已经支持电影混剪、数字人等尚未实现的能力。路由参考案例见 [routing-cases.md](references/routing-cases.md)。
