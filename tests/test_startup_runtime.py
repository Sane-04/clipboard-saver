import os

import pytest

from startup_runtime import clean_runtime_path, load_matched_ssl


def test_runtime_path_keeps_environment_first_and_excludes_other_ssl_locations(tmp_path):
    root = tmp_path / "conda"
    environment = root / "envs" / "sane"
    foreign = tmp_path / "codex-runtimes" / "dependencies" / "poppler" / "bin"
    system = tmp_path / "System32"
    value = os.pathsep.join(map(str, [foreign, root, root / "Library" / "bin",
                                      environment / "Library" / "bin", system, system]))
    result = clean_runtime_path(value, environment).split(os.pathsep)
    assert result[:4] == list(map(str, [environment, environment / "Library" / "bin",
                                       environment / "DLLs", environment / "Scripts"]))
    assert str(foreign) not in result and str(root / "Library" / "bin") not in result
    assert result.count(str(system)) == 1


def test_ssl_pair_is_loaded_by_absolute_path_from_same_environment(tmp_path):
    directory = tmp_path / "Library" / "bin"
    directory.mkdir(parents=True)
    for name in ("libcrypto-3-x64.dll", "libssl-3-x64.dll"):
        (directory / name).write_bytes(b"synthetic not loaded")
    calls = []
    def loader(path, **options):
        calls.append((path, options))
        return path
    handles = load_matched_ssl(tmp_path, loader)
    assert handles == [str(directory / "libcrypto-3-x64.dll"), str(directory / "libssl-3-x64.dll")]
    assert all(options == {"winmode": 0x1100} for _, options in calls)


def test_incomplete_ssl_pair_does_not_fall_back_to_foreign_dll(tmp_path):
    calls = []
    with pytest.raises(RuntimeError, match="不完整"):
        load_matched_ssl(tmp_path, lambda *args, **kwargs: calls.append(args))
    assert not calls


def test_safe_search_directories_still_support_qt_svg_plugins(qt_app):
    from pathlib import Path
    from PySide6.QtGui import QImageReader
    root = Path(__file__).resolve().parents[1]
    for name in ("check.svg", "chevron-down.svg"):
        reader = QImageReader(str(root / "assets" / name))
        assert not reader.read().isNull(), reader.errorString()
