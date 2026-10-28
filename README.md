# 语音书架 voiceRead

PC 端小说朗读工具：本地/在线语音朗读 + 悬浮字幕 + 每本书断点续读。

## 功能

| 能力 | 说明 |
|---|---|
| 朗读 | **分块合成**：10~16 句（约 280 字 / 40~70 秒音频）拼成一段连续 wav，块内零间隙；播放当前块时已预取后两块 |
| 悬浮字幕 | 无边框置顶、半透明、可拖动、字号/配色/不透明度/对齐可调、显示上下句、鼠标穿透 |
| 托盘 | 关闭窗口不退出，托盘菜单控制播放、字幕、设置 |
| 语音切换 | Edge 在线（免费，多音色）/ 曼波本地（GPT-SoVITS）/ Windows SAPI 离线 |
| 语速语调 | 基础语速 0.5–2.0×、音调 ±20Hz；再加一层「上下文语调」自动调节 |
| 上下文语调 | 问句上扬、感叹加快、引号对话与旁白区分、省略号/破折号加长停顿、短句放慢强调 |
| 断点续读 | 每本书单独记录章节 + 句子 + 百分比，下次打开自动回到上次位置 |
| 书架 | TXT（UTF-8/GBK/GB18030/Big5 自动探测）+ EPUB（stdlib 解析，按 spine 分章） |

## 快速开始（源码直接跑）

```powershell
cd <项目目录>
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\pythonw.exe run.py
```

或双击 `启动语音书架.bat`。

