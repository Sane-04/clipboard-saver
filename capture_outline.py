"""长截图范围提示：四条独立边线，选区外的部分无需在采集时闪烁。"""
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget


class OutlineBand(QWidget):
    def __init__(self, area):
        super().__init__()
        self.color = "#448aff"
        self.setWindowTitle("剪贴收藏 · 长截图范围")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.WindowTransparentForInput |
                            Qt.WindowType.WindowDoesNotAcceptFocus |
                            Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setGeometry(area)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(self.color))
        # 白色细线使边框在深浅背景上都容易辨认；全部像素只属于提示窗口。
        painter.setPen(QColor("white"))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))


class CaptureOutline:
    def __init__(self, selection, screen):
        width = 4
        left, top = selection.x(), selection.y()
        right, bottom = left + selection.width(), top + selection.height()
        # Qt 窗口使用逻辑坐标。向外绘制不会改变原像素范围，包括高 DPI 的奇数边界。
        areas = [
            QRect(left - width, top - width, selection.width() + width * 2, width),
            QRect(left - width, bottom, selection.width() + width * 2, width),
            QRect(left - width, top, width, selection.height()),
            QRect(right, top, width, selection.height()),
        ]
        fallbacks = [
            QRect(left, top, selection.width(), width),
            QRect(left, bottom - width, selection.width(), width),
            QRect(left, top, width, selection.height()),
            QRect(right - width, top, width, selection.height()),
        ]
        self.bands = []
        for area, fallback in zip(areas, fallbacks):
            # 贴屏幕边缘时边线向内回退；控制器会在两次稳定性读取之前隐藏重叠边线。
            clipped = area.intersected(screen)
            if clipped.width() < min(width, area.width()) or clipped.height() < min(width, area.height()):
                clipped = fallback.intersected(screen)
            if not clipped.isEmpty():
                self.bands.append(OutlineBand(clipped))

    def show(self):
        for band in self.bands:
            band.show()

    def set_paused(self, paused):
        for band in self.bands:
            band.color = "#d5972a" if paused else "#448aff"
            band.update()

    def dispose(self):
        for band in self.bands:
            band.hide()
            band.deleteLater()
        self.bands = []
