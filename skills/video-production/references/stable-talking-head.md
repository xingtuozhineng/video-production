# 固定口播流水线

用于普通真人剪辑、分页字幕和重点短语。Agent 只编辑内容数据：选段范围、气口决定、字幕分组/强调/校字。时间计算、索引、词ID映射、字体、渲染、停止、QC和剪映导出由统一 CLI 完成。复杂滚动动效或额外音轨按对应参考执行，不在这条基础路线内临时搭 Remotion 工程。

使用 `video-production-deps/tools.json` 中的 Python 运行 `<skill-root>/scripts/video_production.py`。下面的 `vp` 表示这两个绝对路径组成的命令，不要求安装额外可执行文件。参数不清楚时才运行 `vp contract --command <子命令>`，已按本页给出的命令无需逐个重复查help/contract。保持PowerShell输出UTF-8，同一批独立查询可合并一次调用；程序返回的摘要及审核表优先于大型JSON全文。

普通口播的效率目标是完整基础字幕＋少量重点、选段先复核、字幕集中调整，减少因漏做前置检查而重复全片渲染。角色/疑词核对、必要的短窗复识别、校字和断行修正属于常规制作，投入随素材与转写质量而变，不以修改次数多就判为无效工作。临时脚本编写调试、重复读取及检查时机过晚造成的连带返工分别记录。环境准备独立完成；制作时环境门成功即继续，失败只按具体错误修复当前入口，不扫描宿主安装包或递归修改整个工作区权限。

## 默认批量入口：渲染前完成五阶段交接

新口播制作在`init/check`后使用`talking-batch`，下文原子命令仅供已有工程或具体异常定位。批量入口复用相同的角色校验、气口编译、字幕覆盖和交付器，不降低验收要求，也不调用额外的决策大模型。

```text
vp talking-batch --workspace-root <workspace> --project-dir <project> --stage prepare --execute --audio-review
vp talking-batch --workspace-root <workspace> --project-dir <project> --stage select
vp talking-batch --workspace-root <workspace> --project-dir <project> --stage compile
vp talking-batch --workspace-root <workspace> --project-dir <project> --stage review --execute --audio-review
vp talking-batch --workspace-root <workspace> --project-dir <project> --stage deliver --execute --audio-review
```

只有用户已要求音频听审时才加`--audio-review`，原片、同时间轴PCM预听和实际成片共用原有0.2元预算。`prepare`不带`--execute`为预检；prepare/review的执行和deliver默认后台运行，使用返回的job_id/job_dir运行一次`job-watch`并由宿主等待终态，接着读取`work/batch/state.json`。select/compile直接返回审核包路径。

|阶段|程序连续完成|Agent集中处理后再进入下一阶段|
|---|---|---|
|prepare|转写与原片听审可并行；均匀词时码检查/限量局部重识别、词句索引、逐句/逐词电平候选、近邻重说候选、分说话人开中末代表帧|通读`source-review.md`全部正文及音频观察、查看拼图；完成`work/index/recording-review.json`和`selection.json`。全文已带每个源词索引，不另查一遍完整词JSON|
|select|角色/用途校验、源规格探测、选段、所有气口候选及对应电平，保留段全文与前后被删词|通读`selection-review.md`，解决边缘与段内混声；在`work/pause-authoring.json`按下文格式批量标注所有气口，修选段时重跑本阶段|
|compile|应用气口、时间轴和词映射校验、同revision字幕分页、全部接缝与每页精确词索引|通读`caption-review.md`；一次写`work/caption-edits.json`，`review=reviewed`，其余为下文的稀疏校字/重点/异常页。没有修改也明确记录已审核|
|review|构建最终字幕；集中返回字幕错误、全部最终页和接缝；请求听审时生成真实PCM并检查连续声音与全部切口|通读渲染前审核包；先解决会改选段/气口/字幕的已知问题，再在返回的decisions.json记录当前输入审核及声音疑点依据|
|deliver|验证当前输入的前置审核；通过后连续渲染、技术QC、同步、剪映导出和可选成片听审|读取handoff、成片拼图和`work/batch/final-audio.json`；只处理实际编码、同步或新增异常，分别报告覆盖完成和未确认项|

