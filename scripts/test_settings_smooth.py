"""回归测试：改设置不能打断正在播放的声音（平缓切换）。

覆盖：
  * 只改「预加载队列深度」→ 不动播放器、不重拼当前段，只扩/缩队列
  * 改语速/语调 → 当前段继续播，新参数队列在后台准备，段尾无缝接上
  * 暂停状态下改设置 → 只重建队列，不会自己播起来

用法： python scripts/test_settings_smooth.py
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


def write_wav(path: Path, seconds: float = 0.3) -> None:
    sr, n = 32000, int(32000 * seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"".join(
            struct.pack("<h", int(1100 * math.sin(2 * math.pi * 210 * i / sr))) for i in range(n)
        ))


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    settings = Settings.load()
    settings.prefetch = 5
    settings.speed = round(1.0 + random.uniform(0.05, 0.4), 3)
    service = SynthesisService(settings)
    salt = format(random.getrandbits(24), "06x")
    _orig = service.key_for
    service.key_for = lambda text, req: _orig(text, req) + salt

    defer = {"on": False}
    pending: list[str] = []

    def fake_request(key: str, req) -> Path | None:
        path = CACHE_DIR / f"{key}.wav"
        if defer["on"]:
            if key not in pending:
                pending.append(key)
            return None
        write_wav(path)
        return path

    service.request = fake_request  # type: ignore[assignment]

    ctl = PlaybackController(settings, service)
    stub = StubPlayer()
    ctl.player = stub  # type: ignore[assignment]

    hints: list[str] = []
    ctl.statusChanged.connect(hints.append)

    p = Path(tempfile.gettempdir()) / "vr_smooth_test.txt"
    p.write_text(
        "第一章 测试\n\n"
        + "\n\n".join(["他推开门，风雪灌了进来。", "“你来了。”老人抬起头。"] * 20),
        encoding="utf-8",
    )
    book = import_book(p)

    # 先让队头几句就绪，再开播
    ctl.load(book, 0, 0)
    for i in range(ctl._queue_head, min(len(ctl.sentences), ctl._queue_head + 5)):
        item = ctl._items.get(i)
        if item is not None and item.state != "ready":
            write_wav(CACHE_DIR / f"{item.key}.wav")
            ctl._on_ready(item.key)
    ctl.play()
    pump(app, 0.1)
    assert ctl._segment_playing, "没有开始播放"
    segment_before = list(ctl._segment)

    # ---- 1) 只改预加载深度：不影响播放
    stub.calls.clear()
    settings.prefetch = 15
    ctl.set_depth()
    pump(app, 0.1)
    assert "stop" not in stub.calls and "setSource" not in stub.calls, f"改深度打断了播放：{stub.calls}"
    assert ctl._segment == segment_before, "改深度导致当前播放段被重建"
    assert ctl._want_playing is True
    assert len(ctl._items) > len(segment_before), f"队列没有扩容：{len(ctl._items)}"
    print(f"① 深度 5→15：播放不中断，队列扩到 {len(ctl._items)} 条  ✓")

    # ---- 2) 改语速：当前段继续播，不 stop、不 setSource
    old_keys = {it.key for it in ctl._items.values() if it.key}
    settings.speed = round(settings.speed + 0.08, 3)
    stub.calls.clear()
    ctl.apply_settings_changed()
    pump(app, 0.1)
    assert "stop" not in stub.calls and "setSource" not in stub.calls, f"改语速打断了播放：{stub.calls}"
    assert ctl._segment == segment_before, "改语速把当前段重建了"
    assert ctl._segment_playing is True
    new_keys = {it.key for it in ctl._items.values() if it.key}
    assert new_keys and new_keys != old_keys, "新语速没有生成新的缓存 key"
    assert any("本段播完后生效" in h for h in hints), hints[-3:]
    print(f"② 改语速：当前段继续播，新队列已备好（{len(new_keys)} 条新 key）  ✓")

    # ---- 3) 本段播完 → 自动接上新参数的段
    consumed = list(ctl._segment)
    for i in range(ctl._queue_head, min(len(ctl.sentences), ctl._queue_head + 5)):
        item = ctl._items.get(i)
        if item is not None and item.state != "ready":
            write_wav(CACHE_DIR / f"{item.key}.wav")
            ctl._on_ready(item.key)
    stub.status = QMediaPlayer.MediaStatus.EndOfMedia
    stub.state = QMediaPlayer.PlaybackState.StoppedState
    stub.calls.clear()
    ctl._on_media_status(QMediaPlayer.MediaStatus.EndOfMedia)
    pump(app, 0.4)
    assert ctl.index == consumed[-1] + 1, (ctl.index, consumed)
    assert "setSource" in stub.calls, "段尾没有接上新段"
    assert ctl._segment and ctl._segment[0] == ctl.index
    print(f"③ 段尾无缝接上新参数的段（从第 {ctl.index + 1} 句）  ✓")

    # ---- 4) 暂停状态改设置：只重建队列，不自己播
    ctl.pause()
    stub.calls.clear()
    settings.speed = round(settings.speed + 0.05, 3)
    ctl.apply_settings_changed()
    pump(app, 0.3)
    assert "play" not in stub.calls, f"暂停时改设置不该自动播放：{stub.calls}"
    assert ctl._want_playing is False
    print("④ 暂停时改设置：只重建队列，不会自动播放  ✓")

    print("\n全部通过：改设置不再打断播放，切换平缓。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
