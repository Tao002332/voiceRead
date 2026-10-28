"""应用装配：QApplication、托盘、悬浮字幕、全局热键、单实例、断点续读。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from .config import Settings
from .core.library import Library
from .core.player import PlaybackController, prune_chunk_cache
from .core.progress import ProgressStore
from .core.tts.service import SynthesisService
from .ui.hotkeys import MOD_ALT, MOD_CONTROL, GlobalHotkeys
from .ui.main_window import MainWindow
from .ui.subtitle_window import SubtitleWindow
from .ui.theme import QSS, make_app_icon, make_tray_icon

SERVER_NAME = "VoiceRead.SingleInstance.v1"


class VoiceReadApp:
    def __init__(self, app: QApplication) -> None:
        self.app = app
        self.app.setApplicationName("语音书架")
        self.app.setOrganizationName("voiceRead")
        self.app.setApplicationDisplayName("语音书架")
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setStyleSheet(QSS)
        self.app.setWindowIcon(make_app_icon())

        self.settings = Settings.load()
        self.library = Library()
        self.progress = ProgressStore()
        self.service = SynthesisService(self.settings)
        self.app.aboutToQuit.connect(self.service.shutdown)
        self.controller = PlaybackController(self.settings, self.service)
        self.subtitle = SubtitleWindow(self.settings)

        self.window = MainWindow(
            self.settings, self.library, self.progress, self.service,
            self.controller, self.subtitle,
        )
        self.window.hiddenToTray.connect(self._on_hidden_to_tray)

        self._build_tray()
        self._register_hotkeys()

        self.controller.sentenceChanged.connect(lambda *_: self._update_tray_text())

        self._apply_visibility()
        self._start_mambo_if_needed()
        self._maybe_first_run()
        QTimer.singleShot(1500, self._warmup_engine)

        # 缓存维护：每 60 秒清理超过保留时长的音频（默认 10 分钟）
        self._maintenance = QTimer(self.app)
        self._maintenance.setInterval(60_000)
        self._maintenance.timeout.connect(self._maintain_cache)
        self._maintenance.start()
        QTimer.singleShot(4000, self._maintain_cache)

        if "--tray" not in sys.argv:
            self.window.show()

        # 自检模式：VOICEREAD_SELFTEST_SECONDS=9 时，N 秒后自动退出（给 CI/冒烟用）
        selftest = os.environ.get("VOICEREAD_SELFTEST_SECONDS", "")
        if selftest:
            try:
                QTimer.singleShot(int(float(selftest) * 1000), self.app.quit)
            except (TypeError, ValueError):
                pass

    # ================================================================ 托盘
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_tray_icon(), self.app)
        self.tray.setToolTip("语音书架")
        menu = QMenu()
        self.act_show = menu.addAction("显示主界面")
        self.act_show.triggered.connect(self._restore_ui)

        self.act_sub = menu.addAction("显示悬浮字幕")
        self.act_sub.setCheckable(True)
        self.act_sub.setChecked(bool(self.settings.subtitle.visible))
        self.act_sub.toggled.connect(self.window.set_subtitle_visible)

        menu.addSeparator()
        self.act_play = menu.addAction("播放 / 暂停")
        self.act_play.triggered.connect(self.controller.toggle)
        act_next = menu.addAction("下一句")
        act_next.triggered.connect(lambda: self.controller.next_sentence())
        act_prev = menu.addAction("上一句")
        act_prev.triggered.connect(lambda: self.controller.prev_sentence())

        menu.addSeparator()
        act_settings = menu.addAction("设置…")
        act_settings.triggered.connect(self.window.open_settings)
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self.quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.setVisible(QSystemTrayIcon.isSystemTrayAvailable())

    def _restore_ui(self) -> None:
        """从托盘/悬浮球恢复界面；若字幕是被自动收起的，顺手放回来。"""
        self.window.show_and_raise()
        if getattr(self, "_subtitle_auto_hidden", False):
            self._subtitle_auto_hidden = False
            self.window.set_subtitle_visible(True)

    def _on_tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._restore_ui()

    def _update_tray_text(self) -> None:
        text = self.controller.sentences[self.controller.index].text if self.controller.sentences else ""
        if text:
            self.tray.setToolTip(f"语音书架 · {text[:28]}")

    def statusbar_hint(self, text: str) -> None:
        """在底栏给用户一句提示（同时托盘冒个气泡）。"""
        try:
            self.window.status_label.setText(text)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.tray.isVisible():
                self.tray.showMessage("语音书架", text, make_tray_icon(), 3000)
        except Exception:  # noqa: BLE001
            pass

    def _on_hidden_to_tray(self) -> None:
        self.window.hide()
        if self.settings.subtitle.visible:
            self.statusbar_hint("主界面已收起，字幕继续显示；入口在托盘图标")

    # ================================================================ 热键
    def _register_hotkeys(self) -> None:
        self.hotkeys = GlobalHotkeys(int(self.window.winId()))
        mapping = [
            ("播放/暂停", MOD_CONTROL | MOD_ALT, "P", self.controller.toggle),
            ("字幕开关", MOD_CONTROL | MOD_ALT, "F", self._toggle_subtitle),
            ("上一句", MOD_CONTROL | MOD_ALT, "left", lambda: self.controller.prev_sentence()),
            ("下一句", MOD_CONTROL | MOD_ALT, "right", lambda: self.controller.next_sentence()),
            ("加速", MOD_CONTROL | MOD_ALT, "up", lambda: self._nudge_speed(+0.05)),
            ("减速", MOD_CONTROL | MOD_ALT, "down", lambda: self._nudge_speed(-0.05)),
        ]
        for label, mods, key, callback in mapping:
            try:
                self.hotkeys.register(label, mods, key, callback)
            except Exception:  # noqa: BLE001
                continue

    def _nudge_speed(self, delta: float) -> None:
        self.window.set_speed(self.settings.speed + delta)

    def _toggle_subtitle(self) -> None:
        self.window.set_subtitle_visible(not self.settings.subtitle.visible)

    # ================================================================ 启动
    def _apply_visibility(self) -> None:
        if self.settings.subtitle.visible:
            self.subtitle.apply_settings()
            self.subtitle.show()
        self.window.refresh_engine_combo()
        self.window.set_speed(self.settings.speed)
        self.act_sub.blockSignals(True)
        self.act_sub.setChecked(bool(self.settings.subtitle.visible))
        self.act_sub.blockSignals(False)

    def _start_mambo_if_needed(self) -> None:
        m = self.settings.mambo
        if not (m.auto_start and m.start_command):
            return
        from .core.tts.registry import build_engine

        try:
            if build_engine("mambo", self.settings).probe()[0]:
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            workdir = getattr(m, "workdir", "") or ""
            if not (workdir and Path(workdir).is_dir()):
                workdir = None
            # 结构化启动：命令只负责进程，工作目录交给 Popen，
            # 避免 "cmd /c cd /d ... && ..." 在某些环境下静默失败或跑到别的 python
            # 显式断开 stdio：否则子进程会一直持有父进程的管道句柄，调用方永远等不到 EOF
            subprocess.Popen(
                m.start_command,
                shell=True,
                cwd=workdir,
                creationflags=flags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            pass

    def _warmup_engine(self) -> None:
        """预热：首次合成要加载 g2pw / CUDA kernel，实测首句要 90 秒。

        启动后先在后台合成一句极短的问候，用户真正点播放时就是秒开。
        非曼波引擎不需要（Edge 首句约 1 秒）。
        """
        if self.settings.engine_id != "mambo":
            return
        import tempfile
        import threading

        from .core.tts.base import SpeechRequest

        def _work() -> None:
            try:
                req = SpeechRequest(
                    text="你好。",
                    rate=1.0,
                    pitch=0,
                    voice=str(self.settings.mambo.active_preset),
                )
                out = Path(tempfile.gettempdir()) / "voiceread_warmup.wav"
                self.service.engine.synthesize(req, out)
            except Exception:  # noqa: BLE001 - 预热失败不应打扰用户
                pass

        threading.Thread(target=_work, name="tts-warmup", daemon=True).start()

    def _maintain_cache(self) -> None:
        """按时间清理音频缓存，只保留最近 N 分钟。"""
        try:
            minutes = int(getattr(self.settings, "cache_retention_minutes", 10))
            removed = self.service.prune_by_age(minutes) + prune_chunk_cache(minutes)
            if removed:
                self.window.status_label.setText(
                    f"已清理过期语音缓存 {removed} 个（保留 {minutes} 分钟）"
                )
        except Exception:  # noqa: BLE001 - 维护任务失败不影响播放
            pass

    def _maybe_first_run(self) -> None:
        if os.environ.get("VOICEREAD_NO_WELCOME"):
            self.settings.first_run_done = True
            self.settings.save()
            return
        if self.settings.first_run_done:
            return
        self.settings.first_run_done = True
        self.settings.save()
        QTimer.singleShot(500, self._show_welcome)

    def _show_welcome(self) -> None:
        box = QMessageBox(self.window)
        box.setWindowTitle("欢迎使用语音书架")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("三步开始听书")
        box.setInformativeText(
            "1. 点左侧「导入 TXT / EPUB」，或把文件拖进窗口\n"
            "2. 默认用免费 Edge 语音，点「▶ 朗读」即可听\n"
            "3. 想用曼波音色：设置 → 语音引擎 → 曼波本地服务，填参考音频并启动 GPT-SoVITS\n\n"
            "隐藏窗口后，右下角悬浮球就是入口；单击字幕可暂停/继续，滚轮调语速。"
        )
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    # ================================================================ 退出
    def quit(self) -> None:
        try:
            self.settings.save()
            if self.window._current_book:
                self.progress.set(
                    self.window._current_book.id,
                    self.controller.chapter_index,
                    self.controller.index,
                    self.controller.progress_percent(),
                )
        except Exception:  # noqa: BLE001
            pass
        try:
            self.hotkeys.unregister_all()
            self.service.shutdown()
            self.tray.hide()
        except Exception:  # noqa: BLE001
            pass
        self.app.quit()

    def run(self) -> int:
        return self.app.exec()


def _already_running() -> bool:
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    QApplication.setApplicationName("语音书架")

    if _already_running():
        return 0

    server = QLocalServer()
    QLocalServer.removeServer(SERVER_NAME)
    server.listen(SERVER_NAME)

    instance = VoiceReadApp(app)

    def _on_new_connection() -> None:
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.readyRead.connect(lambda: (conn.readAll(), instance.window.show_and_raise()))
        instance.window.show_and_raise()

    server.newConnection.connect(_on_new_connection)
    return instance.run()


if __name__ == "__main__":
    sys.exit(main())
