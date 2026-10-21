"""便携目录必须跟随 EXE，不能写入解包资源或继承另一台电脑的库路径。"""
import sys

from app_paths import application_root, default_model_dir, resource_root
from startup_runtime import bundled_runtime_path, runtime_layout


def test_frozen_application_models_and_runtime_roots_are_separate(tmp_path, monkeypatch):
    executable = tmp_path / "解压位置 含空格" / "剪贴收藏.exe"
    internal = executable.parent / "_internal"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert application_root() == executable.parent
    assert default_model_dir() == executable.parent / "ocr-models"
    assert resource_root() == internal
    assert runtime_layout() == (internal, internal / "PySide6", True)


def test_frozen_dll_search_does_not_inherit_user_path(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("PATH", r"F:\Miniconda3\Library\bin;C:\other-qt")
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    directory = tmp_path / "portable" / "_internal"
    paths = bundled_runtime_path(directory).split(os.pathsep)
    assert paths[0] == str(directory)
    assert r"F:\Miniconda3\Library\bin" not in paths
    assert r"C:\other-qt" not in paths
    assert str(directory / "PySide6") in paths


def test_source_keeps_existing_data_and_model_location():
    from pathlib import Path
    project = Path(__file__).resolve().parents[1]
    assert application_root() == project
    assert resource_root() == project
    assert default_model_dir() == project / "data" / "ocr-models"
