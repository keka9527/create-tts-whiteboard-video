# create-tts-whiteboard-video

把中文文案或 SRT 制作成带本地中文配音、同步字幕和流畅素描动画的 16:9 白板视频 Codex Skill。

这个版本解决的核心问题不是“给视频后贴一条音轨”，而是先生成 TTS，再用每条 WAV 的真实时长反推字幕区间、场景时长和素描绘制进度。这样配音是主时间轴，画面跟着声音完成，不会因为固定镜头长度而出现口播结束后画面还在慢慢画、或为了追配音而突然卡住的问题。

## 主要能力

- 本地中文 TTS，默认适配 MeloTTS
- TTS 真实时长驱动字幕、场景和素描
- 分段音频缓存，修改一句只重做对应分段
- SRT 烧录、H.264/AAC 成片输出
- 语义区域标注与逐元素揭示
- 60fps 内部动画、30fps CFR 成片
- 输出前自动检查时长、字幕、音视频流和冻结帧

## 效果演示

![白板动画动态预览](demo/preview.gif)

[▶ 点击播放带中文配音和同步字幕的完整演示视频（MP4，约 18.9 MB）](demo/whiteboard-demo.mp4)

[![完整演示视频封面](demo/poster.jpg)](demo/whiteboard-demo.mp4)

演示成片为 1920×1080、30fps、H.264 + AAC，时长约 2 分 26 秒。仓库只保留这一版公开演示，不提交测试文案、原始图片、TTS 分段、工程中间文件或其他测试成片。

## 安装

在 Windows PowerShell 中执行：

```powershell
git clone https://github.com/keka9527/create-tts-whiteboard-video.git `
  "$env:USERPROFILE\.codex\skills\create-tts-whiteboard-video"
```

安装渲染环境：

```powershell
python scripts/prepare_env.py
```

还需要：

- Python 3.10+
- FFmpeg / FFprobe（建议加入 `PATH`）
- 一个可用的 MeloTTS Python 环境；MeloTTS 是独立项目，请按其官方说明安装

渲染核心依赖见 [requirements.txt](requirements.txt)。`prepare_env.py` 会联网调用 pip 安装这些依赖；视频文案、音频和图片不会因此上传。

## 在 Codex 中使用

安装后，可以直接对 Codex 说：

```text
用 create-tts-whiteboard-video 把这份中文文案做成 16:9 横版白板动画。
需要本地中文配音、同步字幕，跳过中间确认直接输出成片。
```

也可以提供已有 SRT、场景图片和语义标注。完整工作流、参数和命令在 [SKILL.md](SKILL.md)；标注结构见 [annotation-schema.md](references/annotation-schema.md)，验收规则见 [quality-gates.md](references/quality-gates.md)。

## 工作流

```text
中文文案 / SRT
      ↓
本地 TTS 分句生成并测量真实时长
      ↓
组合连续音轨 + 生成字幕时间轴
      ↓
按音轨时间重写场景与元素绘制时长
      ↓
逐幕渲染与拼接
      ↓
烧录字幕、合成音轨、CFR 转码
      ↓
冻结帧与音视频完整性校验
```

关键原则：**声音定时间，画面填时间。** 不让配音追固定动画，也不让两者一起等待渲染进度。

## 隐私与安全

- 工作流本身不包含遥测、账号登录、浏览器 Cookie 读取或素材上传逻辑。
- 脚本只处理命令行中明确提供的本地路径，并在输出目录中生成中间文件和成片。
- `prepare_env.py` 会连接 Python 包索引安装依赖。
- 脚本会调用本机 Python、FFmpeg 和 FFprobe，并会清理自己产生的部分临时中间文件。
- 不要把 `.env`、密钥、TTS 模型、虚拟环境、私有文案、配音或生成视频提交到 GitHub；本仓库的 [.gitignore](.gitignore) 已默认排除这些内容。

## 开源说明

本项目基于 [geeklee/srt-whiteboard-animation](https://github.com/geeklee/srt-whiteboard-animation) 的白板动画能力继续扩展，保留原项目 MIT 许可证与版权声明。新增部分主要包括配音优先时间轴、TTS 分段缓存、字幕合成、逐幕渲染编排和输出质量检查。

详细第三方说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## License

[MIT](LICENSE)
