"""悬浮字幕窗：无边框、置顶、半透明、可拖动、可鼠标穿透。"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QPushButton, QVBoxLayout, QWidget

from ..config import Settings


class SubtitleWindow(QWidget):
    clicked = Signal()             # 单击正文区：播放/暂停
    doubleClicked = Signal()       # 双击：打开主窗口
    wheelSpeed = Signal(int)       # 滚轮：调整语速 (+1/-1)
    requestSettings = Signal()
    requestHide = Signal()
    prevRequested = Signal()
    toggleRequested = Signal()
    nextRequested = Signal()
    prevChapterRequested = Signal()
    nextChapterRequested = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(None)
        self.settings = settings
        self._drag_offset: QPoint | None = None
        self._press_pos: QPoint | None = None
        self._dragging = False
        self._current_text = ""
        self._prev_text = ""
        self._next_text = ""

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle("语音书架 · 字幕")

        self.prev_label = QLabel(self)
        self.cur_label = QLabel(self)
        self.next_label = QLabel(self)
        for lb in (self.prev_label, self.cur_label, self.next_label):
            lb.setWordWrap(True)
            lb.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            lb.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            lb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 10, 22, 10)
        layout.setSpacing(2)
        layout.addWidget(self.prev_label)
        layout.addWidget(self.cur_label)
        layout.addWidget(self.next_label)

        # 控制条：上一句 / 播放暂停 / 下一句 / 隐藏
        self.bar = QWidget(self)
        bar_layout = QHBoxLayout(self.bar)
        bar_layout.setContentsMargins(0, 2, 0, 0)
        bar_layout.setSpacing(10)
        bar_layout.addStretch(1)
        self.btn_prev_chapter = self._mini_button("⇤", "上一章")
        self.btn_prev = self._mini_button("⏮", "上一句")
        self.btn_play = self._mini_button("▶", "播放 / 暂停")
        self.btn_next = self._mini_button("⏭", "下一句")
        self.btn_next_chapter = self._mini_button("⇥", "下一章")
        self.btn_hide = self._mini_button("✕", "隐藏字幕（只留托盘图标）")
        self.btn_prev_chapter.clicked.connect(self.prevChapterRequested.emit)
        self.btn_prev.clicked.connect(self.prevRequested.emit)
        self.btn_play.clicked.connect(self.toggleRequested.emit)
        self.btn_next.clicked.connect(self.nextRequested.emit)
        self.btn_next_chapter.clicked.connect(self.nextChapterRequested.emit)
        self.btn_hide.clicked.connect(self.requestHide.emit)
        for btn in (
            self.btn_prev_chapter, self.btn_prev, self.btn_play,
            self.btn_next, self.btn_next_chapter, self.btn_hide,
        ):
            bar_layout.addWidget(btn)
        bar_layout.addStretch(1)
        layout.addWidget(self.bar)

        self.apply_settings()
        self._restore_position()

    def _mini_button(self, text: str, tip: str) -> QPushButton:
        btn = QPushButton(text, self)
        btn.setToolTip(tip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(30, 24)
        btn.setStyleSheet(
            "QPushButton {"
            "  background: rgba(255,255,255,26);"
            "  border: none; border-radius: 6px;"
            "  color: #E8EEF3; font-size: 13px; padding: 0;"
            "}"
            "QPushButton:hover { background: rgba(63,169,245,110); }"
            "QPushButton:pressed { background: rgba(63,169,245,180); }"
        )
        return btn

    def set_playing(self, playing: bool) -> None:
        self.btn_play.setText("⏸" if playing else "▶")
        self.btn_play.setToolTip("暂停" if playing else "播放")

    # ---------------------------------------------------------- 外观
    def apply_settings(self) -> None:
        s = self.settings.subtitle
        fam = s.font_family
        cur_size = max(10, int(s.font_size))
        small = max(9, cur_size - 6)

        cur_font = QFont(fam, cur_size)
        cur_font.setBold(True)
        self.cur_label.setFont(cur_font)
        self.cur_label.setStyleSheet(
            f"color: {s.fg_color}; background: transparent;"
            f"font-size: {cur_size}px; font-weight: 700;"
        )
        self.prev_label.setFont(QFont(fam, small))
        self.prev_label.setStyleSheet(
            f"color: {s.highlight_color}; background: transparent; font-size: {small}px;"
        )
        self.next_label.setFont(QFont(fam, small))
        self.next_label.setStyleSheet(
            f"color: {s.fg_color}; background: transparent; font-size: {small}px;"
            f"opacity: 0.6;"
        )

        show_ctx = bool(s.show_context)
        self.prev_label.setVisible(show_ctx and bool(self._prev_text))
        self.next_label.setVisible(show_ctx and bool(self._next_text))

        width = max(280, int(s.width))
        self.setFixedWidth(width)
        # 不再用 maxHeight 硬裁（裁出来就是「挤压」），改成按行数预算省略
        screen = self.screen().availableGeometry() if self.screen() else None
        self.setMaximumHeight(int(screen.height() * 0.45) if screen else 600)
        self._fit_height()

        align = {
            "left": Qt.AlignmentFlag.AlignLeft,
            "center": Qt.AlignmentFlag.AlignHCenter,
            "right": Qt.AlignmentFlag.AlignRight,
        }.get(getattr(s, "text_align", "center"), Qt.AlignmentFlag.AlignHCenter)
        for lb in (self.prev_label, self.cur_label, self.next_label):
            lb.setAlignment(align | Qt.AlignmentFlag.AlignVCenter)

        # 鼠标穿透时控制条不可点，直接藏掉省得误判
        self.bar.setVisible(not s.click_through)
        self.set_click_through(bool(s.click_through))
        self.update()

    def _fit_height(self) -> None:
        self.layout().activate()
        h = self.sizeHint().height()
        self.resize(self.width(), max(60, h))

    def set_click_through(self, enabled: bool) -> None:
        current = bool(self.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        if current == enabled:
            return
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, enabled)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, enabled)
        if self.isVisible():
            self.show()

    # ---------------------------------------------------------- 位置
    def _restore_position(self) -> None:
        s = self.settings.subtitle
        screen = self.screen().availableGeometry() if self.screen() else None
        if s.x >= 0 and s.y >= 0:
            self.move(s.x, s.y)
        elif screen:
            self.move(
                screen.center().x() - max(280, s.width) // 2,
                screen.bottom() - self.height() - 110,
            )

    def _save_position(self) -> None:
        self.settings.subtitle.x = self.x()
        self.settings.subtitle.y = self.y()

    @staticmethod
    def _fit_text(text: str, width: int, lines: int, base_size: int) -> tuple[str, int]:
        """先缩字号把整句塞进去，缩到下限还放不下才省略。

        中文约一个字宽 = 字号，据此估算每行字数，避免 QLabel 换行后
        超出 max_lines 被硬裁（那就是「挤压」的观感）。
        """
        if not text:
            return "", base_size
        lines = max(1, lines)
        min_size = max(11, int(base_size * 0.6))
        size = base_size
        while size > min_size:
            per_line = max(1, int(width / (size * 1.06)))
            if len(text) <= per_line * lines:
                return text, size
            size -= 1
        per_line = max(1, int(width / (size * 1.06)))
        budget = per_line * lines
        if len(text) <= budget:
            return text, size
        return (text if budget <= 1 else text[: budget - 1] + "…"), size

    # ---------------------------------------------------------- 内容
    def set_sentence(self, current: str, prev: str = "", nxt: str = "") -> None:
        self._current_text, self._prev_text, self._next_text = current, prev, nxt
        s = self.settings.subtitle
        inner = max(120, int(s.width) - 46)
        base = max(10, int(s.font_size))
        small = max(9, base - 6)

        text, size = self._fit_text(current, inner, max(1, s.max_lines), base)
        self.cur_label.setText(text)
        self.cur_label.setStyleSheet(
            f"color: {s.fg_color}; background: transparent; font-size: {size}px; font-weight: 700;"
        )

        prev_text, _ = self._fit_text(prev, inner, 1, small)
        next_text, _ = self._fit_text(nxt, inner, 1, small)
        self.prev_label.setText(prev_text)
        self.next_label.setText(next_text)

        self.cur_label.setToolTip(current)
        self.prev_label.setToolTip(prev)
        self.next_label.setToolTip(nxt)
        self.prev_label.setVisible(bool(s.show_context and prev))
        self.next_label.setVisible(bool(s.show_context and nxt))
        self._fit_height()

    # ---------------------------------------------------------- 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        s = self.settings.subtitle
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        bg = QColor(s.bg_color)
        bg.setAlpha(int(max(0, min(100, s.opacity)) / 100 * 255))
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 16, 16)
        p.fillPath(path, bg)
        # 顶部细边框，弱化突兀感
        border = QColor(s.fg_color)
        border.setAlpha(28)
        p.setPen(border)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()

    # ---------------------------------------------------------- 交互
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
            self._drag_offset = self._press_pos - self.frameGeometry().topLeft()
            self._dragging = False
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        pos = event.globalPosition().toPoint()
        if not self._dragging and self._press_pos and (pos - self._press_pos).manhattanLength() > 6:
            self._dragging = True
        if self._dragging:
            self.move(pos - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        was_drag = self._dragging
        self._drag_offset = None
        self._press_pos = None
        self._dragging = False
        if was_drag:
            self._save_position()
        else:
            self.clicked.emit()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()
            event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta:
            self.wheelSpeed.emit(1 if delta > 0 else -1)
            event.accept()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        s = self.settings.subtitle
        act_through = menu.addAction("鼠标穿透")
        act_through.setCheckable(True)
        act_through.setChecked(bool(s.click_through))
        act_ctx = menu.addAction("显示上下句")
        act_ctx.setCheckable(True)
        act_ctx.setChecked(bool(s.show_context))
        menu.addSeparator()
        act_edit = menu.addAction("字幕外观…")
        act_hide = menu.addAction("隐藏字幕（入口在托盘图标）")
        act_quit = menu.addAction("退出语音书架")

        chosen = menu.exec(event.globalPos())
        if chosen == act_through:
            s.click_through = act_through.isChecked()
            self.set_click_through(s.click_through)
            self.settings.save()
        elif chosen == act_ctx:
            s.show_context = act_ctx.isChecked()
            self.apply_settings()
            self.set_sentence(self._current_text, self._prev_text, self._next_text)
            self.settings.save()
        elif chosen == act_edit:
            self.requestSettings.emit()
        elif chosen == act_hide:
            self.requestHide.emit()
        elif chosen == act_quit:
            from PySide6.QtWidgets import QApplication

            QApplication.instance().quit()
