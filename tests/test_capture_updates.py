"""范围提示、鼠标锚点缩放与日期清理；只使用合成图和隔离库。"""
from copy import deepcopy
from datetime import datetime
import sqlite3
from shiboken6 import isValid

import pytest
from PySide6.QtCore import QDate, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from capture_outline import CaptureOutline
from history_settings import DateCleanupDialog
from image_editor import ImageEditor
from storage import HistoryStorage, image_fingerprint
from test_long_capture import controller, source_image
from test_local_ocr import wait_for


def timestamp(day):
    return int(datetime.fromisoformat(day).timestamp()) * 1_000_000_000


def date_storage(tmp_path, qt_app):
    now = [timestamp("2026-10-01T23:59:59")]
    store = HistoryStorage(tmp_path, clock=lambda: now[0])
    old_text = store.add_text("旧文字")
    old_image = store.add_image(source_image(80, 90))
    shared = store.add_text("新旧两天共享")
    pin = store.add_text("旧固定")
    store.toggle_pin(pin)
    mark = store.add_text("旧标记")
    store.set_mark(mark, True, "保留")
    both = store.add_text("固定及标记")
    store.toggle_pin(both)
    store.set_mark(both, True, "保留")
    now[0] = timestamp("2026-10-02T00:00:00")
    boundary = store.add_text("截止日零点")
    store.touch(shared)
    now[0] = timestamp("2026-10-03T10:00:00")
    future = store.add_text("较新")
    return store, (old_text, old_image, shared, pin, mark, both, boundary, future)


def test_cleanup_exclusive_boundary_preserves_newer_shared_and_protected(qt_app, tmp_path):
    store, ids = date_storage(tmp_path, qt_app)
    old_text, old_image, shared, pin, mark, both, boundary, future = ids
    shared_before = store.get_record(shared)
    try:
        assert store.cleanup_before_stats("2026-10-02") == {
            "days": 3, "records": 2, "images": 1, "protected_days": 3}
        result = store.delete_before("2026-10-02")
        assert result["records"] == 2
        assert store.get_record(old_text) is None and store.get_record(old_image) is None
        assert store.get_record(shared) == shared_before
        assert {r["id"] for r in store.list_records(start="2026-10-01")} == {pin, mark, both}
        assert {r["id"] for r in store.list_records(start="2026-10-02")} == {boundary, shared}
        assert store.get_record(future) is not None
        assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not store.connection.execute("PRAGMA foreign_key_check").fetchall()
        assert store.delete_before("2026-10-02")["days"] == 0
    finally:
        store.close()


def test_cleanup_explicit_include_protected_and_reopen(qt_app, tmp_path):
    store, ids = date_storage(tmp_path, qt_app)
    try:
        assert store.delete_before("2026-10-02", True) == {
            "days": 6, "records": 5, "images": 1, "protected_days": 0}
    finally:
        store.close()
    reopened = HistoryStorage(tmp_path)
    try:
        assert {r["id"] for r in reopened.list_records()} == {ids[2], ids[6], ids[7]}
        assert not reopened.list_records(start="2026-10-01")
    finally:
        reopened.close()


def test_cleanup_failure_rolls_back_content_images_and_days(qt_app, tmp_path):
    store, ids = date_storage(tmp_path, qt_app)
    try:
        before = [store.get_record(value) for value in ids]
        days = list(store.connection.execute("SELECT * FROM clip_copy_days ORDER BY clip_id,copy_day"))
        store.connection.execute(f"""
            CREATE TRIGGER fail_old_day BEFORE DELETE ON clip_copy_days
            WHEN OLD.clip_id={ids[2]} AND OLD.copy_day='2026-10-01'
            BEGIN SELECT RAISE(ABORT,'synthetic failure'); END
        """)
        with pytest.raises(sqlite3.IntegrityError):
            store.delete_before("2026-10-02")
        assert [store.get_record(value) for value in ids] == before
        assert list(store.connection.execute("SELECT * FROM clip_copy_days ORDER BY clip_id,copy_day")) == days
    finally:
        store.close()


