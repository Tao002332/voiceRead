"""回归测试：点「朗读」后必须真的开始播放。

曾经的 bug：_start_chunk() 只 setSource() 不 play()，而 play() 藏在
_on_media_status 里且带「_pending_seek_ms 不为 None」的前提，
导致从段首（偏移 0）开始读时永远没有声音。

用法： python scripts/test_playback_start.py
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

from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.config import CACHE_DIR, Settings  # noqa: E402
from src.core.library import import_book  # noqa: E402
from src.core.player import PlaybackController  # noqa: E402
from src.core.tts.service import SynthesisService  # noqa: E402


class StubPlayer(QObject):
    """记录调用顺序的假播放器。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self._state = QMediaPlayer.PlaybackState.StoppedState

    def setSource(self, url) -> None:  # noqa: N802
        self.calls.append(("setSource", Path(str(url)).name))

    def play(self) -> None:
        self.calls.append(("play",))
        self._state = QMediaPlayer.PlaybackState.PlayingState

    def pause(self) -> None:
        self.calls.append(("pause",))
        self._state = QMediaPlayer.PlaybackState.PausedState

    def stop(self) -> None:
        self.calls.append(("stop",))
        self._state = QMediaPlayer.PlaybackState.StoppedState

    def playbackState(self):  # noqa: N802
        return self._state

    def setPosition(self, ms: int) -> None:  # noqa: N802
        self.calls.append(("setPosition", ms))

    def position(self) -> int:
        return 0

    def mediaStatus(self):  # noqa: N802
        return QMediaPlayer.MediaStatus.NoMedia


def make_book() -> Path:
    paras = [
        "他推开门，风雪灌了进来。",
        "“你终于来了。”老人抬起头。",
        "壁炉里的火噼啪作响。",
        "外面的风停了。",
    ] * 4
    p = Path(tempfile.gettempdir()) / "vr_play_test.txt"
    p.write_text("第一章 风雪\n\n" + "\n\n".join(paras), encoding="utf-8")
    return p


def main() -> int:
    app = QApplication.instance() or QApplication([])  # noqa: F841
    settings = Settings.load()
    service = SynthesisService(settings)
    ctl = PlaybackController(settings, service)

    def fake_synth(key: str, req) -> Path:
        path = CACHE_DIR / f"{key}.wav"
        if not path.exists():
            sr, n = 32000, 9600
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(b"".join(
                    struct.pack("<h", int(2500 * math.sin(2 * math.pi * 200 * i / sr))) for i in range(n)
                ))
        return path

    service.request = fake_synth  # type: ignore[assignment]

    book = import_book(make_book())
    ctl.load(book, 0, 0)
    print(f"载入：{len(ctl.sentences)} 句 / {len(ctl.chunks)} 块")

    stub = StubPlayer()
    ctl.player = stub  # type: ignore[assignment]

    # ---- 场景 1：从段首（偏移 0）点朗读
    ctl.index = 0
    ctl.chunks  # noqa: B018
    for chunk in ctl.chunks:
        ctl._prepare(chunk)
    ctl._want_playing = False
    ctl.play()

    kinds = [c[0] for c in stub.calls]
    print(f"调用序列：{kinds}")
    assert "setSource" in kinds, "没有加载音频"
    assert "play" in kinds, f"从段首开始朗读时没有调用 play：{kinds}"
    first_play = kinds.index("play")
    assert first_play > kinds.index("setSource"), "应当先 setSource 再 play"
    print("① 段首朗读 → 调用了 play  ✓")

    # ---- 场景 2：媒体就绪回调（异步加载完成且无待定位）也应开始播放
    stub.calls.clear()
    stub._state = QMediaPlayer.PlaybackState.StoppedState
    ctl._want_playing = True
    ctl._pending_seek_ms = None
    ctl._on_media_status(QMediaPlayer.MediaStatus.LoadedMedia)
    kinds = [c[0] for c in stub.calls]
    print(f"LoadedMedia 回调：{kinds}")
    assert "play" in kinds, f"LoadedMedia 时没有开始播放：{kinds}"
    assert "setPosition" not in kinds, "无待定位时不应 setPosition"
    print("② LoadedMedia（偏移 0）→ 调用了 play  ✓")

    # ---- 场景 3：带定位的跳转，应先 setPosition 再 play
    stub.calls.clear()
    stub._state = QMediaPlayer.PlaybackState.StoppedState
    ctl._want_playing = True
    ctl._pending_seek_ms = 1500
    ctl._on_media_status(QMediaPlayer.MediaStatus.LoadedMedia)
    kinds = [c[0] for c in stub.calls]
    print(f"跳转回调：{stub.calls}")
    assert "setPosition" in kinds and "play" in kinds, kinds
    assert kinds.index("setPosition") < kinds.index("play"), "应先定位再播放"
    assert stub.calls[0][1] == 1500, stub.calls
    print("③ 带定位跳转 → setPosition(1500) 后 play  ✓")

    # ---- 场景 4：暂停态按继续，不应重新加载
    stub.calls.clear()
    stub._state = QMediaPlayer.PlaybackState.PausedState
    ctl._want_playing = False
    ctl.play()
    kinds = [c[0] for c in stub.calls]
    print(f"暂停恢复：{kinds}")
    assert "play" in kinds and "setSource" not in kinds, kinds
    print("④ 暂停恢复 → 直接 play，不重新 setSource  ✓")

    # ---- 场景 5：左键选中（当前规格）—— 播放中不暂停、不动位置、不碰播放器
    stub.calls.clear()
    stub._state = QMediaPlayer.PlaybackState.PlayingState
    ctl._want_playing = True
    ctl.index = 0
    selected: list[int] = []
    ctl.sentenceSelected.connect(lambda i, t: selected.append(i))
    ctl.select_sentence(3)
    print(f"左键选中：index={ctl.index} 播放中={ctl._want_playing} 调用={[c[0] for c in stub.calls]}")
    assert ctl.index == 0, f"播放中选中不应改变播放位置：{ctl.index}"
    assert ctl._want_playing is True, "播放中选中不应暂停"
    assert selected and selected[-1] == 3, selected
    assert stub.calls == [], f"选中不应触发任何播放器操作：{stub.calls}"
    print("⑤ 播放中左键选中 → 不暂停、不动位置、不碰播放器  ✓")

    print("\n全部通过：点朗读会真正出声。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
