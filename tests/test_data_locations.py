"""六类位置的实际数据迁移、回滚和 Qt 保存入口，全部使用隔离文件。"""
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu

from api_settings import ApiSettings, SettingsStore
from app import ClipboardApplication
from data_locations import (
    DataLocations, LOCATION_LABELS, MODEL_FILES, LocationMigration, LocationSettingsDialog, LocationStore,
    database_digest, export_png, file_digest, write_exception_log,
)
from image_editor import ImageEditor
from local_ocr import shared_service
from storage import HistoryStorage, image_fingerprint
from test_history import FakeClipboard, picture


def source_locations(tmp_path):
    locations = DataLocations.defaults(tmp_path / "旧数据")
    locations.models.mkdir(parents=True)
    for name in MODEL_FILES:
        (locations.models / name).write_bytes(("synthetic " + name).encode())
    return locations


def target_locations(tmp_path):
    return DataLocations(**{name: tmp_path / "新位置 含空格" / name for name in LOCATION_LABELS})


def controller_with_locations(qt_app, tmp_path):
    locations = source_locations(tmp_path)
    store = LocationStore(tmp_path / "启动配置", locations)
    controller = ClipboardApplication(qt_app, locations.history, enable_hotkeys=False,
                                      enable_tray=False, clipboard=FakeClipboard(), location_store=store)
    return controller


def close(controller):
    controller.cleanup()
    controller.window.exiting = True
    controller.window.close()


def test_locations_roundtrip_separate_categories_and_no_api_key(qt_app, tmp_path):
    locations = replace(target_locations(tmp_path), auto_export=True)
    store = LocationStore(tmp_path / "启动位置")
    store.save(locations)
    reopened = LocationStore(store.root)
    assert reopened.locations == locations
    assert set(json.loads(store.path.read_text(encoding="utf-8"))) == set(LOCATION_LABELS) | {"auto_export"}
    assert "key" not in store.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("patch", [{"history": ""}, {"config": "relative/folder"}, {"auto_export": "false"}])
def test_invalid_locations_do_not_replace_saved_config(qt_app, tmp_path, patch):
    store = LocationStore(tmp_path)
    store.save(store.locations)
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.apply(dict(store.locations.values(), **patch))
    assert store.path.read_bytes() == before


