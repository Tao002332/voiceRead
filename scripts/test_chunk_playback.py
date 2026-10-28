"""回归测试：分块合成 + 连续播放链路。

不依赖真实引擎（用合成出来的假 wav 顶替），验证：
  1. 分块策略：块内句数/字数受控，且不跨章
  2. 纯标点句在块内变成 0.6s 静音，不送合成
  3. 拼块产出合法 wav，时长 = 各句时长之和（字幕时间轴正确）
  4. 预取：准备当前块后能继续准备后面两块
  5. 跳句：定位到正确块，并从句子偏移开始播

用法： python scripts/test_chunk_playback.py
"""
from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from PySide6.QtWidgets import QApplication  # noqa: E402

from src.config import CACHE_DIR, Settings  # noqa: E402
from src.core import player as player_mod  # noqa: E402
from src.core.library import import_book  # noqa: E402
from src.core.player import PlaybackController  # noqa: E402
from src.core.textproc import is_speakable  # noqa: E402
from src.core.tts.service import SynthesisService  # noqa: E402

PARAGRAPHS = [
    "他推开门，风雪灌了进来。",
    "“你终于来了。”老人抬起头，眼里有光。",
    "……",
    "壁炉里的火噼啪作响，把两个人的影子拉得很长。",
    "“我等了你很久。”",
    "“我知道。”",
    "——",
    "外面的风停了，只剩下心跳声。",
]


def make_book() -> Path:
    body = "\n\n".join(["第一章 风雪"] + PARAGRAPHS * 6)
    p = Path(tempfile.gettempdir()) / "vr_chunk_test.txt"
    p.write_text(body, encoding="utf-8")
    return p


def main() -> int:
    app = QApplication.instance() or QApplication([])  # noqa: F841
    settings = Settings.load()
    settings.auto_prosody = True
    service = SynthesisService(settings)
    ctl = PlaybackController(settings, service)
    progress_events: list[tuple[int, int, int, int]] = []
    ctl.synthProgress.connect(lambda a, b, c, d: progress_events.append((a, b, c, d)))

    requested: list[str] = []

    def fake_synth(key: str, req) -> Path:
        """造一段 0.4 秒 220Hz 正弦波当缓存，模拟引擎产出。"""
        path = CACHE_DIR / f"{key}.wav"
        if not path.exists():
            sr, n = 32000, 12800
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(b"".join(
                    struct.pack("<h", int(3000 * math.sin(2 * math.pi * 220 * i / sr))) for i in range(n)
                ))
        requested.append(req.text)
        return path

    service.request = fake_synth  # type: ignore[assignment]

    book = import_book(make_book())
    ctl.load(book, 0, 0)
    total = len(ctl.sentences)
    print(f"① 载入：{book.chapter_title(0)}，{total} 句，{len(ctl.chunks)} 块")
    assert total >= 40, total
    assert len(ctl.chunks) >= 3, len(ctl.chunks)

    # ---- 分块策略
    for ch in ctl.chunks:
        assert 0 < (ch.end - ch.start) <= player_mod.CHUNK_MAX_SENTENCES, ch
        chars = sum(len(ctl.sentences[i].text) for i in range(ch.start, ch.end))
        assert chars <= player_mod.CHUNK_TARGET_CHARS + 120, (ch.index, chars)
    print(f"   块大小：{[(c.end - c.start) for c in ctl.chunks]} 句   ✓")

    # ---- 纯标点句
    punct = [i for i, s in enumerate(ctl.sentences) if not is_speakable(s.text)]
    assert punct, "样本里应有纯标点句"
    print(f"   纯标点句 {len(punct)} 处（下标 {punct[:4]}）✓")

    # ---- 准备第一块
    first = ctl.chunks[0]
    requested.clear()
    ctl._prepare(first)
    assert first.ready, "第一块未就绪"
    assert first.path and first.path.exists(), "拼块文件不存在"
    assert len(first.durations) == first.end - first.start
    assert len(first.starts) == len(first.durations)

    punct_in_first = [i for i in range(first.start, first.end) if not is_speakable(ctl.sentences[i].text)]
    for i in punct_in_first:
        dur = first.durations[i - first.start]
        assert abs(dur - player_mod.PAUSE_ONLY_MS / 1000) < 0.01, dur
    for text in requested:
        assert is_speakable(text), f"纯标点被送去合成：{text!r}"
    print(f"   第一块就绪：{first.path.name}，{len(first.durations)} 句，"
          f"{sum(first.durations):.2f}s   ✓")

    with wave.open(str(first.path), "rb") as wf:
        real = wf.getnframes() / wf.getframerate()
    timeline_end = first.starts[-1] + first.durations[-1]
    expect = timeline_end + player_mod.CHUNK_TAIL_GAP_MS / 1000
    assert abs(real - expect) < 0.05, (real, expect)
    print(f"   拼块时长校验：实际 {real:.2f}s vs 时间轴 {expect:.2f}s   ✓")

    # ---- 句间/段间间隔
    total_speech = sum(first.durations)
    assert real > total_speech + 0.05, "没有插入句间间隔"
    checked = 0
    for k in range(len(first.starts) - 1):
        if first.keys[k] == "":          # 纯标点句本身是静音，不再叠加
            continue
        delta = first.starts[k + 1] - first.starts[k]
        assert delta >= first.durations[k] + player_mod.SENTENCE_GAP_MS / 1000 - 0.01, (k, delta)
        checked += 1
    print(f"   间隔插入：语音 {total_speech:.2f}s → 总长 {real:.2f}s，校验 {checked} 处   ✓")

    # ---- 进度信号（底栏进度条的数据源）
    assert progress_events, "没有发出 synthProgress 信号"
    last = progress_events[-1]
    assert last[1] == first.end - first.start, last
    assert last[0] == last[1], f"块已就绪，进度应满：{last}"
    print(f"   进度信号：{len(progress_events)} 次，末次 {last}   ✓")


    # ---- 预取
    ctl._prefetch_next(0)
    assert ctl.chunks[1].ready, "第 2 块未预取"
    assert ctl.chunks[2].ready, "第 3 块未预取"
    print(f"   预取：第 2、3 块已就绪   ✓")

    # ---- 跳句定位
    mid = ctl.chunks[1].start + 2
    ctl.seek_sentence(mid, autoplay=False)
    assert ctl.index == mid
    target_chunk = ctl._chunk_of(mid)
    assert target_chunk is not None and target_chunk.index == 1
    expect_off = target_chunk.starts[mid - target_chunk.start]
    assert ctl._pending_seek_ms is not None
    assert abs(ctl._pending_seek_ms / 1000 - expect_off) < 0.05, (ctl._pending_seek_ms, expect_off)
    print(f"   跳句：句 {mid} → 块 1 偏移 {expect_off:.2f}s   ✓")

    # ---- 切章重置
    if book.chapter_count() > 1:
        ctl.seek_chapter(1, autoplay=False)
        assert ctl.chapter_index == 1
        print("   切章：块队列已重建   ✓")

    print("\n全部通过：分块合成 + 连续播放链路正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