每阶段审核包完整保留当前需要判断的正文、词索引和候选，不截断中间内容；原始JSON与全部中间产物留在原路径。模型无需重复抄写资料、手算词映射或再创建诊断脚本。需要额外源画面时，一次用`talking-batch --stage evidence --ranges 100:106,200:206`批量取得三时刻连续抽帧拼图（示例秒数须替换）；额外精确查询仍用`inspect`。电平和相似度只生成线索，不能自动决定身份、试词或删除。

阶段交接不是要求一条模型回复判断全片并一次写完所有JSON。长原片先确认角色，再按最多20个ASR句为一批连续处理当前正文；每批将spans/选段理由写回原文件，再处理下一批，不重复解释整片。完整审核包始终保留，批量限制只约束单次判断及输出，避免触及单次输出上限后丢失尚未落盘的决定；不另开上下文、不主动压缩、不新增决策模型。

选段审核包首先列出原片听审候选与保留区间的交叉项。ASR把领读和主角合成同一句时，整句speaker和平均音量不能放行：核对该候选的源片短窗，真领读/交流按原方式改spans及selection；确为正常正文时，仅为这个候选在角色表增加`audio_observations: [{"event_id":"审核包中的ID","decision":"retain","evidence":"实际回查区间、方法与依据"}]`。模型时码只是线索，不直接按该秒数剪字。原片听审不完整或保留的候选没有处理决定时，compile/deliver会拒绝继续。自动检查只保证候选被处理，不冒充判断本身一定正确。

同一ASR句的局部弱声也会以`level-...`候选列出，仅生成线索、不自动判成领读。保留的候选需按相同`audio_observations`格式回源核对；候选不在保留区间里时不重复审。用一次批量`audio-review --stage source --source-focus ...`对照原声和实际嘴型，或复用已完成同源证据。正文轻声和正常语气词可以保留，证据说明即可。

`prepare`发现连续较长一组词时长几乎完全相同、无真实间隔时，使用同一百炼空间的`fun-asr`只重识别受影响的最多3个短窗（每窗≤60秒），不是重转全片。保留原始结果、源偏移和新旧ID组映射；新窗口speaker用独立作用域，按实际原声/画面重新对应角色。仍不可靠的词标`UNRELIABLE_UNIFORM`：与选段无关的拍摄废片可排除，但不能靠一句“已审核”将不可靠字级时码用于切片。优先选同内容可靠的完整take；没有可用边界时停在具体问题，不均分字数或夹短字音。

已确认的决定继续写在原角色表、选段、气口及字幕编辑文件里。没有新证据时复用，输入变化只重做受影响阶段；程序拒绝在选段/气口修改后直接交付旧时间轴。阶段状态是进度索引，不取代这些完整决定。字幕草稿按revision保存在`work/caption-drafts/`，复跑同revision保留人工修改。

已有明确短窗证据仅需修正源词边界时，将`source_revision`及`corrections: [{word_id, old_range_s, new_range_s, evidence}]`写入`work/asr/source-boundary-edits.json`后重跑select；只能改已存在词的规范化源时码，不能跨相邻词、替换正文或修改原始供应商时码。证据需注明真实短窗和核实过的源偏移，不凭听审模型的估计秒数直接填值。原角色与局部混声决定仅在内容、ID及相关候选数据未变时继承，变化候选仍需审核。

同源删除少量未完成词或上述有证据的时码修正时，先保留旧mapped-words、字幕草稿和气口决定，用`pause-rebase`只迁移左右源词及上下文均未变的气口，处理返回的pending后重新compile；用`caption-rebase`按稳定源词ID迁移已审核的分页、校字和重点，只检查changed_pages并重新做全量覆盖/画面校验。新增take、重排或改文不走此捷径；部分删除重点短语须明确重审。音频改动先按音频参考做PCM短窗复核，已知问题尽量在长渲染前解决。

