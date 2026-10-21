"""可拖动的编辑控件；只移动界面，不改变图片坐标和编辑版本。"""
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLayout, QPushButton, QVBoxLayout, QWidget

from ui_theme import icon


class DragHandle(QWidget):
    def __init__(self, panel, title=""):
        super().__init__(panel)
        self.panel, self.offset = panel, None
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip("按住这里拖动")
        if title:
            layout = QHBoxLayout(self)
            layout.setContentsMargins(12, 9, 8, 9)
            grip = QLabel()
            grip.setPixmap(icon("grip").pixmap(18, 18))
            grip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(grip)
            label = QLabel(title)
            label.setObjectName("panelTitle")
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(label, 1)
            close = QPushButton()
            close.setObjectName("quiet")
            close.setProperty("iconOnly", True)
            close.setIcon(icon("close"))
            close.setFixedSize(28, 28)
            close.setToolTip("收起面板，保留本次内容")
            close.clicked.connect(panel.hide_by_user)
            layout.addWidget(close)
        else:
            self.setFixedWidth(18)
            self.setMinimumHeight(30)

    def paintEvent(self, event):
        if not self.layout():
            painter = QPainter(self)
            icon("grip").paint(painter, self.rect())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.offset = event.globalPosition().toPoint() - self.panel.mapToGlobal(QPoint())
            self.panel.manually_placed = True
            self.grabMouse()
        event.accept()

    def mouseMoveEvent(self, event):
        if self.offset is not None:
            target = self.panel.parentWidget().mapFromGlobal(event.globalPosition().toPoint() - self.offset)
            self.panel.move(self.panel.clamped(target))
            self.panel.raise_()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.offset is not None:
            self.offset = None
            self.releaseMouse()
        event.accept()


class ResizeHandle(QWidget):
    """浮动子面板自行缩放，避免 QSizeGrip 意外缩放整个截图窗口。"""

    def __init__(self, panel, edges):
        super().__init__(panel)
        self.panel, self.edges = panel, edges
        self.start = self.original = None
        cursors = {
            ("left",): Qt.CursorShape.SizeHorCursor,
            ("right",): Qt.CursorShape.SizeHorCursor,
            ("top",): Qt.CursorShape.SizeVerCursor,
            ("bottom",): Qt.CursorShape.SizeVerCursor,
            ("left", "bottom"): Qt.CursorShape.SizeBDiagCursor,
            ("right", "bottom"): Qt.CursorShape.SizeFDiagCursor,
        }
        self.setCursor(cursors[edges])
        self.setToolTip("拖动调整面板大小")

    def paintEvent(self, event):
        if self.edges == ("right", "bottom"):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor("#a5afc0"), 1.4))
            for inset in (4, 8, 12):
                painter.drawLine(self.width()-inset, self.height()-4,
                                 self.width()-4, self.height()-inset)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start = event.globalPosition().toPoint()
            self.original = self.panel.geometry()
            self.panel.manually_placed = True
            self.grabMouse()
        event.accept()

    def mouseMoveEvent(self, event):
        if self.start is not None:
            delta = event.globalPosition().toPoint() - self.start
            bounds = self.panel.placement_bounds()
            left, top = self.original.x(), self.original.y()
            right, bottom = left+self.original.width(), top+self.original.height()
            min_width = min(self.panel.resize_minimum[0], bounds.width())
            min_height = min(self.panel.resize_minimum[1], bounds.height())
            if "left" in self.edges:
                left = max(bounds.left(), min(left+delta.x(), right-min_width))
            if "right" in self.edges:
                right = min(bounds.right()+1, max(right+delta.x(), left+min_width))
            if "top" in self.edges:
                top = max(bounds.top(), min(top+delta.y(), bottom-min_height))
            if "bottom" in self.edges:
                bottom = min(bounds.bottom()+1, max(bottom+delta.y(), top+min_height))
            self.panel.preferred_size = (right-left, bottom-top)
            self.panel.setGeometry(QRect(left, top, right-left, bottom-top))
            self.panel.raise_()
            self.panel.size_changed.emit()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.start is not None:
            self.start = self.original = None
            self.releaseMouse()
        event.accept()


class FloatingPanel(QFrame):
    visibility_changed = Signal()
    size_changed = Signal()

    def __init__(self, parent, title=None, name="properties", *, resizable=False):
        super().__init__(parent)
        self.setObjectName(name)
        self.manually_placed = False
        self.user_hidden = False
        self.preferred_size = None
        self.available_bounds = None
        self.resize_minimum = (300, 350)
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(1, 1, 1, 1)
        self.outer.setSpacing(0)
        self.handle = DragHandle(self, title) if title else None
        if self.handle:
            self.outer.addWidget(self.handle)
        self.resize_handles = {}
        if resizable:
            # 小窗口由内部滚动区承接内容，不让布局最小高度把面板撑出屏幕。
            self.outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
            for edges in (("left",), ("right",), ("top",), ("bottom",),
                          ("left", "bottom"), ("right", "bottom")):
                self.resize_handles[edges] = ResizeHandle(self, edges)

    def clamped(self, point):
        bounds = self.placement_bounds()
        return QPoint(max(bounds.left(), min(point.x(), bounds.right() - self.width() + 1)),
                      max(bounds.top(), min(point.y(), bounds.bottom() - self.height() + 1)))

    def placement_bounds(self):
        return self.available_bounds if self.available_bounds is not None else \
            self.parentWidget().rect().adjusted(8, 8, -8, -8)

    def place(self, x, y, width, height):
        # 保留用户尺寸与位置；窗口缩小时临时夹紧，重新放大可恢复偏好尺寸。
        if self.preferred_size:
            width, height = self.preferred_size
        bounds = self.placement_bounds()
        width = min(width, max(1, bounds.width()))
        max_height = bounds.height() if self.available_bounds is not None else self.parentWidget().height()-48
        height = min(height, max(1, max_height))
        self.resize(width, height)
        self.move(self.clamped(self.pos() if self.manually_placed else QPoint(x, y)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "resize_handles") and self.resize_handles:
            width, height = self.width(), self.height()
            geometry = {
                ("left",): (0, 6, 6, max(1, height-24)),
                ("right",): (width-6, 6, 6, max(1, height-24)),
                ("top",): (0, 0, width, 6),
                ("bottom",): (18, height-6, max(1, width-36), 6),
                ("left", "bottom"): (0, height-18, 18, 18),
                ("right", "bottom"): (width-18, height-18, 18, 18),
            }
            for edges, handle in self.resize_handles.items():
                handle.setGeometry(*geometry[edges])
                handle.raise_()

    def hide_by_user(self):
        self.user_hidden = True
        self.hide()

    def showEvent(self, event):
        super().showEvent(event)
        self.visibility_changed.emit()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.visibility_changed.emit()