## 打包给别的电脑用

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
Compress-Archive -Path 'dist\语音书架\*' -DestinationPath 'dist\语音书架-win64.zip' -Force
```

产物：`dist\语音书架\语音书架.exe`（onedir，约 136MB，解压后 zip 约 56MB）。
目标机**不需要装 Python**，解压双击即可；书架/进度/设置存在 `%APPDATA%\VoiceRead`，
升级时只覆盖程序文件不会丢数据。

细节（曼波怎么带过去、目标机要求、常见问题）见 `docs/打包与分发.md`。

### 两个分发包

| 包 | 大小 | 内容 | 目标机还需要 |
|---|---|---|---|
| `语音书架-win64.zip` | 56 MB | exe + 全部依赖 + 文档 | 无（Edge/SAPI 语音直接可用） |
| `曼波模型-win64.zip` | ≈1.6 GB | GPT-SoVITS 源码 + 1.15GB 模型 + 64MB g2pw + 参考音频 + 安装脚本 | 联网装 torch+依赖（约 2.5–4GB） |

曼波包解压后放到 `语音书架.exe` 同级目录，跑完三个脚本，工具会自动认到
`mambo\refs`、自动推导服务启动命令，不用手填任何路径。详见 `docs/INSTALL-MAMBO.md`。



## 快捷键

| 键 | 作用 |
|---|---|
| Ctrl+Alt+P | 播放 / 暂停 |
| Ctrl+Alt+F | 显示 / 隐藏字幕 |
| Ctrl+Alt+← / → | 上一句 / 下一句 |
| Ctrl+Alt+↑ / ↓ | 语速 +/- |
| 字幕上滚轮 | 语速微调 |
| 单击字幕 | 暂停继续 |
| 双击字幕 | 打开主窗口 |

## 目录结构

```
voiceRead/
├─ run.py                    入口
├─ requirements.txt
├─ 启动语音书架.bat
├─ 初始化环境并启动.bat      建 venv + 装依赖 + 启动
├─ src/
│  ├─ app.py                 装配：托盘、字幕、热键、单实例
│  ├─ config.py              %APPDATA%\VoiceRead 下的设置/数据
│  ├─ core/
│  │  ├─ library.py          TXT/EPUB 导入、分章
│  │  ├─ textproc.py         分句、语调分析、章节渲染
│  │  ├─ player.py           朗读控制器（合成+播放+预取）
│  │  ├─ progress.py         每本书阅读进度
│  │  └─ tts/                引擎抽象 + edge / mambo / sapi
│  └─ ui/
│     ├─ main_window.py      书架 + 阅读区 + 控制栏
│     ├─ subtitle_window.py  悬浮字幕
│     ├─ settings_dialog.py  设置
│     ├─ hotkeys.py          全局热键
│     └─ theme.py            配色/QSS/程序内图标
├─ docs/
│  ├─ 使用说明.md
│  └─ 曼波模型部署.md
└─ packaging/                PyInstaller 打包
```

## 数据位置

`%APPDATA%\VoiceRead\`

- `settings.json` 设置
- `library.json` 书架索引
- `books/<id>.json` 各书章节正文
- `progress.json` 阅读进度
- `cache/tts/` 语音缓存（可在设置里限制大小、一键清空）

## 当前状态

- 代码完整度：功能链路齐全
- **已验证**（本机 RTX 3060 Laptop 6GB / Python 3.10.7 / PySide6 6.12）：
  - 冒烟测试 8/8 通过（分章分句、编码探测、进度、设置、引擎、Qt 装配、章节载入、播放器）
  - 应用启动自检退出码 0
  - 曼波本地服务真实合成通过：某本 780 章的 EPUB 第一章，115 句，RTF 0.55–0.76（快于实时）
- 待人工确认：界面视觉细节、字幕悬浮体验、全局热键在你的其他软件里是否冲突

## 本机部署记录

- GPT-SoVITS 与模型：`<项目目录>\mambo\`（源码 + 1.14GB 模型 + 独立 venv）
- 曼波音色：`mambo\refs\mambo_ref_clean.wav`（修剪掉首尾静音/换气）
- 参考文本：`mambo\refs\prompt_text.txt`（funasr 自动转写，**音质关键项**，见下）
- 环境四个坑已固化：`scripts\patch_mambo_env.ps1`

### 音质调优实测（`scripts\diagnose_voice.py`）

同一组测试句，只改一个变量：

| 配置 | 句首爬升最大 | 字/秒极差 |
|---|---|---|
| 原始参考音频 + 空参考文本 | 1180ms | 4.63 |
| 修剪参考音频 | 580ms | 4.15 |
| 修剪 + 稳参(t=0.7) | 600ms | 4.20 |
| **修剪 + 参考文本** | **120ms** | **2.88** |
| 修剪 + 文本 + 稳参 | 140ms | **2.65** |

结论：**填 `prompt_text` 是消除句首吸气声、收敛语速差异最有效的一步**，
其次是修剪参考音频首尾，最后才是采样参数。

另外两条经人耳确认的定稿规则（`scripts/compare_prosody.py` 可复现）：

1. **送 TTS 前剥掉首尾引号/括号**。`“……额，那个……”` 这类文本直接喂给模型会
   在开口与收尾处产生多余动作，实测同句语速被压到 5.01 字/秒（去引号后 4.11）。
2. **对话不做人为加速/升调**（原 1.05×/+3Hz → 现 1.00×/0Hz）。
   模型对引号内台词已有自然韵律，叠加偏移反而让口语台词听起来假。

对照结果：`demo_out\prosody\` 里的 A（旧）/ C（新），C 被确认更自然。

### 真实吞吐（`scripts\test_real_chunk.py`，第1章 115 句）

```
块 0:  9 句 / 278 字 / 音频 56.7s / 合成  0.0s（命中缓存）
块 1: 10 句 / 277 字 / 音频 51.9s / 合成 20.8s / RTF 0.40
块 2:  8 句 / 287 字 / 音频 55.1s / 合成 20.7s / RTF 0.38
整体 RTF 0.25 → 预取 1 块即有 2.5 倍余量，播放中不会长停顿
```

## 已知限制

- EPUB 容器用标准库解析，非常规排版（大量内联样式、非 XHTML）可能分章不理想
- SAPI 引擎不支持音调
- 曼波引擎需要独立部署 GPT-SoVITS 服务，6GB 显存是下限
- 中文多音字未做词典纠正（如「银行/行走」）
