"""主窗口：书架 + 阅读区 + 朗读控制栏。"""
from __future__ import annotations

from pathlib import Path
import threading
import traceback

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSlider, QSplitter, QTextEdit,
    QVBoxLayout, QWidget,
)

from ..config import Settings
from ..core.library import Book, Library, import_book
from ..core.player import PlaybackController
from ..core.progress import ProgressStore
from ..core.tts.service import SynthesisService
from .settings_dialog import SettingsDialog
from .subtitle_window import SubtitleWindow
from .theme import ACCENT, BORDER


class ReaderView(QTextEdit):
    """只读正文区：左键选中句子，右键弹功能菜单。"""

    sentenceClicked = Signal(int)        # 左键：文档字符位置
    sentenceRightClicked = Signal(int)   # 右键：文档字符位置

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("reader")
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setCursorWidth(0)
        font = QFont("Microsoft YaHei UI", 12)
        self.setFont(font)
        self.document().setDocumentMargin(8)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            pos = self.cursorForPosition(event.position().toPoint()).position()
            self.sentenceClicked.emit(pos)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        # 右键走自定义菜单（不弹 Qt 默认的复制菜单）
        pos = self.cursorForPosition(event.pos()).position()
        self.sentenceRightClicked.emit(pos)
        event.accept()


