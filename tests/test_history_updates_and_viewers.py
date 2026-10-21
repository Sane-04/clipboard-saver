"""隔离历史/模拟剪贴板验证显式更新、独立查看和置顶不改内容。"""
import sqlite3
from copy import deepcopy
from datetime import datetime

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app import ClipboardApplication
from history_edit import TextRecordDialog
from image_editor import ImageEditor
from storage import HistoryStorage, image_fingerprint
from test_editor import fixture_image
from test_history import FakeClipboard, picture
from ui import HistoryWindow


def date_rows(store):
    return [tuple(r) for r in store.connection.execute(
        "SELECT * FROM clip_copy_days ORDER BY clip_id,copy_day")]


def test_update_text_preserves_id_dates_protection_format_and_fingerprint(qt_app, tmp_path):
    clock = [int(datetime(2026, 10, 1, 12).timestamp()) * 10**9]
    store = HistoryStorage(tmp_path, clock=lambda: clock[0])
    record_id = store.add_text("old")
    store.toggle_pin(record_id)
    store.set_mark(record_id, True, "手动标签")
    clock[0] += 86400 * 10**9
    store.touch(record_id)
    before, days = store.get_record(record_id), date_rows(store)
    updated = "  中文修改\n    code()\n"
    store.update_text(record_id, updated, expected=before["fingerprint"])
    current = store.get_record(record_id)
    assert current["id"] == record_id and current["text"] == updated
    assert all(current[k] == before[k] for k in ("pinned", "marked", "tag", "updated_at"))
    assert current["fingerprint"] != before["fingerprint"] and date_rows(store) == days
    assert store.add_text(updated) == record_id and store.counts()["total"] == 1
    store.close()
    store = HistoryStorage(tmp_path)
    assert store.get_record(record_id)["text"] == updated
    store.close()


def test_image_update_refreshes_original_thumbnail_and_preserves_metadata(qt_app, tmp_path):
    store = HistoryStorage(tmp_path)
    record_id = store.add_image(picture())
    store.set_mark(record_id, True, "图片")
    before, days = store.get_record(record_id), date_rows(store)
    new = picture(60, 40)
    new.setPixelColor(2, 2, QColor("#ff0000"))
    store.update_image(record_id, new, expected=before["fingerprint"])
    record = store.get_record(record_id)
    assert record["width"] == 60 and record["height"] == 40 and record["tag"] == "图片"
    assert image_fingerprint(QImage.fromData(record["image"])) == image_fingerprint(new)
    thumbnail = QImage.fromData(store.thumbnail(record_id))
    assert thumbnail.size() == new.size().scaled(140, 96, Qt.AspectRatioMode.KeepAspectRatio)
    assert image_fingerprint(thumbnail) != image_fingerprint(QImage.fromData(before["thumbnail"]))
    assert thumbnail.pixelColor(5, 5).red() > thumbnail.pixelColor(5, 5).green()
    assert date_rows(store) == days and store.counts()["total"] == 1
    store.close()


def test_duplicate_stale_deleted_and_empty_edits_never_overwrite_records(qt_app, tmp_path):
    store = HistoryStorage(tmp_path)
    first, second = store.add_text("one"), store.add_text("two")
    before = deepcopy(store.get_record(first))
    with pytest.raises(ValueError, match="相同内容"):
        store.update_text(first, "two")
    with pytest.raises(ValueError, match="不能为空"):
        store.update_text(first, " \n ")
    assert store.get_record(first) == before and store.get_record(second)["text"] == "two"
    store.update_text(first, "new")
    with pytest.raises(ValueError, match="编辑期间"):
        store.update_text(first, "stale", expected=before["fingerprint"])
    assert store.get_record(first)["text"] == "new"
    store.delete(first)
    with pytest.raises(ValueError, match="删除"):
        store.update_text(first, "deleted")
    store.close()


