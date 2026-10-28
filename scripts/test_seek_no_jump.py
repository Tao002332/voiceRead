"""回归测试：右键「从这一句开始朗读」不得跳回本段更早的句子。

曾经的 bug：setPosition() 异步生效，而位置轮询在「已请求定位、尚未生效」
的间隙读到 position=0，把当前句映射回本段第一句（看起来像随机乱跳）。

用法： python scripts/test_seek_no_jump.py
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
    """可控 position / mediaStatus 的假播放器。"""

    def __init__(self) -> None:
        super().__init__()
        self.pos = 0
        self.status = QMediaPlayer.MediaStatus.NoMedia
        self.state = QMediaPlayer.PlaybackState.StoppedState
        self.seeks: list[int] = []
        self.calls: list[str] = []
        self.ignore_seek = False

    def setSource(self, url) -> None:  # noqa: N802
        self.calls.append("setSource")
        self.pos = 0
        self.status = QMediaPlayer.MediaStatus.LoadedMedia

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
        self.calls.append("setPosition")
        self.seeks.append(ms)
        if not self.ignore_seek:      # ignore_seek=True 用来模拟「后端丢掉 seek」
            self.pos = ms


def main() -> int:
    app = QApplication.instance() or QApplication([])  # noqa: F841
    settings = Settings.load()
    service = SynthesisService(settings)
    ctl = PlaybackController(settings, service)

    def fake_synth(key: str, req) -> Path:
        path = CACHE_DIR / f"{key}.wav"
        if not path.exists():
            sr, n = 32000, 16000
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(b"".join(
                    struct.pack("<h", int(1800 * math.sin(2 * math.pi * 210 * i / sr))) for i in range(n)
                ))
        return path

    service.request = fake_synth  # type: ignore[assignment]

    p = Path(tempfile.gettempdir()) / "vr_seek_test.txt"
    p.write_text("第一章 测试\n\n" + "\n\n".join(["他推开门，风雪灌了进来。", "“你来了。”老人说。"] * 12), encoding="utf-8")
    book = import_book(p)
    ctl.load(book, 0, 0)

    stub = StubPlayer()
    ctl.player = stub  # type: ignore[assignment]

    chunk = ctl.chunks[0]
    ctl._prepare(chunk)
    assert chunk.ready, "块未就绪"

    target = 6                      # 本段中间的一句
    assert target < chunk.end, (target, chunk.end)
    expect_ms = int(chunk.starts[target - chunk.start] * 1000)
    print(f"目标：块 {chunk.index} 的第 {target} 句，块内偏移 {expect_ms}ms")

    # ---- 播放中右键「从这一句开始朗读」
    ctl._want_playing = True
    stub.calls.clear()
    ctl.seek_sentence(target, autoplay=True)
    assert ctl.index == target, ctl.index
    assert "play" not in stub.calls, f"有偏移时不应先 play（会被后端丢掉 seek）：{stub.calls}"
    print(f"① 选中后 index={ctl.index}，且未抢先 play  ✓")

    # 全新加载：媒体还没就绪，position=0，此时轮询不得改 index
    stub.pos = 0
    stub.status = QMediaPlayer.MediaStatus.NoMedia
    ctl._on_tick()
    assert ctl.index == target, f"NoMedia 时被改成 {ctl.index}"
    stub.status = QMediaPlayer.MediaStatus.LoadingMedia
    ctl._on_tick()
    assert ctl.index == target, f"LoadingMedia 时被改成 {ctl.index}"
    print("② 加载中（position=0）不覆盖选中句  ✓")

    # 媒体就绪：控制器应请求定位到目标，且顺序是「先定位再播放」
    stub.status = QMediaPlayer.MediaStatus.LoadedMedia
    stub.calls.clear()
    ctl._on_media_status(QMediaPlayer.MediaStatus.LoadedMedia)
    assert stub.seeks and stub.seeks[-1] == expect_ms, stub.seeks
    assert stub.calls[:2] == ["setPosition", "play"], stub.calls
    print(f"③ 已请求定位到 {stub.seeks[-1]}ms，顺序 setPosition→play  ✓")

    # setPosition 尚未生效：position 仍是 0，轮询必须继续按住
    stub.pos = 0
    ctl._on_tick()
    assert ctl.index == target, f"定位未生效时被改成 {ctl.index}（这就是原来的乱跳）"
    print("④ 定位未生效（position 仍为 0）不乱跳  ✓")

    # 定位生效：position 到达目标附近，允许按位置映射
    stub.pos = expect_ms + 40
    ctl._on_tick()
    assert ctl.index == target, f"定位生效后应停在 {target}，实际 {ctl.index}"
    print("⑤ 定位生效后仍锁定在同句  ✓")

    # 正常推进：位置往后走，应该跟着走到下一句
    nxt_ms = int(chunk.starts[target - chunk.start + 1] * 1000) + 50
    stub.pos = nxt_ms
    ctl._on_tick()
    assert ctl.index == target + 1, f"应推进到 {target + 1}，实际 {ctl.index}"
    print(f"⑥ 位置推进后可正常跟到第 {ctl.index} 句  ✓")

    # ---- 左键选中：只发 sentenceSelected，绝不发 sentenceChanged（字幕不动）
    changed: list[int] = []
    selected: list[int] = []
    ctl.sentenceChanged.connect(lambda i, t, p: changed.append(i))
    ctl.sentenceSelected.connect(lambda i, t: selected.append(i))
    ctl._want_playing = False
    ctl.select_sentence(4)
    assert ctl.index == 4, ctl.index
    assert selected and selected[-1] == 4, selected
    assert not changed, f"左键选中不该更新悬浮字幕，却发了 sentenceChanged：{changed}"
    print("⑦ 左键选中只发 selection，不发 sentenceChanged（字幕不动）  ✓")

    # ---- 播放中左键选中：不暂停、不改播放位置、不发 sentenceChanged
    ctl._want_playing = True
    ctl.index = 6
    changed_before = len(changed)
    ctl.select_sentence(2)
    assert ctl._want_playing is True, "播放中左键选中不应暂停朗读"
    assert ctl.index == 6, f"播放中左键选中不应改变播放位置，实际 {ctl.index}"
    assert selected and selected[-1] == 2, selected
    assert len(changed) == changed_before, "播放中选中不该发 sentenceChanged"
    print("⑧ 播放中左键选中：不暂停、不动播放位置、不动字幕  ✓")

    # ---- seek 被后端丢弃时：自动重试，两次失败后退回块首并提示
    import time as _time

    ctl._want_playing = True
    ctl._awaiting_seek = 5000
    ctl._awaiting_since = _time.time()
    ctl._seek_retries = 0
    stub.pos = 0
    stub.ignore_seek = True       # 模拟后端把 seek 丢掉：position 不动
    base_seeks = len(stub.seeks)
    ctl._verify_seek()
    assert len(stub.seeks) == base_seeks + 1 and ctl._seek_retries == 1, ctl._seek_retries
    ctl._verify_seek()
    assert ctl._seek_retries == 2, ctl._seek_retries
    msgs: list[str] = []
    ctl.statusChanged.connect(msgs.append)
    ctl._verify_seek()
    assert ctl._awaiting_seek is None, "两次重试后应放弃等待"
    assert any("定位未生效" in m for m in msgs), msgs
    print("⑨ seek 被丢弃 → 重试 2 次 → 放弃并提示  ✓")

    print("\n全部通过：选句朗读不会跳回本段更早的句子。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
