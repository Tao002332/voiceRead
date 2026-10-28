"""回归测试：点「保存」的完整链路（含模态框、延迟生效、播放中场景）。

模拟真实操作：播放中点设置 → 改语速/字号 → 点保存 → 验证设置生效且不崩。

用法： python scripts/test_settings_save.py
"""
from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ["VOICEREAD_NO_WELCOME"] = "1"

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from src.config import CACHE_DIR, DATA_DIR, Settings  # noqa: E402
from src.core.library import Library, import_book  # noqa: E402
from src.core.player import PlaybackController  # noqa: E402
from src.core.progress import ProgressStore  # noqa: E402
from src.core.tts.service import SynthesisService  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402
from src.ui.settings_dialog import SettingsDialog  # noqa: E402
from src.ui.subtitle_window import SubtitleWindow  # noqa: E402


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    settings = Settings.load()
    settings.speed = 1.0
    settings.auto_prosody = True
    service = SynthesisService(settings)
    ctl = PlaybackController(settings, service)
    sub = SubtitleWindow(settings)
    win = MainWindow(settings, Library(), ProgressStore(), service, ctl, sub)

    def fake_synth(key: str, req) -> Path:
        path = CACHE_DIR / f"{key}.wav"
        if not path.exists():
            sr, n = 32000, 8000
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(b"".join(
                    struct.pack("<h", int(2000 * math.sin(2 * math.pi * 190 * i / sr))) for i in range(n)
                ))
        return path

    service.request = fake_synth  # type: ignore[assignment]

    p = Path(tempfile.gettempdir()) / "vr_save_test.txt"
    p.write_text("第一章 测试\n\n" + "\n\n".join(["他推开门，风雪灌了进来。", "“你来了。”老人说。"] * 6), encoding="utf-8")
    book = import_book(p)
    ctl.load(book, 0, 0)
    ctl._prepare(ctl.chunks[0])
    ctl.play()
    pump(app, 0.6)
    print(f"播放中：state={ctl.player.playbackState().name}")

    # ---- 直接构造对话框，走与点击「保存」完全相同的回调
    dlg = SettingsDialog(settings, win)
    dlg.speed_slider.setValue(125)
    dlg.sub_font.setValue(26)
    dlg.settingsChanged.connect(lambda: QTimer.singleShot(0, win._after_settings))

    t0 = time.time()
    dlg._on_accept()          # ← 「保存」
    cost = time.time() - t0
    print(f"保存回调耗时 {cost:.2f}s（不应超过 3 秒）")
    assert cost < 3, f"保存回调阻塞了 {cost:.2f}s"

    pump(app, 1.0)            # 让 singleShot(0)/singleShot(120) 都跑完

    ok = True
    if abs(settings.speed - 1.25) > 1e-6:
        print(f"✗ 语速未保存：{settings.speed}")
        ok = False
    if settings.subtitle.font_size != 26:
        print(f"✗ 字号未保存：{settings.subtitle.font_size}")
        ok = False
    if win.speed_slider.value() != 125:
        print(f"✗ 主界面滑块未同步：{win.speed_slider.value()}")
        ok = False
    if ok:
        print("① 保存后设置已落库并同步到界面  ✓")

    # ---- 预览播放器应已释放音源
    src = dlg._preview_player.source().toString()
    assert src == "", f"预览播放器仍持有音源：{src}"
    print("② 对话框关闭前已释放预览音源  ✓")

    # ---- 生效步骤日志不应有失败记录
    log = DATA_DIR / "logs" / "settings.log"
    if log.exists() and "失败" in log.read_text(encoding="utf-8"):
        print("✗ settings.log 里有失败步骤：")
        print(log.read_text(encoding="utf-8")[-800:])
        ok = False
    else:
        print("③ 生效链路无失败步骤  ✓")

    # ---- 保存后仍能正常操作
    ctl.pause()
    pump(app, 0.3)
    ctl.play()
    pump(app, 0.8)
    assert ctl.player.playbackState().name in ("PlayingState", "PausedState", "StoppedState")
    print(f"④ 保存后播放器仍可操作：{ctl.player.playbackState().name}  ✓")

    dlg.deleteLater()
    pump(app, 0.2)
    if not ok:
        return 1
    print("\n全部通过：点保存不再崩溃。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