迁移后的字幕交给`talking-batch --stage review --caption-draft <迁移并审核后的草稿.json>`，通过审核后deliver沿用同一参数；不手改compiled-input状态文件。它仍经caption-build检查同revision、全文覆盖、时码和布局后才渲染。

### 单片上下文与实测口径

按用户选定的500K上下文完成一条视频，片内不主动调用压缩、不另开上下文丢弃操作历史；精简的是工具首次输出和不必要往返，不是正文与判断证据。Skill不修改宿主全局模型配置。定时任务在视频完成后另行安排下一条的上下文交接；若宿主自动触发压缩，记录实际事件，不能声称本次满足无压缩条件。

性能从制作prompt提交至最终答复计时，开发/环境安装另计；记录主模型轮次、工具调用、缓存/未缓存输入、输出、ASR和听审用量、实际渲染与返工。40分钟是需要同素材实测的目标，不通过省略内容/角色/全部切口听审或降低分辨率帧率来达到。

## 1. 转写与选段

主 Skill 已完成环境门、init和转写时复用结果。

本次要求自动音频听审时，按 [Qwen-Omni音频复核](audio-review.md) 用`audio-review --stage source`检查原片声音，在选段前处理领读/非正式发言候选；同一项目的原片与最终成片共用0.2元费用账本。

有脚本文档、多原片或需要查原句／词范围时，先用 [输入查询](input-inspection.md) 的 `vp inspect`。它保留原索引及分页标记，不另写文档提取、词稿dump或临时区间检查程序；多原片仍须有核验过的单源母版和转写映射。

```text
vp index --transcript <transcript.source.json> --out <work/index>
```

产物是保留说话人字段的`words.tsv`、`utterances.tsv`、`recording-review.json`和`selection.json`。选段前读取 [拍摄角色与发言用途](../../talking-head-cut/references/recording-roles.md)，依次确认主角/领读身份、正式内容/试词/拍摄交流，再比较同句各take。角色表的`content_review`完成一次总体用途核对说明，局部非正式发言用`spans.role=production`，不确定处保持`unknown`。Agent按索引填写`ranges`，每项只有`first/last/reason`；范围顺序就是成片顺序，不重写文案反查词ID。

```text
vp select --workspace-root <workspace> --source <raw> --transcript <transcript.source.json> --selection <work/index/selection.json> --review <work/index/recording-review.json> --out <work/edit>
```

程序探测源帧率、旋转和尺寸，生成 `selection-plan.json`、`words.selected.json`、`pause-decisions.json`。口播视频、短样、预览/审片画面、字幕画布及剪映画布统一竖屏（height > width）。先按旋转信息取得源片显示宽高：竖屏原片保留原尺寸，横屏原片交换长短边生成竖屏画布；方形原片以原边长为宽、按9:16补高并向上取偶数。4K 原片输出4K竖屏 MP4，50fps 原片输出50fps，分数帧率保留精确的 `num/den`，不按固定规格降分辨率或降帧。例如3840×2160的横屏原片，以及同尺寸带rotation=90°的竖屏原片，均默认输出2160×3840。横转竖默认等比完整放入画布、居中上下补边，不拉伸、不把人物横转90°；用户要求满屏或重新取景时才依据实际人物构图裁切，并检查脸、嘴、手势及重要内容。

仅当用户明确要求其他分辨率或帧率时，才传对应的 `--fps` 或成对的 `--width --height`，尺寸仍须为竖屏。只做原速单源剪辑；不能把多原片当成一个source。

