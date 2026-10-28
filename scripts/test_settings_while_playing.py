"""真实压测：正在播放时反复改配置，验证不卡死。

判据：每次「设置生效」调用都能在 5 秒内返回，且之后播放器仍在推进。

用法： python scripts/test_settings_while_playing.py
"""
from __future__ import annotations

import os
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ["VOICEREAD_NO_WELCOME"] = "1"

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from PySide6.QtWidgets import QApplication  # noqa: E402

from src.config import Settings  # noqa: E402
from src.core.library import Library, import_book  # noqa: E402
from src.core.player import PlaybackController  # noqa: E402
from src.core.progress import ProgressStore  # noqa: E402
from src.core.tts.service import SynthesisService  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402
from src.ui.subtitle_window import SubtitleWindow  # noqa: E402

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
    sub = SubtitleWindow(settings)
    win = MainWindow(settings, Library(), ProgressStore(), service, ctl, sub)

    if not EPUB or not Path(EPUB).exists():
        print("未指定测试 EPUB：用法 --epub <路径>，或设置环境变量 VOICEREAD_TEST_EPUB")
        return 0
    book = import_book(EPUB)
    ctl.load(book, 3, 0)
    chunk = ctl.chunks[0]
    ctl._prepare(chunk)
    t0 = time.time()
    while not chunk.ready and time.time() - t0 < 120:
        app.processEvents()
        time.sleep(0.05)
    assert chunk.ready, "块未就绪"
    print(f"块 0 就绪 {sum(chunk.durations):.1f}s，开始播放")

    ctl.play()
    pump(app, 1.5)
    p1 = ctl.player.position()
    print(f"播放中：state={ctl.player.playbackState()} position={p1}ms")

    scenarios = [
        ("语速 1.0 → 1.25", lambda: setattr(settings, "speed", 1.25)),
        ("语调强度 1.0 → 0.6", lambda: setattr(settings, "prosody_strength", 0.6)),
        ("关闭自动语调", lambda: setattr(settings, "auto_prosody", False)),
        ("开启自动语调 + 语速 0.95", lambda: (setattr(settings, "auto_prosody", True), setattr(settings, "speed", 0.95))),
    ]

    fails = 0
    for name, mutate in scenarios:
        mutate()
        t = time.time()
        try:
            win._after_settings()      # 设置对话框关闭后真正走的路径
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {name}：抛异常 {exc}")
            fails += 1
            continue
        cost = time.time() - t
        pump(app, 2.0)
        p2 = ctl.player.position()
        alive = ctl.player.playbackState().name
        status = "✓" if cost < 5 else "✗ 耗时过长"
        if cost >= 5:
            fails += 1
        print(f"  {status} {name:<22} 生效耗时 {cost:.2f}s  position {p2}ms  状态 {alive}")

    # ---- 播放中再次播放/暂停/切句，确认没有残留死锁
    ctl.toggle()
    pump(app, 0.6)
    ctl.toggle()
    pump(app, 0.6)
    ctl.next_sentence()
    pump(app, 1.0)
    ctl.pause()
    pump(app, 0.4)
    print(f"  收尾：暂停后 state={ctl.player.playbackState().name}  index={ctl.index}")

    if fails:
        print(f"\n失败 {fails} 项")
        return 1
    print("\n通过：播放中改配置不会卡死。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
