"""独立只读图片窗口；保留自己的原像素快照，主历史窗口隐藏时仍显示。"""
from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMainWindow, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ui_theme import STYLE
from window_pin import TopmostButton


class ImageSurface(QWidget):
    def __init__(self, image):
        super().__init__()
        self.image = image.copy()
        self.image.setDevicePixelRatio(1)
        self.display_size = self.image.size()
        tile = QPixmap(24, 24)
        tile.fill(QColor("white"))
        painter = QPainter(tile)
        painter.fillRect(12, 0, 12, 12, QColor("#eceef3"))
        painter.fillRect(0, 12, 12, 12, QColor("#eceef3"))
        painter.end()
        self.checker = QBrush(tile)

    def paintEvent(self, event):
        painter = QPainter(self)
        target = QRectF(self.rect())
        # 放大长图时只填本次可见脏区；用Qt平铺纹理，不在Python逐格遍历整张长图。
        painter.fillRect(event.rect(), self.checker)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(target, self.image)

    def set_scale(self, factor):
        self.display_size = QSize(max(1, round(self.image.width() * factor)),
                                  max(1, round(self.image.height() * factor)))
        self.setFixedSize(self.display_size)
        self.update()


class ImageViewer(QMainWindow):
    closed = Signal(object)

    def __init__(self, image, *, original_size=False):
        # 不设 HistoryWindow 为父窗口；Qt 的父子显隐关系会使图片随主窗口消失。
        super().__init__(None)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f"图片查看 · {image.width()} × {image.height()} · 剪贴收藏")
        self.setStyleSheet(STYLE)
        self.setMinimumSize(320, 240)
        self.fit = not original_size
        self.zoom_factor, self.zoom_serial = 1.0, 0
        self.surface = ImageSurface(image)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 10, 12, 12)
        bar = QHBoxLayout()
        self.dimensions = QLabel(f"{image.width()} × {image.height()}")
        self.dimensions.setObjectName("muted")
        bar.addWidget(self.dimensions, 1)
        for title, fit in (("适应窗口", True), ("100%", False)):
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, value=fit: self.set_fit(value))
            bar.addWidget(button)
        self.topmost_button = TopmostButton(self)
        bar.addWidget(self.topmost_button)
        layout.addLayout(bar)
        self.setMinimumWidth(max(430, bar.sizeHint().width() + 24))
        self.scroll = QScrollArea()
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setWidget(self.surface)
        self.scroll.viewport().installEventFilter(self)
        self.surface.installEventFilter(self)
        self.scroll.setToolTip("Ctrl＋向上滚轮放大，Ctrl＋向下滚轮缩小；普通滚轮滚动图片")
        layout.addWidget(self.scroll, 1)
        available = self.screen().availableGeometry()
        self.resize(min(max(480, image.width() + 30), int(available.width() * .8)),
                    min(max(360, image.height() + 90), int(available.height() * .8)))
        self.escape = QShortcut(QKeySequence("Escape"), self)
        self.escape.activated.connect(self.close)
        self.rescale()

    def set_fit(self, fit):
        self.zoom_serial += 1
        self.fit = fit
        if not fit:
            self.zoom_factor = 1
        self.rescale()

    def fit_factor(self):
        # 适应窗口按滚动条收起后的空间计算，避免从放大状态复位时缩小两次。
        source, area = self.surface.image.size(), self.scroll.maximumViewportSize()
        return min(1, max(1, area.width() - 4) / source.width(),
                   max(1, area.height() - 4) / source.height())

    def rescale(self):
        if not hasattr(self, "scroll"):
            return
        if self.fit:
            self.zoom_factor = self.fit_factor()
        self.surface.set_scale(self.zoom_factor)
        self.statusBar().showMessage(f"显示 {self.zoom_factor * 100:.1f}% · Ctrl＋滚轮缩放 · 普通滚轮滚动")

    def set_zoom(self, factor, *, pixel=None, position=None):
        self.zoom_serial += 1
        token = self.zoom_serial
        self.fit = False
        self.zoom_factor = max(min(.01, self.fit_factor(), self.zoom_factor), min(8, factor))
        self.rescale()
        if pixel is None:
            return
        def anchor():
            if token != self.zoom_serial:
                return
            mapped = QPointF(self.surface.mapTo(self.scroll.viewport(), QPoint())) + pixel * self.zoom_factor
            delta = mapped - position
            horizontal, vertical = self.scroll.horizontalScrollBar(), self.scroll.verticalScrollBar()
            horizontal.setValue(horizontal.value() + round(delta.x()))
            vertical.setValue(vertical.value() + round(delta.y()))
        anchor()
        # 滚动条显隐会重新布局；只允许最新滚轮事件在下一轮校准，避免快速缩放跳回。
        QTimer.singleShot(0, self, anchor)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120 if event.angleDelta().y() else event.pixelDelta().y() / 60
            if steps:
                origin = QPointF(self.surface.mapTo(self.scroll.viewport(), QPoint()))
                position = QPointF(watched.mapTo(self.scroll.viewport(), QPoint())) + event.position()
                pixel = (position - origin) / self.zoom_factor
                if not QRectF(self.surface.image.rect()).contains(pixel):
                    # 鼠标在图片外的空白处时，以当前视口中心缩放。
                    position = QPointF(self.scroll.viewport().rect().center())
                    pixel = (position - origin) / self.zoom_factor
                factor = self.zoom_factor * 1.2 ** max(-10, min(10, steps))
                self.set_zoom(factor, pixel=pixel, position=position)
            event.accept()
            return True
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize and self.fit:
            QTimer.singleShot(0, self, self.rescale)
        if watched is self.surface and event.type() == QEvent.Type.Wheel:
            self.scroll.wheelEvent(event)
            return True
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.rescale()

    def showEvent(self, event):
        super().showEvent(event)
        self.rescale()

    def closeEvent(self, event):
        self.zoom_serial += 1
        self.closed.emit(self)
        super().closeEvent(event)
