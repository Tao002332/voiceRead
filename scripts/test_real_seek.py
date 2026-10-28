"""真实播放器验证：右键「从这一句开始朗读」是否真的从该句开始。

这是本次 bug 的最终判据——之前 seek 被 Windows/FFmpeg 后端丢弃，
播放位置会落回块首（本段第一句）。这里直接看真实 position。

用法： python scripts/test_real_seek.py
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

    chunk = ctl.chunks[0]
    ctl._prepare(chunk)
    t0 = time.time()
    while not chunk.ready and time.time() - t0 < 300:
        app.processEvents()
        time.sleep(0.05)
    assert chunk.ready, "块未就绪"

    fails = 0
    for target in (2, 6, 9):
        if target >= chunk.end:
            continue
        expect_ms = int(chunk.starts[target - chunk.start] * 1000)
        ctl._want_playing = False
        ctl.seek_sentence(target, autoplay=True)
        pump(app, 1.2)                      # 只播 1.2 秒，偏差应 ≈ 播放量
        pos = ctl.player.position()
        drift = pos - expect_ms
        state = ctl.player.playbackState().name
        # 位置应落在 [目标-0.3s, 目标+2s]：既不能回落到块首，也不该跑过头
        good = -300 <= drift <= 2000 and ctl.index >= target
        verdict = "✓" if good else "✗"
        if not good:
            fails += 1
        print(f"  {verdict} 目标第 {target} 句 期望 {expect_ms}ms → 实际 {pos}ms "
              f"(偏差 {drift:+d}ms)  index={ctl.index}  {state}")
        ctl.pause()
        pump(app, 0.3)

    if fails:
        print(f"\n失败 {fails} 项：seek 没有落到目标附近")
        return 1
    print("\n✓ 真实 seek 通过：从选中的那一句开始播放，不再回落到本段开头。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