def test_corrupt_location_config_stops_instead_of_opening_empty_default(qt_app, tmp_path):
    (tmp_path / "data-locations.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="无法读取"):
        LocationStore(tmp_path)


def test_live_migration_keeps_records_tags_dates_key_and_original_files(qt_app, tmp_path):
    controller = controller_with_locations(qt_app, tmp_path)
    old = controller.locations
    first = controller.storage.add_text("  完整文字\n    code()  ")
    image = controller.storage.add_image(picture())
    controller.storage.toggle_pin(first)
    controller.storage.set_mark(image, True, "图片标签")
    controller.storage.set_limit(17)
    controller.settings_store.save(ApiSettings(key="synthetic-dpapi-key", model="custom-preserved"))
    controller.shortcut_store.save({**controller.shortcut_store.bindings, "history": "Alt+F9"})
    before = database_digest(controller.storage.connection)
    key_hash = file_digest(controller.settings_store.path)
    new = target_locations(tmp_path)
    controller.location_store.apply(new.values())
    assert controller.locations == new and controller.storage.data_dir == new.history
    assert database_digest(controller.storage.connection) == before
    assert controller.storage.limit == 17 and controller.storage.get_record(image)["tag"] == "图片标签"
    assert controller.window.model.storage is controller.storage and controller.recorder.storage is controller.storage
    assert file_digest(new.config / "api-settings.json") == key_hash
    assert SettingsStore(new.config).settings.key == "synthetic-dpapi-key"
    assert controller.shortcut_store.path == new.config / "shortcuts.json"
    assert controller.settings_store.export_dir == new.exports
    assert (old.history / "clipboard.sqlite3").is_file() and (old.config / "api-settings.json").is_file()
    assert all(file_digest(old.models / name) == file_digest(new.models / name) for name in MODEL_FILES)
    assert list(new.backups.glob("before-location-change-*.sqlite3"))
    controller.recorder.clipboard.put_text("迁移后新增")
    assert controller.storage.counts()["total"] == 3
    with sqlite3.connect(old.history / "clipboard.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM clips").fetchone()[0] == 2
    controller.settings_store.save(replace(controller.settings_store.settings, language="日本語"))
    assert file_digest(old.config / "api-settings.json") == key_hash
    reopened = LocationStore(controller.location_store.root)
    assert reopened.locations == new
    close(controller)
    reopened_controller = ClipboardApplication(qt_app, new.history, enable_hotkeys=False, enable_tray=False,
                                               clipboard=FakeClipboard(), location_store=reopened)
    assert reopened_controller.storage.counts()["total"] == 3
    assert reopened_controller.settings_store.settings.language == "日本語"
    assert reopened_controller.storage.get_record(first)["pinned"]
    close(reopened_controller)


def test_destination_history_conflict_never_overwrites_either_library(qt_app, tmp_path):
    controller = controller_with_locations(qt_app, tmp_path)
    source = controller.storage.add_text("当前记录")
    new = target_locations(tmp_path)
    foreign = HistoryStorage(new.history)
    other = foreign.add_text("目标已有自己的内容")
    foreign_before = database_digest(foreign.connection)
    foreign.close()
    with pytest.raises(ValueError, match="不同的历史"):
        controller.apply_locations(new)
    assert controller.storage.get_record(source)["text"] == "当前记录"
    target = HistoryStorage(new.history)
    assert database_digest(target.connection) == foreign_before and target.get_record(other)
    target.close()
    assert not controller.location_store.path.exists()
    close(controller)


def test_destination_config_conflict_rolls_back_staged_database(qt_app, tmp_path):
    controller = controller_with_locations(qt_app, tmp_path)
    controller.settings_store.save(ApiSettings(key="fake"))
    new = target_locations(tmp_path)
    new.config.mkdir(parents=True)
    target = new.config / "api-settings.json"
    target.write_bytes(b"foreign-config-preserved")
    with pytest.raises(ValueError, match="不同的数据"):
        controller.apply_locations(new)
    assert target.read_bytes() == b"foreign-config-preserved"
    assert not (new.history / "clipboard.sqlite3").exists()
    assert not list(tmp_path.rglob(".clipboard-migrate-*.tmp"))
    close(controller)


def test_route_save_failure_rolls_back_copies_and_keeps_live_connections(qt_app, tmp_path, monkeypatch):
    controller = controller_with_locations(qt_app, tmp_path)
    record = controller.storage.add_text("原库仍可用")
    old = controller.locations
    controller.location_store.save(old)
    before = controller.location_store.path.read_bytes()
    def fail(_):
        raise OSError("simulated disk full")
    monkeypatch.setattr(controller.location_store, "save", fail)
    new = target_locations(tmp_path)
    with pytest.raises(OSError):
        controller.apply_locations(new)
    assert controller.locations == old and controller.storage.get_record(record)
    assert controller.location_store.path.read_bytes() == before
    assert not (new.history / "clipboard.sqlite3").exists()
    assert not any((new.models / name).exists() for name in MODEL_FILES)
    controller.recorder.clipboard.put_text("失败后继续记录")
    assert controller.storage.counts()["total"] == 2
    close(controller)


def test_open_new_database_failure_rolls_back_before_route_commit(qt_app, tmp_path, monkeypatch):
    import app
    controller = controller_with_locations(qt_app, tmp_path)
    record = controller.storage.add_text("保持原连接")
    old = controller.locations
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("simulated new db open failure")
    monkeypatch.setattr(app, "HistoryStorage", fail)
    new = target_locations(tmp_path)
    with pytest.raises(sqlite3.Error):
        controller.apply_locations(new)
    assert controller.locations == old and controller.storage.get_record(record)
    assert not (new.history / "clipboard.sqlite3").exists()
    close(controller)


def test_missing_models_do_not_save_an_unusable_model_location(qt_app, tmp_path):
    controller = controller_with_locations(qt_app, tmp_path)
    (controller.locations.models / MODEL_FILES[0]).unlink()
    with pytest.raises(ValueError, match="模型不完整"):
        controller.apply_locations(target_locations(tmp_path))
    assert not controller.location_store.path.exists()
    close(controller)


def test_reusing_identical_destination_database_is_safe(qt_app, tmp_path):
    controller = controller_with_locations(qt_app, tmp_path)
    record = controller.storage.add_text("相同内容")
    new = target_locations(tmp_path)
    new.history.mkdir(parents=True)
    with sqlite3.connect(new.history / "clipboard.sqlite3") as target:
        controller.storage.connection.backup(target)
    controller.apply_locations(new)
    assert controller.storage.get_record(record) and controller.storage.counts()["total"] == 1
    close(controller)


def test_location_menu_dialog_saves_and_browse_uses_selected_folder(qt_app, tmp_path, monkeypatch):
    controller = controller_with_locations(qt_app, tmp_path)
    new = target_locations(tmp_path)
    def save_locations():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, LocationSettingsDialog)
        for name in LOCATION_LABELS:
            dialog.fields[name].setText(str(getattr(new, name)))
        dialog.save()
        if dialog.result() != dialog.DialogCode.Accepted:
            dialog.reject()
    QTimer.singleShot(0, save_locations)
    actions = [a for menu in controller.window.findChildren(QMenu) for a in menu.actions()
               if a.text() == "数据保存位置"]
    assert len(actions) == 1
    actions[0].trigger()
    assert controller.locations == new
    dialog = LocationSettingsDialog(controller.location_store)
    folder = tmp_path / "另选的导出文件夹"
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(folder))
    dialog.browse("exports")
    assert dialog.fields["exports"].text() == str(folder)
    dialog.restore_defaults()
    assert dialog.fields["history"].text() == str(controller.location_store.defaults.history)
    dialog.reject()
    close(controller)


