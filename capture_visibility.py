"""Windows自身工具窗口不进入截图，也无需每次采集时隐藏/显示。"""
import ctypes
import os
from ctypes import wintypes
from PySide6.QtWidgets import QApplication


def exclude_capture(widget):
    widget.capture_excluded = False
    if os.name == "nt" and QApplication.platformName() == "windows":
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        api.SetWindowDisplayAffinity.restype = wintypes.BOOL
        # 0x11让合成桌面捕获底下原画面；不支持时控制器继续使用避让/隐藏回退。
        widget.capture_excluded = bool(api.SetWindowDisplayAffinity(int(widget.winId()), 0x11))
    return widget.capture_excluded
