"""真实播放验证：用真 QMediaPlayer 播一段真合成的音频，看播放位置是否前进。

这是最终判据——光「调用了 play()」不够，必须 position 真的在走。

用法： python scripts/test_real_play.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.config import Settings  # noqa: E402
from src.core.library import import_book  # noqa: E402
from src.core.player import PlaybackController  # noqa: E402
from src.core.tts.service import SynthesisService  # noqa: E402

EPUB = os.getenv("VOICEREAD_TEST_EPUB", "")


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    settings = Settings.load()
    service = SynthesisService(settings)
    ok, msg = service.probe()
    print(f"引擎自检 {'✓' if ok else '✗'} {msg}")
    if not ok:
        return 1

    ctl = PlaybackController(settings, service)
    if not EPUB or not Path(EPUB).exists():
        print("未指定测试 EPUB：用法 --epub <路径>，或设置环境变量 VOICEREAD_TEST_EPUB")
        return 0
    book = import_book(EPUB)
    ctl.load(book, 3, 0)
    print(f"章节《{book.chapter_title(3)}》 {len(ctl.sentences)} 句 / {len(ctl.chunks)} 块")

    chunk = ctl.chunks[0]
    t0 = time.time()
    ctl._prepare(chunk)
    while not chunk.ready and time.time() - t0 < 300:
        app.processEvents()
        time.sleep(0.05)
    assert chunk.ready, "块未就绪"
    print(f"块 0 就绪：{chunk.path.name}  {sum(chunk.durations):.1f}s  （含等待 {time.time() - t0:.1f}s）")

    print("\n点「朗读」…")
    ctl.index = 0
    ctl._want_playing = True
    ctl._start_chunk(chunk, 0)

    pump(app, 1.0)
    state = ctl.player.playbackState()
    pos = ctl.player.position()
    print(f"  1s 后：state={state}  position={pos}ms")

    samples: list[tuple[float, int]] = []
    for i in range(6):
        pump(app, 0.5)
        samples.append((time.time(), ctl.player.position()))
        print(f"  +{(i + 1) * 0.5:.1f}s：position={ctl.player.position()}ms  status={ctl.player.mediaStatus()}")

    positions = [p for _, p in samples]
    advanced = max(positions) - min(positions)
    print(f"\n位置推进：{advanced}ms")

    ctl.pause()
    pump(app, 0.3)
    paused_pos = ctl.player.position()
    pump(app, 0.6)
    still = ctl.player.position()
    print(f"暂停后位置：{paused_pos}ms → {still}ms（应基本不动）")
    ctl.stop()

    fails = 0
    if state != QMediaPlayer.PlaybackState.PlayingState:
        print("✗ 播放状态不是 Playing")
        fails += 1
    if advanced < 1500:
        print(f"✗ 位置几乎没有推进（{advanced}ms），可能没有真正出声")
        fails += 1
    if abs(still - paused_pos) > 400:
        print("✗ 暂停后位置仍在前进")
        fails += 1

    if fails:
        print(f"\n失败 {fails} 项")
        return 1
    print("\n✓ 真实播放通过：点朗读后音频在走、暂停能停住。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
