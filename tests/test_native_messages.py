import ctypes
from ctypes import wintypes
from types import SimpleNamespace

from windows_integration import HotkeyFilter, WM_HOTKEY


def test_native_filter_allows_clipboard_messages(qt_app):
    # 回归真实桌面发现的问题：必须返回 False，不能返回会被视为真的非空元组。
    message = wintypes.MSG()
    message.message = 0x031D  # WM_CLIPBOARDUPDATE
    fake_filter = SimpleNamespace(registered={1}, callbacks={})
    assert HotkeyFilter.nativeEventFilter(
        fake_filter, b"windows_generic_MSG", ctypes.addressof(message)) is False
    events = []
    message.message = WM_HOTKEY
    message.wParam = 1
    fake_filter.callbacks = {1: lambda: events.append("trigger")}
    assert HotkeyFilter.nativeEventFilter(
        fake_filter, b"windows_dispatcher_MSG", ctypes.addressof(message)) is True
    # 同一热键随后抵达窗口阶段，只执行一次；不同系统时间是另一次按键。
    assert HotkeyFilter.nativeEventFilter(
        fake_filter, b"windows_generic_MSG", ctypes.addressof(message)) is True
    assert events == ["trigger"]
    message.time = 1
    assert HotkeyFilter.nativeEventFilter(
        fake_filter, b"windows_generic_MSG", ctypes.addressof(message)) is True
    assert events == ["trigger", "trigger"]