`selection-plan.json.source` 记录编码宽高、旋转、显示宽高及源帧率；计划顶层的 `width/height/fps` 是实际输出规格，`output_orientation: "portrait"` 标记视频与工程的竖屏约束。`style.json` 和交付说明使用本次计算后的数值，并说明横转竖的补边/裁切方式；代理和预览可以缩小但画布仍为竖屏，正式渲染回源原片。字幕布局、ASS 画布和字号按输出画幅生成。`render` 未传尺寸时消费计划宽高，缺失时补齐计划，不回退到固定画幅。返工先核对旧计划的规格，横屏旧计划在重新交付前重建竖屏计划、字幕与渲染；仅调整Skill时不自动改写历史工程。原片、分析音频和草稿内完整源素材保持原样，JSON/SRT等文字产物没有画面方向。

### 进入字幕前：完成保留区间复核

角色表填完、`select`或时间轴校验通过，只代表已填写的数据一致，不代表实际声音中没有领读残留。字幕制作前完成以下复核，并把依据与待核项写回已有角色表、选段理由和QC记录，不另建审批流程：

- 执行 `vp inspect --kind selection --input <work/index/selection.json> --words <transcript.source.json> --limit 200`，核对每段全文、首尾词及相邻被删词，处理漏字、重拍残留及重复起头。
- 确认保留的是出镜者的正式内容，而不是同一人说的试词、自我纠正或拍摄交流。`content_review`未完成时不能select；证据不足的局部例外保持unknown，不能仅靠主角身份放行。
- 检查删前删后的句法是否闭合。“找到各种各样的……”等缺宾语、悬空修饰语或未完成谓语不能因为字音完整就写成“完整句”；结合前后take选择已说完的一遍。正常口语省略或引用不按关键词自动删，保留必须说明上下文为何完整。
- 核对保留区间内的角色混淆、同一ASR句中串人、段首尾保护余量及无词空隙。对已发现的声音/画面/文字证据不一致处，集中回查原片短窗；整句的speaker编号或平均音量正常，不能替代局部混声检查。漏识别的领读写入`excluded_audio`，识别成词的角色例外写入`spans`，修正后重跑select。
- 气口编译后核对最终保留源区间，复查本次改变的切口及已知风险处，确认没有重新带入已排除的声音，再锁定时间轴制作字幕。上述内容检查在首次正式全片渲染前完成，不留到渲染等待期间或交付收尾时首次执行。

已知领读残留、角色归属不明或会改变剪切范围的问题未解决时，先留在选段阶段。仅影响字幕显示、暂时无法确认的疑词按校字/听审规则记录，可交付标为`review_required`的审片版，不能猜测后写成已确认。角色关系及已核对区间复用，只补异常或发生变化的部分。

完成条件：保留范围的角色、内容、首尾与段内已知风险已核对，重拍取舍及不确定项有记录，实际规格与角色表写入同一计划。渲染后若出现新证据仍应修正；“减少重渲染”不是带着已知错误交付的理由。

## 2. 气口和时间轴

读取子 Skill 的语义气口参考，先 `vp inspect --kind pauses --input <work/edit/pause-decisions.json> --limit 200`，通读每处上下文，再写紧凑的 `pause-authoring.json`。同类语义依据的候选可分组，例外单独列组；每个序号恰好覆盖一次，不能因省请求忽略长空档或待核字音：

```json
{"revision":"本次selection revision","groups":[
  {"indexes":[1,3],"category":"within_sentence","reason":"同一句未完成的语法连接"},
  {"indexes":[2],"category":"sentence","reason":"完整判断结束，下一独立句"},
  {"indexes":[4],"category":"within_sentence","reason":"疑似未识别字母，保留待听审","preserve":true}
]}
```

```text
vp pause-author --decisions <work/edit/pause-decisions.json> --draft <work/pause-authoring.json> --out <work/edit/pause-decisions.json>
```

程序校验revision、候选覆盖、分类及目标值，批量填写原决定；无需写apply_pauses.py、映射词ID或手算帧数。

用 `vp inspect --kind pauses --input <pause-decisions.json>` 分页查看候选并批量编辑JSON。最终接缝查看使用 `--kind joins --input <edit-plan.json> --words <mapped-words.json>`，不拿编译前的instance ID反查最终词表。

