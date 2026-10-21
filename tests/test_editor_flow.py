"""进入自动提取、用户主动翻译，以及拖动控件不改变图片的回归。"""
from threading import Event
from types import SimpleNamespace

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest

from api_settings import SettingsStore
from image_editor import ImageEditor
from local_ocr import LocalOcrService
from screenshot import SelectionOverlay
from storage import image_fingerprint
from test_editor import fixture_image
from test_local_ocr import output, wait_for


def test_open_auto_extract_manual_translate_and_cached_reopen(qt_app, tmp_path):
    captures, api_calls = [], []
    def engine(array):
        captures.append(array.copy())
        return output("Original")
    service = LocalOcrService(engine_factory=lambda: engine)
    store = SettingsStore(tmp_path)
    store.settings.key = "synthetic-not-sent"
    editor = ImageEditor(fixture_image(), store, local_service=service)
    editor.client.request = lambda settings, kind, prompt, *args: api_calls.append((kind, prompt))
    original = image_fingerprint(editor.doc.render())
    editor.show()
    editor.open_text_panel()
    wait_for(lambda: bool(editor.blocks))
    assert len(captures) == 1 and not api_calls
    assert image_fingerprint(editor.doc.render()) == original
    assert not editor.doc.objects
    assert editor.translate_button.isEnabled()
    assert not editor.apply_button.isEnabled()
    editor.panel.hide()
    editor.open_text_panel()
    assert len(captures) == 1
    editor.source_text.setPlainText("Corrected original")
    editor.translate_button.click()
    assert len(api_calls) == 1 and api_calls[0][0] == "translate"
    assert "Corrected original" in api_calls[0][1]
    editor.api_result("translate", {"translations": [{"id": "1", "text": "校对后的译文"}]})
    assert len(editor.doc.objects) == 1
    assert editor.doc.objects[0]["text"] == "校对后的译文"
    assert editor.apply_button.isEnabled()
    copied = []
    editor.text_copied.connect(copied.append)
    editor.copy_translation_button.click()
    assert copied == ["校对后的译文"]
    editor.cancel()
    service.shutdown()


def test_open_during_recognition_no_duplicate_or_translation(qt_app, tmp_path):
    entered, release, calls, api_calls = Event(), Event(), [], []
    def engine(array):
        calls.append(True)
        entered.set()
        release.wait(2)
        return output()
    service = LocalOcrService(engine_factory=lambda: engine)
    editor = ImageEditor(fixture_image(), SettingsStore(tmp_path), local_service=service)
    editor.client.request = lambda *args: api_calls.append(args)
    editor.open_text_panel()
    wait_for(entered.is_set)
    assert not editor.translate_button.isEnabled()
    editor.open_text_panel()
    editor.translate()
    assert len(calls) == 1 and not api_calls
    release.set()
    wait_for(lambda: bool(editor.blocks))
    assert not api_calls and editor.translate_button.isEnabled()
    editor.extract()
    wait_for(lambda: len(calls) == 2 and not editor.request_active)
    assert not api_calls
    editor.cancel()
    service.shutdown()


def test_empty_recognition_has_no_translate_or_stale_fields(qt_app):
    service = LocalOcrService(engine_factory=lambda: lambda array:
        SimpleNamespace(boxes=None, txts=None, scores=None))
    editor = ImageEditor(fixture_image(), local_service=service)
    editor.open_text_panel()
    wait_for(lambda: not editor.request_active)
    assert not editor.blocks and not editor.translate_button.isEnabled()
    assert not editor.apply_button.isEnabled()
    assert editor.source_text.toPlainText() == ""
    assert "没有识别到文字" in editor.panel_note.text()
    editor.cancel()
    service.shutdown()


def test_edit_invalidates_manual_translation_without_reextract(qt_app, tmp_path):
    calls, api_calls = [], []
    def engine(array):
        calls.append(True)
        return output()
    service = LocalOcrService(engine_factory=lambda: engine)
    editor = ImageEditor(fixture_image(), SettingsStore(tmp_path), local_service=service)
    editor.client.request = lambda *args: api_calls.append(args)
    editor.open_text_panel()
    wait_for(lambda: bool(editor.blocks))
    editor.doc.add([{"kind": "cover", "rect": (30, 20, 30, 20), "color": "#000000"}])
    assert not editor.translate_button.isEnabled()
    editor.translate()
    assert not api_calls and len(calls) == 1
    editor.cancel()
    service.shutdown()


