"""主动框选截图：先取原始画面，再显示覆盖层。"""

from math import ceil, floor

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QApplication
from image_editor import ImageEditor


def selection_to_pixels(start, end, geometry, pixel_size, ratio):
    """全局逻辑坐标 -> 原始图像像素矩形，采用半开边界而非 QRect 的含端点宽度。"""
    sx, sy, logical_width, logical_height = geometry
    left = max(0, min(start[0], end[0]) - sx)
    top = max(0, min(start[1], end[1]) - sy)
    right = min(logical_width, max(start[0], end[0]) - sx)
    bottom = min(logical_height, max(start[1], end[1]) - sy)
    if right <= left or bottom <= top:
        return None
    px_left, px_top = max(0, floor(left * ratio)), max(0, floor(top * ratio))
    px_right = min(pixel_size[0], ceil(right * ratio))
    px_bottom = min(pixel_size[1], ceil(bottom * ratio))
    if px_right <= px_left or px_bottom <= px_top:
        return None
    return px_left, px_top, px_right - px_left, px_bottom - px_top


class SelectionOverlay(ImageEditor):
    """原屏幕像素保持不变，松开只进入编辑，完成时才发出成品。"""

    def __init__(self, snapshot, geometry, settings_store=None, shortcut_store=None):
        self.snapshot = snapshot
        self.display_ratio = snapshot.devicePixelRatio()
        self.screen_geometry = geometry
        super().__init__(snapshot.toImage(), settings_store, fullscreen=True, geometry=geometry,
                         shortcut_store=shortcut_store)

    def pixel_rect(self):
        if not self.doc.crop:
            return None
        area = self.doc.export_rect()
        return area.x(), area.y(), area.width(), area.height()

    def capture_payload(self):
        pixels = self.pixel_rect()
        if pixels is None:
            return None
        ratio, geometry = self.display_ratio, self.screen_geometry
        # 逻辑区域向外取整，实际采集再裁回原始像素区域，保留高 DPI 的奇数边界。
        from PySide6.QtCore import QRect
        left, top = floor(pixels[0]/ratio), floor(pixels[1]/ratio)
        right, bottom = ceil((pixels[0]+pixels[2])/ratio), ceil((pixels[1]+pixels[3])/ratio)
        logical = QRect(geometry.x()+left, geometry.y()+top, right-left, bottom-top)
        return logical, pixels, ratio, geometry


class ScreenshotController(QObject):
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, window, on_image, parent=None, settings_store=None, on_text=None, shortcut_store=None,
                 overlay_factory=None):
        super().__init__(parent)
        self.window = window
        self.on_image = on_image
        self.settings_store = settings_store
        self.shortcut_store = shortcut_store
        self.on_text = on_text
        self.on_long_capture = None
        self.overlay_factory = overlay_factory or SelectionOverlay
        self.active = False
        self.overlay = None
        self.restore_widgets = []
        self.screen = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._capture)

    def start(self):
        if self.active:
            return
        self.active = True
        self.screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        self.restore_widgets = [self.window] if self.window.isVisible() else []
        for widget in QApplication.topLevelWidgets():
            ancestor = widget.parentWidget()
            while ancestor is not None and ancestor is not self.window:
                ancestor = ancestor.parentWidget()
            if widget is not self.window and widget.isVisible() and ancestor is self.window:
                self.restore_widgets.append(widget)
        popup = QApplication.activePopupWidget()
        if popup is not None:
            popup.hide()
        for widget in self.restore_widgets:
            widget.hide()
        # 非阻塞等待桌面合成器完成隐藏，截图不包含应用自己和托盘菜单。
        self.timer.start(180)

    def _capture(self):
        try:
            snapshot = self.screen.grabWindow(0)
            if snapshot.isNull():
                raise RuntimeError("屏幕捕获为空")
            self.overlay = self.overlay_factory(snapshot, self.screen.geometry(), self.settings_store, self.shortcut_store)
            self.overlay.selected.connect(self._selected)
            self.overlay.long_capture_requested.connect(self.start_long_capture)
            if self.on_text:
                self.overlay.text_copied.connect(self.on_text)
            self.overlay.cancelled.connect(self._cancelled)
            self.overlay.show()
            self.overlay.raise_()
            self.overlay.activateWindow()
            self.overlay.setFocus()
        except RuntimeError:
            self._cancelled()
            self.failed.emit("暂时无法截取这个屏幕，请重试或切换到普通桌面窗口。")

    def _selected(self, image):
        self.active = False
        self.overlay = None
        self.restore_widgets = []
        self.on_image(image)

    def start_long_capture(self):
        if self.on_long_capture is not None and self.overlay is not None:
            self.on_long_capture(self)

    def release_to_long_capture(self):
        """采集完成接管编辑器，原截图控制器不再恢复窗口或提交一次原图。"""
        self.active = False
        self.overlay = None
        self.restore_widgets = []

    def _cancelled(self):
        self.active = False
        self.overlay = None
        for widget in self.restore_widgets:
            widget.show()
        if self.restore_widgets:
            self.window.raise_()
            self.window.activateWindow()
        self.restore_widgets = []
        self.cancelled.emit()

    def stop(self):
        self.timer.stop()
        self.restore_widgets = []
        if self.overlay is not None:
            self.overlay.cancel()
        self.overlay = None
        self.active = False
