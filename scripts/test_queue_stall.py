"""回归测试：队列卡住时的兜底（看门狗）。

对应现象：预加载队列已生成满、播放队列却是 0，音频不消费。
两个常见成因都在这里覆盖：
  1. 有可播条目但播放器闲着（某次 ready 事件没触发播放）
  2. 段其实已经播完，但没收到 EndOfMedia（Qt 后端偶发丢事件）

用法： python scripts/test_queue_stall.py
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
        self.plays = 0
        self.sources = 0

    def setSource(self, url) -> None:  # noqa: N802
        self.sources += 1
        self.pos = 0

    def play(self) -> None:
        self.plays += 1
        self.state = QMediaPlayer.PlaybackState.PlayingState

    def pause(self) -> None:
        self.state = QMediaPlayer.PlaybackState.PausedState

    def stop(self) -> None:
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
            struct.pack("<h", int(1000 * math.sin(2 * math.pi * 200 * i / sr))) for i in range(n)
        ))


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    settings = Settings.load()
    settings.prefetch = 6
    settings.speed = round(1.0 + random.uniform(0.05, 0.4), 3)
    service = SynthesisService(settings)
    salt = format(random.getrandbits(24), "06x")
    _orig = service.key_for
    service.key_for = lambda text, req: _orig(text, req) + salt

    def fake_request(key: str, req) -> Path | None:
        path = CACHE_DIR / f"{key}.wav"
        write_wav(path)
        return path            # 直接命中，模拟"全都能生成"

    service.request = fake_request  # type: ignore[assignment]

    ctl = PlaybackController(settings, service)
    stub = StubPlayer()
    ctl.player = stub  # type: ignore[assignment]

    p = Path(tempfile.gettempdir()) / "vr_stall_test.txt"
    p.write_text(
        "第一章 测试\n\n"
        + "\n\n".join(["他推开门，风雪灌了进来。", "“你来了。”老人抬起头。"] * 20),
        encoding="utf-8",
    )
    ctl.load(import_book(p), 0, 0)

    # ---- 1) 队列全就绪但从未播放：看门狗必须把它播起来
    assert ctl._ready_ahead() > 0, f"队列应当已就绪：{len(ctl._items)} 条"
    assert ctl._segment_playing is False
    ctl._want_playing = True          # 模拟用户点了播放，但 ready 事件没触发播放
    ctl._guard_tick()
    pump(app, 0.1)
    assert stub.plays >= 1, "有可播条目却没被消费"
    assert ctl._segment_playing is True
    print(f"① 队列就绪但闲着 → 看门狗开播（play 调用 {stub.plays} 次）  ✓")

    # ---- 2) 段播完但 EndOfMedia 丢失：看门狗主动推进
    consumed = list(ctl._segment)
    stub.status = QMediaPlayer.MediaStatus.BufferedMedia   # 故意不给 EndOfMedia
    stub.pos = int((ctl._segment_starts[-1] + ctl._segment_durs[-1] + 3) * 1000)
    before = stub.sources
    ctl._guard_tick()
    pump(app, 0.3)
    assert ctl.index == consumed[-1] + 1, f"没有推进：{ctl.index} vs {consumed[-1] + 1}"
    assert stub.sources > before, "没有拼下一段"
    print(f"② 结束事件丢失 → 看门狗推进到第 {ctl.index + 1} 句  ✓")

    # ---- 3) 统计口径：queued 是实际条数，深度是目标值
    stats: list[tuple[int, int, int, int]] = []
    ctl.synthProgress.connect(lambda a, b, c, d: stats.append((a, b, c, d)))
    ctl.set_depth()
    pump(app, 0.1)
    ready, queued, running, played = stats[-1]
    assert queued == len(ctl._items), (queued, len(ctl._items))
    assert ctl.queue_depth == settings.prefetch, (ctl.queue_depth, settings.prefetch)
    print(f"③ 统计口径正确：可播 {ready} / 队列内 {queued} / 深度 {ctl.queue_depth}  ✓")

    # ---- 4) 播放中改深度不会让看门狗乱播
    before_plays = stub.plays
    settings.prefetch = 12
    ctl.set_depth()
    pump(app, 0.2)
    assert stub.plays == before_plays, "改深度导致重复 start"
    print("④ 改深度不触发重新播放  ✓")

    print("\n全部通过：队列不会卡住不消费。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
