"""窗口身份、原生层级与物理坐标；仅在主动选窗时读取鼠标下的窗口标题。"""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowTarget:
    hwnd: int
    pid: int
    thread: int
    class_name: str
    title: str
    topmost: bool
    minimized: bool = False
    visible: bool = True
    cloaked: bool = False

    @property
    def identity(self):
        return self.hwnd, self.pid, self.thread, self.class_name


class WindowsWindowBackend:
    def __init__(self, *, exclude_pid=None, allowed_pids=None):
        self.exclude_pid = exclude_pid
        # 合成QA先筛PID再读取标题，不把私人窗口当作测试素材。
        self.allowed_pids = allowed_pids
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.api.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.api.EnumWindows.restype = wintypes.BOOL
        for name in ("IsWindow", "IsWindowVisible", "IsIconic"):
            getattr(self.api, name).argtypes = [wintypes.HWND]
            getattr(self.api, name).restype = wintypes.BOOL
        self.api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.api.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.api.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.api.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        self.api.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self.api.GetWindow.restype = wintypes.HWND
        for name in ("GetWindowTextW", "GetClassNameW"):
            getattr(self.api, name).argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            getattr(self.api, name).restype = ctypes.c_int
        self.api.GetShellWindow.restype = self.api.GetDesktopWindow.restype = wintypes.HWND
        self.api.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.api.SetWindowPos.restype = wintypes.BOOL
        self.api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.api.GetWindowRect.restype = wintypes.BOOL
        self.api.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.api.GetCursorPos.restype = wintypes.BOOL
        self.api.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.api.MonitorFromWindow.restype = wintypes.HANDLE
        self.api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self.api.GetMonitorInfoW.restype = wintypes.BOOL
        self.api.GetDpiForWindow.argtypes = [wintypes.HWND]
        self.api.GetDpiForWindow.restype = wintypes.UINT
        self.dwm = ctypes.WinDLL("dwmapi")
        self.dwm.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        self.dwm.DwmGetWindowAttribute.restype = ctypes.c_long

    def query(self, hwnd, *, visible_only=True):
        if not self.api.IsWindow(hwnd):
            return None
        pid = wintypes.DWORD()
        thread = self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not thread or pid.value == self.exclude_pid or (
            self.allowed_pids is not None and pid.value not in self.allowed_pids
        ):
            return None
        if hwnd in (self.api.GetShellWindow(), self.api.GetDesktopWindow()):
            return None
        style = self.api.GetWindowLongPtrW(hwnd, -20)
        if style & 0x80:
            return None
        visible = bool(self.api.IsWindowVisible(hwnd))
        cloaked = wintypes.DWORD()
        self.dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if visible_only and (not visible or cloaked.value):
            return None
        title, class_name = ctypes.create_unicode_buffer(1024), ctypes.create_unicode_buffer(256)
        self.api.GetWindowTextW(hwnd, title, len(title))
        if visible_only and not title.value.strip():
            return None
        if not self.api.GetClassNameW(hwnd, class_name, len(class_name)):
            return None
        return WindowTarget(int(hwnd), pid.value, thread, class_name.value, title.value,
                            bool(style & 8), bool(self.api.IsIconic(hwnd)), visible, bool(cloaked.value))

    def current(self, target):
        # 隐藏/最小化并不表示窗口死亡，仍保留身份，恢复后图钉可继续跟随。
        current = self.query(target.hwnd, visible_only=False)
        if current is None or current.identity != target.identity:
            raise ValueError("目标窗口已关闭或身份改变，请重新点击图钉选择。")
        return current

    def set_topmost(self, target, enabled):
        self.current(target)
        # 一次异步写入；不移动、不缩放、不抢焦点、不影响owner层级。
        if not self.api.SetWindowPos(target.hwnd, -1 if enabled else -2, 0, 0, 0, 0, 0x4213):
            raise OSError("未能修改窗口层级，目标可能已关闭或权限不允许。")

    def rect(self, hwnd):
        rect = wintypes.RECT()
        if self.dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
            if not self.api.GetWindowRect(hwnd, ctypes.byref(rect)):
                raise OSError("暂时无法读取目标窗口的位置。")
        return rect.left, rect.top, rect.right, rect.bottom

    def cursor(self):
        point = wintypes.POINT()
        if not self.api.GetCursorPos(ctypes.byref(point)):
            raise OSError("暂时无法读取鼠标位置。")
        return point.x, point.y

    def window_at(self, point, excluded=()):
        """按原生Z序命中首个可见矩形；不穿过不可选择的工具/桌面去操作底下窗口。"""
        hit = []
        @self.callback_type
        def collect(hwnd, _):
            if hwnd in excluded or not self.api.IsWindowVisible(hwnd) or self.api.IsIconic(hwnd):
                return True
            cloaked = wintypes.DWORD()
            self.dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
            if cloaked.value:
                return True
            try:
                left, top, right, bottom = self.rect(hwnd)
            except OSError:
                return True
            if left <= point[0] < right and top <= point[1] < bottom:
                hit.append(self.query(hwnd))
                return False
            return True
        self.api.EnumWindows(collect, 0)
        return hit[0] if hit else None

    def monitor_rect(self, hwnd):
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        monitor = self.api.MonitorFromWindow(hwnd, 2)
        if not self.api.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise OSError("暂时无法读取显示器的位置。")
        rect = info.monitor
        return rect.left, rect.top, rect.right, rect.bottom

    def position_badge(self, hwnd, target):
        left, top, right, bottom = self.rect(target.hwnd)
        scale = (self.api.GetDpiForWindow(target.hwnd) or 96) / 96
        size = round(30 * scale)
        x = round((left + right - size) / 2)
        y = top + round(5 * scale)
        desired = (x, y, x+size, y+size)
        # 跟随时不重复抬升Z序，否则会把其他置顶窗口下的标记强行抬上来。
        if self.rect(hwnd) != desired and not self.api.SetWindowPos(hwnd, None, x, y, size, size, 0x14):
            raise OSError("暂时无法显示窗口图钉。")
        return x + size // 2, y + size // 2