```text
vp compile --selection <work/edit/selection-plan.json> --transcript <transcript.source.json> --decisions <work/edit/pause-decisions.json> --out-dir <work/compiled>
```

输出 `edit-plan.json`、`mapped-words.json`、`pause-report.json`、`timeline-check.json`。revision由选择、词稿、气口和采样率生成；修改任何这些输入会使旧字幕失效。语义不确定时保留原间隔并注明待听审。

完成条件：编译通过；切口与词映射同revision。

## 3. 字幕创作

```text
vp caption-draft --words <work/compiled/mapped-words.json> --plan <work/compiled/edit-plan.json> --out <work/caption-authoring.json>
```

程序生成完整单/双行分页草稿、词索引TSV和紧凑的 `.review.tsv`（页号、词范围、文本、短卡/长行标记）。先通读审核表，普通页保持自动分页和全白，只在 `caption-edits.json` 中写校字、少量完整重点和需要修正的页。无需逐页生成takeaway或重写全部JSON：

```json
{
  "revision": "复制生成的revision",
  "review": "not_checked",
  "corrections": [{"word": 8, "text": "AI", "reason": "依据原声修正识别"}],
  "highlights": [{"first": 9, "last": 12, "role": "focus", "reason": "核心判断短语"}],
  "pages": [{"page": 2, "lines": [[15, 22], [23, 30]]}]
}
```

`first/last/word/lines`为**成片词索引**，`page`为自动草稿的页号。只列需要替换的页；普通页由程序保留。所有页按序恰好覆盖全部词，重点须在同一页内；跨页时同时调整相邻两页的词范围。`role`为`focus`（白色加粗）或`structure`（主题色）；重点以关键结论、数字和行动为主，不追求每页都有。`corrections`仅修改显示，删声音回到选段。用户明确要求逐页精修时才直接编辑完整draft。

```text
vp caption-build --words <work/compiled/mapped-words.json> --plan <work/compiled/edit-plan.json> --draft <work/caption-authoring.json> --edits <work/caption-edits.json> --out <work/captions>
```

没有校字/重点/异常页时可省略`--edits`。产出JSON、SRT、ASS、字体和合并后的`authoring.resolved.json`。错误统一返回页码、重叠帧数及漏词/重词列表；集中修正再编译，不逐页反复调用。CLI会从输入所在工作区解析共享Python，避免宿主Python缺fontTools。固定样式为底部单/双行字幕；自动草稿未经通读，不等于内容验收通过。

## 4. 渲染前集中审核

新项目使用上面的review阶段；已有工程用以下入口，消费已构建的最终字幕：

```text
vp pre-render-review --workspace-root <workspace> --project-dir <project> --plan <work/compiled/edit-plan.json> --captions-dir <work/captions> --execute --audio-review
```

程序返回`work/pre-render/<输入指纹>/review.md`和`decisions.json`，此时不编码视频。审核包包括全部字幕页、短卡、接缝首尾词，以及请求过的真实PCM听审异常。确认字幕校字、词组断行和短卡；核对领读残留、非正式发言、吞字及换气截断。正常项复用已确认依据，只回查具体异常。

在返回的决定文件中将`status`设为`reviewed`，`checks.selection_boundaries`与`checks.captions`分别填`status=reviewed`和实际核对依据；请求了听审时，同样填写`checks.audio`。`pending_changes`列出待应用修改，有已知修改时先修原选段/气口/字幕数据，再重建和重审。声音异常逐项填写`audio_dispositions`：已核实采样边缘用`sampling_edge`，回源确认正常发音用`verified_source_delivery`，仍不确定用`uncertain_review_version`并保留成片待审；不能将未知写成pass。

字幕、字体、角色决定、选段或时间轴变化会使旧审核失效；只改字幕时复用同时间轴的PCM与听审报告。时间轴变化重新检查受影响声音，沿用同一账本。完整MP4的听审仍在渲染后执行，以检测实际编码结果。前置听审不替代最终解码、同步、音画检查和人工验收。

