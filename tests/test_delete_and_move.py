from copy import deepcopy
from datetime import datetime, timedelta

from PySide6.QtCore import QPoint, QPointF, QItemSelectionModel, Qt
from PySide6.QtTest import QTest

from api_settings import SettingsStore
from image_editor import ImageEditor
from storage import HistoryStorage, image_fingerprint
from test_editor import fixture_image
from ui import HistoryWindow


def editor_with_blocks(qt_app, tmp_path):
    editor = ImageEditor(fixture_image(600, 320), SettingsStore(tmp_path))
    editor.resize(1100, 750)
    editor.show()
    editor.show_panel()
    qt_app.processEvents()
    editor.request_revision, editor.request_size, editor.request_offset = editor.doc.revision, (600, 320), (0, 0)
    editor.api_result("ocr", {"blocks": [
        {"id": "a", "text": "First", "bbox": [.1, .15, .35, .21]},
        {"id": "b", "text": "Second", "bbox": [.55, .5, .8, .56]},
    ]})
    return editor


def canvas_point(editor, pixel):
    scale, origin = editor.canvas.transform()
    return (origin + QPointF(*pixel)*scale).toPoint()


def drag_rect(editor, rect, delta=(20, 10)):
    center = (rect[0]+rect[2]/2, rect[1]+rect[3]/2)
    start = canvas_point(editor, center)
    end = canvas_point(editor, (center[0]+delta[0], center[1]+delta[1]))
    QTest.mousePress(editor.canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(editor.canvas, end, 10)
    QTest.mouseRelease(editor.canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)


class Reply:
    def __init__(self):
        self.aborted = False

    def abort(self):
        self.aborted = True

    def deleteLater(self):
        pass


def start_translation(editor):
    reply, requests = Reply(), []

    def request(settings, kind, prompt, *args):
        requests.append((kind, prompt))
        editor.client.reply = reply
        editor.client.busy_changed.emit(True)

    editor.client.request = request
    editor.translate()
    return reply, requests


def finish_translation(editor):
    editor.client.reply = None
    editor.client.busy_changed.emit(False)
    editor.api_result("translate", {"translations": [
        {"id": wire_id, "text": "第一" if block_id == "a" else "第二"}
        for wire_id, block_id in editor.translation_id_map.items()]})


def test_click_other_short_ocr_box_moves_that_box_and_can_undo(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    before, pixels = deepcopy(editor.blocks), image_fingerprint(editor.doc.render())
    drag_rect(editor, before[1]["rect"])
    assert editor.canvas.selected_block == 1
    assert editor.blocks[0]["rect"] == before[0]["rect"]
    assert abs(editor.blocks[1]["rect"][0]-before[1]["rect"][0]-20) < 2
    assert abs(editor.blocks[1]["rect"][1]-before[1]["rect"][1]-10) < 2
    assert editor.blocks[1]["rect"][2:] == before[1]["rect"][2:]
    assert image_fingerprint(editor.doc.render()) == pixels and editor.ocr_revision == editor.doc.revision
    editor.undo()
    assert editor.blocks == before and image_fingerprint(editor.doc.render()) == pixels
    editor.redo()
    assert editor.blocks[1]["rect"] != before[1]["rect"] and editor.ocr_revision == editor.doc.revision
    editor.cancel()


def test_move_and_select_during_translation_keeps_request_and_uses_new_position(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    before = deepcopy(editor.blocks)
    reply, requests = start_translation(editor)
    assert editor.block_list.isEnabled() and editor.source_text.isReadOnly()
    drag_rect(editor, before[1]["rect"])
    assert not reply.aborted and editor.request_active and len(requests) == 1
    assert editor.request_revision == editor.doc.revision
    new_rect = editor.blocks[1]["rect"]
    finish_translation(editor)
    layer = next(obj for obj in editor.doc.objects if obj.get("block_id") == "b")
    assert layer["rect"] == new_rect and editor.ocr_revision == editor.doc.revision
    editor.cancel()


def test_translation_response_during_drag_waits_for_release(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    reply, _ = start_translation(editor)
    rect = editor.blocks[1]["rect"]
    center = rect[0]+rect[2]/2, rect[1]+rect[3]/2
    start = canvas_point(editor, center)
    end = canvas_point(editor, (center[0]+20, center[1]+10))
    QTest.mousePress(editor.canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(editor.canvas, end, 10)
    finish_translation(editor)
    assert editor.pending_translation_preview and not editor.doc.objects and editor.canvas.tool == "ocrbox"
    QTest.mouseRelease(editor.canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    assert not editor.pending_translation_preview and len(editor.doc.objects) == 2
    layer = next(obj for obj in editor.doc.objects if obj.get("block_id") == "b")
    assert layer["rect"] == editor.blocks[1]["rect"] and not reply.aborted
    assert editor.blocks[1]["translated"] == "第二"
    editor.cancel()


def test_move_translated_layers_and_update_keeps_positions_and_ocr(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    start_translation(editor)
    finish_translation(editor)
    editor.block_list.setCurrentRow(0)
    assert editor.canvas.tool == "select"
    first = deepcopy(editor.doc.objects[0])
    coverage = first["source_rect"]
    drag_rect(editor, first["rect"])
    moved = deepcopy(next(obj for obj in editor.doc.objects if obj.get("block_id") == "a"))
    assert moved["rect"] != first["rect"] and moved["source_rect"] == coverage
    assert editor.ocr_revision == editor.doc.revision and editor.block_list.isEnabled()
    editor.translated_text.setPlainText("修改后的第一")
    editor.apply_button.click()
    layer = next(obj for obj in editor.doc.objects if obj.get("block_id") == "a")
    assert layer["rect"] == moved["rect"] and layer["source_rect"] == coverage
    assert layer["text"] == "修改后的第一" and len(editor.doc.objects) == 2
    editor.block_list.setCurrentRow(1)
    second = deepcopy(next(obj for obj in editor.doc.objects if obj.get("block_id") == "b"))
    drag_rect(editor, second["rect"], (-20, 10))
    assert next(obj for obj in editor.doc.objects if obj.get("block_id") == "b")["rect"] != second["rect"]
    assert editor.ocr_revision == editor.doc.revision
    editor.doc.add([{"kind": "cover", "rect": (20, 20, 10, 10), "color": "#000000"}])
    assert editor.ocr_revision is None and not editor.translate_button.isEnabled()
    editor.cancel()


def test_delete_ocr_box_and_translation_layer_are_undoable(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    before = deepcopy(editor.blocks)
    original = image_fingerprint(editor.doc.render())
    editor.delete_object()
    assert [block["id"] for block in editor.blocks] == ["b"]
    assert image_fingerprint(editor.doc.render()) == original and editor.ocr_revision == editor.doc.revision
    editor.undo()
    assert editor.blocks == before
    start_translation(editor)
    finish_translation(editor)
    translated = image_fingerprint(editor.doc.render())
    editor.block_list.setCurrentRow(0)
    editor.delete_object()
    assert len(editor.doc.objects) == 1 and not editor.blocks[0]["apply"]
    assert editor.ocr_revision == editor.doc.revision
    editor.undo()
    assert len(editor.doc.objects) == 2 and editor.blocks[0]["apply"]
    assert image_fingerprint(editor.doc.render()) == translated
    editor.redo()
    assert len(editor.doc.objects) == 1 and not editor.blocks[0]["apply"]
    editor.apply_translations()
    assert len(editor.doc.objects) == 1
    editor.cancel()


def test_delete_an_ocr_box_during_translation_ignores_its_late_result(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    reply, _ = start_translation(editor)
    editor.delete_object()
    assert not reply.aborted and [b["id"] for b in editor.blocks] == ["b"]
    finish_translation(editor)
    assert [obj["block_id"] for obj in editor.doc.objects] == ["b"]
    editor.undo()
    assert not editor.doc.objects and editor.blocks[0]["translated"] == "第二"
    editor.undo()
    assert [block["translated"] for block in editor.blocks] == ["第一", "第二"]
    editor.cancel()


def test_delete_from_block_list_and_undo_keep_later_text_corrections(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    editor.activateWindow()
    editor.block_list.setFocus()
    qt_app.processEvents()
    QTest.keyClick(editor.block_list, Qt.Key.Key_Delete)
    assert [block["id"] for block in editor.blocks] == ["b"]
    editor.source_text.setPlainText("Corrected second")
    editor.undo()
    assert [block["id"] for block in editor.blocks] == ["a", "b"]
    assert editor.blocks[1]["text"] == "Corrected second"
    editor.cancel()


def test_layer_movement_undo_keeps_corrections_entered_after_move(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    start_translation(editor)
    finish_translation(editor)
    editor.block_list.setCurrentRow(0)
    layer = deepcopy(next(obj for obj in editor.doc.objects if obj.get("block_id") == "a"))
    drag_rect(editor, layer["rect"])
    editor.translated_text.setPlainText("后续校对")
    editor.undo()
    assert next(obj for obj in editor.doc.objects if obj.get("block_id") == "a")["rect"] == layer["rect"]
    assert editor.blocks[0]["translated"] == "后续校对"
    assert editor.ocr_revision == editor.doc.revision
    editor.cancel()

def test_history_delete_key_does_not_delete_search_and_selects_neighbour(qt_app, tmp_path):
    store = HistoryStorage(tmp_path)
    ids = [store.add_text(f"record {i}") for i in range(4)]
    window = HistoryWindow(store)
    window.delete_requested.connect(lambda record_id: (store.delete(record_id), window.refresh()))
    window.show()
    window.activateWindow()
    qt_app.processEvents()
    window.refresh(select_id=ids[2])
    window.search.setFocus()
    window.search.setText("record")
    window.search.setCursorPosition(0)
    QTest.keyClick(window.search, Qt.Key.Key_Delete)
    assert store.counts()["total"] == 4 and window.search.text() == "ecord"
    window.search.clear()
    window.refresh(select_id=ids[2])
    window.list_view.setFocus()
    qt_app.processEvents()
    QTest.keyClick(window.list_view, Qt.Key.Key_Delete)
    assert store.get_record(ids[2]) is None and window.current_id == ids[1]
    assert window.delete_button.isEnabled()
    window.exiting = True
    window.close()
    store.close()


def test_multiday_multiselect_delete_removes_unique_content_and_all_days(qt_app, tmp_path):
    day = datetime(2026, 10, 5, 12)
    now = [int(day.timestamp())*1_000_000_000]
    store = HistoryStorage(tmp_path, clock=lambda: now[0])
    one = store.add_text("one")
    two = store.add_text("two")
    store.toggle_pin(two)
    now[0] += int(timedelta(days=1).total_seconds())*1_000_000_000
    store.add_text("one")
    three = store.add_text("three")
    window = HistoryWindow(store)
    window.delete_many_requested.connect(lambda ids: (store.delete_many(ids), window.refresh()))
    window.set_date_mode("custom", "2026-10-05", "2026-10-06")
    window.show()
    selection = window.list_view.selectionModel()
    selection.clearSelection()
    for index, row in enumerate(window.model.rows):
        if not row.get("header") and row["id"] in (one, two):
            selection.select(window.model.index(index, 0), QItemSelectionModel.SelectionFlag.Select)
    assert set(window.selected_record_ids()) == {one, two} and window.delete_button.text() == "删除 2 项"
    window.refresh()
    assert set(window.selected_record_ids()) == {one, two}
    window.delete_button.click()
    assert store.counts()["total"] == 1 and store.get_record(three) is not None
    assert not store.connection.execute("SELECT * FROM clip_copy_days WHERE clip_id IN (?,?)", (one, two)).fetchall()
    assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    window.exiting = True
    window.close()
    store.close()
