"""用户主动框选后采集滚动内容；默认手动，图像匹配在单独工作线程。"""
import ctypes
import os
from copy import copy, deepcopy
from math import floor
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QScreen
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from image_editor import ImageEditor
from image_document import move_object, object_rect, rect_of, rect_tuple
from long_stitch import VerticalStitcher, gray
from long_preview import CapturePreview
from capture_outline import CaptureOutline
from capture_visibility import exclude_capture
from screenshot import ScreenshotController, SelectionOverlay
from ui_theme import STYLE


class LongSelection(SelectionOverlay):
    def __init__(self, *args):
        super().__init__(*args)
        self.setWindowTitle("剪贴收藏 · 长截图选区")
        self.commit_button.setText("开始长截图")
        self.hint.setText("框选滚动内容 · 松开可调整 · 点击开始后自己向下滚动")
        self.hint.adjustSize()
        self.context.hide()
        self.text_entry.hide()
        self.history_tools.hide()
        self.long_capture_button.hide()
        for name, button in self.tool_buttons.items():
            if name != "crop":
                button.hide()
        for button in self.tools.findChildren(QPushButton):
            if button is not self.commit_button and button.text() != "取消":
                button.hide()
        for button in self.tools.findChildren(QComboBox):
            button.hide()
        # 更多菜单在选区阶段不能保存原图或进入识别；只保留选区/开始/取消。
        for button in self.tools.findChildren(QWidget):
            if button.metaObject().className() == "QToolButton" and button not in self.tool_buttons.values():
                button.hide()

    def commit(self):
        if self.finished or not self.doc.crop:
            return
        payload = self.capture_payload()
        self.finished = True
        self.client.cancel()
        self.local_client.cancel()
        self.hide()
        self.selected.emit(payload)
        self.close()


