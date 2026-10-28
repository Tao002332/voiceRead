"""界面主题：配色、QSS、程序内生成的图标（不依赖外部图片资源）。"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap

ACCENT = "#3FA9F5"
ACCENT_DIM = "#2C7FBF"
BG_DARK = "#14181C"
BG_PANEL = "#1B2126"
BG_CARD = "#222A31"
TEXT = "#E8EEF3"
TEXT_DIM = "#93A1AC"
BORDER = "#2E3942"

QSS = f"""
QWidget {{
    background: {BG_PANEL};
    color: {TEXT};
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI";
    font-size: 13px;
}}
QMainWindow, QDialog {{ background: {BG_DARK}; }}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 14px;
    padding: 10px 8px 8px 8px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {ACCENT};
}}
QPushButton {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 20px;
}}
QPushButton:hover {{ background: #2A343D; border-color: {ACCENT_DIM}; }}
QPushButton:pressed {{ background: #1C242B; }}
QPushButton:checked {{ background: {ACCENT_DIM}; border-color: {ACCENT}; color: #fff; }}
QPushButton#primary {{
    background: {ACCENT_DIM};
    border-color: {ACCENT};
    color: #FFFFFF;
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {ACCENT}; }}
QPushButton#danger:hover {{ background: #6B2B2B; border-color: #C0504D; }}
QListWidget, QTextEdit, QPlainTextEdit, QTreeWidget, QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px;
    selection-background-color: {ACCENT_DIM};
}}
QListWidget::item {{ padding: 8px 6px; border-radius: 4px; }}
QListWidget::item:selected {{ background: {ACCENT_DIM}; }}
QListWidget::item:hover {{ background: #29323A; }}
QTextEdit#reader {{
    background: #10151A;
    border: 1px solid {BORDER};
    font-size: 17px;
    line-height: 170%;
    padding: 14px;
    selection-background-color: transparent;
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT_DIM};
}}
QSlider::groove:horizontal {{
    height: 4px; background: {BORDER}; border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT_DIM}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {ACCENT}; width: 14px; margin: -6px 0; border-radius: 7px;
}}
QLabel#hint {{ color: {TEXT_DIM}; font-size: 12px; }}
QLabel#title {{ font-size: 16px; font-weight: 600; }}
QLabel#status {{ color: {TEXT_DIM}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; top: -1px; }}
QTabBar::tab {{
    background: transparent; padding: 8px 16px; color: {TEXT_DIM};
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {ACCENT}; border-bottom: 2px solid {ACCENT}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #3A4652; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT_DIM}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QCheckBox {{ spacing: 8px; }}
QProgressBar {{
    background: #2A343D;
    border: none;
    border-radius: 3px;
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 3px;
}}
QMenu {{
    background: {BG_CARD}; border: 1px solid {BORDER}; padding: 6px; border-radius: 8px;
}}
QMenu::item {{ padding: 7px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT_DIM}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QToolTip {{
    background: {BG_CARD}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px;
}}
"""


def make_app_icon(size: int = 256) -> QIcon:
    """画一个「书 + 声波」的图标，避免依赖外部 png。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0, QColor("#2C7FBF"))
    grad.setColorAt(1, QColor("#3FA9F5"))
    p.fillPath(path, grad)

    # 书页
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#FFFFFF"))
    w, h = size * 0.30, size * 0.40
    p.drawRoundedRect(QRectF(size * 0.20, size * 0.30, w, h), size * 0.04, size * 0.04)
    p.setBrush(QColor("#D7E9F7"))
    p.drawRoundedRect(QRectF(size * 0.50, size * 0.30, w, h), size * 0.04, size * 0.04)

    # 声波
    pen = p.pen()
    pen.setColor(QColor("#FFFFFF"))
    pen.setWidthF(size * 0.035)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    cx, cy = size * 0.52, size * 0.50
    for i, r in enumerate((0.13, 0.21, 0.29)):
        alpha = 255 - i * 55
        pen.setColor(QColor(255, 255, 255, alpha))
        p.setPen(pen)
        rect = QRectF(cx - size * r, cy - size * r, size * r * 2, size * r * 2)
        p.drawArc(rect, -50 * 16, 100 * 16)

    p.end()
    return QIcon(pm)


def make_tray_icon() -> QIcon:
    return make_app_icon(128)


def title_font(size: int = 15, bold: bool = True) -> QFont:
    f = QFont("Microsoft YaHei UI", size)
    f.setBold(bold)
    return f
