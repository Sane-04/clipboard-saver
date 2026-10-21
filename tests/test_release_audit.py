"""分享前的隐私审计必须拒绝已有用户数据，包含嵌套位置。"""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("portable_builder",
    Path(__file__).resolve().parents[1] / "packaging" / "build_portable.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.mark.parametrize("relative", [
    "clipboard.sqlite3", "_internal/api-settings.json", "shortcuts.json", "data-locations.json",
    "data/用户的图片.png", "backups/旧库.sqlite3", "exports/用户的截图.png",
    "_internal/last-error.txt", "启动剪贴收藏.lnk",
])
def test_release_refuses_private_or_machine_bound_files(tmp_path, relative):
    file = tmp_path / relative
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("synthetic-private-content", encoding="utf-8")
    with pytest.raises(AssertionError):
        builder.audit_package(tmp_path)


def test_clean_release_inventory_contains_model_and_dependencies_only(tmp_path):
    for relative in ("剪贴收藏.exe", "_internal/PySide6/Qt6Core.dll", "_internal/cv2/data/__init__.py",
                     "ocr-models/PP-OCRv6_det_small.onnx", "使用说明.txt"):
        file = tmp_path / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"synthetic")
    inventory = builder.audit_package(tmp_path)
    assert len(inventory) == 5
    assert all(item["bytes"] == 9 and len(item["sha256"]) == 64 for item in inventory)