@pytest.mark.parametrize("cutoff", ["not-a-date", "2026-02-30"])
def test_cleanup_invalid_date_has_no_write(tmp_path, cutoff):
    store = HistoryStorage(tmp_path)
    try:
        record = store.add_text("保留")
        with pytest.raises(ValueError):
            store.delete_before(cutoff)
        assert store.get_record(record) is not None
    finally:
        store.close()


def test_cleanup_dialog_cancel_confirm_refresh_and_failure(qt_app, tmp_path, monkeypatch):
    store, ids = date_storage(tmp_path, qt_app)
    calls = []
    def apply(cutoff, protected):
        calls.append((cutoff, protected))
        return store.delete_before(cutoff, protected)
    dialog = DateCleanupDialog(store, apply)
    try:
        dialog.cutoff.setDate(QDate(2026, 10, 2))
        assert not dialog.include_protected.isChecked() and dialog.stats["records"] == 2
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
        dialog.delete()
        assert not calls and store.counts()["total"] == 8
        # 预览后新复制过旧内容，确认时重新计算，不能误删现在仍需保留的共享数据。
        store.clock = lambda: timestamp("2026-10-03T11:00:00")
        store.touch(ids[0])
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
        dialog.delete()
        assert calls == [("2026-10-02", False)]
        assert dialog.result_stats["records"] == 1
        assert store.get_record(ids[0]) is not None
        assert dialog.result() == dialog.DialogCode.Accepted
        empty = DateCleanupDialog(store, apply)
        empty.cutoff.setDate(QDate(2026, 10, 2))
        assert not empty.delete_button.isEnabled()
        empty.include_protected.setChecked(True)
        assert empty.stats["records"] == 3 and empty.delete_button.isEnabled()
        def fail(*args):
            raise sqlite3.OperationalError("test")
        empty.apply_handler = fail
        empty.delete()
        assert not empty.error.isHidden() and store.get_record(ids[3]) is not None
        empty.reject()
    finally:
        dialog.close()
        store.close()


@pytest.mark.parametrize("selection,screen", [
    (QRect(100, 80, 300, 350), QRect(0, 0, 900, 700)),
    (QRect(-1200, 100, 300, 350), QRect(-1400, 0, 1400, 900)),
])
def test_outline_is_outside_click_through_and_no_focus(qt_app, selection, screen):
    outline = CaptureOutline(selection, screen)
    try:
        outline.show()
        assert len(outline.bands) == 4
        for band in outline.bands:
            assert band.isVisible() and screen.contains(band.geometry())
            assert not band.geometry().intersects(selection)
            assert band.windowFlags() & Qt.WindowType.WindowTransparentForInput
            assert band.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
        outline.set_paused(True)
        assert {band.color for band in outline.bands} == {"#d5972a"}
    finally:
        bands = list(outline.bands)
        outline.dispose()
        assert all(not band.isVisible() for band in bands)


def test_screen_edge_outline_hidden_before_both_reads_and_cleanup(qt_app):
    current, source, outputs, window = controller(qt_app)
    current.stop()
    current.begin((QRect(0, 0, 300, 400), (0, 0, 300, 400), 1, QRect(0, 0, 1000, 700)))
    wait_for(lambda: current.stitcher is not None)
    reads, capture = [], source.capture
    def guarded():
        for band in current.outline.bands:
            if band.geometry().intersects(current.payload[0]):
                assert not band.isVisible()
        reads.append(True)
        return capture()
    source.capture = guarded
    try:
        current.timer.stop()
        assert all(band.isVisible() for band in current.outline.bands)
        source.top = 100
        current.capture_frame(current.serial)
        wait_for(lambda: current.stitcher.height == 500)
        current.timer.stop()
        assert len(reads) == 2
        assert all(band.isVisible() for band in current.outline.bands)
        current.toggle_pause()
        assert {band.color for band in current.outline.bands} == {"#d5972a"}
        assert image_fingerprint(current.stitcher.render()) == image_fingerprint(
            source.original.copy(0, 0, 300, 500))
        bands = list(current.outline.bands)
        current.finish()
        wait_for(lambda: current.editor is not None)
        assert current.outline is None and all(not isValid(band) or not band.isVisible() for band in bands)
        assert not outputs
    finally:
        current.stop()
        window.close()


