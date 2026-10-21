"""统一图钉选窗与随窗取消标记；覆盖层只绘制提示，不截取桌面。"""
from dataclasses import dataclass
from time import monotonic

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from external_windows import WindowsWindowBackend
from ui_theme import ACCENT, icon


def selection_cursor():
    pixmap = QPixmap(36, 36)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(Qt.GlobalColor.white, 4))
    for x1, y1, x2, y2 in ((2, 9, 16, 9), (9, 2, 9, 16)):
        painter.drawLine(x1, y1, x2, y2)
    painter.setPen(QPen(QColor(ACCENT), 2))
    painter.drawLine(2, 9, 16, 9)
    painter.drawLine(9, 2, 9, 16)
    icon("pin", ACCENT).paint(painter, 16, 16, 19, 19)
    painter.end()
    return QCursor(pixmap, 9, 9)


class PickerOverlay(QWidget):
    def __init__(self, controller, screen):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.controller = controller
        self.setWindowTitle("选择要置顶的窗口")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(screen.geometry())
        self.setCursor(selection_cursor())
        self.setMouseTracking(True)
        self.pressed = None

    def paintEvent(self, event):
        painter = QPainter(self)
        # Windows分层窗口的全透明像素会鼠标穿透；极低alpha保证选择点击被覆盖层消费。
        painter.fillRect(self.rect(), QColor(0, 0, 0, 2))
        target = self.controller.candidate
        if target is not None:
            try:
                rect = self.controller.backend.rect(target.hwnd)
                origin = self.controller.backend.monitor_rect(int(self.winId()))
                ratio = self.devicePixelRatioF()
                box = QRectF((rect[0]-origin[0])/ratio, (rect[1]-origin[1])/ratio,
                             (rect[2]-rect[0])/ratio, (rect[3]-rect[1])/ratio)
                painter.setPen(QPen(QColor(ACCENT), 3))
                painter.setBrush(QColor(66, 101, 232, 14))
                painter.drawRoundedRect(box.adjusted(2, 2, -2, -2), 6, 6)
            except OSError:
                pass
        cursor = self.mapFromGlobal(QCursor.pos())
        if not self.rect().contains(cursor):
            return
        width = min(330, self.width() - 20)
        label = QRectF(max(10, min(cursor.x()+22, self.width()-width-10)),
                       max(10, min(cursor.y()+26, self.height()-78)), width, 56)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#202633"))
        painter.drawRoundedRect(label, 9, 9)
        painter.setPen(Qt.GlobalColor.white)
        painter.setFont(QFont("Microsoft YaHei UI", 10))
        title = "点击窗口置顶" if target is not None else "移到要置顶的软件窗口"
        painter.drawText(label.adjusted(13, 6, -10, -25), Qt.AlignmentFlag.AlignVCenter, title)
        painter.setPen(QColor("#cbd4e6"))
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        painter.drawText(label.adjusted(13, 29, -8, -5), "左键选择 · Esc / 右键取消")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.controller.cancel()
        elif event.button() == Qt.MouseButton.LeftButton:
            self.controller.update_candidate()
            self.pressed = self.controller.candidate
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.controller.update_candidate()
            target, self.pressed = self.pressed, None
            if target and self.controller.candidate and target.identity == self.controller.candidate.identity:
                self.controller.choose(target)
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.controller.cancel()
        event.accept()

    def closeEvent(self, event):
        if self.controller.selecting:
            self.controller.cancel()
        event.accept()


class PinBadge(QPushButton):
    def __init__(self, controller, target):
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setWindowTitle("取消窗口置顶 · 剪贴收藏")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("已置顶 · 点击取消")
        self.setAccessibleName("取消此窗口置顶")
        self.setFixedSize(30, 30)
        self.setIcon(icon("pin", "#ffffff"))
        self.setStyleSheet("QPushButton { background: #4265e8; border: 1px solid #ffffff;"
                           "border-radius: 8px; padding: 0; }"
                           "QPushButton:hover { background: #284aca; }"
                           "QPushButton:disabled { background: #8c9bcf; }")
        self.clicked.connect(lambda: controller.unpin(target))


@dataclass
class PinEntry:
    target: object
    original_topmost: bool
    badge: PinBadge
    confirmed: bool = False


