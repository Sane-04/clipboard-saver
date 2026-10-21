"""自启动只操作本工具的值；所有写入测试使用内存后端，不碰真实启动项。"""
import ctypes
import subprocess
import winreg
from pathlib import Path

import pytest
from PySide6.QtTest import QTest

from app import ClipboardApplication
from startup_settings import StartupManager, launch_arguments
from test_history import FakeClipboard
from windows_integration import SingleInstance


class MemoryRun:
    def __init__(self, value=None):
        self.value = value
        self.fail_write = self.fail_delete = False
        self.writes = []

    def read(self):
        return self.value

    def write(self, value, kind=winreg.REG_SZ):
        self.writes.append((value, kind))
        self.value = (value, kind)
        if self.fail_write:
            self.fail_write = False
            raise PermissionError("synthetic write failure")

    def delete(self):
        self.value = None
        if self.fail_delete:
            self.fail_delete = False
            raise PermissionError("synthetic delete failure")


def manager(tmp_path, backend=None, *, frozen=False, data_dir=None, short_command=True):
    root = tmp_path / "程序 中文 空格"
    root.mkdir(exist_ok=True)
    executable = root / ("剪贴收藏.exe" if frozen else "python.exe")
    (root / "pythonw.exe").touch()
    (root / "app.py").touch()
    executable.touch()
    result = StartupManager(root, executable=executable, frozen=frozen, data_dir=data_dir,
                            backend=backend if backend is not None else MemoryRun())
    if short_command:
        # pytest 的深目录超过 Run 的 260 字符；状态/失败测试用短的内存命令，
        # 完整绝对路径引用另用 Windows CommandLineToArgvW 做真实解析验证。
        result.command = "synthetic-clipboard-test --hidden --autostart"
    return result


def windows_arguments(command):
    api = ctypes.WinDLL("shell32")
    api.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    api.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    count = ctypes.c_int()
    arguments = api.CommandLineToArgvW(command, ctypes.byref(count))
    kernel = ctypes.WinDLL("kernel32")
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    try:
        return [arguments[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(arguments)


@pytest.mark.parametrize("frozen", [False, True])
def test_windows_roundtrip_absolute_entry_and_independent_data(tmp_path, frozen):
    current = manager(tmp_path, frozen=frozen, data_dir=tmp_path / "自己的 数据", short_command=False)
    assert windows_arguments(current.command) == current.arguments
    assert current.arguments[0].endswith(".exe")
    assert "--hidden" in current.arguments and "--autostart" in current.arguments
    assert current.arguments[-2:] == ["--data-dir", str(tmp_path / "自己的 数据")]
    if not frozen:
        assert current.arguments[0].endswith("pythonw.exe")
        assert current.arguments[1].endswith("app.py")


def test_enabling_this_copy_replaces_other_copy_but_disabling_does_not(tmp_path):
    other = ("other installation command", winreg.REG_SZ)
    backend = MemoryRun(other)
    current = manager(tmp_path, backend)
    current.set_enabled(False)
    assert backend.value == other and not backend.writes
    current.set_enabled(True)
    assert current.is_enabled()


@pytest.mark.parametrize("previous", [None, ("other installation", winreg.REG_SZ)])
def test_partial_write_failure_restores_previous_value(tmp_path, previous):
    backend = MemoryRun(previous)
    current = manager(tmp_path, backend)
    backend.fail_write = True
    with pytest.raises(PermissionError):
        current.set_enabled(True)
    assert backend.value == previous


def test_delete_failure_restores_enabled_value(tmp_path):
    current = manager(tmp_path)
    current.set_enabled(True)
    previous = current.backend.value
    current.backend.fail_delete = True
    with pytest.raises(PermissionError):
        current.set_enabled(False)
    assert current.backend.value == previous and current.is_enabled()


def test_missing_entry_refuses_enable_but_can_disable_own_stale_value(tmp_path):
    current = manager(tmp_path)
    current.set_enabled(True)
    Path(current.arguments[0]).unlink()
    with pytest.raises(ValueError, match="入口"):
        current.set_enabled(True)
    current.set_enabled(False)
    assert current.backend.value is None


def test_long_command_refuses_change(tmp_path):
    current = manager(tmp_path)
    current.command = "x" * 261
    with pytest.raises(ValueError, match="路径过长"):
        current.set_enabled(True)
    assert current.backend.value is None


def test_menu_toggle_and_write_failure_restore_checkbox(qt_app, tmp_path):
    startup = manager(tmp_path)
    controller = ClipboardApplication(qt_app, tmp_path / "data", enable_hotkeys=False,
                                      enable_tray=False, clipboard=FakeClipboard(), startup_manager=startup)
    try:
        action = controller.window.autostart_action
        assert not action.isChecked()
        action.trigger()
        assert action.isChecked() and startup.is_enabled()
        action.trigger()
        assert not action.isChecked() and not startup.is_enabled()
        startup.backend.fail_write = True
        action.trigger()
        assert not action.isChecked() and not startup.is_enabled()
        assert "自启动设置未完成" in controller.last_error
    finally:
        controller.cleanup()
        controller.window.exiting = True
        controller.window.close()


def test_tray_and_history_toggles_stay_in_sync(qt_app, tmp_path):
    startup = manager(tmp_path)
    controller = ClipboardApplication(qt_app, tmp_path / "data", enable_hotkeys=False,
                                      clipboard=FakeClipboard(), startup_manager=startup)
    try:
        controller.autostart_action.trigger()
        assert controller.window.autostart_action.isChecked() and startup.is_enabled()
        controller.window.autostart_action.trigger()
        assert not controller.autostart_action.isChecked() and not startup.is_enabled()
    finally:
        controller.cleanup()
        controller.window.exiting = True
        controller.window.close()


def test_quiet_second_instance_does_not_request_show_but_manual_does(qt_app, tmp_path):
    primary = SingleInstance(tmp_path / "instance")
    events = []
    primary.show_requested.connect(lambda: events.append("show"))
    try:
        quiet = SingleInstance(tmp_path / "instance", show_existing=False)
        QTest.qWait(40)
        assert not quiet.primary and not events
        manual = SingleInstance(tmp_path / "instance")
        QTest.qWait(40)
        assert not manual.primary and events == ["show"]
        quiet.close()
        manual.close()
    finally:
        primary.close()
