# 导出剪映可编辑工程（Windows）

完整视频制作时读取本文件。主 Skill 和两个子 Skill 共用此流程，默认同时交付 MP4 和剪映可编辑草稿。

## 双产物交付流程

在 `production.json` 写入 `export_format: "both"`。制作开始时读取本节支持范围，并消费主 Skill 环境门给出的导出依赖状态；最终时间轴确定后，从同一个 revision 分别渲染 `final.mp4` 和生成 `剪映工程/`。已有且通过检查的同版本产物可复用。导出失败时修复或报告阻塞，不自动降为只交 MP4。

交付目录至少包含 MP4 和完整草稿包；`production.json.outputs` 记录 `mp4`、`jianying` 两个路径及共同的 `revision`。依次完成下文的生成、素材校验和应用验收，保留样式差异说明。仅转写、单条素材生成及明确的局部修改按原任务范围执行。

剪映工程保留独立片段和原素材范围，字幕是文本片段，BGM 是独立音轨。完整原素材随包复制，便于拖动片段边缘恢复被删除的气口；不要先把整条成片压成单段 MP4 再冒充可编辑剪辑工程。

## 支持范围

| 内容 | 当前导出行为 |
| --- | --- |
| 真人原片 | 每个保留段独立、原速、保留同期声与完整源文件 |
| 字幕 | 可改文字、分页、起止时间；基础样式可编辑 |
| BGM、配音、音效 | 独立音轨，可设原素材起点、音量、淡入淡出 |
| 既有图片、视频素材 | 独立视频轨，位置和缩放可编辑 |
| 标题、钩子文案 | 独立文本轨，基础字号、颜色、加粗、位置 |
| Remotion 动效、重点短语样式、自定义字体、调色 | 未自动转换；在报告中说明差异，保留原工程及计划，按需求在剪映重做 |

当前导出原速时间轴；MP4和规范计划保留源片精确帧率。剪映库的编辑网格只接受整数：分数帧率草稿使用最近整数网格，音视频片段和字幕时码仍按原分数帧率换算成微秒，不变速、不重写规范计划；报告明确两种帧率及应用内待验边界。变速、交叉淡化/J/L cut 不静默近似。采样级气口写入微秒时码；剪映打开后的帧量化、接句自然度及音画同步仍需实际检查。移动切口后需要检查字幕和其他轨道是否跟随，不能假定应用会自动联动所有轨道。

## 导出依赖

完整视频流程需要此导出器；仅转写或单素材生成无需安装。导出器使用 Python 3.10+（当前验证为 3.12）。在主 Skill 目录，用当前项目的 Python 环境安装：

```shell
python -m pip install -r scripts/requirements-jianying.txt
```

使用 [pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft/tree/c3318066d964744e2bfc66f75c71745fe8cea52a)，Apache-2.0，固定提交 `c3318066d964744e2bfc66f75c71745fe8cea52a`（0.3.0）。库负责草稿结构和素材解析，本 Skill 负责已有时间轴转换、素材打包、重定位和验证。没有调用它的 UI 自动导出或模板解密功能。

这是社区草稿格式适配，不是剪映官方导入 API。上游声称已测部分 10.8 功能，不代表所有新版都兼容；必须记录目标 Windows 剪映的实际版本。生成新草稿和读取剪映保存后的加密草稿是不同能力，本流程只承诺前者的生成实现。

## 真人口播输入

口播草稿画布与同revision的MP4统一竖屏，消费计划的 `width/height/output_orientation`；横屏原片保持原样作为可编辑素材，在竖屏画布内等比放置，不通过旋转人物或裁掉内容来冒充横转竖。尺寸计算与构图规则见 [固定口播流水线](stable-talking-head.md)。

使用当前 `edit-plan.json`：`revision`、`source.path/duration_s/fps`、`segments`、成片 `fps`、`duration_frames`，并从本次输出规格补入 `width`、`height`。源帧字段按 `source.fps`，成片帧字段按成片 `fps`；二者不同是正常情况。各段保留源 `source_in_s/out_s` 和最终 `final_in_s/out_s`。不为导出重新剪辑或另造一条时间轴。

字幕输入是 `caption_pages.py` 产出的 JSON（`revision`、`fps`、`captions[].start_frame/end_frame/lines`），也支持 `rolling_captions.py` 的 `lines[]` 结构：按每行说话起止生成独立文本，滚动动画和前句叠行不自动转换。不是把已有烧录字幕视频当字幕层。没有字幕时省略参数；有字幕但没有可编辑文字/时码时先补齐。旧计划中的 `source.source_path` 与 `source.path` 均可读取。

可选 `jianying-layers.json`，与编辑计划使用同一个 revision。媒体相对路径均相对于 `edit-plan.json` 所在目录；也可使用绝对路径。示例：