def test_drag_controls_preserves_pixels_and_stays_after_layout(qt_app):
    editor = SelectionOverlay(QPixmap.fromImage(fixture_image(1000, 700)), QRect(0, 0, 1000, 700))
    editor.doc.crop = (80, 100, 500, 300)
    editor.show()
    editor.show_tools()
    editor.show_panel()
    qt_app.processEvents()
    before, revision, crop = image_fingerprint(editor.doc.render()), editor.doc.revision, editor.doc.crop
    for frame in (editor.tools, editor.panel):
        frame.move(100, 50)
        a, b = QPoint(8, 12), QPoint(48, 32)
        QTest.mousePress(frame.handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, a)
        QTest.mouseMove(frame.handle, b, 10)
        QTest.mouseRelease(frame.handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, b)
        position = frame.pos()
        assert frame.manually_placed and position != QPoint(100, 50)
        editor.api_busy(False)
        editor.layout_panels()
        assert frame.pos() == position
    assert editor.doc.revision == revision and editor.doc.crop == crop
    assert image_fingerprint(editor.doc.render()) == before
    editor.resize(800, 550)
    qt_app.processEvents()
    for frame in (editor.tools, editor.panel):
        assert frame.x() >= 8 and frame.y() >= 8
        assert frame.geometry().right() <= editor.width()-8
        assert frame.geometry().bottom() <= editor.height()-8
    editor.cancel()


def test_context_only_shows_related_properties(qt_app):
    editor = ImageEditor(fixture_image())
    editor.show()
    editor.set_tool("arrow")
    assert not editor.style_groups["line_width"].isHidden()
    assert editor.style_groups["font_size"].isHidden()
    assert editor.style_groups["mosaic_strength"].isHidden()
    editor.set_tool("mosaic")
    assert editor.color_button.isHidden()
    assert not editor.style_groups["mosaic_strength"].isHidden()
    editor.set_tool("text")
    assert not editor.style_groups["font_size"].isHidden()
    assert editor.style_groups["line_width"].isHidden()
    editor.cancel()


def test_hidden_panel_stays_hidden_when_ocr_finishes_and_reuses_result(qt_app):
    entered, release, calls = Event(), Event(), []
    def engine(array):
        entered.set()
        calls.append(True)
        release.wait(2)
        return output()
    service = LocalOcrService(engine_factory=lambda: engine)
    editor = ImageEditor(fixture_image(), local_service=service)
    editor.show()
    editor.open_text_panel()
    wait_for(entered.is_set)
    editor.panel.hide_by_user()
    release.set()
    wait_for(lambda: bool(editor.blocks))
    assert editor.panel.isHidden()
    editor.open_text_panel()
    assert not editor.panel.isHidden() and len(calls) == 1
    editor.cancel()
    service.shutdown()


def test_zoom_scroll_keeps_export_pixels_and_crop_coordinates(qt_app):
    editor = ImageEditor(fixture_image(1200, 700))
    editor.resize(800, 500)
    editor.show()
    qt_app.processEvents()
    editor.doc.crop = (50, 60, 200, 150)
    editor.doc.add([{"kind": "cover", "rect": (70, 80, 40, 30), "color": "#000000"}])
    before = image_fingerprint(editor.doc.render())
    editor.set_zoom(2)
    qt_app.processEvents()
    assert editor.scroll.horizontalScrollBar().maximum() > 0
    assert editor.scroll.verticalScrollBar().maximum() > 0
    editor.scroll.horizontalScrollBar().setValue(200)
    editor.scroll.verticalScrollBar().setValue(120)
    assert image_fingerprint(editor.doc.render()) == before
    assert editor.doc.crop == (50, 60, 200, 150)
    editor.set_zoom(0)
    qt_app.processEvents()
    assert editor.scroll.horizontalScrollBar().maximum() == 0
    assert image_fingerprint(editor.doc.render()) == before
    editor.cancel()


def test_new_tool_style_does_not_write_to_previous_selected_object(qt_app):
    editor = ImageEditor(fixture_image())
    obj = {"kind": "arrow", "points": [(20, 20), (80, 80)], "width": 3, "color": "#000000"}
    editor.doc.add([obj])
    editor.canvas.selected = 0
    revision = editor.doc.revision
    editor.set_tool("text")
    editor.font_size.setValue(40)
    assert editor.doc.objects == [obj] and editor.doc.revision == revision
    editor.cancel()


