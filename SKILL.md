---
name: create-tts-whiteboard-video
description: 将中文文案或 SRT 制作成带本地中文配音、同步字幕和流畅素描动画的白板视频。以每条 TTS WAV 的真实时长作为唯一主时间轴，自动反推字幕区间、场景时长和分区绘制时长，避免固定镜头时长造成配音结束后画面仍停顿或素描卡住。用户要求“文案生成白板动画”“给白板视频加中文配音和字幕”“配音驱动画面”“修复白板动画卡顿/不同步”“直接生成白板成片”时使用。
---

# 配音驱动白板视频

## 核心原则

始终先生成 TTS，再让真实音频时长驱动字幕、场景和素描。不要先做固定 10 秒字幕槽、固定 30 秒分镜或固定总时长后再挤压配音。

锁定以下成片参数，除非用户明确要求改变：

- 中文 TTS：MeloTTS，CPU，`speed=1.08`，44.1 kHz 单声道 PCM16 中间文件。
- 时间轴：开场 150ms、同幕口播间隔 300ms、普通幕尾 350ms、最终幕尾 500ms。
- 绘制：比口播早 50ms 起笔，比口播早 180ms 完成，同一口播内多元素间隔 60ms。
- 动画：60fps 内部渲染、`grid-edge=5`、`grid + contour-wipe + smooth + pause off + bare-tip`。
- 成片：1920×1080、30fps CFR、H.264 CRF 18、AAC 192 kbps、字幕烧录。
- 阈值：计划绘制停顿不超过 800ms，连续口播间隔不超过 550ms。

需要字段格式时读取 [annotation-schema.md](references/annotation-schema.md)；需要验收与排障时读取 [quality-gates.md](references/quality-gates.md)。

## 输入与目录

至少取得中文文案或 SRT。确认输出比例；未指定时沿用项目平台目标，当前工作流默认 16:9 横版。

在项目内使用下列位置：

- `文案.md` 或单独的 `口播分句.txt`：一条口播一行。
- `素材/白板动画/`：原始场景图和 `.annotation.json`。
- `素材/白板动画/配音连续版/`：按真实音轨改写后的场景素材。
- `音频/`：TTS 分段缓存和最终旁白 WAV。
- `工程文件/`：逐幕视频、合并中间视频和校验报告。
- `成品/`：最终 MP4 与同名 SRT。

保留已有版本，使用明确的新版本后缀，不覆盖用户确认过的成片。

隐私约束：只处理用户明确提供的本地文件，不读取浏览器 Cookie、账号凭据或无关目录，不上传文案、图片、音频和视频。`scripts/prepare_env.py` 仅为安装渲染依赖而访问 Python 包索引。

## 工作流

### 1. 整理口播分句

把文案整理为一条口播一行的 UTF-8 文本，或使用已有 SRT。每条通常承载一个完整意思，目标约 5–10 秒；不要为了凑固定数量而切断语义。

保留原意和事实，不擅自补充未核实信息。字幕文本必须与实际送入 TTS 的文本逐字一致。

### 2. 先生成并测量 TTS

用已经安装 MeloTTS 的 Python 运行：

```powershell
& <MeloTTS环境的python.exe> scripts/generate_tts_segments.py `
  --cues <口播分句.txt或字幕.srt> `
  --segments-dir <音频分段目录> `
  --speed 1.08
