"""设置对话框：语音引擎、曼波服务、字幕外观、缓存与高级选项。"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSlider, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from ..config import DATA_DIR, Settings
from ..core.tts.registry import all_engine_meta, build_engine


class _PreviewJob:
    """极简任务句柄，替代 ThreadPoolExecutor.Future（后者是非 daemon 线程）。"""

    __slots__ = ("done", "result", "error")

    def __init__(self) -> None:
        self.done = False
        self.result = None
        self.error = None


class SettingsDialog(QDialog):
    settingsChanged = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("设置")
        self.setMinimumWidth(620)
        self.resize(680, 640)

        self._preview_player = QMediaPlayer(self)
        self._preview_audio = QAudioOutput(self)
        self._preview_player.setAudioOutput(self._preview_audio)
        self._preview_audio.setVolume(0.9)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(250)
        self._poll_timer.timeout.connect(self._poll_preview)
        self._preview_future = None

        tabs = QTabWidget(self)
        tabs.addTab(self._build_voice_tab(), "语音引擎")
        tabs.addTab(self._build_subtitle_tab(), "字幕外观")
        tabs.addTab(self._build_advanced_tab(), "高级与缓存")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)
        self._load_values()

    # ================================================================ 语音页
    def _build_voice_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setSpacing(10)

        box = QGroupBox("引擎与音色")
        form = QFormLayout(box)

        self.engine_combo = QComboBox()
        for eid, label, desc in all_engine_meta():
            self.engine_combo.addItem(label, eid)
            self.engine_combo.setItemData(self.engine_combo.count() - 1, desc, Qt.ItemDataRole.ToolTipRole)
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        form.addRow("语音引擎", self.engine_combo)

        self.engine_status = QLabel("—")
        self.engine_status.setObjectName("hint")
        self.engine_status.setWordWrap(True)
        form.addRow("引擎状态", self.engine_status)

        self.voice_combo = QComboBox()
        self.voice_combo.currentIndexChanged.connect(self._on_voice_changed)
        form.addRow("音色", self.voice_combo)

        self.description_label = QLabel("")
        self.description_label.setObjectName("hint")
        self.description_label.setWordWrap(True)
        form.addRow("", self.description_label)
        root.addWidget(box)

        # ---- 语速 / 语调
        prosody_box = QGroupBox("语速与语调")
        form2 = QFormLayout(prosody_box)

        speed_row = QHBoxLayout()
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(50, 200)
        self.speed_slider.setSingleStep(5)
        self.speed_value = QLabel("1.00×")
        self.speed_value.setFixedWidth(56)
        self.speed_slider.valueChanged.connect(
            lambda v: self.speed_value.setText(f"{v / 100:.2f}×")
        )
        speed_row.addWidget(self.speed_slider, 1)
        speed_row.addWidget(self.speed_value)
        form2.addRow("基础语速", self._wrap(speed_row))

        pitch_row = QHBoxLayout()
        self.pitch_slider = QSlider(Qt.Orientation.Horizontal)
        self.pitch_slider.setRange(-20, 20)
        self.pitch_value = QLabel("0 Hz")
        self.pitch_value.setFixedWidth(56)
        self.pitch_slider.valueChanged.connect(
            lambda v: self.pitch_value.setText(f"{v:+d} Hz" if v else "0 Hz")
        )
        pitch_row.addWidget(self.pitch_slider, 1)
        pitch_row.addWidget(self.pitch_value)
        form2.addRow("基础音调", self._wrap(pitch_row))

        self.auto_prosody = QCheckBox("根据上下文自动调整语调（问句上扬、感叹加快、对话/旁白区分）")
        self.auto_prosody.setChecked(True)
        form2.addRow("", self.auto_prosody)

        strength_row = QHBoxLayout()
        self.strength_slider = QSlider(Qt.Orientation.Horizontal)
        self.strength_slider.setRange(0, 200)
        self.strength_value = QLabel("100%")
        self.strength_value.setFixedWidth(56)
        self.strength_slider.valueChanged.connect(
            lambda v: self.strength_value.setText(f"{v}%")
        )
        strength_row.addWidget(self.strength_slider, 1)
        strength_row.addWidget(self.strength_value)
        form2.addRow("语调强度", self._wrap(strength_row))

        self.prefetch_spin = QSpinBox()
        self.prefetch_spin.setRange(1, 30)
        self.prefetch_spin.setToolTip(
            "预加载队列深度：提前把后面多少句排进队列去合成。\n"
            "已经生成好的部分会立刻播放，不需要等队列填满"
        )
        form2.addRow("预加载队列深度", self.prefetch_spin)

        test_row = QHBoxLayout()
        self.test_btn = QPushButton("试听当前音色")
        self.test_btn.clicked.connect(self._on_test_voice)
        self.test_status = QLabel("")
        self.test_status.setObjectName("hint")
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_status, 1)
        form2.addRow("", self._wrap(test_row))
        root.addWidget(prosody_box)

        # ---- 曼波本地服务
        self.mambo_box = QGroupBox("曼波本地服务（GPT-SoVITS）")
        mform = QFormLayout(self.mambo_box)

        mrow = QHBoxLayout()
        self.mambo_url = QLineEdit()
        self.mambo_url.setPlaceholderText("http://127.0.0.1:9880")
        self.mambo_check_btn = QPushButton("测试连接")
        self.mambo_check_btn.clicked.connect(self._on_mambo_check)
        mrow.addWidget(self.mambo_url, 1)
        mrow.addWidget(self.mambo_check_btn)
        mform.addRow("服务地址", self._wrap(mrow))

        self.mambo_status = QLabel("—")
        self.mambo_status.setObjectName("hint")
        self.mambo_status.setWordWrap(True)
        mform.addRow("连接状态", self.mambo_status)

        prow = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        add_preset = QPushButton("新增音色")
        add_preset.clicked.connect(self._on_add_preset)
        del_preset = QPushButton("删除")
        del_preset.clicked.connect(self._on_del_preset)
        prow.addWidget(self.preset_combo, 1)
        prow.addWidget(add_preset)
        prow.addWidget(del_preset)
        mform.addRow("音色预设", self._wrap(prow))

        self.preset_name = QLineEdit()
        mform.addRow("预设名称", self.preset_name)

        ref_row = QHBoxLayout()
        self.ref_audio = QLineEdit()
        self.ref_audio.setPlaceholderText("曼波参考音频（wav，3~10 秒最稳）")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._on_browse_ref)
        ref_row.addWidget(self.ref_audio, 1)
        ref_row.addWidget(browse)
        mform.addRow("参考音频", self._wrap(ref_row))

        self.prompt_text = QLineEdit()
        self.prompt_text.setPlaceholderText("参考音频里说的那句话（必须完全一致）")
        mform.addRow("参考文本", self.prompt_text)

        self.auto_start = QCheckBox("启动软件时自动拉起曼波服务")
        mform.addRow("", self.auto_start)

        self.start_command = QLineEdit()
        self.start_command.setPlaceholderText(
            r'例：D:\GPT-SoVITS\runtime\python.exe D:\GPT-SoVITS\api_v2.py -a 127.0.0.1 -p 9880'
        )
        mform.addRow("启动命令", self.start_command)

        hint = QLabel(
            "曼波音色由参考音频决定：换一段参考音频就是换一个人声。"
            "服务端需运行 GPT-SoVITS 的 api_v2.py（详见 docs/曼波模型部署.md）。"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        mform.addRow("", hint)
        root.addWidget(self.mambo_box)
        root.addStretch(1)
        return page

    # ================================================================ 字幕页
    def _build_subtitle_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        box = QGroupBox("字幕窗")
        form = QFormLayout(box)

        self.sub_font = QSpinBox()
        self.sub_font.setRange(12, 56)
        form.addRow("字号", self.sub_font)

        self.sub_width = QSpinBox()
        self.sub_width.setRange(320, 1500)
        self.sub_width.setSingleStep(20)
        form.addRow("宽度", self.sub_width)

        self.sub_opacity = QSlider(Qt.Orientation.Horizontal)
        self.sub_opacity.setRange(10, 100)
        form.addRow("背景不透明度", self.sub_opacity)

        self.sub_lines = QSpinBox()
        self.sub_lines.setRange(1, 4)
        form.addRow("最多行数", self.sub_lines)

        self.sub_align = QComboBox()
        for key, label in (("left", "左对齐"), ("center", "居中"), ("right", "右对齐")):
            self.sub_align.addItem(label, key)
        form.addRow("对齐", self.sub_align)

        color_row = QHBoxLayout()
        self.fg_btn = QPushButton("文字色")
        self.fg_btn.clicked.connect(lambda: self._pick_color("fg_color", self.fg_btn))
        self.hl_btn = QPushButton("高亮色")
        self.hl_btn.clicked.connect(lambda: self._pick_color("highlight_color", self.hl_btn))
        self.bg_btn = QPushButton("背景色")
        self.bg_btn.clicked.connect(lambda: self._pick_color("bg_color", self.bg_btn))
        for b in (self.fg_btn, self.hl_btn, self.bg_btn):
            color_row.addWidget(b)
        form.addRow("配色", self._wrap(color_row))

        self.sub_context = QCheckBox("显示上一句 / 下一句")
        form.addRow("", self.sub_context)

        self.sub_through = QCheckBox("鼠标穿透（点不到字幕，只能从托盘/悬浮球操作）")
        form.addRow("", self.sub_through)
        root.addWidget(box)

        hint = QLabel("字幕窗在本对话框关闭后按新外观重绘；位置可用鼠标直接拖动，会自动记住。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        root.addWidget(hint)
        root.addStretch(1)
        return page

    # ================================================================ 高级页
    def _build_advanced_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        cache_box = QGroupBox("缓存")
        cform = QFormLayout(cache_box)
        self.cache_limit = QSpinBox()
        self.cache_limit.setRange(100, 8000)
        self.cache_limit.setSingleStep(100)
        self.cache_limit.setSuffix(" MB")
        cform.addRow("大小上限", self.cache_limit)

        self.cache_retention = QSpinBox()
        self.cache_retention.setRange(1, 720)
        self.cache_retention.setSuffix(" 分钟")
        self.cache_retention.setToolTip("超过这个时长的合成音频会被自动删除，只保留最近生成的")
        cform.addRow("保留时长", self.cache_retention)

        row = QHBoxLayout()
        self.cache_label = QLabel("—")
        clear_btn = QPushButton("清空缓存")
        clear_btn.setObjectName("danger")
        clear_btn.clicked.connect(self._on_clear_cache)
        row.addWidget(self.cache_label, 1)
        row.addWidget(clear_btn)
        cform.addRow("当前占用", self._wrap(row))
        root.addWidget(cache_box)

        data_box = QGroupBox("数据位置")
        dform = QFormLayout(data_box)
        path_label = QLabel(str(DATA_DIR))
        path_label.setObjectName("hint")
        path_label.setWordWrap(True)
        dform.addRow("目录", path_label)
        open_btn = QPushButton("打开数据目录")
        open_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(DATA_DIR)))
        )
        dform.addRow("", open_btn)
        root.addWidget(data_box)

        keys_box = QGroupBox("快捷键")
        kform = QFormLayout(keys_box)
        for k, v in (
            ("Ctrl+Alt+P", "播放 / 暂停"),
            ("Ctrl+Alt+F", "显示 / 隐藏字幕"),
            ("Ctrl+Alt+←", "上一句"),
            ("Ctrl+Alt+→", "下一句"),
            ("Ctrl+Alt+↑ / ↓", "语速 +/-"),
            ("滚轮（字幕上）", "语速微调"),
            ("单击字幕 / 悬浮球", "播放暂停 / 切换字幕"),
        ):
            kform.addRow(QLabel(k), QLabel(v))
        root.addWidget(keys_box)
        root.addStretch(1)
        return page

    # ================================================================ 工具
    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)
        return w

    # ================================================================ 载入
    def _load_values(self) -> None:
        s = self.settings

        idx = self.engine_combo.findData(s.engine_id)
        self.engine_combo.setCurrentIndex(max(0, idx))
        self._refresh_voices()

        self.speed_slider.setValue(int(round(s.speed * 100)))
        self.speed_value.setText(f"{s.speed:.2f}×")
        self.pitch_slider.setValue(int(s.pitch))
        self.pitch_value.setText(f"{s.pitch:+d} Hz" if s.pitch else "0 Hz")
        self.auto_prosody.setChecked(bool(s.auto_prosody))
        self.strength_slider.setValue(int(round(s.prosody_strength * 100)))
        self.strength_value.setText(f"{int(round(s.prosody_strength * 100))}%")
        self.prefetch_spin.setValue(int(s.prefetch))

        self.mambo_url.setText(s.mambo.base_url)
        self.auto_start.setChecked(bool(s.mambo.auto_start))
        self.start_command.setText(s.mambo.start_command)
        self._refresh_presets()

        sub = s.subtitle
        self.sub_font.setValue(int(sub.font_size))
        self.sub_width.setValue(int(sub.width))
        self.sub_opacity.setValue(int(sub.opacity))
        self.sub_lines.setValue(int(sub.max_lines))
        ai = self.sub_align.findData(getattr(sub, "text_align", "center"))
        self.sub_align.setCurrentIndex(max(0, ai))
        self.sub_context.setChecked(bool(sub.show_context))
        self.sub_through.setChecked(bool(sub.click_through))
        self._paint_color_buttons()

        self.cache_limit.setValue(int(s.cache_limit_mb))
        self.cache_retention.setValue(int(getattr(s, "cache_retention_minutes", 10)))
        self._refresh_cache_label()
        self._update_mambo_box_visibility()
        self._refresh_engine_status()

    # ---------------- 引擎 ----------------
    def _current_engine_id(self) -> str:
        return self.engine_combo.currentData() or "edge"

    def _on_engine_changed(self) -> None:
        self._refresh_voices()
        self._update_mambo_box_visibility()
        self._refresh_engine_status()

    def _update_mambo_box_visibility(self) -> None:
        self.mambo_box.setVisible(self._current_engine_id() == "mambo")

    def _refresh_engine_status(self) -> None:
        try:
            engine = build_engine(self._current_engine_id(), self.settings)
            ok, msg = engine.probe()
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, str(exc)
        prefix = "✓ " if ok else "✗ "
        self.engine_status.setText(prefix + (msg or ("可用" if ok else "不可用")))

    def _refresh_voices(self) -> None:
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        eid = self._current_engine_id()
        engine = build_engine(eid, self.settings)
        voices = []
        try:
            voices = engine.voices()
        except Exception:  # noqa: BLE001
            voices = []
        if not voices:
            voices = [("", "（使用引擎默认音色）")]
        for vid, name in voices:
            self.voice_combo.addItem(name, vid)
        saved = {
            "edge": self.settings.edge_voice,
            "sapi": self.settings.sapi_voice,
            "mambo": str(self.settings.mambo.active_preset),
        }.get(eid, "")
        pos = self.voice_combo.findData(saved)
        self.voice_combo.setCurrentIndex(max(0, pos))
        self.voice_combo.blockSignals(False)
        self.description_label.setText(getattr(engine, "description", ""))

    def _on_voice_changed(self) -> None:
        vid = self.voice_combo.currentData() or ""
        eid = self._current_engine_id()
        if eid == "mambo":
            idx = int(vid) if str(vid).isdigit() else 0
            self.settings.mambo.active_preset = idx
            self.preset_combo.setCurrentIndex(max(0, idx))
            self._refresh_preset_fields()
        self._refresh_engine_status()

    # ---------------- 曼波预设 ----------------
    def _presets(self) -> list[dict]:
        presets = [p for p in (self.settings.mambo.presets or []) if isinstance(p, dict)]
        if not presets:
            presets = [{"name": "曼波", "ref_audio_path": "", "prompt_text": "", "prompt_lang": "zh"}]
            self.settings.mambo.presets = presets
        return presets

    def _refresh_presets(self) -> None:
        presets = self._presets()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for i, p in enumerate(presets):
            self.preset_combo.addItem(p.get("name") or f"预设{i + 1}", i)
        cur = max(0, min(len(presets) - 1, int(self.settings.mambo.active_preset)))
        self.preset_combo.setCurrentIndex(cur)
        self.preset_combo.blockSignals(False)
        self._refresh_preset_fields()

    def _refresh_preset_fields(self) -> None:
        idx = self.preset_combo.currentIndex()
        presets = self._presets()
        if not (0 <= idx < len(presets)):
            return
        p = presets[idx]
        self.preset_name.setText(p.get("name", ""))
        self.ref_audio.setText(p.get("ref_audio_path", ""))
        self.prompt_text.setText(p.get("prompt_text", ""))

    def _store_preset_fields(self) -> None:
        idx = self.preset_combo.currentIndex()
        presets = self._presets()
        if not (0 <= idx < len(presets)):
            return
        presets[idx].update(
            {
                "name": self.preset_name.text().strip() or f"预设{idx + 1}",
                "ref_audio_path": self.ref_audio.text().strip(),
                "prompt_text": self.prompt_text.text().strip(),
                "prompt_lang": presets[idx].get("prompt_lang", "zh"),
            }
        )

    def _on_preset_changed(self) -> None:
        self._refresh_preset_fields()

    def _on_add_preset(self) -> None:
        self._store_preset_fields()
        presets = self._presets()
        presets.append(
            {"name": f"音色{len(presets) + 1}", "ref_audio_path": "", "prompt_text": "", "prompt_lang": "zh"}
        )
        self.settings.mambo.presets = presets
        self.settings.mambo.active_preset = len(presets) - 1
        self._refresh_presets()
        self._refresh_voices()

    def _on_del_preset(self) -> None:
        presets = self._presets()
        if len(presets) <= 1:
            self.mambo_status.setText("至少保留一个音色预设")
            return
        idx = self.preset_combo.currentIndex()
        if 0 <= idx < len(presets):
            presets.pop(idx)
        self.settings.mambo.presets = presets
        self.settings.mambo.active_preset = max(0, min(idx, len(presets) - 1))
        self._refresh_presets()
        self._refresh_voices()

    def _on_browse_ref(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择参考音频", str(Path.home()), "音频文件 (*.wav *.mp3 *.flac);;所有文件 (*.*)"
        )
        if path:
            self.ref_audio.setText(path)

    def _on_mambo_check(self) -> None:
        self._store_preset_fields()
        self.settings.mambo.base_url = self.mambo_url.text().strip() or "http://127.0.0.1:9880"
        engine = build_engine("mambo", self.settings)
        ok, msg = engine.probe()
        self.mambo_status.setText(("✓ " if ok else "✗ ") + msg)
        self._refresh_engine_status()

    # ---------------- 试听 ----------------
    def _on_test_voice(self) -> None:
        self._store_preset_fields()
        self._apply_to_settings(partial=True)
        eid = self._current_engine_id()
        engine = build_engine(eid, self.settings)
        text = "你好，我是曼波。这段话用来试听声音，一二三四五。" if eid == "mambo" else "你好，这是一段试听，一二三四五。"
        from ..core.tts.base import SpeechRequest

        req = SpeechRequest(
            text=text,
            rate=max(0.5, min(2.0, self.speed_slider.value() / 100)),
            pitch=self.pitch_slider.value(),
            voice=self.voice_combo.currentData() or "",
        )
        out = DATA_DIR / "preview" / f"preview{engine.file_suffix}"
        out.parent.mkdir(parents=True, exist_ok=True)
        self.test_btn.setEnabled(False)
        self.test_status.setText("合成中…")

        job = _PreviewJob()

        def _work() -> None:
            try:
                engine.synthesize(req, out)
                job.result = out
            except Exception as exc:  # noqa: BLE001
                job.error = exc
            finally:
                job.done = True

        # daemon 线程：即使合成卡在网络上，也不会拖住进程退出
        threading.Thread(target=_work, name="tts-preview", daemon=True).start()
        self._preview_future = job
        self._preview_path = out
        self._poll_timer.start()

    def _poll_preview(self) -> None:
        job = self._preview_future
        if job is None:
            self._poll_timer.stop()
            return
        if not job.done:
            return
        self._poll_timer.stop()
        self.test_btn.setEnabled(True)
        if job.error is not None:
            self.test_status.setText(f"失败：{job.error}")
            return
        path = job.result or self._preview_path
        self.test_status.setText("播放中")
        self._preview_player.setSource(QUrl.fromLocalFile(str(path)))
        self._preview_player.play()

    # ---------------- 颜色 ----------------
    def _pick_color(self, attr: str, button: QPushButton) -> None:
        current = QColor(getattr(self.settings.subtitle, attr))
        color = QColorDialog.getColor(current, self, "选择颜色")
        if color.isValid():
            setattr(self.settings.subtitle, attr, color.name())
            self._paint_color_buttons()

    def _paint_color_buttons(self) -> None:
        sub = self.settings.subtitle
        for btn, attr in (
            (self.fg_btn, "fg_color"),
            (self.hl_btn, "highlight_color"),
            (self.bg_btn, "bg_color"),
        ):
            color = getattr(sub, attr)
            btn.setStyleSheet(
                f"border: 1px solid #2E3942; border-left: 14px solid {color};"
                f"padding-left: 6px; text-align: left;"
            )

    # ---------------- 缓存 ----------------
    def _refresh_cache_label(self) -> None:
        total = 0
        from ..config import CACHE_DIR

        for p in CACHE_DIR.glob("*"):
            if p.is_file():
                total += p.stat().st_size
        self.cache_label.setText(f"{total / 1024 / 1024:.1f} MB")

    def _on_clear_cache(self) -> None:
        from ..config import CACHE_DIR

        removed = 0
        for p in CACHE_DIR.glob("*"):
            if p.is_file():
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        self.cache_label.setText("0.0 MB")
        self.test_status.setText(f"已清理 {removed} 个缓存文件")

    # ---------------- 保存 ----------------
    def _apply_to_settings(self, partial: bool = False) -> None:
        s = self.settings
        s.engine_id = self._current_engine_id()
        voice = self.voice_combo.currentData() or ""
        if s.engine_id == "edge":
            s.edge_voice = voice or s.edge_voice
        elif s.engine_id == "sapi":
            s.sapi_voice = voice
        elif s.engine_id == "mambo":
            s.mambo.active_preset = int(voice) if str(voice).isdigit() else 0

        s.speed = max(0.5, min(2.0, self.speed_slider.value() / 100))
        s.pitch = int(self.pitch_slider.value())
        s.auto_prosody = self.auto_prosody.isChecked()
        s.prosody_strength = max(0.0, min(2.0, self.strength_slider.value() / 100))
        s.prefetch = int(self.prefetch_spin.value())

        self._store_preset_fields()
        s.mambo.base_url = self.mambo_url.text().strip() or "http://127.0.0.1:9880"
        s.mambo.auto_start = self.auto_start.isChecked()
        s.mambo.start_command = self.start_command.text().strip()

        sub = s.subtitle
        sub.font_size = int(self.sub_font.value())
        sub.width = int(self.sub_width.value())
        sub.opacity = int(self.sub_opacity.value())
        sub.max_lines = int(self.sub_lines.value())
        sub.text_align = self.sub_align.currentData() or "center"
        sub.show_context = self.sub_context.isChecked()
        sub.click_through = self.sub_through.isChecked()

        s.cache_limit_mb = int(self.cache_limit.value())
        s.cache_retention_minutes = int(self.cache_retention.value())
        if not partial:
            s.save()

    def _on_accept(self) -> None:
        self._apply_to_settings()
        self.settings.save()
        self.settingsChanged.emit()
        self.accept()

    def _release_preview(self) -> None:
        """关闭前先停掉预览播放器，避免带音源的对象被销毁时触发平台层崩溃。"""
        try:
            self._poll_timer.stop()
            self._preview_player.stop()
            self._preview_player.setSource(QUrl())
        except Exception:  # noqa: BLE001
            pass

    def accept(self) -> None:  # noqa: D401
        self._release_preview()
        super().accept()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._release_preview()
        super().closeEvent(event)