def test_update_database_failure_rolls_back_all_content(qt_app, tmp_path):
    store = HistoryStorage(tmp_path)
    record_id = store.add_text("unchanged")
    before, days = store.get_record(record_id), date_rows(store)
    store.connection.execute("""
        CREATE TRIGGER reject_update BEFORE UPDATE OF fingerprint ON clips
        BEGIN SELECT RAISE(ABORT,'synthetic failure'); END
    """)
    with pytest.raises(sqlite3.Error):
        store.update_text(record_id, "failed")
    assert store.get_record(record_id) == before and date_rows(store) == days
    store.close()


def test_text_dialog_cancel_failure_and_success_are_explicit(qt_app, tmp_path):
    store = HistoryStorage(tmp_path)
    record_id = store.add_text("original")
    dialog = TextRecordDialog("original", lambda text: store.update_text(record_id, text))
    dialog.text.setPlainText("cancelled")
    dialog.reject()
    assert store.get_record(record_id)["text"] == "original"
    dialog = TextRecordDialog("original", lambda text: store.update_text(record_id, text))
    dialog.text.setPlainText("")
    dialog.save()
    assert "不能为空" in dialog.error.text()
    assert store.get_record(record_id)["text"] == "original"
    dialog.text.setPlainText("changed")
    dialog.save()
    assert store.get_record(record_id)["text"] == "changed"
    store.close()


def test_unchanged_text_dialog_preserves_original_crlf_without_writing(qt_app):
    saved = []
    dialog = TextRecordDialog("  line one\r\n    line two\r\n", saved.append)
    dialog.save()
    assert not saved


def test_controller_text_editor_saves_original_without_copying(qt_app, tmp_path):
    clipboard = FakeClipboard()
    controller = ClipboardApplication(qt_app, tmp_path, enable_hotkeys=False, enable_tray=False,
                                      clipboard=clipboard)
    record_id = controller.storage.add_text("before")
    def fill_dialog():
        dialog = next(w for w in QApplication.topLevelWidgets()
                      if isinstance(w, TextRecordDialog) and w.isVisible())
        dialog.text.setPlainText("after")
        dialog.save()
    QTimer.singleShot(0, fill_dialog)
    controller.edit_saved_text(record_id)
    assert controller.storage.get_record(record_id)["text"] == "after"
    assert controller.storage.counts()["total"] == 1 and not clipboard.mimeData().hasText()
    controller.cleanup()
    controller.window.exiting = True
    controller.window.close()


def test_image_editor_update_original_keeps_clipboard_and_source_id(qt_app, tmp_path):
    clipboard = FakeClipboard()
    controller = ClipboardApplication(qt_app, tmp_path, enable_hotkeys=False, enable_tray=False,
                                      clipboard=clipboard)
    record_id = controller.storage.add_image(fixture_image(400, 250))
    controller.storage.set_mark(record_id, True, "保留")
    controller.window.refresh(select_id=record_id)
    before, days = controller.storage.get_record(record_id), date_rows(controller.storage)
    controller.edit_image()
    editor = controller.editor
    editor.doc.add([{"kind": "cover", "rect": (10, 10, 30, 30), "color": "#000000"}])
    editor.save_mode.setCurrentIndex(editor.save_mode.findData("update"))
    editor.commit()
    QTest.qWait(5)
    row = controller.storage.get_record(record_id)
    assert controller.storage.counts()["total"] == 1 and row["tag"] == "保留"
    assert row["updated_at"] == before["updated_at"] and date_rows(controller.storage) == days
    assert QImage.fromData(row["image"]).pixelColor(20, 20) == QColor("#000000")
    assert not clipboard.mimeData().hasImage()
    controller.cleanup()
    controller.window.exiting = True
    controller.window.close()


