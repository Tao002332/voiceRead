"""回归测试：块播完等待下一块时不得重播；未就绪时不得可播。

复现的两个 bug：
  1. 预加载段播完后、下一段还没生成好时，后端补发的 Loaded/Buffered 事件
     被当成"该播了"，把已播完的块从头再放一遍。
  2. 未就绪时点播放再点暂停，暂停没有取消待播，加载完又自己播起来。

用法： python scripts/test_endofchunk.py
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
        self.status = QMediaPlayer.MediaStatus.NoMedia
        self.state = QMediaPlayer.PlaybackState.StoppedState
        self.calls: list[tuple] = []
        self.source_name = ""

    def setSource(self, url) -> None:  # noqa: N802
        self.source_name = str(url)
        self.calls.append(("setSource", self.source_name))
        self.pos = 0
        self.status = QMediaPlayer.MediaStatus.LoadedMedia

    def play(self) -> None:
        self.calls.append(("play",))
        self.state = QMediaPlayer.PlaybackState.PlayingState

    def pause(self) -> None:
        self.calls.append(("pause",))
        self.state = QMediaPlayer.PlaybackState.PausedState

    def stop(self) -> None:
        self.calls.append(("stop",))
        self.state = QMediaPlayer.PlaybackState.StoppedState

    def playbackState(self):  # noqa: N802
        return self.state

    def mediaStatus(self):  # noqa: N802
        return self.status

    def position(self) -> int:
        return self.pos

    def setPosition(self, ms: int) -> None:  # noqa: N802
        self.calls.append(("setPosition", ms))
        self.pos = ms

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


def write_wav(path: Path, seconds: float = 0.3) -> None:
    sr, n = 32000, int(32000 * seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"".join(
            struct.pack("<h", int(1500 * math.sin(2 * math.pi * 200 * i / sr))) for i in range(n)
        ))


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    settings = Settings.load()
    # 随机一档语速/语调强度：保证每次运行都是全新缓存 key，
    # 否则会命中上次跑测试留下的音频，块一秒就绪、测不到"生成慢"的分支
    settings.speed = round(1.0 + random.uniform(0.05, 0.45), 3)
    settings.prosody_strength = round(random.uniform(0.6, 0.95), 2)
    service = SynthesisService(settings)
    ctl = PlaybackController(settings, service)

    pending_keys: list[str] = []
    defer = {"on": False}

    def fake_request(key: str, req) -> Path | None:
        """defer 打开时：文件先造好，但返回 None 表示"还没生成完"。

        这样不依赖缓存是否命中，测试结果稳定。
        """
        path = CACHE_DIR / f"{key}.wav"
        if not path.exists():
            write_wav(path)
        if defer["on"]:
            if key not in pending_keys:
                pending_keys.append(key)
            return None
        return path

    service.request = fake_request  # type: ignore[assignment]

    p = Path(tempfile.gettempdir()) / "vr_endchunk.txt"
    p.write_text("第一章 测试\n\n" + "\n\n".join(["他推开门，风雪灌了进来。", "“你来了。”老人说。"] * 24),
                 encoding="utf-8")
    book = import_book(p)
    ctl.load(book, 0, 0)

    stub = StubPlayer()
    ctl.player = stub  # type: ignore[assignment]
    assert len(ctl.chunks) >= 3, len(ctl.chunks)

    loading_events: list[bool] = []
    ctl.loadingChanged.connect(loading_events.append)

    def finish(chunk) -> None:
        """补齐该块的缓存文件，并通过 _on_ready 回调模拟后台生成完成。"""
        for key in list(chunk.keys):
            if not key:
                continue
            path = CACHE_DIR / f"{key}.wav"
            if not path.exists():
                write_wav(path)
        for key in list(chunk.keys):
            if key:
                ctl._on_ready(key)
        pump(app, 0.1)

    # ---- 第 0 块就绪并开始播放
    chunk0 = ctl.chunks[0]
    finish(chunk0)
    assert chunk0.ready, "第 0 块未就绪"
    defer["on"] = True                   # 之后的预取/生成一律挂起（模拟生成慢）
    ctl._want_playing = True
    ctl._start_chunk(chunk0, chunk0.start)
    stub.calls.clear()
    print(f"① 第 0 块开始播放，loading={ctl.is_loading}  ✓")

    # ---- 播完，但下一块还没生成好（预取仍在跑）
    assert not ctl.chunks[1].ready, "第 1 块应仍在生成中"
    stub.calls.clear()
    stub.status = QMediaPlayer.MediaStatus.EndOfMedia
    stub.state = QMediaPlayer.PlaybackState.StoppedState
    ctl._on_media_status(QMediaPlayer.MediaStatus.EndOfMedia)
    pump(app, 0.3)                        # 让 singleShot(0) 执行
    assert ctl._pending_play is not None, "没有挂起待播"
    assert ("play",) not in stub.calls, f"播完又重播了：{stub.calls}"
    print(f"② 播完等待下一块，未触发重播  调用={stub.kinds()}  ✓")

    # ---- 后端补发 Ready 事件，仍不得重播
    for status in (QMediaPlayer.MediaStatus.BufferedMedia, QMediaPlayer.MediaStatus.LoadedMedia):
        ctl._on_media_status(status)
    assert ("play",) not in stub.calls, f"补发事件导致重播：{stub.calls}"
    print("③ 补发 Buffered/Loaded 事件也不再重播  ✓")

    # ---- 下一块生成完成 → 应自动接上
    stub.calls.clear()
    finish(ctl.chunks[1])
    pump(app, 0.2)

    assert ctl._chunk_cursor == 1, ctl._chunk_cursor
    assert "setSource" in stub.kinds(), f"没有切到下一块：{stub.calls}"
    assert ctl.chunks[1].path.name in stub.source_name, (stub.source_name, ctl.chunks[1].path.name)
    print(f"④ 下一块就绪后自动接上 {ctl.chunks[1].path.name}  ✓")

    # ---- 未就绪时暂停：必须取消待播，加载完不许自己播
    defer["on"] = True
    chunk2 = ctl.chunks[2]
    # 重置该块，模拟"这一块还没生成过"
    chunk2.ready = False
    chunk2.building = False
    chunk2.keys = []
    chunk2.path = None
    chunk2.pending = 0
    ctl._want_playing = False
    ctl.seek_sentence(chunk2.start, autoplay=True)     # 挂起等待
    assert ctl._pending_play is not None
    ctl.pause()
    assert ctl._pending_play is None, "暂停没有取消待播"
    assert ctl._want_playing is False
    stub.calls.clear()
    finish(chunk2)
    pump(app, 0.2)
    assert ("play",) not in stub.calls, f"暂停后仍被自动播放：{stub.calls}"
    print("⑤ 加载中暂停 → 取消待播，就绪后不自动播  ✓")

    # ---- loadingChanged 状态序列
    assert True in loading_events, f"没有发出加载中状态：{loading_events}"
    assert loading_events[-1] is False, f"最后应为就绪：{loading_events}"
    print(f"⑥ loadingChanged 序列 {loading_events}  ✓")

    print("\n全部通过：块尾不重播，未就绪不可播。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
