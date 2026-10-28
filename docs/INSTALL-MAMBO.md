# 曼波音色包 · 安装说明

这个包只含**模型与源码**（约 1.8 GB 解压后），不含 torch 和依赖——
那部分体积大且与显卡驱动相关，需要在你自己的机器上联网安装（约 2.5–4 GB 下载）。

整个包解压后应该是这样的结构（**目录名不要改**）：

```
曼波模型-win64\
├─ mambo\
│  ├─ GPT-SoVITS\          GPT-SoVITS 源码 + 1.8GB 模型
│  │  └─ GPT_SoVITS\pretrained_models\...
│  └─ refs\                曼波参考音频 + prompt_text.txt
├─ scripts\                安装/启动/补丁脚本（路径已改为相对，不用改）
└─ INSTALL-MAMBO.md        本文件
```

---

## 前提

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 / 11 x64 |
| 显卡 | NVIDIA，显存 ≥ 6GB（RTX 3060 笔记本可跑） |
| 驱动 | 已装 NVIDIA 驱动（`nvidia-smi` 能输出即可） |
| Python | **3.10**（3.11+ 部分依赖无轮子）。没装的话去 python.org 下 3.10.x，勾选 Add to PATH |
| 网络 | 需要，用来下 torch + 依赖 |
| 磁盘 | 再留 8 GB（torch 与依赖约 4–5 GB） |

> 建议放在**全英文路径**下，例如 `D:\voiceRead\mambo-pack`。

---

## 三步装好

在解压出来的目录里打开 PowerShell：

```powershell
# ① 建 venv + 装 torch + 补缺失模型（已有模型会跳过）
powershell -ExecutionPolicy Bypass -File scripts\setup_mambo_win.ps1

# ② 装推理依赖（跳过需要编译的语种包）
powershell -ExecutionPolicy Bypass -File scripts\install_gsv_deps.ps1

# ③ 打 4 个已知补丁（jieba 垫片 / transformers 版本 / 语言识别离线化）
powershell -ExecutionPolicy Bypass -File scripts\patch_mambo_env.ps1
```

脚本会按**自己所在目录**推导路径（`scripts\..\mambo`），不需要改任何配置。
Python 不在 PATH 时用 `-Py` 指定：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_mambo_win.ps1 -Py "C:\Python310\python.exe"
```

---

## 启动服务

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_mambo_server.ps1
```

看到 `TTS server listening on 127.0.0.1:9880` 就成了，**窗口保持开着**。
首次加载模型约 15–40 秒。

自测（另开一个窗口）：

```powershell
python scripts\test_mambo_tts.py
```

能打印「合成成功」并生成 wav 就通了。

---

## 接回朗读工具

把本包解压到 `语音书架.exe` **同级目录**下（即让 `mambo\` 与 exe 同级），
工具启动时会自动：

1. 认到 `mambo\refs\mambo_ref_clean.wav`
2. 把服务启动命令推导成 `mambo\venv\Scripts\python.exe api_v2.py`
3. 勾上「自动拉起曼波服务」

也就是说：**装好环境后直接开工具，曼波音色就自动可用**，不用进设置手填路径。
如果工具已经在跑，重启一次让它重新读配置。

---

## 常见问题

**`setup` 卡在 torch 下载**：默认走 pytorch 官方源（cu121），失败会自动回退清华源
（可能装成 CPU 版，速度会慢 10 倍以上）。可手动指定源重装：

```powershell
mambo\venv\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
```

**CUDA out of memory**：确认 `mambo\GPT-SoVITS\GPT_SoVITS\configs\tts_infer.yaml` 里
`custom.is_half: true`；关掉其他占显存的程序；工具已把句子切到 ~90 字以内。

**声音不对 / 有杂音**：参考音频决定音色。`refs\mambo_ref_clean.wav` 是修剪过首尾静音的版本，
想换音色就把新的 3–10 秒曼波原声放进来，并把 `prompt_text.txt` 一起换掉（工具会自动读取）。

**服务起来了但工具报「服务未启动」**：检查端口是否被占用；
或在设置里把服务地址改成实际端口。

**换了盘符导致失效**：工具按「参考音频所在位置」反推服务路径，
所以只要 `mambo` 目录整体移动（`refs` 与 `GPT-SoVITS` 保持相对位置），仍然可用。
