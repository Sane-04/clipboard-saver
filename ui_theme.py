"""统一的轻量桌面样式与本地矢量图标，不依赖图标包。"""
from pathlib import Path
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

ACCENT = "#4265e8"
INK = "#202633"
MUTED = "#747d8d"

STYLE = """
QWidget { font-family: "Microsoft YaHei UI"; font-size: 13px; color: #202633; }
QMainWindow, QDialog, QWidget#root { background: #f7f8fa; }
QLabel { background: transparent; }
QLabel#heading { font-size: 20px; font-weight: 600; }
QLabel#muted { color: #747d8d; }
QLabel#error { color: #b44242; }
QLabel#status { color: #508a72; font-size: 12px; }
QLabel#panelTitle { font-size: 14px; font-weight: 600; }
QFrame#preview, QFrame#tools, QFrame#properties {
    background: white; border: 1px solid #e3e7ee; border-radius: 10px;
}
QWidget#panelBody, QWidget#toolBody { background: transparent; }
QPushButton, QToolButton {
    background: white; border: 1px solid #e1e5ed; border-radius: 7px;
    padding: 7px 11px; min-height: 20px;
}
QToolButton[iconOnly="true"] {
    padding: 0; min-height: 32px; max-height: 32px; min-width: 32px; max-width: 32px;
}
QToolButton[iconOnly="true"][splitMenu="true"] { min-width: 44px; max-width: 44px; }
QToolButton[iconOnly="true"][historyAction="true"] { min-width: 30px; max-width: 30px; }
QToolButton[iconOnly="true"][smallAction="true"] { min-width: 27px; max-width: 27px; }
QPushButton[iconOnly="true"] {
    padding: 0; min-height: 26px; max-height: 26px; min-width: 26px; max-width: 26px;
}
QPushButton:hover, QToolButton:hover { background: #f1f4fb; border-color: #cbd4ea; }
QPushButton:pressed, QToolButton:pressed { background: #e6ecfc; }
QPushButton:focus, QToolButton:focus { border-color: #4265e8; }
QToolButton::menu-button { width: 12px; border: 0; border-left: 1px solid #e1e5ed; background: transparent; }
QToolButton::menu-button:hover { background: #edf1ff; }
QToolButton[iconOnly="true"]::menu-arrow {
    image: url("__DOWN_ICON__"); width: 10px; height: 10px;
}
QToolButton[iconOnly="true"]::menu-indicator { image: none; }
QFrame#toolSeparator {
    background: #dfe4ed; border: 0; border-radius: 0;
}
QPushButton:checked, QToolButton:checked { background: #edf1ff; color: #4265e8; border-color: #ced8ff; }
QPushButton:disabled, QToolButton:disabled { color: #a4abba; background: #f5f6f9; border-color: #e7eaf0; }
QPushButton#primary, QPushButton#commit { background: #4265e8; color: white; border-color: #4265e8; }
QPushButton#primary:hover, QPushButton#commit:hover { background: #3354d2; }
QPushButton#primary:disabled, QPushButton#commit:disabled { background: #c7d0ec; border-color: #c7d0ec; }
QPushButton#quiet { background: transparent; border-color: transparent; color: #747d8d; }
QPushButton#quiet:checked { background: #edf1ff; color: #4265e8; }
QPushButton#quiet:hover { background: #eef1f7; }
QToolButton#longScreenshot {
    background: #edf1ff; color: #3354d2; border-color: #cbd7ff;
    padding: 0 9px; min-height: 32px; font-weight: 600;
}
QToolButton#longScreenshot:hover { background: #dfe7ff; border-color: #9eb2f4; }
QToolButton#longScreenshot:disabled {
    color: #a4abba; background: #f5f6f9; border-color: #e7eaf0;
}
QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox {
    background: white; border: 1px solid #e1e5ed; border-radius: 7px;
    padding: 6px 8px; min-height: 20px; selection-background-color: #dce4ff;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QDateEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus { border-color: #4265e8; }
QComboBox::drop-down, QDateEdit::drop-down { border: 0; width: 21px; }
QComboBox#targetLanguage, QComboBox#modelChoice { padding-right: 30px; }
QComboBox#targetLanguage::drop-down, QComboBox#modelChoice::drop-down {
    width: 28px; border-left: 1px solid #e1e5ed;
}
QComboBox#targetLanguage::down-arrow, QComboBox#modelChoice::down-arrow {
    image: url("__DOWN_ICON__"); width: 14px; height: 14px;
}
QComboBox#targetLanguage QLineEdit, QComboBox#modelChoice QLineEdit {
    background: transparent; border: 0; border-radius: 0; padding: 0; min-height: 0;
}
QPlainTextEdit {
    background: #fafbfc; border: 1px solid #e5e8ef; border-radius: 7px;
    padding: 7px; selection-background-color: #dce4ff;
}
QPlainTextEdit#textPreview { background: white; border: 0; font-size: 14px; }
QListView, QListWidget { background: transparent; border: 0; outline: 0; }
QListWidget::item { padding: 7px 6px; border-radius: 5px; }
QListWidget::item:selected { background: #edf1ff; color: #202633; }
QListWidget::item:hover { background: #f4f6fb; }
QScrollArea { border: 0; background: transparent; }
QSplitter::handle { background: transparent; width: 14px; }
QSplitter#textSplitter::handle:vertical {
    height: 10px; background: #eef1f6; border-radius: 4px; margin: 2px 0;
}
QSplitter#textSplitter::handle:vertical:hover { background: #dce4fb; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 0; }
QScrollBar::handle:vertical { background: #c8ceda; min-height: 26px; border-radius: 3px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QMenu { background: white; border: 1px solid #e1e5ed; border-radius: 6px; padding: 5px; }
QMenu::item { padding: 8px 24px 8px 10px; border-radius: 4px; }
QMenu::item:selected { background: #edf1ff; }
QMenu::item:disabled { color: #a4abba; }
QMenu::separator { height: 1px; background: #e6e9ef; margin: 5px; }
QToolTip { background: #202633; color: white; border: 0; padding: 5px 8px; }
QCheckBox { spacing: 6px; }
QCheckBox::indicator { width: 15px; height: 15px; }
QCheckBox::indicator:unchecked, QListView::indicator:unchecked {
    background: white; border: 1px solid #b9c2d4; border-radius: 3px;
}
QCheckBox::indicator:checked, QListView::indicator:checked {
    background: #4265e8; border: 1px solid #4265e8; border-radius: 3px;
    image: url("__CHECK_ICON__");
}
QListView::indicator { width: 15px; height: 15px; }
""".replace("__CHECK_ICON__", (Path(__file__).parent / "assets" / "check.svg").as_posix()).replace(
    "__DOWN_ICON__", (Path(__file__).parent / "assets" / "chevron-down.svg").as_posix())