class MainWindow(QMainWindow):
    hiddenToTray = Signal()
    engineProbed = Signal(bool, str, str)   # ok, 引擎名, 说明

    def __init__(
        self,
        settings: Settings,
        library: Library,
        progress: ProgressStore,
        service: SynthesisService,
        controller: PlaybackController,
        subtitle: SubtitleWindow,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.library = library
        self.progress = progress
        self.service = service
        self.controller = controller
        self.subtitle = subtitle

        self._current_book: Book | None = None
        self._suppress_click = False
        self._last_saved_index = -1
        self._primary_index: int | None = None    # 朗读位置（填充高亮）
        self._selected_index: int | None = None   # 左键选中（下划线标记）
        self._loading = False                     # 当前段语音是否还没就绪

        self._speed_timer = QTimer(self)
        self._speed_timer.setSingleShot(True)
        self._speed_timer.setInterval(450)
        self._speed_timer.timeout.connect(self._apply_speed)

        self.setWindowTitle("语音书架 · 小说朗读")
        self.resize(1120, 720)
        self.setAcceptDrops(True)
        self._build_ui()
        self._connect()

        self.refresh_books()
        self._restore_last_book()
        QTimer.singleShot(300, self._prime_engine_status)

    # ================================================================ UI
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 10, 10, 8)
        outer.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_shelf())
        splitter.addWidget(self._build_reader())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 820])
        outer.addWidget(splitter, 1)

        outer.addWidget(self._build_transport())

    def _build_shelf(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(8)

        title = QLabel("我的书架")
        title.setObjectName("title")
        layout.addWidget(title)

        self.book_list = QListWidget()
        self.book_list.setAlternatingRowColors(False)
        self.book_list.setSpacing(2)
        layout.addWidget(self.book_list, 1)

        row1 = QHBoxLayout()
        self.btn_import_txt = QPushButton("导入 TXT")
        self.btn_import_epub = QPushButton("导入 EPUB")
        row1.addWidget(self.btn_import_txt)
        row1.addWidget(self.btn_import_epub)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_remove_book = QPushButton("移除")
        self.btn_reset_progress = QPushButton("从头读")
        row2.addWidget(self.btn_remove_book)
        row2.addWidget(self.btn_reset_progress)
        layout.addLayout(row2)

        hint = QLabel("支持拖拽 txt/epub 到窗口；移除只删书架记录，不动原文件。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return panel

    def _build_reader(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self.book_title = QLabel("未选择书籍")
        self.book_title.setObjectName("title")
        self.chapter_combo = QComboBox()
        self.chapter_combo.setMinimumWidth(220)
        self.progress_label = QLabel("0.0%")
        self.progress_label.setObjectName("hint")
        header.addWidget(self.book_title, 1)
        header.addWidget(self.chapter_combo)
        header.addWidget(self.progress_label)
        layout.addLayout(header)

        self.reader = ReaderView()
        layout.addWidget(self.reader, 1)
        return panel

    def _build_transport(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet(f"QWidget {{ border-top: 1px solid {BORDER}; }}")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(2, 8, 2, 0)
        layout.setSpacing(6)

        row = QHBoxLayout()
        self.btn_prev = self._tool_button("⟨ 上一句", "Ctrl+Alt+←")
        self.btn_play = self._tool_button("▶ 朗读", "Ctrl+Alt+P")
        self.btn_play.setObjectName("primary")
        self.btn_next = self._tool_button("下一句 ⟩", "Ctrl+Alt+→")
        self.btn_prev_chapter = self._tool_button("⇤ 上一章")
        self.btn_next_chapter = self._tool_button("下一章 ⇥")
        for b in (
            self.btn_prev, self.btn_play, self.btn_next,
            self.btn_prev_chapter, self.btn_next_chapter,
        ):
            row.addWidget(b)

        row.addSpacing(12)
        row.addWidget(QLabel("语速"))
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(50, 200)
        self.speed_slider.setFixedWidth(130)
        self.speed_label = QLabel("1.00×")
        self.speed_label.setFixedWidth(48)
        row.addWidget(self.speed_slider)
        row.addWidget(self.speed_label)

        row.addSpacing(12)
        row.addWidget(QLabel("语音"))
        self.engine_combo = QComboBox()
        self.engine_combo.setMinimumWidth(150)
        row.addWidget(self.engine_combo)

        row.addStretch(1)
        self.btn_subtitle = self._tool_button("字幕", "Ctrl+Alt+F")
        self.btn_subtitle.setCheckable(True)
        self.btn_subtitle.setChecked(True)
        self.btn_settings = self._tool_button("设置")
        row.addWidget(self.btn_subtitle)
        row.addWidget(self.btn_settings)
        layout.addLayout(row)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("status")

        # 语音生成进度：常驻底栏，避免「还没生成」看起来像卡住
        prog_row = QHBoxLayout()
        prog_row.setSpacing(8)
        self.synth_label = QLabel("语音：待生成")
        self.synth_label.setObjectName("hint")
        self.synth_label.setMinimumWidth(210)
        self.synth_bar = QProgressBar()
        self.synth_bar.setTextVisible(False)
        self.synth_bar.setFixedHeight(6)
        self.synth_bar.setMaximumWidth(240)
        self.synth_bar.setRange(0, 1)
        self.synth_bar.setValue(0)
        prog_row.addWidget(self.status_label, 1)
        prog_row.addWidget(self.synth_label)
        prog_row.addWidget(self.synth_bar)
        layout.addLayout(prog_row)
        return panel

    def _tool_button(self, text: str, tip: str = "") -> QPushButton:
        btn = QPushButton(text)
        if tip:
            btn.setToolTip(tip)
        return btn

    # ================================================================ 连接
    def _connect(self) -> None:
        self.btn_import_txt.clicked.connect(lambda: self._import(("txt",)))
        self.btn_import_epub.clicked.connect(lambda: self._import(("epub",)))
        self.btn_remove_book.clicked.connect(self._remove_book)
        self.btn_reset_progress.clicked.connect(self._reset_progress)
        self.book_list.currentItemChanged.connect(self._on_book_selected)
        self.chapter_combo.currentIndexChanged.connect(self._on_chapter_combo)

        self.btn_prev.clicked.connect(lambda: self.controller.prev_sentence())
        self.btn_play.clicked.connect(self.controller.toggle)
        self.btn_next.clicked.connect(lambda: self.controller.next_sentence())
        self.btn_prev_chapter.clicked.connect(
            lambda: self.controller.seek_chapter(self.controller.chapter_index - 1)
        )
        self.btn_next_chapter.clicked.connect(
            lambda: self.controller.seek_chapter(self.controller.chapter_index + 1)
        )

        self.speed_slider.valueChanged.connect(self._on_speed_slider)
        self.engine_combo.currentIndexChanged.connect(self._on_engine_combo)
        self.btn_subtitle.toggled.connect(self._on_subtitle_toggle)
        self.btn_settings.clicked.connect(self.open_settings)

        self.reader.sentenceClicked.connect(self._on_reader_click)
        self.reader.sentenceRightClicked.connect(self._on_reader_context)
        self.engineProbed.connect(self._on_engine_probed)

        c = self.controller
        c.sentenceChanged.connect(self._on_sentence)
        c.sentenceSelected.connect(self._on_selection)
        c.chapterChanged.connect(self._on_chapter)
        c.playingChanged.connect(self._on_playing)
        c.progressChanged.connect(self._on_progress)
        c.statusChanged.connect(self.status_label.setText)
        c.error.connect(self._on_error)
        c.loaded.connect(self._on_chapter_loaded)
        c.synthProgress.connect(self._on_synth_progress)
        c.loadingChanged.connect(self._on_loading)

        self.service.engine_changed.connect(lambda _eid: self._prime_engine_status())

        self.subtitle.clicked.connect(self.controller.toggle)
        self.subtitle.doubleClicked.connect(self.show_and_raise)
        self.subtitle.wheelSpeed.connect(self._on_wheel_speed)
        self.subtitle.requestSettings.connect(self.open_settings)
        self.subtitle.requestHide.connect(lambda: self.set_subtitle_visible(False))
        self.subtitle.prevRequested.connect(lambda: self.controller.prev_sentence())
        self.subtitle.toggleRequested.connect(self.controller.toggle)
        self.subtitle.nextRequested.connect(lambda: self.controller.next_sentence())
        self.subtitle.prevChapterRequested.connect(
            lambda: self.controller.seek_chapter(self.controller.chapter_index - 1)
        )
        self.subtitle.nextChapterRequested.connect(
            lambda: self.controller.seek_chapter(self.controller.chapter_index + 1)
        )

    # ================================================================ 书架
    def refresh_books(self) -> None:
        self.book_list.blockSignals(True)
        self.book_list.clear()
        for book in self.library.books:
            item = QListWidgetItem(self._book_label(book))
            item.setData(Qt.ItemDataRole.UserRole, book.id)
            item.setToolTip(book.path)
            self.book_list.addItem(item)
        self.book_list.blockSignals(False)

    def _book_label(self, book: Book) -> str:
        prog = self.progress.get(book.id)
        pct = f"  ·  {prog.percent * 100:.0f}%" if prog.updated_at else ""
        return f"{book.title}{pct}\n{book.chapter_count()} 章 · {book.fmt.upper()}"

    def _import(self, kinds: tuple[str, ...]) -> None:
        filters = {
            "txt": "文本文件 (*.txt)",
            "epub": "EPUB 电子书 (*.epub)",
        }
        pattern = ";;".join(filters[k] for k in kinds if k in filters) + ";;所有文件 (*.*)"
        paths, _ = QFileDialog.getOpenFileNames(self, "选择小说文件", str(Path.home()), pattern)
        for path in paths:
            self._add_path(path)

    def _add_path(self, path: str) -> None:
        try:
            book = import_book(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "导入失败", f"{Path(path).name}\n\n{exc}")
            return
        self.library.add(book)
        self.refresh_books()
        self._select_book(book.id)

    def _remove_book(self) -> None:
        book = self._selected_book()
        if not book:
            return
        answer = QMessageBox.question(
            self, "移除书籍", f"从书架移除《{book.title}》？\n（不会删除原文件，阅读进度也会清掉）"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.library.remove(book.id)
        self.progress.forget(book.id)
        self._current_book = None
        self.refresh_books()
        self.reader.setPlainText("")
        self.book_title.setText("未选择书籍")
        self.chapter_combo.blockSignals(True)
        self.chapter_combo.clear()
        self.chapter_combo.blockSignals(False)
        self.controller.stop()

    def _reset_progress(self) -> None:
        book = self._selected_book()
        if not book:
            return
        self.progress.forget(book.id)
        self.refresh_books()
        self.open_book(book, restore=False)
        self.controller.seek_chapter(0)

    def _selected_book(self) -> Book | None:
        item = self.book_list.currentItem()
        if item is None:
            return None
        return self.library.get(item.data(Qt.ItemDataRole.UserRole))

    def _select_book(self, book_id: str) -> None:
        for i in range(self.book_list.count()):
            item = self.book_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == book_id:
                self.book_list.setCurrentItem(item)
                return

    def _on_book_selected(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is None:
            return
        book = self.library.get(current.data(Qt.ItemDataRole.UserRole))
        if book and (self._current_book is None or book.id != self._current_book.id):
            self.open_book(book, restore=True)

    def open_book(self, book: Book, restore: bool = True, autoplay: bool = False) -> None:
        self._current_book = book
        self.book_title.setText(book.title)
        self.settings.last_book_id = book.id
        self.settings.save()

        chapter, sentence = 0, 0
        if restore:
            prog = self.progress.get(book.id)
            chapter, sentence = prog.chapter, prog.sentence
        self._fill_chapter_combo(book)
        self.controller.load(book, chapter, sentence, autoplay=autoplay)
        self.refresh_books()

    def _restore_last_book(self) -> None:
        book = self.library.get(self.settings.last_book_id) if self.settings.last_book_id else None
        if book is None and self.library.books:
            book = self.library.books[0]
        if book is None:
            return
        self._select_book(book.id)
        if self._current_book is None:
            self.open_book(book, restore=True)

    def _on_chapter_combo(self, index: int) -> None:
        """用户在章节下拉框里选章节。程序内改动已 blockSignals，不会误触发。"""
        if index < 0 or index == self.controller.chapter_index:
            return
        self.controller.seek_chapter(index)

    def _fill_chapter_combo(self, book: Book) -> None:
        self.chapter_combo.blockSignals(True)
        self.chapter_combo.clear()
        titles = book.chapter_titles or [f"第{i + 1}章" for i in range(book.chapter_count())]
        for i, t in enumerate(titles):
            self.chapter_combo.addItem(t, i)
        self.chapter_combo.blockSignals(False)

    # ================================================================ 播放联动
    def _on_chapter_loaded(self) -> None:
        text = self.controller.display_text
        self.reader.setPlainText(text)
        self._primary_index = None
        self._selected_index = None
        if self.controller.chapter_index < self.chapter_combo.count():
            self.chapter_combo.blockSignals(True)
            self.chapter_combo.setCurrentIndex(self.controller.chapter_index)
            self.chapter_combo.blockSignals(False)

    def _on_chapter(self, chapter: int) -> None:
        if 0 <= chapter < self.chapter_combo.count():
            self.chapter_combo.blockSignals(True)
            self.chapter_combo.setCurrentIndex(chapter)
            self.chapter_combo.blockSignals(False)
        if self._current_book:
            self.book_title.setText(
                f"{self._current_book.title}  ·  {self._current_book.chapter_title(chapter)}"
            )

    def _on_selection(self, index: int, _text: str) -> None:
        """左键选中：只做正文标记。

        播放中——加下划线，不暂停、不改播放位置、不动悬浮字幕；
        暂停时——把阅读位置移过去，方便右键从这里开始读。
        """
        if self.controller.is_playing:
            self._selected_index = index
            self._apply_highlights()
            self.progress_label.setText(f"已选第 {index + 1} 句（朗读不受影响，右键可选从此朗读）")
            return
        self._primary_index = index
        self._selected_index = None
        self._apply_highlights()
        self.progress_label.setText(
            f"已选第 {index + 1} 句 / 本章共 {len(self.controller.sentences)} 句"
        )

    def _on_sentence(self, index: int, text: str, prosody) -> None:
        self._highlight(index)
        sentences = self.controller.sentences
        prev_text = sentences[index - 1].text if index - 1 >= 0 else ""
        next_text = sentences[index + 1].text if index + 1 < len(sentences) else ""
        self.subtitle.set_sentence(text, prev_text, next_text)
        if prosody is not None:
            self.status_label.setText(
                f"{getattr(prosody, 'kind_label', '旁白')} · 语速 {getattr(prosody, 'rate', 1.0):.2f}×"
                f" · 音调 {getattr(prosody, 'pitch', 0):+d}Hz"
            )
        self._save_progress(index)

    def _make_selection(self, index: int, filled: bool) -> "QTextEdit.ExtraSelection":
        sents = self.controller.sentences
        cursor = QTextCursor(self.reader.document())
        cursor.setPosition(sents[index].start)
        cursor.setPosition(min(sents[index].end, len(self.reader.toPlainText())),
                           QTextCursor.MoveMode.KeepAnchor)
        fmt = QTextCharFormat()
        if filled:
            fmt.setBackground(QColor(63, 169, 245, 38))
            fmt.setForeground(QColor("#EAF6FF"))
        else:
            # 播放中选中：只加下划线标记，不抢播放高亮
            fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SingleUnderline)
            fmt.setUnderlineColor(QColor(ACCENT))
        sel = QTextEdit.ExtraSelection()
        sel.cursor = cursor
        sel.format = fmt
        return sel

    def _apply_highlights(self) -> None:
        """同时渲染两种高亮：播放位置（填充）与左键选中（下划线）。"""
        sents = self.controller.sentences
        selections = []
        if self._primary_index is not None and 0 <= self._primary_index < len(sents):
            selections.append(self._make_selection(self._primary_index, filled=True))
        if (
            self._selected_index is not None
            and 0 <= self._selected_index < len(sents)
            and self._selected_index != self._primary_index
        ):
            selections.append(self._make_selection(self._selected_index, filled=False))
        self.reader.setExtraSelections(selections)

        anchor = self._primary_index if self._primary_index is not None else self._selected_index
        if anchor is not None and 0 <= anchor < len(sents):
            nav = QTextCursor(self.reader.document())
            nav.setPosition(sents[anchor].start)
            self._suppress_click = True
            self.reader.setTextCursor(nav)
            self._suppress_click = False
            self.reader.ensureCursorVisible()

    def _highlight(self, index: int) -> None:
        """朗读位置变化：更新主高亮，并清掉临时选中标记。"""
        self._primary_index = index
        self._selected_index = None
        self._apply_highlights()

    def _paragraph_first(self, idx: int) -> int:
        """同一段落内相邻句子首尾相接（没有换行），以此回退到段首。"""
        sents = self.controller.sentences
        cur = idx
        while cur > 0 and sents[cur].start == sents[cur - 1].end:
            cur -= 1
        return cur

    def _sentence_at(self, pos: int) -> int | None:
        for i, sent in enumerate(self.controller.sentences):
            if sent.start <= pos <= sent.end:
                return i
        return None

    def _on_reader_click(self, pos: int) -> None:
        """左键：只选中这一句（不朗读、不弹菜单）。"""
        if self._suppress_click:
            return
        hit = self._sentence_at(pos)
        if hit is None:
            return
        self.controller.select_sentence(hit)
        self.status_label.setText(
            f"已选中第 {hit + 1} 句 · 右键可选「从这句朗读 / 从本段开头朗读」"
        )

    def _on_reader_context(self, pos: int) -> None:
        """右键：在这一句上给动作菜单。"""
        hit = self._sentence_at(pos)
        if hit is None:
            return
        self.controller.select_sentence(hit)

        para_first = self._paragraph_first(hit)
        sentence_text = self.controller.sentences[hit].text

        menu = QMenu(self)
        act_here = menu.addAction("▶ 从这一句开始朗读")
        act_para = menu.addAction("⏵ 从本段开头朗读") if para_first != hit else None
        menu.addSeparator()
        act_copy = menu.addAction("复制这一句")

        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            return
        if chosen == act_here:
            self.controller.seek_sentence(hit, autoplay=True)
        elif act_para is not None and chosen == act_para:
            self.controller.seek_sentence(para_first, autoplay=True)
        elif chosen == act_copy:
            QApplication.clipboard().setText(sentence_text)
            self.status_label.setText("已复制这一句")

    def _on_synth_progress(self, cur_ready: int, cur_total: int, nxt_ready: int, nxt_total: int) -> None:
        """底栏进度：本段没就绪就显示本段，否则显示下一段的后台生成进度。"""
        if cur_total and cur_ready < cur_total:
            self.synth_bar.setRange(0, cur_total)
            self.synth_bar.setValue(cur_ready)
            self.synth_label.setText(f"语音生成：当前段 {cur_ready}/{cur_total} 句")
        elif nxt_total and nxt_ready < nxt_total:
            self.synth_bar.setRange(0, nxt_total)
            self.synth_bar.setValue(nxt_ready)
            self.synth_label.setText(f"语音生成：下一段 {nxt_ready}/{nxt_total} 句")
        elif cur_total:
            self.synth_bar.setRange(0, 1)
            self.synth_bar.setValue(1)
            self.synth_label.setText("语音：本段已就绪")
        else:
            self.synth_bar.setRange(0, 1)
            self.synth_bar.setValue(0)
            self.synth_label.setText("语音：待生成")

    def _on_playing(self, playing: bool) -> None:
        self._refresh_play_button()

    def _on_loading(self, loading: bool) -> None:
        """当前段语音还没生成好：禁用播放按钮，避免点了没反应。"""
        self._loading = bool(loading)
        self._refresh_play_button()

    def _refresh_play_button(self) -> None:
        if getattr(self, "_loading", False):
            self.btn_play.setText("⏳ 语音加载中…")
            self.btn_play.setEnabled(False)
            self.subtitle.btn_play.setEnabled(False)
            return
        playing = self.controller.is_playing
        self.btn_play.setEnabled(True)
        self.subtitle.btn_play.setEnabled(True)
        self.btn_play.setText("⏸ 暂停" if playing else "▶ 朗读")
        self.subtitle.set_playing(playing)

    def _on_progress(self, chapter: int, sentence: int) -> None:
        pct = self.controller.progress_percent()
        self.progress_label.setText(f"{pct * 100:.1f}% · 第 {chapter + 1}/{self._book_chapter_count()} 章")
        self._save_progress(sentence)

    def _book_chapter_count(self) -> int:
        return self._current_book.chapter_count() if self._current_book else 0

    def _save_progress(self, sentence: int) -> None:
        if not self._current_book:
            return
        if sentence == self._last_saved_index and self.controller.chapter_index == getattr(
            self, "_last_saved_chapter", -1
        ):
            return
        self._last_saved_index = sentence
        self._last_saved_chapter = self.controller.chapter_index
        self.progress.set(
            self._current_book.id,
            self.controller.chapter_index,
            sentence,
            self.controller.progress_percent(),
        )

    def _on_error(self, message: str) -> None:
        self.status_label.setText(f"⚠ {message}")
        self._refresh_play_button()

    # ================================================================ 控制栏
    def _on_speed_slider(self, value: int) -> None:
        """拖动时只更新显示，停手 450ms 后再真正生效。

        逐格生效会反复重建整块音频（速率变了就换缓存 key，全块重合成），
        既卡顿，又会让字幕和正在播放的音频错位。
        """
        self.settings.speed = max(0.5, min(2.0, value / 100))
        self.speed_label.setText(f"{self.settings.speed:.2f}×")
        self._speed_timer.start()

    def _apply_speed(self) -> None:
        self.settings.save()
        self.controller.apply_settings_changed()

    def _on_wheel_speed(self, direction: int) -> None:
        value = max(50, min(200, self.speed_slider.value() + direction * 5))
        self.speed_slider.setValue(value)

    def set_speed(self, speed: float) -> None:
        self.speed_slider.setValue(int(round(max(0.5, min(2.0, speed)) * 100)))

    def _on_engine_combo(self) -> None:
        eid = self.engine_combo.currentData()
        if not eid:
            return
        self.service.switch_engine(eid)
        self.controller.apply_settings_changed()
        self.settings.save()

    def refresh_engine_combo(self) -> None:
        from ..core.tts.registry import all_engine_meta

        self.engine_combo.blockSignals(True)
        self.engine_combo.clear()
        for eid, label, _desc in all_engine_meta():
            self.engine_combo.addItem(label, eid)
        idx = self.engine_combo.findData(self.settings.engine_id)
        self.engine_combo.setCurrentIndex(max(0, idx))
        self.engine_combo.blockSignals(False)

    def _prime_engine_status(self) -> None:
        """引擎自检放到后台线程做：曼波探测要发 HTTP，播放中不能在 UI 线程里等。"""
        engine = self.service.engine
        self.status_label.setText(f"{engine.label} · 检测中…")

        def _work() -> None:
            try:
                ok, msg = self.service.probe()
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, str(exc)
            self.engineProbed.emit(bool(ok), engine.label, msg)

        threading.Thread(target=_work, name="engine-probe", daemon=True).start()

    def _on_engine_probed(self, ok: bool, label: str, msg: str) -> None:
        mark = "✓" if ok else "✗"
        self.status_label.setText(f"{mark} {label}{(' · ' + msg) if msg else ''}")

    # ================================================================ 字幕
    def set_subtitle_visible(self, visible: bool) -> None:
        self.settings.subtitle.visible = visible
        self.settings.save()
        if visible:
            self.subtitle.apply_settings()
            self.subtitle.show()
            self.subtitle.raise_()
        else:
            self.subtitle.hide()
        self.btn_subtitle.blockSignals(True)
        self.btn_subtitle.setChecked(visible)
        self.btn_subtitle.blockSignals(False)

    def _on_subtitle_toggle(self, checked: bool) -> None:
        self.set_subtitle_visible(checked)

    # ================================================================ 设置
    def open_settings(self) -> None:
        # 防止重复打开（字幕右键、托盘、按钮都可能触发）
        if getattr(self, "_settings_open", False):
            return
        self._settings_open = True
        try:
            dialog = SettingsDialog(self.settings, self)
            # 设置生效推迟到对话框关闭之后：避免在模态框还开着、
            # 预览播放器还活着的时候就去重建音频（播放中容易卡死）
            dialog.settingsChanged.connect(lambda: QTimer.singleShot(0, self._after_settings))
            old_engine = self.settings.engine_id
            dialog.exec()
            if self.settings.engine_id != old_engine:
                self.refresh_engine_combo()
        finally:
            self._settings_open = False

    def _after_settings(self) -> None:
        """设置生效：分步执行、单步兜底；窗口层的改动再往后放一拍。"""
        steps = (
            ("刷新引擎下拉", self.refresh_engine_combo),
            ("重载引擎", self.service.reload_engine),
            ("按大小清理缓存", self.service.prune_cache),
            ("同步语速滑块", self._sync_speed_slider),
            ("重建音频", self.controller.apply_settings_changed),
            ("刷新引擎状态", self._prime_engine_status),
        )
        for name, fn in steps:
            try:
                fn()
            except Exception:  # noqa: BLE001 - 单步失败不能拖垮整个保存流程
                self.status_label.setText(f"设置生效部分失败：{name}")
                with open(self._settings_step_log(), "a", encoding="utf-8") as fh:
                    fh.write(f"\n--- {name} 失败 ---\n{traceback.format_exc()}")

        # 改窗口 flag / show 这类操作与对话框销毁挤在一起容易出问题，延后一拍
        QTimer.singleShot(120, self._apply_subtitle_settings)

    @staticmethod
    def _settings_step_log() -> Path:
        from ..config import DATA_DIR

        logs = DATA_DIR / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        return logs / "settings.log"

    def _sync_speed_slider(self) -> None:
        self.speed_slider.blockSignals(True)
        self.speed_slider.setValue(int(round(max(0.5, min(2.0, self.settings.speed)) * 100)))
        self.speed_slider.blockSignals(False)
        self.speed_label.setText(f"{self.settings.speed:.2f}×")

    def _apply_subtitle_settings(self) -> None:
        try:
            self.subtitle.apply_settings()
            if self.settings.subtitle.visible:
                self.subtitle.show()
        except Exception:  # noqa: BLE001
            with open(self._settings_step_log(), "a", encoding="utf-8") as fh:
                fh.write(f"\n--- 字幕应用失败 ---\n{traceback.format_exc()}")

    # ================================================================ 拖拽
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith((".txt", ".epub")):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith((".txt", ".epub")):
                self._add_path(path)
        event.acceptProposedAction()

    # ================================================================ 窗口
    def show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.settings.save()
        if self._current_book:
            self.progress.set(
                self._current_book.id,
                self.controller.chapter_index,
                self.controller.index,
                self.controller.progress_percent(),
            )
        self.hiddenToTray.emit()
        event.ignore()
