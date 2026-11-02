"""回归测试：音频与文本/字幕必须严格对应（音字不符）。

做法：给每句造**时长不同**的音频，然后按播放位置逐句反查，
断言"位置 → 应显示的句子"与控制器发出的 sentenceChanged 完全一致。
另外覆盖两条容易错位的路径：
  * 暂停后左键换句再播放，必须重拼段（不能续播旧音频）
  * 暂停后直接播放（位置仍在本段内），应续播、不重拼

用法： python scripts/test_audio_text_sync.py
"""
from __future__ import annotations

import math
import os
import random
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.config import CACHE_DIR, Settings  # noqa: E402
from src.core.library import import_book  # noqa: E402
from src.core.player import PlaybackController  # noqa: E402
from src.core.tts.service import SynthesisService  # noqa: E402


class StubPlayer(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.pos = 0
        self.status = QMediaPlayer.MediaStatus.LoadedMedia
        self.state = QMediaPlayer.PlaybackState.StoppedState
        self.calls: list[str] = []

    def setSource(self, url) -> None:  # noqa: N802
        self.calls.append("setSource")
        self.pos = 0

    def play(self) -> None:
        self.calls.append("play")
        self.state = QMediaPlayer.PlaybackState.PlayingState

    def pause(self) -> None:
        self.calls.append("pause")
        self.state = QMediaPlayer.PlaybackState.PausedState

    def stop(self) -> None:
        self.calls.append("stop")
        self.state = QMediaPlayer.PlaybackState.StoppedState

    def playbackState(self):  # noqa: N802
        return self.state

    def mediaStatus(self):  # noqa: N802
        return self.status

    def position(self) -> int:
        return self.pos

    def setPosition(self, ms: int) -> None:  # noqa: N802
        self.pos = ms


def write_wav(path: Path, seconds: float) -> None:
    sr, n = 32000, int(32000 * seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"".join(
            struct.pack("<h", int(900 * math.sin(2 * math.pi * 200 * i / sr))) for i in range(n)
        ))


def main() -> int:
    app = QApplication.instance() or QApplication([])
    settings = Settings.load()
    settings.prefetch = 8
    settings.speed = round(1.0 + random.uniform(0.05, 0.4), 3)
    settings.prosody_strength = round(random.uniform(0.6, 0.95), 2)
    service = SynthesisService(settings)
    ctl = PlaybackController(settings, service)

    stub = StubPlayer()
    ctl.player = stub  # type: ignore[assignment]
    emitted: list[int] = []
    ctl.sentenceChanged.connect(lambda i, t, p: emitted.append(i))

    # 每句给不同时长，方便从位置反查句子
    durations: dict[int, float] = {}

    def wav_len(index: int) -> float:
        return durations.setdefault(index, 0.30 + 0.07 * (index % 5))

    def fake_request(key: str, req) -> Path | None:
        path = CACHE_DIR / f"{key}.wav"
        if not path.exists():
            (CACHE_DIR / f"{key}.tmp").write_bytes(b"pending")
        return None

    def complete(index: int) -> None:
        key = ctl._items[index].key
        write_wav(CACHE_DIR / f"{key}.wav", wav_len(index))
        ctl._on_ready(key)

    service.request = fake_request  # type: ignore[assignment]

    p = Path(tempfile.gettempdir()) / "vr_sync_test.txt"
    p.write_text(
        "第一章 测试\n\n"
        + "\n\n".join(["他推开门，风雪灌了进来。", "“你来了。”老人抬起头。"] * 15),
        encoding="utf-8",
    )
    book = import_book(p)
    ctl.load(book, 0, 0)

    # 把前 8 条都生成好
    for i in range(ctl._queue_head, min(len(ctl.sentences), ctl._queue_head + 6)):
        complete(i)
    ctl.play()
    time.sleep(0.05)
    app.processEvents()
    assert ctl._segment, "没有拼出播放段"
    segment = list(ctl._segment)
    print(f"① 播放段 {segment[0]}..{segment[-1]}（{len(segment)} 句）  ✓")

    # ---- 逐句核对：位置 → 应显示句子
    fails = 0
    for k, idx in enumerate(segment):
        start = ctl._segment_starts[k]
        dur = ctl._segment_durs[k]
        for frac in (0.1, 0.5, 0.9):
            stub.pos = int((start + dur * frac) * 1000)
            emitted.clear()
            ctl._on_tick()
            got = ctl.index            # 播放头 = 界面高亮/字幕的位置
            if got != idx:
                print(f"  ✗ 位置 {stub.pos}ms 应显示第 {idx} 句，实际 {got}")
                fails += 1
    assert fails == 0, f"{fails} 处位置与句子不一致"
    print(f"② 段内 {len(segment)} 句 × 3 个位置，字幕与音频完全对应  ✓")

    # ---- 暂停后直接继续：位置仍在本段，不重拼
    ctl.pause()
    stub.pos = int((ctl._segment_starts[1] + 0.1) * 1000)
    ctl._on_tick()
    stub.calls.clear()
    ctl.play()
    assert "setSource" not in stub.calls, f"本段内续播不该重拼：{stub.calls}"
    print("③ 暂停→继续（同段内）：直接续播，不重拼  ✓")

    # ---- 暂停后左键换句再播放：必须重拼到新句
    ctl.pause()
    target = min(len(ctl.sentences) - 1, segment[-1] + 5)
    ctl.select_sentence(target)
    assert ctl.index == target
    assert ctl._segment_path is None, "换位置后旧段没有被丢弃"
    for i in range(target, min(len(ctl.sentences), target + 4)):
        if ctl._items.get(i) is not None and ctl._items[i].state != "ready":
            complete(i)
    stub.calls.clear()
    ctl.play()
    app.processEvents()
    assert ctl._segment and ctl._segment[0] == target, (ctl._segment[:1], target)
    assert "setSource" in stub.calls
    stub.pos = 0
    ctl._on_tick()
    assert ctl.index == target, (ctl.index, target)
    print(f"④ 暂停→左键换到第 {target + 1} 句→播放：重拼新段且字幕同步  ✓")

    # ---- 段播完推进：index 与队列头一起前进
    consumed = list(ctl._segment)
    stub.status = QMediaPlayer.MediaStatus.EndOfMedia
    stub.state = QMediaPlayer.PlaybackState.StoppedState
    ctl._on_media_status(QMediaPlayer.MediaStatus.EndOfMedia)
    end = time.time() + 0.5
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)
    assert ctl.index == consumed[-1] + 1, (ctl.index, consumed)
    assert ctl._queue_head == ctl.index, (ctl._queue_head, ctl.index)
    print(f"⑤ 段播完：index 与队列头同步推进到 {ctl.index}  ✓")

    print("\n全部通过：音频、正文高亮、悬浮字幕三者一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
