"""真实链路验证：预加载队列在真引擎下"就绪即播、边播边补"。

用法：
    set VOICEREAD_TEST_EPUB=D:\books\某本书.epub
    python scripts/test_real_queue.py --chapter 3 --seconds 40
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "scripts"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from _common import require_epub  # noqa: E402
from src.config import Settings  # noqa: E402
from src.core.library import import_book  # noqa: E402
from src.core.player import PlaybackController  # noqa: E402
from src.core.tts.service import SynthesisService  # noqa: E402


def pump(app: QApplication, seconds: float, sample: list | None = None) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if sample is not None:
            sample.append((time.time(), 0))
        time.sleep(0.02)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chapter", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=40.0)
    args = ap.parse_args()

    epub = require_epub()
    if not epub:
        return 0

    app = QApplication.instance() or QApplication([])
    settings = Settings.load()
    print(f"预加载队列深度 = {settings.prefetch}  引擎 = {settings.engine_id}")
    service = SynthesisService(settings)
    ok, msg = service.probe()
    print(f"引擎自检 {'✓' if ok else '✗'} {msg}")
    if not ok:
        return 1

    ctl = PlaybackController(settings, service)
    book = import_book(epub)
    ctl.load(book, args.chapter, 0)
    print(f"章节《{book.chapter_title(args.chapter)}》 {len(ctl.sentences)} 句")

    t0 = time.time()
    marks: dict = {"first_ready": None}

    def _on_loading(loading: bool) -> None:
        if not loading and marks["first_ready"] is None:
            marks["first_ready"] = time.time()

    ctl.loadingChanged.connect(_on_loading)
    ctl.play()

    positions: list[tuple[float, int]] = []
    deadline = time.time() + args.seconds
    while time.time() < deadline:
        app.processEvents()
        positions.append((time.time() - t0, ctl.player.position()))
        time.sleep(0.05)

    first_ready = marks["first_ready"]
    advanced = (max(p for _, p in positions) - min(p for _, p in positions)) if positions else 0
    print(f"\n首条就绪耗时 : {('%.1f s' % (first_ready - t0)) if first_ready else '未就绪'}")
    print(f"播放位置推进 : {advanced} ms")
    print(f"已播条数     : {ctl._played_total}")
    print(f"队列状态     : {len(ctl._items)} 条排队，其中可播 {ctl._ready_ahead()} 条，"
          f"生成中 {ctl._running_count()} 条")
    for row in ctl.recent_items(5):
        print(f"   第 {row['index']:>3} 句  {row['state']:<6} {row['chars']:>3} 字  {row['cost']:>5.2f}s")

    fails = 0
    if advanced < 2000:
        print("✗ 播放位置几乎没推进")
        fails += 1
    if ctl._played_total < 1:
        print("✗ 没有消费任何队列条目")
        fails += 1
    if ctl._ready_ahead() < 1 and ctl._played_total < 2:
        print("✗ 队列没有持续补充")
        fails += 1

    ctl.pause()
    if fails:
        print(f"\n失败 {fails} 项")
        return 1
    print("\n✓ 真实流水线通过：就绪即播、边播边补、耗时可见。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