def test_failed_image_update_keeps_editor_and_can_save_new(qt_app, tmp_path):
    clipboard = FakeClipboard()
    controller = ClipboardApplication(qt_app, tmp_path, enable_hotkeys=False, enable_tray=False,
                                      clipboard=clipboard)
    record_id = controller.storage.add_image(fixture_image(400, 250))
    controller.window.refresh(select_id=record_id)
    controller.edit_image()
    editor = controller.editor
    editor.doc.add([{"kind": "cover", "rect": (20, 20, 30, 30), "color": "#000000"}])
    controller.storage.delete(record_id)
    editor.save_mode.setCurrentIndex(editor.save_mode.findData("update"))
    editor.commit()
    assert not editor.finished and editor.isVisible() and "删除" in editor.status.text()
    editor.save_mode.setCurrentIndex(editor.save_mode.findData("new"))
    editor.commit()
    assert clipboard.mimeData().hasImage() and controller.storage.counts()["total"] == 1
    controller.cleanup()
    controller.window.exiting = True
    controller.window.close()


def test_double_click_image_opens_independent_viewer_and_text_still_copies(qt_app, tmp_path):
    store = HistoryStorage(tmp_path)
    text_id = store.add_text("double click text")
    image_id = store.add_image(picture())
    window = HistoryWindow(store)
    copied = []
    window.copy_requested.connect(copied.append)
    window.show()
    window.refresh(select_id=image_id)
    QTest.mouseDClick(window.image_preview, Qt.MouseButton.LeftButton,
                     pos=window.image_preview.rect().center())
    assert len(window.image_viewers) == 1 and not copied
    first = window.image_viewers[0]
    assert first.parentWidget() is None and first.isVisible()
    window.close()
    assert not window.isVisible() and first.isVisible()
    window.show_history()
    window.refresh(select_id=image_id)
    index = window.list_view.currentIndex()
    window.activate_record(index)
    assert len(window.image_viewers) == 2
    second = window.image_viewers[1]
    first.close()
    assert window.image_viewers == [second] and second.isVisible()
    window.refresh(select_id=text_id)
    window.activate_record(window.list_view.currentIndex())
    assert copied == [text_id]
    window.exiting = True
    window.close()
    assert not window.image_viewers
    store.close()


def test_viewer_snapshot_and_independent_pin_do_not_change_history(qt_app, tmp_path):
    store = HistoryStorage(tmp_path)
    image_id = store.add_image(picture())
    window = HistoryWindow(store)
    window.show()
    window.refresh(select_id=image_id)
    before, days = store.get_record(image_id), date_rows(store)
    viewer = window.open_image_viewer()
    pixels = image_fingerprint(viewer.surface.image)
    requests = []
    window.topmost_button.requested.connect(lambda: requests.append("history"))
    viewer.topmost_button.requested.connect(lambda: requests.append("viewer"))
    window.topmost_button.click()
    viewer.topmost_button.click()
    window.topmost_button.click()
    assert requests == ["history", "viewer", "history"]
    window.hide_window()
    assert viewer.isVisible() and store.get_record(image_id) == before and date_rows(store) == days
    replacement = picture()
    replacement.fill(QColor("#ff0000"))
    store.update_image(image_id, replacement)
    assert image_fingerprint(viewer.surface.image) == pixels
    viewer.set_fit(False)
    assert viewer.surface.size() == viewer.surface.image.size()
    window.exiting = True
    window.close()
    store.close()


def test_editor_pin_preserves_document_and_undo(qt_app):
    editor = ImageEditor(fixture_image())
    editor.show()
    editor.doc.add([{"kind": "cover", "rect": (10, 10, 20, 20), "color": "#000000"}])
    pixels, revision, undo = image_fingerprint(editor.doc.render()), editor.doc.revision, len(editor.doc.undo_states)
    editor.topmost_button.click()
    editor.topmost_button.click()
    assert image_fingerprint(editor.doc.render()) == pixels and editor.doc.revision == revision
    assert len(editor.doc.undo_states) == undo and editor.isVisible()
    editor.cancel()
