"""Windows 热键与 Qt 本地单实例通信。"""

import ctypes
import hashlib
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QAbstractNativeEventFilter, QLockFile, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QKeySequenceEdit
from shortcut_settings import DEFAULT_SHORTCUTS, global_key

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
WM_HOTKEY = 0x0312


class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, application, window, show_history, screenshot, bindings=None, *, backend=None,
                 long_screenshot=None):
        super().__init__()
        self.application = application
        self.hwnd = int(window.winId())
        self.actions = {"history": show_history, "screenshot": screenshot}
        if long_screenshot is not None:
            self.actions["long_screenshot"] = long_screenshot
        self.callbacks = {}
        self.sequences = {}
        self.specs = {}
        self.backend = backend or user32
        self.next_id = len(self.actions)+1
        self.registered = set()
        self.last_hotkey_message = None
        self.errors = []
        application.installNativeEventFilter(self)
        bindings = bindings or DEFAULT_SHORTCUTS
        for key_id, name in enumerate(self.actions, 1):
            modifiers, key = global_key(bindings[name])
            if self.backend.RegisterHotKey(self.hwnd, key_id, modifiers | MOD_NOREPEAT, key):
                self.registered.add(key_id)
                self.specs[key_id] = (modifiers, key)
                self.sequences[key_id] = bindings[name]
                self.callbacks[key_id] = self.actions[name]
            else:
                self.errors.append(f"{bindings[name]} 注册失败，可能被其他软件占用；可使用窗口或托盘按钮。")

    def apply(self, bindings, persist=None):
        """先注册新键并保存，再释放旧键；任何失败都保留原来的可用热键。"""
        callbacks, sequences, specs, newly_registered = {}, {}, {}, []
        try:
            for name in self.actions:
                spec = global_key(bindings[name])
                key_id = next((i for i, current in self.specs.items() if current == spec), None)
                if key_id is None:
                    key_id = self.next_id
                    self.next_id += 1
                    if not self.backend.RegisterHotKey(self.hwnd, key_id, spec[0] | MOD_NOREPEAT, spec[1]):
                        raise ValueError(f"{bindings[name]} 已被占用或 Windows 不允许注册，请换一个组合键。")
                    newly_registered.append(key_id)
                callbacks[key_id] = self.actions[name]
                sequences[key_id] = bindings[name]
                specs[key_id] = spec
            if len(specs) != len(self.actions):
                raise ValueError("全局快捷键不能相同。")
            if persist:
                persist()
        except (OSError, ValueError):
            for key_id in newly_registered:
                self.backend.UnregisterHotKey(self.hwnd, key_id)
            raise
        obsolete = self.registered-set(specs)
        self.callbacks, self.sequences, self.specs = callbacks, sequences, specs
        self.registered = set(specs)
        for key_id in obsolete:
            self.backend.UnregisterHotKey(self.hwnd, key_id)
        self.errors = []

    def nativeEventFilter(self, event_type, message):
        # Qt 传递的是原生 MSG 指针，wParam 对应分别注册的热键 ID。
        if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam in self.registered:
                # Windows Qt 会把同一消息分别交给消息泵和窗口处理；指针不同，
                # 但窗口/键/参数/系统时间一致。去重只针对这一条消息，下一次按键仍可用。
                identity = (msg.hWnd, msg.wParam, msg.lParam, msg.time)
                if identity == getattr(self, "last_hotkey_message", None):
                    return True
                self.last_hotkey_message = identity
                # 设置页录入旧热键时，Windows 只发 WM_HOTKEY；转成字段输入而不启动截图。
                field = QApplication.focusWidget()
                while field is not None and not isinstance(field, QKeySequenceEdit):
                    field = field.parentWidget()
                if field is not None and field.isVisible() and field.window().isActiveWindow():
                    field.setKeySequence(self.sequences[msg.wParam])
                    return True
                self.callbacks[msg.wParam]()
                return True
        # PySide6 的这个绑定接收布尔值；(False, 0) 是非空元组，会拦截普通消息。
        return False

    def close(self):
        for key_id in self.registered:
            self.backend.UnregisterHotKey(self.hwnd, key_id)
        self.registered.clear()
        self.application.removeNativeEventFilter(self)


def foreground(window, show=None):
    (show or window.show_history)()
    window.raise_()
    window.activateWindow()
    user32.SetForegroundWindow(int(window.winId()))


class SingleInstance(QObject):
    show_requested = Signal()

    def __init__(self, data_dir, parent=None, *, show_existing=True):
        super().__init__(parent)
        directory = Path(data_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(str(directory).casefold().encode("utf-8")).hexdigest()[:24]
        self.name = f"clipboard-saver-{digest}"
        self.lock = QLockFile(str(directory / "instance.lock"))
        # 不因为运行时间长删除活跃锁；Qt 仍会检查残留锁中的进程是否已经结束。
        self.lock.setStaleLockTime(0)
        self.primary = self.lock.tryLock(0)
        self.server = None
        if not self.primary:
            socket = QLocalSocket(self)
            # 首个实例可能刚拿到锁、还没建立通信端点，给它有限的启动时间。
            for _ in range(8):
                socket.abort()
                socket.connectToServer(self.name)
                if socket.waitForConnected(200):
                    # 登录触发可以重复抵达，安静通知不改变现有窗口/编辑器的可见状态。
                    socket.write(b"show" if show_existing else b"quiet")
                    socket.waitForBytesWritten(500)
                    socket.disconnectFromServer()
                    break
            else:
                raise RuntimeError("已有实例正在启动或数据目录被占用，请稍后再试。")
            return
        # 只在持有锁时清理旧端点，不允许第二次启动破坏正在运行的实例。
        QLocalServer.removeServer(self.name)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self.server.listen(self.name):
            self.lock.unlock()
            raise RuntimeError("无法建立本地应用通信。")
        self.server.newConnection.connect(self._connected)

    def _connected(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            socket.readyRead.connect(lambda peer=socket: self._read(peer))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self._read(socket)

    def _read(self, socket):
        if bytes(socket.readAll()) == b"show":
            self.show_requested.emit()
        socket.disconnectFromServer()

    def close(self):
        if self.server is not None:
            self.server.close()
            self.lock.unlock()
