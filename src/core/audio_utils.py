"""音频后处理：裁掉合成结果首尾的静音/换气段。

曼波（GPT-SoVITS）在零样本模式下，偶尔会把参考音频里的换气声复现到句首，
表现为「开口前先吸一口气」。这里在拿到 wav 后做一次能量裁剪：
  - 找第一个超过峰值的 3% 且不低于绝对阈值的采样点
  - 前面只保留 40ms，避免切掉气口导致爆音
  - 尾部同理
只依赖标准库（应用 venv 里没有 numpy）。
"""
from __future__ import annotations

import array
import sys
import wave
from pathlib import Path

ABS_THRESHOLD = 0.012      # 绝对阈值（-38dBFS 左右）
REL_THRESHOLD = 0.03       # 相对峰值比例
KEEP_HEAD_MS = 40
KEEP_TAIL_MS = 60
MIN_DURATION_MS = 120      # 太短的音频不处理


def trim_wav_silence(
    path: Path,
    abs_threshold: float = ABS_THRESHOLD,
    rel_threshold: float = REL_THRESHOLD,
    keep_head_ms: int = KEEP_HEAD_MS,
    keep_tail_ms: int = KEEP_TAIL_MS,
) -> tuple[float, float]:
    """就地裁剪 wav，返回 (裁掉的头 ms, 裁掉的尾 ms)。失败时返回 (0, 0)。"""
    try:
        with wave.open(str(path), "rb") as wf:
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            sr = wf.getframerate()
            nframes = wf.getnframes()
            raw = wf.readframes(nframes)
    except (wave.Error, OSError):
        return 0.0, 0.0
    if sw != 2 or nframes <= 0 or sr <= 0:
        return 0.0, 0.0
    if nframes / sr * 1000 < MIN_DURATION_MS:
        return 0.0, 0.0

    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()

    peak = 0
    for v in samples:
        if v < 0:
            v = -v
        if v > peak:
            peak = v
    if peak == 0:
        return 0.0, 0.0
    thresh = max(int(abs_threshold * 32767), int(peak * rel_threshold))

    total_frames = len(samples) // nch
    head = 0
    for i in range(total_frames):
        base = i * nch
        if any(abs(samples[base + c]) > thresh for c in range(nch)):
            head = i
            break
    tail = total_frames
    for i in range(total_frames - 1, -1, -1):
        base = i * nch
        if any(abs(samples[base + c]) > thresh for c in range(nch)):
            tail = i + 1
            break

    head_keep = int(sr * keep_head_ms / 1000)
    tail_keep = int(sr * keep_tail_ms / 1000)
    start = max(0, head - head_keep)
    end = min(total_frames, tail + tail_keep)
    trimmed_head_ms = head / sr * 1000
    trimmed_tail_ms = (total_frames - tail) / sr * 1000

    if start <= 0 and end >= total_frames:
        return trimmed_head_ms, trimmed_tail_ms
    if end - start < int(sr * MIN_DURATION_MS / 1000):
        return trimmed_head_ms, trimmed_tail_ms

    try:
        with wave.open(str(path), "wb") as out:
            out.setnchannels(nch)
            out.setsampwidth(sw)
            out.setframerate(sr)
            out.writeframes(samples[start * nch : end * nch].tobytes())
    except OSError:
        return 0.0, 0.0
    return trimmed_head_ms, trimmed_tail_ms
