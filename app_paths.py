"""源码与便携版共用路径；资源只读，用户数据放在启动目录。"""
import sys
from pathlib import Path


def application_root():
    # PyInstaller 的 __file__ 位于 _internal，不能把用户历史写进运行库目录。
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent


def resource_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()


def default_model_dir():
    root = application_root()
    return root / "ocr-models" if getattr(sys, "frozen", False) else root / "data" / "ocr-models"