def wheel(widget, position, delta, ctrl=True, pixel=False):
    event = QWheelEvent(QPointF(position), QPointF(widget.mapToGlobal(position)),
        QPoint(0, delta) if pixel else QPoint(), QPoint() if pixel else QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)
    QApplication.processEvents()
    return event


def test_ctrl_wheel_anchor_both_directions_ordinary_scroll_and_original_pixels(qt_app):
    editor = ImageEditor(source_image(900, 3000), long_image=True)
    try:
        editor.show()
        QApplication.processEvents()
        editor.set_zoom(.8)
        QApplication.processEvents()
        editor.scroll.verticalScrollBar().setValue(700)
        editor.doc.add([{"kind": "cover", "rect": (50, 900, 100, 40), "color": "#000000"}])
        before = image_fingerprint(editor.doc.render())
        objects, history = deepcopy(editor.doc.objects), deepcopy(editor.doc.undo_states)
        position = QPoint(260, 240)
        canvas_position = editor.canvas.mapFrom(editor.scroll.viewport(), position)
        scale, origin = editor.canvas.transform()
        pixel = (QPointF(canvas_position) - origin) / scale
        wheel(editor.canvas, canvas_position, 120)
        assert editor.zoom_factor == pytest.approx(.96)
        mapped = editor.canvas.mapFrom(editor.scroll.viewport(), position)
        actual_scale, actual_origin = editor.canvas.transform()
        assert ((QPointF(mapped)-actual_origin)/actual_scale - pixel).manhattanLength() < 2
        wheel(editor.canvas, mapped, -120)
        assert editor.zoom_factor == pytest.approx(.8)
        old_value = editor.scroll.verticalScrollBar().value()
        wheel(editor.scroll.viewport(), position, -120, ctrl=False)
        assert editor.scroll.verticalScrollBar().value() > old_value and editor.zoom_factor == pytest.approx(.8)
        wheel(editor.canvas, editor.canvas.mapFrom(editor.scroll.viewport(), position), 60, pixel=True)
        assert editor.zoom_factor == pytest.approx(.96)
        assert image_fingerprint(editor.doc.render()) == before
        assert editor.doc.objects == objects and editor.doc.undo_states == history
        editor.set_zoom(0)
        assert editor.scroll.widgetResizable() and editor.zoom_factor == 0
    finally:
        editor.cancel()


def test_zoom_exact_small_image_clamps_and_edit_handles_keep_coordinates(qt_app):
    editor = ImageEditor(source_image(120, 150))
    try:
        editor.show()
        QApplication.processEvents()
        editor.set_zoom(.5)
        QApplication.processEvents()
        assert editor.canvas.transform()[0] == .5
        position = QPoint(300, 220)
        scale, origin = editor.canvas.transform()
        pixel = (QPointF(editor.canvas.mapFrom(editor.scroll.viewport(), position))-origin)/scale
        wheel(editor.canvas, editor.canvas.mapFrom(editor.scroll.viewport(), position), 120)
        actual_scale, actual_origin = editor.canvas.transform()
        after = (QPointF(editor.canvas.mapFrom(editor.scroll.viewport(), position))-actual_origin)/actual_scale
        assert (after-pixel).manhattanLength() < 2
        scale, origin = editor.canvas.transform()
        assert (editor.canvas.to_pixel(origin+QPointF(60, 70)*scale)-QPointF(60, 70)).manhattanLength() < .01
        editor.set_zoom(100)
        assert editor.zoom_factor == 8
        editor.set_zoom(.0001)
        assert editor.zoom_factor == .05
    finally:
        editor.cancel()
