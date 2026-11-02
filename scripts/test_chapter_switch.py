"""回归测试：跨章切换不能卡住。

场景：一章播完 → 自动进入下一章 → 新章节的队列从 0 开始生成 →
生成完第一条就要接着播，不能停在"等待语音生成…"。

用法： python scripts/test_chapter_switch.py
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
        self.sources = 0
        self.plays = 0

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


def write_wav(path: Path, seconds: float = 0.25) -> None:
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
    settings.prefetch = 4
    settings.speed = round(1.0 + random.uniform(0.05, 0.4), 3)
    service = SynthesisService(settings)
    salt = format(random.getrandbits(24), "06x")
    _orig = service.key_for
    service.key_for = lambda text, req: _orig(text, req) + salt

    defer = {"on": False}
    pending: list[str] = []

    def fake_request(key: str, req) -> Path | None:
        path = CACHE_DIR / f"{key}.wav"
        if defer["on"] and not path.exists():
            if key not in pending:
                pending.append(key)
            return None
        write_wav(path)
        return path

    def finish_running() -> int:
        n = 0
        for it in list(ctl._items.values()):
            if it.state == "running":
                write_wav(CACHE_DIR / f"{it.key}.wav")
                ctl._on_ready(it.key)
                n += 1
        return n

    service.request = fake_request  # type: ignore[assignment]

    ctl = PlaybackController(settings, service)
    stub = StubPlayer()
    ctl.player = stub  # type: ignore[assignment]

    p = Path(tempfile.gettempdir()) / "vr_chapter_test.txt"
    p.write_text(
        "第一章 甲\n\n" + "\n\n".join(["第一章的内容在这里。"] * 3)
        + "\n\n第二章 乙\n\n" + "\n\n".join(["第二章开始了。"] * 3)
        + "\n\n第三章 丙\n\n" + "\n\n".join(["第三章也来了。"] * 3),
        encoding="utf-8",
    )
    book = import_book(p)
    print(f"章节数：{book.chapter_count()}  标题：{book.chapter_titles}")

    ctl.load(book, 0, 0)
    finish_running()
    ctl.play()
    pump(app, 0.1)
    assert ctl._segment_playing, "第一章没开播"
    print(f"① 第一章播放中（{len(ctl._segment)} 句）  ✓")

    # 让合成变慢，模拟真实情况（新章节需要等生成）
    defer["on"] = True
    for _ in range(6):
        finish_running()
        pump(app, 0.05)
        if ctl.chapter_index > 0:
            break
        stub.status = QMediaPlayer.MediaStatus.EndOfMedia
        stub.state = QMediaPlayer.PlaybackState.StoppedState
        ctl._on_media_status(QMediaPlayer.MediaStatus.EndOfMedia)
        pump(app, 0.25)

    assert ctl.chapter_index == 1, f"没有进入第二章：chapter_index={ctl.chapter_index}"
    print(f"② 已切到第二章《{book.chapter_title(1)}》  ✓")

    # 新章节：先进"生成中"，完成后必须自动接上
    pump(app, 0.2)
    assert ctl._want_playing is True, "切章后丢失了播放意图"
    pump(app, 0.2)
    n = finish_running()
    pump(app, 0.3)
    assert ctl._want_playing is True, "切章后丢失了播放意图"
    assert ctl._segment_playing is True, (
        f"新章节没有自动续播：items={len(ctl._items)} ready={ctl._ready_ahead()} "
        f"head={ctl._queue_head} index={ctl.index}")
    assert ctl._ready_ahead() > 0, "新章节队列是空的"
    print(f"③ 第二章自动续播（补合成 {n} 条，第 {ctl.index + 1} 句）  ✓")

    # 再切一章，确认可重复
    defer["on"] = False
    for _ in range(6):
        finish_running()
        pump(app, 0.05)
        if ctl.chapter_index > 1:
            break
        stub.status = QMediaPlayer.MediaStatus.EndOfMedia
        stub.state = QMediaPlayer.PlaybackState.StoppedState
        ctl._on_media_status(QMediaPlayer.MediaStatus.EndOfMedia)
        pump(app, 0.25)
    assert ctl.chapter_index == 2, f"没有进入第三章：{ctl.chapter_index}"
    print(f"④ 继续切到第三章《{book.chapter_title(2)}》  ✓")

    print("\n全部通过：跨章切换能自动接上。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