def test_target_language_selection_and_custom_value_reach_translation(qt_app, tmp_path):
    store = SettingsStore(tmp_path)
    store.settings.language = "Deutsch"
    service = LocalOcrService(engine_factory=lambda: lambda array: output("Original"))
    editor = ImageEditor(fixture_image(), store, local_service=service)
    calls = []
    editor.client.request = lambda settings, kind, prompt, *args: calls.append((kind, prompt))
    editor.show()
    editor.open_text_panel()
    wait_for(lambda: bool(editor.blocks))
    assert editor.language.currentText() == "Deutsch"
    assert not calls
    original = image_fingerprint(editor.doc.render())
    editor.language.setCurrentIndex(editor.language.findText("日本語"))
    editor.translate_button.click()
    assert calls[-1][0] == "translate" and "日本語" in calls[-1][1]
    editor.cancel_api()
    editor.language.setEditText("Português")
    editor.translate_button.click()
    assert len(calls) == 2 and "Português" in calls[-1][1]
    assert image_fingerprint(editor.doc.render()) == original
    assert not editor.doc.objects
    editor.cancel()
    service.shutdown()


def test_panel_edge_resize_persists_and_does_not_change_image_or_ocr(qt_app):
    editor = ImageEditor(fixture_image(1000, 700))
    editor.resize(1000, 750)
    editor.show()
    editor.show_panel()
    qt_app.processEvents()
    editor.ocr_revision = editor.doc.revision
    editor.source_text.setPlainText("Text preserved while resizing")
    before = image_fingerprint(editor.doc.render())
    revision, crop, initial_size = editor.doc.revision, editor.doc.crop, editor.panel.size()
    handle = editor.panel.resize_handles[("left",)]
    QTest.mousePress(handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(3, 40))
    QTest.mouseMove(handle, QPoint(-97, 40), 10)
    QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(3, 40))
    qt_app.processEvents()
    assert editor.panel.width() > initial_size.width()
    handle = editor.panel.resize_handles[("bottom",)]
    QTest.mousePress(handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(40, 3))
    QTest.mouseMove(handle, QPoint(40, -77), 10)
    QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(40, 3))
    qt_app.processEvents()
    preferred = editor.panel.size()
    assert preferred.height() < initial_size.height()
    editor.api_busy(False)
    editor.layout_panels()
    editor.panel.hide_by_user()
    editor.open_text_panel()
    qt_app.processEvents()
    assert editor.panel.size() == preferred
    assert editor.source_text.toPlainText() == "Text preserved while resizing"
    assert editor.doc.revision == revision and editor.doc.crop == crop
    assert editor.ocr_revision == revision and image_fingerprint(editor.doc.render()) == before
    editor.resize(760, 490)
    qt_app.processEvents()
    assert editor.panel.geometry().right() < editor.width()-7
    assert editor.panel.geometry().bottom() < editor.height()-7
    editor.resize(1000, 750)
    qt_app.processEvents()
    assert editor.panel.size() == preferred
    editor.cancel()


def test_drag_text_divider_changes_area_without_losing_edits(qt_app):
    editor = ImageEditor(fixture_image(1000, 700))
    editor.resize(1000, 750)
    editor.show()
    editor.show_panel()
    qt_app.processEvents()
    editor.source_text.setPlainText("Long original\n" * 8)
    editor.translated_text.setPlainText("校对的译文\n" * 8)
    before, revision = image_fingerprint(editor.doc.render()), editor.doc.revision
    initial = editor.text_splitter.sizes()
    handle = editor.text_splitter.handle(1)
    start = QPoint(handle.width()//2, handle.height()//2)
    QTest.mousePress(handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(handle, start+QPoint(0, 30), 10)
    QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    qt_app.processEvents()
    adjusted = editor.text_splitter.sizes()
    assert adjusted[0] > initial[0] and adjusted[1] < initial[1]
    editor.api_busy(False)
    editor.layout_panels()
    assert editor.text_splitter.sizes() == adjusted
    assert editor.source_text.toPlainText() == "Long original\n" * 8
    assert editor.translated_text.toPlainText() == "校对的译文\n" * 8
    assert image_fingerprint(editor.doc.render()) == before and editor.doc.revision == revision
    editor.cancel()


def test_grouped_undo_redo_buttons_follow_history_and_restore_pixels(qt_app):
    editor = ImageEditor(fixture_image())
    undo, redo = editor.history_buttons["undo"], editor.history_buttons["redo"]
    assert not undo.isEnabled() and not redo.isEnabled()
    before = image_fingerprint(editor.doc.render())
    editor.doc.add([{"kind": "cover", "rect": (50, 50, 40, 30), "color": "#000000"}])
    changed = image_fingerprint(editor.doc.render())
    assert changed != before and undo.isEnabled() and not redo.isEnabled()
    undo.click()
    assert image_fingerprint(editor.doc.render()) == before
    assert not undo.isEnabled() and redo.isEnabled()
    redo.click()
    assert image_fingerprint(editor.doc.render()) == changed
    assert undo.isEnabled() and not redo.isEnabled()
    editor.cancel()