class WindowPinController(QObject):
    failed = Signal(str)
    changed = Signal()

    def __init__(self, parent=None, *, backend=None, can_start=None):
        super().__init__(parent)
        self.backend = backend or WindowsWindowBackend()
        self.can_start = can_start or (lambda: True)
        self.entries = {}
        self.pending = {}
        self.overlays = []
        self.candidate = None
        self.selecting = False
        self.closed = False
        self.timer = QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self.tick)
        QGuiApplication.instance().screenAdded.connect(self.screen_changed)
        QGuiApplication.instance().screenRemoved.connect(self.screen_changed)
        QGuiApplication.instance().applicationStateChanged.connect(self.application_state_changed)

    def excluded_handles(self):
        return {int(w.winId()) for w in self.overlays} | {int(e.badge.winId()) for e in self.entries.values()}

    def start(self):
        if self.closed or self.selecting:
            return
        if not self.can_start() or QApplication.activeModalWidget() is not None:
            self.failed.emit("请先完成或取消当前截图/设置，再点击图钉选择窗口。")
            return
        self.selecting = True
        try:
            self.overlays = [PickerOverlay(self, s) for s in QGuiApplication.screens()]
            if not self.overlays:
                raise OSError("没有可用的显示器。")
            for overlay in self.overlays:
                overlay.show()
                overlay.raise_()
            active = next((o for o in self.overlays if o.geometry().contains(QCursor.pos())), self.overlays[0])
            active.activateWindow()
            active.setFocus()
            self.update_candidate()
            self.timer.start()
        except OSError as error:
            self.cancel()
            self.failed.emit(str(error))

    def update_candidate(self):
        if not self.selecting:
            return
        try:
            self.candidate = self.backend.window_at(self.backend.cursor(), self.excluded_handles())
        except OSError:
            self.candidate = None
        for overlay in self.overlays:
            overlay.update()

    def cancel(self):
        self.selecting = False
        self.candidate = None
        overlays, self.overlays = self.overlays, []
        for overlay in overlays:
            overlay.close()
            overlay.deleteLater()
        self.update_timer()

    def screen_changed(self, *_):
        if self.selecting:
            self.cancel()

    def application_state_changed(self, state):
        # Alt+Tab/锁屏退出选择，避免失焦覆盖层留在桌面挡住鼠标。
        if self.selecting and state != Qt.ApplicationState.ApplicationActive:
            self.cancel()

    def choose(self, target):
        self.cancel()
        self.apply(target, True)

    def unpin(self, target):
        self.apply(target, False)

    def apply(self, target, enabled):
        key = target.identity
        if self.closed or key in self.pending:
            return
        created = False
        try:
            current = self.backend.current(target)
            entry = self.entries.get(key)
            if entry is None:
                if not enabled:
                    return
                entry = PinEntry(current, current.topmost, PinBadge(self, current))
                self.entries[key] = entry
                created = True
            if current.topmost != enabled:
                self.backend.set_topmost(current, enabled)
            self.pending[key] = (enabled, monotonic()+1.5)
            entry.badge.setEnabled(False)
            self.timer.start()
            self.confirm(key)
        except (OSError, ValueError) as error:
            if created:
                self.remove(key)
            elif key in self.entries:
                self.entries[key].badge.setEnabled(True)
            self.failed.emit(str(error))

    def confirm(self, key):
        entry = self.entries.get(key)
        if entry is None or key not in self.pending:
            return
        enabled, deadline = self.pending[key]
        try:
            current = self.backend.current(entry.target)
        except (OSError, ValueError) as error:
            self.remove(key)
            self.failed.emit(str(error))
            return
        if current.topmost != enabled:
            if monotonic() >= deadline:
                self.pending.pop(key)
                entry.badge.setEnabled(True)
                self.failed.emit("未能确认窗口层级，目标可能尚未响应或自行改变了层级。请稍后重新选择。")
            return
        self.pending.pop(key)
        entry.badge.setEnabled(True)
        if not enabled:
            self.remove(key)
        else:
            entry.confirmed = True
            try:
                self.sync_badge(entry, current)
            except OSError as error:
                # 置顶已经成功，标记暂时显示失败仍需保留身份以便恢复/退出还原。
                entry.badge.hide()
                self.failed.emit(str(error))
        self.changed.emit()

    def sync_badge(self, entry, current):
        badge = entry.badge
        if self.selecting or not self.can_start() or current.minimized or not current.visible or current.cloaked:
            badge.hide()
            return
        point = self.backend.position_badge(int(badge.winId()), current)
        # 同为置顶的其他窗口遮住目标时隐藏图钉，不能把标记挂在无关窗口上。
        hit = self.backend.window_at(point, self.excluded_handles())
        if hit is None or hit.identity != current.identity:
            badge.hide()
        else:
            badge.show()

    def tick(self):
        self.update_candidate()
        for key, entry in list(self.entries.items()):
            try:
                if key in self.pending:
                    self.confirm(key)
                    continue
                current = self.backend.current(entry.target)
                if not current.topmost and entry.confirmed:
                    self.remove(key)
                elif not current.topmost:
                    entry.badge.hide()
                else:
                    entry.confirmed = True
                    self.sync_badge(entry, current)
            except ValueError:
                self.remove(key)
            except OSError:
                entry.badge.hide()
        self.update_timer()

    def remove(self, key):
        self.pending.pop(key, None)
        entry = self.entries.pop(key, None)
        if entry:
            entry.badge.close()
            entry.badge.deleteLater()
        self.update_timer()

    def update_timer(self):
        if not self.selecting and not self.entries:
            self.timer.stop()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancel()
        # 真正退出时恢复由本工具新加的置顶，避免标记消失后留下无法取消的状态。
        # 用户选择前就已置顶的窗口保持原状；不持久化任何第三方窗口身份/标题。
        for key, entry in list(self.entries.items()):
            try:
                if not entry.original_topmost:
                    self.backend.set_topmost(entry.target, False)
            except (OSError, ValueError):
                pass
            self.remove(key)
        self.timer.stop()
        app = QGuiApplication.instance()
        app.screenAdded.disconnect(self.screen_changed)
        app.screenRemoved.disconnect(self.screen_changed)
        app.applicationStateChanged.disconnect(self.application_state_changed)
        app = QApplication.instance()
        if getattr(app, "_window_pin_controller", None) is self:
            del app._window_pin_controller