def test_export_auto_respects_pause_and_png_preserves_pixels(qt_app, tmp_path):
    controller = controller_with_locations(qt_app, tmp_path)
    image = picture(45, 29)
    controller.place_image(image)
    assert not list(controller.locations.exports.glob("*.png"))
    controller.apply_locations(replace(controller.locations, auto_export=True))
    controller.place_image(image)
    exported = list(controller.locations.exports.glob("*.png"))
    assert len(exported) == 1
    assert image_fingerprint(type(image)(str(exported[0]))) == image_fingerprint(image)
    controller.recorder.set_paused(True)
    controller.place_image(picture(50, 35))
    assert len(list(controller.locations.exports.glob("*.png"))) == 1
    close(controller)


def test_manual_png_uses_custom_default_and_keeps_editor_document(qt_app, tmp_path, monkeypatch):
    image = picture()
    settings = SettingsStore(tmp_path)
    settings.export_dir = tmp_path / "单独导出"
    editor = ImageEditor(image, settings)
    expected = image_fingerprint(editor.doc.render())
    captured = []
    target = settings.export_dir / "手动保存.png"
    def choose(*args):
        captured.append(args[2])
        return str(target), "PNG"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
    editor.save_png()
    assert Path(captured[0]).parent == settings.export_dir and target.is_file()
    assert image_fingerprint(type(image)(str(target))) == expected
    assert editor.doc.revision == 0 and not editor.finished
    editor.cancel()


def test_append_extension_does_not_overwrite_an_unconfirmed_existing_png(qt_app, tmp_path, monkeypatch):
    editor = ImageEditor(picture())
    target = tmp_path / "已有文件.png"
    target.write_bytes(b"keep-original")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target.with_suffix("")), "PNG"))
    editor.save_png()
    assert target.read_bytes() == b"keep-original" and "已存在" in editor.status.text()
    editor.cancel()


def test_log_and_migration_backup_follow_custom_paths(qt_app, tmp_path):
    controller = controller_with_locations(qt_app, tmp_path)
    controller.storage.add_text("需要备份")
    new = target_locations(tmp_path)
    controller.apply_locations(new)
    target = write_exception_log(controller.locations, RuntimeError, RuntimeError("synthetic log"), None)
    assert target.parent == new.logs and target.is_file()
    assert not (controller.locations.history / "last-error.txt").exists()
    assert list(new.backups.glob("before-location-change-*.sqlite3"))
    close(controller)


