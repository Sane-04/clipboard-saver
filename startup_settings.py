"""当前 Windows 用户的登录启动项；源码与便携版使用各自的绝对入口。"""
import subprocess
import sys
import winreg
from pathlib import Path

from app_paths import application_root

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ClipboardSaverLocal"


def launch_arguments(root=None, *, executable=None, frozen=None, data_dir=None):
    root = Path(root or application_root()).resolve()
    executable = Path(executable or sys.executable).resolve()
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    arguments = [str(executable)] if frozen else [str(executable.with_name("pythonw.exe")), str(root / "app.py")]
    arguments += ["--hidden", "--autostart"]
    if data_dir is not None:
        arguments += ["--data-dir", str(Path(data_dir).resolve())]
    return arguments


class RunRegistry:
    def __init__(self, name=VALUE_NAME):
        self.name = name

    def read(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                return winreg.QueryValueEx(key, self.name)
        except FileNotFoundError:
            return None

    def write(self, value, kind=winreg.REG_SZ):
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, self.name, 0, kind, value)

    def delete(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, self.name)
        except FileNotFoundError:
            pass


class StartupManager:
    def __init__(self, root=None, *, data_dir=None, backend=None, executable=None, frozen=None):
        frozen = getattr(sys, "frozen", False) if frozen is None else frozen
        self.arguments = launch_arguments(root, executable=executable, frozen=frozen, data_dir=data_dir)
        self.entry_files = self.arguments[:1 if frozen else 2]
        # Windows 的命令行引用规则，保证中文/空格路径与 --data-dir 被正确传回。
        self.command = subprocess.list2cmdline(self.arguments)
        self.backend = backend if backend is not None else RunRegistry()

    def is_enabled(self):
        return self.backend.read() == (self.command, winreg.REG_SZ)

    def set_enabled(self, enabled):
        previous = self.backend.read()
        if enabled:
            if len(self.command) > 260:
                raise ValueError("启动路径过长，请把程序放到更短的文件夹后再开启。")
            # 入口在启用时验证；失效路径仍允许取消自己的旧启动项。
            for filename in self.entry_files:
                if not Path(filename).is_file():
                    raise ValueError("启动入口已移动或不存在，请从新位置运行后再开启。")
            desired = (self.command, winreg.REG_SZ)
        else:
            # 不取消另一份源码/便携目录的启动项，也不改其他软件的注册表值。
            if previous != (self.command, winreg.REG_SZ):
                return
            desired = None
        try:
            if desired is None:
                self.backend.delete()
            else:
                self.backend.write(*desired)
            if self.backend.read() != desired:
                raise OSError("启动项保存后核对失败。")
        except OSError:
            # 单个注册表值写入原子完成；核对失败时恢复原值，不留下假的启用状态。
            if self.backend.read() != previous:
                if previous is None:
                    self.backend.delete()
                else:
                    self.backend.write(*previous)
            raise
