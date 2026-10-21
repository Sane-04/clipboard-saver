"""在 Qt/SSL 导入前固定本进程运行环境，避免同名 DLL 来自不同安装。"""
import ctypes
import os
import sys
from pathlib import Path

_handles = []
_prepared = False


def clean_runtime_path(value, environment):
    environment = Path(environment).resolve()
    conda_root = environment.parent.parent if environment.parent.name.lower() == "envs" else environment
    prefixes = [environment, environment / "Library" / "bin", environment / "DLLs", environment / "Scripts"]
    result, seen = [], set()
    for entry in [str(path) for path in prefixes] + value.split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry:
            continue
        path = Path(entry)
        key = os.path.normcase(os.path.normpath(entry))
        if "codex-runtimes" in key.lower():
            continue
        if path.is_absolute() and path.is_relative_to(conda_root) and not path.is_relative_to(environment):
            continue
        if key not in seen:
            result.append(entry)
            seen.add(key)
    return os.pathsep.join(result)


def load_matched_ssl(environment, loader=None):
    directory = Path(environment) / "Library" / "bin"
    names = ("libcrypto-3-x64.dll", "libssl-3-x64.dll")
    if not all((directory / name).is_file() for name in names):
        raise RuntimeError("运行库的 OpenSSL 文件不完整，请使用原启动入口或重新完整解压便携包。")
    loader = loader or ctypes.WinDLL
    # 先加载同目录的 crypto，再加载 ssl；依赖从 DLL 所在目录、用户 DLL 目录和系统查找。
    return [loader(str(directory / name), winmode=0x1100) for name in names]


def runtime_layout():
    if getattr(sys, "frozen", False):
        environment = Path(sys._MEIPASS).resolve()
        return environment, environment / "PySide6", True
    environment = Path(sys.executable).resolve().parent
    return environment, environment / "Lib" / "site-packages" / "PySide6", False


def bundled_runtime_path(environment):
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    # 便携版只依赖自己的库与 Windows；不会拾取接收者电脑上其他 Python/Qt DLL。
    return os.pathsep.join(str(path) for path in (
        environment, environment / "Library" / "bin", environment / "DLLs",
        environment / "PySide6", system / "System32", system))


def prepare_runtime():
    global _prepared
    if _prepared or os.name != "nt":
        return
    environment, qt_directory, frozen = runtime_layout()
    os.environ["PATH"] = bundled_runtime_path(environment) if frozen else \
        clean_runtime_path(os.environ.get("PATH", ""), environment)
    plugins = qt_directory / "plugins"
    if plugins.is_dir():
        os.environ["QT_PLUGIN_PATH"] = str(plugins)
        if frozen:
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(plugins / "platforms")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetDefaultDllDirectories.argtypes = [ctypes.c_uint]
    kernel.SetDefaultDllDirectories.restype = ctypes.c_bool
    if not kernel.SetDefaultDllDirectories(0x1000):
        raise OSError("无法设置本程序的 DLL 搜索目录。")
    # Qt 的图像插件还会延迟加载 Qt6Svg 等库，需要保留 PySide6 自己的 DLL 目录。
    for directory in (environment, environment / "Library" / "bin", environment / "DLLs", qt_directory):
        if directory.is_dir():
            _handles.append(os.add_dll_directory(str(directory)))
    _handles.extend(load_matched_ssl(environment))
    _prepared = True