def test_ocr_service_switches_to_selected_model_directory(qt_app, tmp_path):
    controller = controller_with_locations(qt_app, tmp_path)
    first = shared_service()
    assert first.model_dir == controller.locations.models
    new = target_locations(tmp_path)
    controller.apply_locations(new)
    second = shared_service()
    assert first.closed and second is not first and second.model_dir == new.models
    close(controller)


def test_schema_migration_uses_custom_backup_directory(tmp_path):
    history = tmp_path / "history"
    initial = HistoryStorage(history)
    record = initial.add_text("迁移内容")
    initial.close()
    with sqlite3.connect(history / "clipboard.sqlite3") as connection:
        connection.execute("DROP TABLE clip_copy_days")
        connection.execute("PRAGMA user_version=1")
    backups = tmp_path / "独立备份"
    upgraded = HistoryStorage(history, backup_dir=backups)
    assert upgraded.get_record(record)["text"] == "迁移内容"
    assert len(list(backups.glob("before-date-migration-*.sqlite3"))) == 1
    assert not (history / "backups").exists()
    upgraded.close()


def test_unwritable_directory_does_not_change_active_locations(qt_app, tmp_path, monkeypatch):
    controller = controller_with_locations(qt_app, tmp_path)
    record = controller.storage.add_text("仍在原位置")
    old, new = controller.locations, target_locations(tmp_path)
    original_open = Path.open
    def guarded_open(path, *args, **kwargs):
        if path.parent == new.exports and path.name.startswith(".clipboard-write-"):
            raise PermissionError("synthetic denied directory")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    with pytest.raises(PermissionError):
        controller.apply_locations(new)
    assert controller.locations == old and controller.storage.get_record(record)
    assert not controller.location_store.path.exists()
    close(controller)


def test_failed_location_dialog_keeps_original_values_and_stays_open(qt_app, tmp_path):
    store = LocationStore(tmp_path)
    original = store.locations
    store.apply_handler = lambda _: (_ for _ in ()).throw(OSError("synthetic write failure"))
    dialog = LocationSettingsDialog(store)
    dialog.fields["history"].setText(str(tmp_path / "新历史"))
    dialog.save()
    assert dialog.result() != dialog.DialogCode.Accepted and not dialog.error.isHidden()
    assert store.locations == original and not store.path.exists()
    dialog.reject()


def test_failed_png_write_keeps_preexisting_file_and_removes_partial_temp(tmp_path):
    from data_locations import write_png
    target = tmp_path / "existing.png"
    target.write_bytes(b"original-existing-file")
    class FailedImage:
        def save(self, filename, format):
            Path(filename).write_bytes(b"partial-output")
            return False
    with pytest.raises(OSError):
        write_png(FailedImage(), target)
    assert target.read_bytes() == b"original-existing-file"
    assert not list(tmp_path.glob(".clipboard-export-*.tmp"))


def test_unavailable_log_directory_reports_error_without_recursive_exception(qt_app, tmp_path, monkeypatch):
    import app
    controller = controller_with_locations(qt_app, tmp_path)
    def fail(*args):
        raise PermissionError("synthetic log directory offline")
    monkeypatch.setattr(app, "write_exception_log", fail)
    controller.handle_exception(RuntimeError, RuntimeError("synthetic exception"), None)
    assert "日志目录暂不可写" in controller.window.error_label.text()
    assert controller.storage.counts()["total"] == 0
    close(controller)


def test_paused_start_does_not_capture_clipboard_and_updates_visible_status(qt_app, tmp_path):
    clipboard = FakeClipboard()
    controller = ClipboardApplication(qt_app, tmp_path, enable_hotkeys=False, enable_tray=False,
                                      clipboard=clipboard, start_paused=True)
    clipboard.put_text("不应采集的合成文字")
    assert controller.storage.counts()["total"] == 0
    assert controller.recorder.paused and "已暂停" in controller.window.status.text()
    close(controller)