class CaptureBar(QWidget):
    pause_requested = Signal()
    undo_requested = Signal()
    finish_requested = Signal()
    cancel_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("剪贴收藏 · 滚动长截图")
        self.setStyleSheet(STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        row = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItem("手动滚动", "manual")
        self.mode.addItem("自动滚动", "auto")
        row.addWidget(self.mode)
        self.pause = QPushButton("暂停")
        row.addWidget(self.pause)
        self.undo = QPushButton("撤回一段")
        row.addWidget(self.undo)
        self.preview_button = QPushButton("实时预览")
        self.preview_button.setCheckable(True)
        self.preview_button.setChecked(True)
        self.preview_button.setToolTip("显示或隐藏已拼接长图的实时预览")
        row.addWidget(self.preview_button)
        self.finish = QPushButton("完成采集")
        self.finish.setObjectName("primary")
        row.addWidget(self.finish)
        cancel = QPushButton("取消")
        row.addWidget(cancel)
        layout.addLayout(row)
        self.note = QLabel("正在准备 · 请只向下缓慢滚动，保留部分重叠")
        self.note.setWordWrap(True)
        self.note.setMinimumWidth(480)
        layout.addWidget(self.note)
        self.pause.clicked.connect(self.pause_requested)
        self.undo.clicked.connect(self.undo_requested)
        self.finish.clicked.connect(self.finish_requested)
        cancel.clicked.connect(self.cancel_requested)

    def closeEvent(self, event):
        event.ignore()
        self.cancel_requested.emit()


class DesktopSource:
    """只向所选窗口输入；未选择自动模式时绝不发送滚轮或改变鼠标位置。"""
    def __init__(self, screen, logical, pixels, ratio, geometry, *, allow_own=False):
        self.screen, self.logical, self.pixels = screen, logical, pixels
        self.screen_name = screen.name()
        self.ratio, self.geometry = ratio, geometry
        self.overlay_windows = set()
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.api.WindowFromPoint.argtypes = [wintypes.POINT]
        self.api.WindowFromPoint.restype = wintypes.HWND
        self.api.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.api.GetAncestor.restype = wintypes.HWND
        self.api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.api.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.api.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.point = self.physical_center()
        self.hwnd = self.root_window()
        owner = wintypes.DWORD()
        self.api.GetWindowThreadProcessId(self.hwnd, ctypes.byref(owner))
        if not self.hwnd or (owner.value == os.getpid() and not allow_own):
            raise ValueError("请框选其他窗口中的滚动内容。")
        self.window_rect = self.target_rect()

    def physical_center(self):
        class Info(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD), ("device", ctypes.c_wchar*32)]
        origin = [self.geometry.x(), self.geometry.y()]
        callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        self.api.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(Info)]
        @callback
        def collect(monitor, dc, rect, data):
            info = Info()
            info.size = ctypes.sizeof(info)
            if self.api.GetMonitorInfoW(monitor, ctypes.byref(info)) and info.device == self.screen.name():
                origin[:] = [info.monitor.left, info.monitor.top]
            return True
        self.api.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback, wintypes.LPARAM]
        self.api.EnumDisplayMonitors(None, None, collect, 0)
        return wintypes.POINT(origin[0]+self.pixels[0]+self.pixels[2]//2,
                             origin[1]+self.pixels[1]+self.pixels[3]//2)

    def root_window(self):
        window = self.api.WindowFromPoint(self.point)
        root = self.api.GetAncestor(window, 2) if window else None
        # 仅认可已经排除捕获的本工具窗口，不能把其他应用的遮挡当成原目标。
        return self.hwnd if root in self.overlay_windows else root

    def set_capture_overlays(self, widgets):
        self.overlay_windows = {int(widget.winId()) for widget in widgets
                                if getattr(widget, "capture_excluded", False)}

    def prepare_resume(self):
        if self.target_rect() != self.window_rect:
            raise ValueError("原目标窗口已移动，请恢复原位置后继续长截图。")
        if not self.api.SetForegroundWindow(self.hwnd):
            raise ValueError("请先切回原滚动窗口，再点击继续长截图。")

    def target_rect(self):
        rectangle = wintypes.RECT()
        if not self.api.GetWindowRect(self.hwnd, ctypes.byref(rectangle)):
            raise ValueError("目标窗口已关闭。")
        return rectangle.left, rectangle.top, rectangle.right, rectangle.bottom

    def capture(self):
        # Qt可能在窗口销毁/显示设备更新后更换QScreen对象，不能继续使用失效的旧包装对象。
        screen = next((item for item in QGuiApplication.screens() if item.name() == self.screen_name), None)
        if screen is None or screen.geometry() != self.geometry or screen.devicePixelRatio() != self.ratio:
            raise ValueError("屏幕或缩放发生变化，请重新框选。")
        if self.root_window() != self.hwnd or self.target_rect() != self.window_rect:
            raise ValueError("目标被遮挡、移动或切换，已暂停；恢复原窗口位置后继续。")
        x, y = self.logical.x()-self.geometry.x(), self.logical.y()-self.geometry.y()
        frame = screen.grabWindow(0, x, y, self.logical.width(), self.logical.height())
        if frame.isNull():
            raise ValueError("无法截取目标画面。")
        image = frame.toImage()
        # 全屏截图中的选区可能落在半个逻辑像素上；抓扩大的逻辑区域，再按像素裁切。
        offset_x = self.pixels[0]-floor(x*self.ratio+.5)
        offset_y = self.pixels[1]-floor(y*self.ratio+.5)
        area = QRect(offset_x, offset_y, self.pixels[2], self.pixels[3])
        if not image.rect().contains(area):
            raise ValueError("捕获区域像素尺寸改变，请重新框选。")
        return image.copy(area)

    def scroll(self):
        if self.root_window() != self.hwnd or self.target_rect() != self.window_rect:
            raise ValueError("目标窗口已变化，自动滚动已暂停。")
        if not self.api.SetForegroundWindow(self.hwnd):
            raise ValueError("自动滚动未能激活目标，请切换手动滚动。")
        self.api.SetCursorPos(self.point.x, self.point.y)
        # 按完整滚轮刻度滚动，保留足够重叠；匹配失败即暂停，不继续向下输入。
        class MouseInput(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("data", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]
        class Payload(ctypes.Union):
            _fields_ = [("mouse", MouseInput)]
        class Input(ctypes.Structure):
            _fields_ = [("kind", wintypes.DWORD), ("payload", Payload)]
        self.api.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.api.SendInput.restype = wintypes.UINT
        delta = -120 if self.logical.height() < 280 else -240
        event = Input(0, Payload(mouse=MouseInput(0, 0, ctypes.c_uint32(delta).value, 0x0800, 0, 0)))
        if self.api.SendInput(1, ctypes.byref(event), ctypes.sizeof(Input)) != 1:
            raise ValueError("Windows 未接受滚动输入，请使用手动模式。")


class LongScreenshotController(QObject):
    failed = Signal(str)
    task_done = Signal(int, str, object, str)

    def __init__(self, window, on_image, parent=None, settings_store=None, on_text=None,
                 shortcut_store=None, *, source_factory=DesktopSource):
        super().__init__(parent)
        self.window, self.on_image, self.on_text = window, on_image, on_text
        self.settings_store, self.shortcut_store = settings_store, shortcut_store
        self.source_factory = source_factory
        self.selector = ScreenshotController(window, self.begin, self, settings_store, None,
                                             shortcut_store, overlay_factory=LongSelection)
        self.selector.failed.connect(self.failed)
        self.selector.cancelled.connect(self.cancel)
        self.active, self.paused, self.busy = False, False, False
        self.bar = self.preview = self.editor = self.source = self.stitcher = self.executor = None
        self.outline = None
        self.return_editor = self.return_owner = None
        self.payload = self.screen = None
        self.layers = []
        self.serial = 0
        self.pending_finish = False
        self.recovering = False
        self.unchanged = 0
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.sample)
        self.task_done.connect(self.delivered, Qt.ConnectionType.QueuedConnection)

    def start(self):
        if self.active:
            return
        self.active = True
        self.selector.start()

    def start_from_screenshot(self, owner):
        if self.active or owner.overlay is None:
            return
        editor = owner.overlay
        payload = editor.capture_payload()
        if payload is None:
            return
        area = editor.doc.export_rect()
        layers = []
        for obj in editor.doc.objects:
            if object_rect(obj).intersects(area):
                obj = deepcopy(obj)
                move_object(obj, QPointF(-area.x(), -area.y()))
                if "source_rect" in obj:
                    obj["source_rect"] = rect_tuple(rect_of(obj["source_rect"]).translated(-area.x(), -area.y()))
                layers.append(obj)
        # 拼接基准必须是未标注的原始像素；标注独立保留，不能干扰重叠匹配。
        first = editor.doc.base.copy(area)
        self.return_editor, self.return_owner = editor, owner
        editor.suspend_for_capture()
        self.begin(payload, screen=owner.screen, first=first, layers=layers)

    def continue_from_editor(self):
        editor = self.editor
        if editor is None or editor.finished:
            return
        payload, screen, previous, target = editor.long_capture_session
        # 浅拷贝像素数据由 Qt 共享；段列表独立，取消追加或撤回不改变原编辑会话。
        stitcher = copy(previous)
        stitcher.parts = list(previous.parts)
        stitcher.previous = previous.previous.copy()
        self.return_editor, self.return_owner = editor, None
        self.editor = None
        editor.suspend_for_capture()
        self.begin(payload, screen=screen, resume=stitcher, target=target, layers=deepcopy(editor.doc.objects))

    def begin(self, payload, *, screen=None, first=None, layers=None, resume=None, target=None):
        self.serial += 1
        self.active, self.paused, self.busy = True, False, False
        self.pending_finish, self.unchanged = False, 0
        self.recovering = False
        self.payload = payload
        self.screen = screen if screen is not None else self.selector.screen
        if isinstance(self.screen, QScreen):
            self.screen = QGuiApplication.screenAt(payload[0].center())
        self.layers = layers or []
        self.bar = CaptureBar()
        logical, pixels, ratio, geometry = payload
        self.outline = CaptureOutline(logical, geometry)
        self.bar.move(geometry.x()+12, geometry.y()+12)
        self.bar.pause_requested.connect(self.toggle_pause)
        self.bar.undo_requested.connect(self.undo)
        self.bar.finish_requested.connect(self.finish)
        self.bar.cancel_requested.connect(self.cancel)
        self.bar.mode.currentIndexChanged.connect(self.mode_changed)
        self.bar.preview_button.toggled.connect(self.set_preview_visible)
        self.bar.adjustSize()
        # 优先避开内容区；没有空位时采集前隐藏工具条，捕获后恢复。
        if logical.top()-geometry.top() >= self.bar.height()+18:
            self.bar.move(geometry.x()+12, logical.top()-self.bar.height()-8)
        elif logical.bottom()+self.bar.height()+18 <= geometry.bottom():
            self.bar.move(geometry.x()+12, logical.bottom()+8)
        self.preview = CapturePreview()
        self.preview.hidden_requested.connect(lambda: self.bar.preview_button.setChecked(False))
        available = self.screen.availableGeometry() if isinstance(self.screen, QScreen) else geometry
        self.preview.place_outside(logical, available, self.bar.geometry().adjusted(-4, -28, 4, 4))
        # 等选择覆盖层销毁后才识别底下的目标窗口，避免把本程序当作目标。
        token = self.serial
        QTimer.singleShot(230, lambda: self.initialize(token, payload, first=first, resume=resume, target=target))

    def initialize(self, token, payload, *, first=None, resume=None, target=None):
        if token != self.serial or not self.active:
            return
        try:
            self.source = target if target is not None else self.source_factory(self.screen, *payload)
            if target is not None and hasattr(target, "prepare_resume"):
                target.prepare_resume()
            widgets = (self.bar, self.preview, *self.outline.bands)
            for widget in widgets:
                exclude_capture(widget)
            if hasattr(self.source, "set_capture_overlays"):
                self.source.set_capture_overlays(widgets)
            current = self.source.capture()
            self.stitcher = resume if resume is not None else VerticalStitcher(
                first if first is not None else current)
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clipboard-long-shot")
            self.update_preview()
            self.preview.set_live_frame(current)
            self.restore_overlays()
            self.update_note("默认手动 · 在内容区缓慢向下滚动")
            if first is not None or resume is not None:
                # 点击前页面可能已滚动；只有确认重叠后才接续，不丢弃原截图重新起算。
                self.submit("add", lambda: self.stitcher.add(current))
            else:
                self.timer.start(120)
        except (ValueError, RuntimeError) as error:
            self.failed.emit(str(error))
            self.cancel()

    def mode_changed(self):
        if self.bar is not None:
            self.pause_with("模式已切换，点击继续后开始；自动模式会滚动选区目标。")

    def sample(self):
        if not self.active or self.paused or self.busy or self.pending_finish:
            return
        hidden = self.hide_overlaps()
        token = self.serial
        if self.bar.mode.currentData() == "auto" and not self.recovering:
            try:
                self.source.scroll()
            except (ValueError, RuntimeError) as error:
                self.restore_overlays()
                self.pause_with(str(error))
                return
            QTimer.singleShot(300, lambda: self.capture_frame(token))
        else:
            if hidden:
                QTimer.singleShot(110, lambda: self.capture_frame(token))
            else:
                self.capture_frame(token)

    def capture_frame(self, token):
        if token != self.serial or not self.active or self.paused or self.pending_finish:
            if self.bar and self.active:
                self.restore_overlays()
            return
        # 用户可在采集间隙移动预览；每次读取前重新检查，等合成器完成隐藏再截图。
        if self.hide_overlaps():
            QTimer.singleShot(110, lambda: self.capture_frame(token))
            return
        try:
            image = self.source.capture()
            self.preview.set_live_frame(image, "正在核对")
            QTimer.singleShot(35, lambda: self.capture_stable(token, image))
        except (ValueError, RuntimeError) as error:
            self.restore_overlays()
            self.pause_with(str(error))

    def capture_stable(self, token, first):
        if token != self.serial or not self.active:
            return
        if self.paused or self.pending_finish:
            self.restore_overlays()
            return
        if self.hide_overlaps():
            QTimer.singleShot(110, lambda: self.capture_stable(token, first))
            return
        try:
            image = self.source.capture()
            self.preview.set_live_frame(image, "正在核对")
            self.restore_overlays()
            if image.size() != first.size() or np.mean(np.abs(gray(first).astype(float)-gray(image))) > 1.2:
                self.update_note("画面仍在移动，请稍停一下")
                self.preview.set_live_status("滚动中 · 尚未接入")
                self.timer.start(60)
                return
            stitcher = self.stitcher
            self.submit("add", lambda: stitcher.add(image))
        except (ValueError, RuntimeError) as error:
            self.restore_overlays()
            self.pause_with(str(error))

    def hide_overlaps(self):
        """仅隐藏进入原选区的自身窗口，区域外的预览保持可见，不每帧闪烁。"""
        hidden = False
        selection = self.payload[0]
        bands = self.outline.bands if self.outline is not None else []
        for widget in (self.bar, self.preview, *bands):
            if widget is not None and widget.isVisible() and \
                    not getattr(widget, "capture_excluded", False) and \
                    (widget.frameGeometry() if widget in bands else
                     widget.frameGeometry().adjusted(-2, -2, 2, 2)).intersects(selection):
                widget.hide()
                hidden = True
        return hidden

    def restore_overlays(self):
        if self.active and self.bar is not None:
            if self.outline is not None:
                self.outline.show()
            self.bar.show()
            if self.preview is not None and self.bar.preview_button.isChecked():
                self.preview.show()

    def set_preview_visible(self, visible):
        if self.preview is None:
            return
        if visible and self.stitcher is not None:
            self.preview.show()
        else:
            self.preview.hide()

    def update_preview(self, added=None):
        if self.preview is not None and self.stitcher is not None:
            self.preview.set_content(self.stitcher.parts, self.stitcher.width,
                                     self.stitcher.height, added, self.layers)

    def submit(self, kind, operation):
        self.busy = True
        token = self.serial
        future = self.executor.submit(operation)
        def complete(future):
            try:
                result, error = future.result(), ""
            except Exception:
                result, error = None, "处理未完成，已有内容保留；可重新框选。"
            try:
                self.task_done.emit(token, kind, result, error)
            except RuntimeError:
                pass
        future.add_done_callback(complete)

    def delivered(self, token, kind, result, error):
        if token != self.serial or not self.active:
            return
        self.busy = False
        if error:
            self.pending_finish = False
            self.pause_with(error)
            return
        if kind == "render":
            self.open_editor(result)
            return
        if kind == "add":
            if result.added:
                self.update_preview(result.added)
            self.unchanged = self.unchanged+1 if result.status == "duplicate" else 0
            reasons = {
                "size_changed": "选区尺寸改变，请重新框选。",
                "limit": "已达到容量上限，请完成现有内容或取消后缩小范围。",
            }
            self.recovering = result.status in ("unmatched", "ambiguous")
            self.preview.set_live_status(
                "已接入" if result.added else "与已拼接内容相同" if result.status == "duplicate" else
                "尚未接入 · 等待恢复重叠",
                accepted=result.status in ("append", "duplicate"))
            if self.recovering:
                self.update_note("重叠不足或画面重复 · 请向上退回一点后小幅向下滚动，恢复后自动继续")
            elif result.status in reasons:
                self.pause_with(reasons[result.status])
            elif self.bar.mode.currentData() == "auto" and self.unchanged >= 3:
                self.pause_with("没有新增画面，可能已到底或自动滚动未生效；可完成或切换手动。")
            else:
                self.update_note("已拼接新内容" if result.added else "画面尚未变化 · 请继续向下缓慢滚动")
        else:
            self.update_preview()
            self.pause_with("已撤回最近一段；请把页面退回至重叠位置，再继续。")
        if self.pending_finish:
            self.submit("render", self.stitcher.render)
        elif not self.paused:
            self.timer.start(120)

    def update_note(self, message):
        if self.bar:
            if self.outline is not None:
                self.outline.set_paused(self.paused)
            height = self.stitcher.height if self.stitcher else 0
            self.bar.note.setText(f"边框内为采集范围 · 当前长度 {height} 像素 · {message}")
            self.bar.undo.setEnabled(self.stitcher is not None and len(self.stitcher.parts) > 1 and not self.busy)
            self.bar.pause.setText("继续" if self.paused else "暂停")

    def pause_with(self, message):
        self.paused = True
        self.timer.stop()
        self.update_note(message)

    def toggle_pause(self):
        if self.stitcher is None or self.pending_finish:
            return
        self.paused = not self.paused
        self.update_note("已暂停，可完成采集" if self.paused else "继续采集 · 保留足够重叠")
        if not self.paused and not self.busy:
            self.timer.start(300)

    def undo(self):
        if self.busy or not self.stitcher:
            return
        self.paused = True
        self.timer.stop()
        self.submit("undo", self.stitcher.undo)

    def finish(self):
        if self.stitcher is None or self.pending_finish:
            return
        self.timer.stop()
        self.pending_finish = True
        if not self.busy:
            self.submit("render", self.stitcher.render)

    def open_editor(self, image):
        self.serial += 1  # 编辑阶段不再接受采集定时器/旧工作任务的回调。
        self.bar.hide()
        self.bar.deleteLater()
        self.bar = None
        self.preview.dispose()
        self.preview = None
        self.outline.dispose()
        self.outline = None
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.executor = None
        # 接续保留同一个捕获目标和窗口位置，不能按编辑器关闭后的前台画面重新猜目标。
        session = (self.payload, self.screen, self.stitcher, self.source)
        self.source = self.stitcher = None
        if self.return_owner is not None:
            self.return_owner.release_to_long_capture()
        if self.return_editor is not None:
            self.return_editor.retire_after_capture()
        self.return_editor = self.return_owner = None
        self.editor = ImageEditor(image, self.settings_store, self.window,
                                  shortcut_store=self.shortcut_store, long_image=True,
                                  long_capture_enabled=True)
        self.editor.long_capture_session = session
        if self.layers:
            self.editor.doc.add(self.layers)
        self.layers = []
        self.editor.selected.connect(self.selected)
        self.editor.cancelled.connect(self.cancel)
        self.editor.long_capture_requested.connect(self.continue_from_editor)
        self.editor.long_capture_button.setText("继续长截图")
        self.editor.long_capture_button.setToolTip("沿用原截图区域，继续在已有长图后追加内容")
        if self.on_text:
            self.editor.text_copied.connect(self.on_text)
        self.editor.show()
        self.editor.set_zoom(max(.1, min(1, (self.editor.width()-70)/image.width())))
        self.editor.raise_()
        self.editor.activateWindow()

    def selected(self, image):
        self.editor = None
        self.stop()
        self.on_image(image)

    def cancel(self):
        if self.return_editor is not None:
            editor, owner = self.return_editor, self.return_owner
            self.return_editor = self.return_owner = None
            self.stop()
            if owner is None:
                self.active, self.editor = True, editor
            # 原框选/编辑窗口只是被隐藏；撤销、标注和提取面板无需重建。
            editor.show()
            editor.raise_()
            editor.activateWindow()
            return
        restore = self.active
        self.stop()
        if restore:
            self.window.show_history()

    def stop(self):
        self.serial += 1
        self.active = False
        self.timer.stop()
        self.selector.stop()
        if self.bar:
            self.bar.hide()
            self.bar.deleteLater()
        if self.preview:
            self.preview.dispose()
        if self.outline:
            self.outline.dispose()
            self.outline = None
        if self.editor and not self.editor.finished:
            editor, self.editor = self.editor, None
            editor.cancel()
        if self.executor:
            self.executor.shutdown(wait=False, cancel_futures=True)
        if self.return_owner is not None:
            self.return_owner.stop()
        elif self.return_editor is not None:
            self.return_editor.cancel()
        self.return_editor = self.return_owner = None
        self.payload = self.screen = None
        self.layers = []
        self.bar = self.preview = self.editor = self.source = self.stitcher = self.executor = None
        self.busy = self.pending_finish = False