def icon(name, color=MUTED):
    """使用统一 24×24 矢量网格，窗口缩放由 Qt 负责。"""
    image = QPixmap(48, 48)
    image.fill(Qt.GlobalColor.transparent)
    image.setDevicePixelRatio(2)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(color), 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                 Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    paths = {
        "crop": [(6, 2), (6, 18), (22, 18), None, (2, 6), (18, 6), (18, 22)],
        "select": [(5, 3), (5, 20), (10, 15), (14, 22), (17, 20), (13, 13), (21, 12), (5, 3)],
        "arrow": [(4, 20), (20, 4), None, (10, 4), (20, 4), (20, 14)],
        "line": [(4, 20), (20, 4)],
        "long-shot": [(5, 8), (5, 3), (19, 3), (19, 8), None,
                      (5, 16), (5, 21), (19, 21), (19, 16), None,
                      (12, 7), (12, 17), None, (8, 13), (12, 17), (16, 13)],
        "text": [(3, 4), (21, 4), None, (12, 4), (12, 21), None, (8, 21), (16, 21)],
        "undo": [(9, 5), (3, 11), (9, 17), None, (3, 11), (15, 11), (20, 15), (20, 20)],
        "redo": [(15, 5), (21, 11), (15, 17), None, (21, 11), (9, 11), (4, 15), (4, 20)],
        "pen": [(3, 20), (6, 13), (17, 3), (21, 7), (10, 18), (3, 20), None, (14, 6), (18, 10)],
        "delete": [(3, 7), (21, 7), None, (9, 7), (9, 3), (15, 3), (15, 7), None,
                   (6, 7), (7, 21), (17, 21), (18, 7), None, (10, 11), (10, 17),
                   None, (14, 11), (14, 17)],
        "close": [(6, 6), (18, 18), None, (18, 6), (6, 18)],
        "pin": [(8, 3), (16, 3), None, (9, 3), (9, 10), (5, 15), (19, 15), (15, 10), (15, 3),
                None, (12, 15), (12, 22)],
        "star": [(12, 2), (15, 8), (22, 9), (17, 14), (18, 21), (12, 18),
                 (6, 21), (7, 14), (2, 9), (9, 8), (12, 2)],
    }
    if name in paths:
        drawing = QPainterPath()
        beginning = True
        for point in paths[name]:
            if point is None:
                beginning = True
            elif beginning:
                drawing.moveTo(*point)
                beginning = False
            else:
                drawing.lineTo(*point)
        p.drawPath(drawing)
    elif name == "mosaic":
        p.setPen(Qt.PenStyle.NoPen)
        for x, y in [(3, 3), (13, 3), (3, 13), (13, 13)]:
            p.setBrush(QColor(color if x == y else "#b5becf"))
            p.drawRoundedRect(QRectF(x, y, 8, 8), 1, 1)
    elif name == "cover":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(3, 4, 18, 16), 3, 3)
    elif name == "calendar":
        p.drawRoundedRect(QRectF(3, 5, 18, 17), 3, 3)
        for a, b in [((3, 10), (21, 10)), ((8, 2), (8, 7)), ((16, 2), (16, 7))]:
            p.drawLine(QPointF(*a), QPointF(*b))
        p.fillRect(QRectF(7, 14, 3, 3), QColor(color))
    elif name in ("more", "grip"):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        positions = [(5, 12), (12, 12), (19, 12)] if name == "more" else [
            (9, 6), (15, 6), (9, 12), (15, 12), (9, 18), (15, 18)]
        for x, y in positions:
            p.drawEllipse(QPointF(x, y), 1.25, 1.25)
    p.end()
    return QIcon(image)