```json
{
  "revision": "cut-001",
  "clips": [
    {"kind":"audio","track":"BGM","path":"music.wav","source_in_s":0,
     "start_s":0,"end_s":7,"volume":0.15,"fade_in_s":0.3,"fade_out_s":0.5},
    {"kind":"text","track":"顶部标题","text":"一个清晰的判断",
     "start_s":0,"end_s":7,"size":10,"bold":true,"color":[1,1,1],"y":0.75}
  ]
}
```

秒数是最终时间轴，`end_s` 是结束时间。音量为线性倍率，`1` 为原始音量；x/y 与 scale 使用库的剪映归一化坐标，y 正值向上。字号是库的剪映单位，不等于 CSS 像素。同一命名轨道不能重叠，需要重叠时使用不同轨道名。文本层总在图片/视频层上方。素材时长不足时报错，不自动循环、变速或冻结。

```shell
python scripts/export_jianying.py --plan /path/to/edit-plan.json --captions /path/to/captions.final.json --layers /path/to/jianying-layers.json --out /path/to/delivery/剪映工程
```

输出目录必须尚不存在，避免覆盖人工编辑。输出包含 `draft_content.json`、`draft_meta_info.json`、`assets/`、`package.json`、原计划和 `export-report.json`。完整素材可能较大，复制前检查磁盘余量；使用者负责原片、音乐等素材的分享范围。

## 钩子视频输入

从同一 `layout-plan.json` 映射最终帧边界，构建用于导出的计划：`revision`、`width`、`height`、`fps`、`duration_frames`、`segments: []`。没有真人原片时不需要 `source`。

把配音/BGM 放进独立 audio 轨，背景/图片/视频放进 video 轨，逐层文案和标题放进 text 轨；字幕使用同 revision 的 captions 文件。至少保留一条可见画面/文本轨。复杂打字机、chip、路径动画目前需在剪映重做；如要求保留原视觉，同时交付原 Remotion 工程和 MP4，不将渲染好的整片作为唯一可编辑内容。

## 安装草稿或跨机器搬运

Windows 默认读取剪映 `Config/globalSetting` 的 `currentCustomDraftPath`，优先采用当前自定义草稿位置；未设置时才使用默认目录。不能因为 C 盘默认目录存在就认为剪映正在使用它。设置的路径不存在时明确报错，不悄悄回退。生成的包搬到其他位置后需重写素材路径；直接复制 JSON 或仅双击它不等于导入。

```shell
python scripts/export_jianying.py --install /path/to/delivery/剪映工程
```

若自动读取不可用，用户在剪映“设置 → 草稿位置”确认目录，再用 `--draft-root "实际草稿目录"` 显式指定。路径含空格时用引号包裹整个参数。命令把完整包复制成草稿根目录下的新文件夹，校验素材 SHA256、重写路径和工程 ID；不覆盖同名草稿，不修改已有工程及根目录索引。完全退出再启动剪映刷新列表；文件安装成功不代表首页已收录，需实际查看。若仍未出现，检查当前设置及首页索引，不能不断重装或覆盖用户草稿。如果安装包文件夹名称已有同名草稿，先给包换一个新名称再安装。

## 验收和报告

脚本输出的 `generated_not_app_verified` 仅表示草稿生成完成。依次记录：

1. 结构：轨道数量、切段源区间与最终时码、完整素材、路径和文本内容可回读。
2. 剪映打开：实际版本、草稿可见、无丢失素材，画面和音频正常。
3. 人工编辑：对已有轨道至少拖动一个切口恢复原片范围、改一处文字、调 BGM 音量，保存后重开仍保留。未使用对应素材的检查记录 `not_applicable` 及原因；不为检查额外添加内容。
4. 视听：切口、字幕同步、各轨道联动与声音混合；同时完成 MP4 的常规成片 QC，确认两项产物对应同一时间轴版本。

未执行的项保留 `not_checked`。保存后的草稿可能加密，不为证明可读而覆盖它；用应用里的回读和截图记录实际编辑结果。

2026-09-17 验证：导出器测试覆盖轨道、素材重定位和当前自定义草稿目录读取；用户截图确认 Windows 剪映 11.4.2.14459 能打开结构测试草稿并显示独立视频切段、BGM、字幕和标题。测试彩条只是夹具，不是实际成片。真实口播工程另行生成，实际显示、修改后保存重开与完整视听尚待用户确认；不据此宣称全部功能已验收。

开发测试：`python -m unittest discover -s scripts -p test_jianying.py -v`。未安装导出依赖会跳过真实草稿写入测试；跳过不能算已验证写入。测试不等于剪映真实打开验收。