```

脚本按文本、语言、语速、采样率和说话人哈希复用缓存。文案改变后只重做对应分段；不要用旧音频冒充新文本。

根据 `tts-segments.json` 的真实时长组合场景。每幕通常累积约 20–30 秒，但这只是构图建议，不是时间槽；一幕可含任意数量的连续口播。

### 3. 设计并生成线稿场景

每幕只表达一个核心结论。先写出：对应口播、叙事事件、主体、动作/因果、建议元素顺序。

生成统一风格图片：16:9、暖米黄纸张底、黑色手绘线稿、少量黄色重点色、构图横向展开、无大段文字、无水印。生成后实际查看图片，确认主体完整、留白合理、没有乱码或多余标识。

默认采用分阶段确认：分镜策略确认 → 线稿确认 → 标注确认 → 成片。用户明确说“跳过确认”“测试直接成片”时可连续执行，但仍必须自行查看图片并运行所有校验。

### 4. 创建语义标注

为每张场景图建立同名 `<scene-name>.annotation.json`。按字幕事件顺序拆分可见主体，不按纯粹的从左到右机械排序。

关键要求：

- `canvas` 必须等于原图像素尺寸。
- `sequence` 从 1 连续递增。
- 每个 `element.subtitle` 必须完整复制一条口播；同一口播可以对应多个元素。
- 同一场景中，各个不同的 `subtitle` 按出现顺序必须与连续口播逐字匹配。
- 初始 `reveal.startMs` 和 `durationMs` 只承担相对顺序与分配权重，最终值由音轨脚本覆盖。
- 重叠主体使用 `protectedRegions`，避免前一个区域提前画出后续主体。

生成静态标注预览：

```powershell
python scripts/render_annotation_preview.py <场景图> <标注.json> <预览图.png>
```

查看预览并修正漏框、越界、错误顺序和大面积重叠。

### 5. 用音轨重建真实时间轴

```powershell
python scripts/build_natural_timeline.py `
  --cues <口播分句.txt或字幕.srt> `
  --source-assets <原始白板素材目录> `
  --segments-dir <TTS分段目录> `
  --output-assets <配音连续版素材目录> `
  --output-audio <最终旁白.wav> `
  --output-srt <最终字幕.srt>
```

必须看到脚本成功输出 `TOTAL_DURATION_MS`、`MAX_DRAW_STILL_MS` 和 `MAX_VOICE_GAP_MS`。不要手工把生成后的字幕重新拉成固定长度。

### 6. 渲染成片

```powershell
python scripts/render_project.py `
  --timed-assets <配音连续版素材目录> `
  --audio <最终旁白.wav> `
  --srt <最终字幕.srt> `
  --work-dir <工程文件/白板渲染目录> `
  --output <成品/16-9_横版_配音驱动白板动画.mp4>
```

脚本为每幕计算图片与标注哈希；输入未变时复用逐幕缓存。使用 `--force` 才强制重绘所有场景。

### 7. 验收

```powershell
python scripts/validate_output.py `
  --video <成品.mp4> `
  --audio <最终旁白.wav> `
  --srt <最终字幕.srt> `
  --timing-report <配音连续版素材目录/natural-audio-timeline-report.json> `
  --report <工程文件/白板渲染目录/final-validation.json>
```

只有 `status=PASS` 才交付。另抽查开头 0–15 秒、每个转场前后 2 秒和用户曾指出的卡顿位置。确认语音开始时画面已经开始落笔，语音结束前当前内容基本画完，下一条口播与下一段绘制共同启动。

## 时长策略

自然成片时长等于真实 TTS、必要短间隔和幕尾之和，不承诺等于文案里预先写的目标数字。

用户要求精确 3:00 时：

- 若自然成片接近 3:00，可小幅调整口播语速或句间空隙，并重新生成时间轴。
- 若自然成片明显不足，增加有信息量的口播和对应视觉元素。
- 禁止用静止画面、无声空白、重复镜头或把单幕强拉几十秒来凑时长。

## 失败处理

- 提示缺少 MeloTTS：改用项目内已安装 MeloTTS 的虚拟环境 Python，不要在普通 Python 中反复安装。
- 字幕匹配失败：逐字对照口播文件与 `element.subtitle`，修正标点、引号或漏字。
- 画面仍卡顿：先看时间轴报告，再检查是否确实用了 `motion-profile smooth`、60fps、5px 网格和 `pause off`；不要仅修改音频速度。
- 最终文件无声音：确认渲染命令同时映射了视频流和旁白流，并用校验脚本检查 AAC 音轨。
- 字幕乱码或滤镜路径失败：让渲染脚本在临时 ASCII 文件名下烧录，不直接把含中文绝对路径塞进 `subtitles=` 滤镜。
