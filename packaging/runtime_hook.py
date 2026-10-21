"""在 Qt 的 PyInstaller hook 之前固定便携版的配套 DLL。"""
from startup_runtime import prepare_runtime

prepare_runtime()