完成条件：当前输入指纹的审核已完成、已知修改全部应用、请求过的PCM听审覆盖完整且异常有处理依据。原子`render/deliver`在口播项目内也执行相同门禁，缺失/过期审核不启动视频编码。

## 5. 后台交付与恢复

```text
vp deliver --workspace-root <workspace> --plan <work/compiled/edit-plan.json> --captions-dir <work/captions> --out-dir <task/output>
```

默认快速返回`job_id/job_dir`。固定任务自动执行短样渲染、全片渲染、技术QC、同时间轴PCM同步检查、剪映工程导出，写入 `<output>/<输入指纹>/`。每个阶段保存检查点，同一输出目录一次只允许运行一个任务。输出文件先写临时位置，成功后才替换目标；输入、代码或配置变化使用新的指纹目录。

```text
vp job-status --job-dir <返回的job_dir>
vp job-stop --job-dir <返回的job_dir>
vp job-resume --job-dir <返回的job_dir>
```

宿主支持后台工具等待时，启动一次 `vp job-watch --job-dir <job_dir> --job-id <job_id> --timeout 3600`，用宿主的后台结果等待机制接收终态；例如DSH的 `job_output`，而不是模型不断调用 status／写sleep脚本。watch只在工具进程内读状态，不调用模型；超时保留原任务，不自动停止／重启，失败返回非零。收到终态后读取handoff，按未完成项继续；没有等待能力时保持原宿主调度查询方式。

停止会处理整个进程树。resume复用仍有效的阶段文件；阶段失败需按状态中的日志定位，不用旧日志猜任务是否成功。成功后读 `<output>/handoff.json`，取得成片、SRT、工程、QC、同步报告和竖屏抽帧拼图的路径，以及实测规格/时长/编码器摘要。查看拼图和报告中的异常后直接交付；无异常不重复probe、重跑相同QC或寻找旧项目。项目根部存在`production.json`时程序同步登记输出。人工改过的工程不会被自动覆盖；需要新版本时改变内容数据。

后台任务完成通知到达后再读取状态；没有通知能力的宿主由宿主调度查询。状态未变时保持安静，模型无需执行数分钟sleep或为了缓存不断发请求。

本次要求自动音频听审时，再用`audio-review --stage final`消费handoff中的实际MP4和快照计划，核对连续声音与全部内部切口；共享原片听审费用预算。根据音频模型报告回查异常，分别报告实际覆盖、用量折算费用和耗时；它不覆盖人工听审状态。

## GPU与缓存成本

安装的`prepare`会在`video-production-deps/hardware.json`记录GPU/驱动和实际编码测试。每次`auto`渲染比对硬件、驱动、FFmpeg和检测版本；未变化且检测未超过24小时才复用。支持NVENC/QSV/AMF和macOS VideoToolbox，硬件不可用才选libx264；实际设备/编码器失败只回退CPU一次并记录原因。显式指定编码器失败会报告错误。GPU编码不等于滤镜和解码全部由GPU执行。

长任务的状态在文件中，不依赖模型会话缓存。`handoff.json`与完整阶段产物支持异常中断后恢复，但不是要求片内主动压缩。异步返回和简短状态不会自动删掉DSH已有对话，也不保证服务端缓存TTL；不为保活发送无业务作用的模型请求。

完成条件：技术检查通过且双产物存在；ffprobe 核验最终 MP4 的实际显示画幅为竖屏、尺寸与计划一致，默认规格下长短边与源片一致（方形按上述补高规则）、帧率与源帧率一致；短样/预览及剪映草稿画布也须为竖屏。用户明确要求转换时按指定竖屏规格核验并说明转换，不能只核对文件名或计划。听审和剪映实际打开/编辑保持独立状态。未经这些人工检查，整体为`review_required`。制作时遇到工具缺陷，返回失败阶段、错误摘要和可恢复路径；公共脚本修改由明确的工具维护任务处理。
