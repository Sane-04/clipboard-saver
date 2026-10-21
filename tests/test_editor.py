from copy import deepcopy

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtTest import QTest

from image_document import ImageDocument, handles, move_object, rect_of, resize_rect, text_overflows
from image_editor import ImageEditor
from screenshot import SelectionOverlay
from storage import image_fingerprint


def fixture_image(width=600, height=400):
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(QColor("#ffffff"))
    return image


def drag(canvas, start, end):
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(*start))
    QTest.mouseMove(canvas, QPoint(*end), 10)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(*end))


def pixel_point(canvas, point):
    scale, origin = canvas.transform()
    p = origin + QPointF(*point) * scale
    return round(p.x()), round(p.y())


def test_release_waits_for_commit_and_cancel_preserves(qt_app):
    pixmap = QPixmap.fromImage(fixture_image())
    pixmap.setDevicePixelRatio(1.5)
    editor = SelectionOverlay(pixmap, QRect(-400, -100, 400, 267))
    exported, cancelled = [], []
    editor.selected.connect(exported.append)
    editor.cancelled.connect(lambda: cancelled.append(True))
    editor.show()
    qt_app.processEvents()
    drag(editor.canvas, (40, 40), (120, 120))
    assert not exported and not cancelled and editor.isVisible()
    assert editor.tools.isVisible()
    assert editor.doc.render().width() in (120, 121)
    editor.commit()
    assert len(exported) == 1
    assert exported[0].devicePixelRatio() == 1
    editor.commit()
    assert len(exported) == 1
    other = SelectionOverlay(pixmap, QRect(0, 0, 400, 267))
    other.selected.connect(exported.append)
    other.cancelled.connect(lambda: cancelled.append(True))
    other.show()
    drag(other.canvas, (40, 40), (120, 120))
    other.cancel()
    assert len(exported) == 1 and len(cancelled) == 1


@pytest.mark.parametrize("index", range(8))
def test_all_resize_handles_bound_and_change(index):
    rect = QRectF(100, 100, 200, 100)
    point = handles(rect)[index] + QPointF(-40 if index in (0, 6, 7) else 40,
                                         -30 if index in (0, 1, 2) else 30)
    changed = resize_rect(rect, index, point, QRectF(0, 0, 400, 400))
    assert changed != rect
    assert QRectF(0, 0, 400, 400).contains(changed)
    clamped = resize_rect(rect, index, QPointF(-1000, 2000), QRectF(0, 0, 400, 400))
    assert clamped.width() >= 4 and clamped.height() >= 4
    assert QRectF(0, 0, 400, 400).contains(clamped)


def test_crop_moving_does_not_stretch_annotations(qt_app):
    editor = ImageEditor(fixture_image())
    editor.show()
    qt_app.processEvents()
    editor.doc.crop = (100, 100, 100, 100)
    obj = {"kind": "cover", "rect": (110, 110, 30, 30), "color": "#000000"}
    editor.doc.add([obj])
    editor.set_tool("crop")
    drag(editor.canvas, pixel_point(editor.canvas, (150, 150)), pixel_point(editor.canvas, (180, 170)))
    assert abs(editor.doc.crop[0] - 130) < 2
    assert editor.doc.objects[0] == obj
    assert editor.doc.render().width() in (100, 101)
    editor.undo()
    assert editor.doc.crop == (100, 100, 100, 100)
    editor.cancel()


def test_rasterization_redaction_mosaic_annotation_and_undo(qt_app):
    image = fixture_image(200, 160)
    for y in range(160):
        for x in range(200):
            image.setPixelColor(x, y, QColor(x % 255, y % 255, (x * y) % 255))
    original = image_fingerprint(image)
    doc = ImageDocument(image, (10, 10, 180, 140))
    doc.add([
        {"kind": "cover", "rect": (20, 20, 40, 30), "color": "#000000"},
        {"kind": "mosaic", "rect": (80, 20, 40, 40), "strength": 12},
        {"kind": "arrow", "points": [(20, 90), (70, 110)], "color": "#ff0000", "width": 5},
        {"kind": "freehand", "points": [(140, 80), (155, 85), (145, 100)], "color": "#0000ff", "width": 4},
        {"kind": "text", "rect": (20, 120, 130, 25), "text": "中文\n", "font_size": 14, "color": "#000000"},
    ])
    result = doc.render()
    assert result.size().width() == 180
    assert result.pixelColor(25, 25).name() == "#000000"
    assert result.pixelColor(120, 10) == image.pixelColor(130, 20)
    assert doc.full_image().pixelColor(88, 29) != image.pixelColor(88, 29)
    assert image_fingerprint(image) == original
    rendered = image_fingerprint(result)
    doc.undo()
    assert not doc.objects
    doc.redo()
    assert image_fingerprint(doc.render()) == rendered
    doc.undo()
    doc.add([{"kind": "cover", "rect": (0, 0, 10, 10), "color": "#000000"}])
    assert not doc.redo_states


def test_translation_selected_region_undo_moving_and_overflow(qt_app):
    image = fixture_image(400, 200)
    image.setPixelColor(30, 40, QColor("#000000"))
    editor = ImageEditor(image)
    editor.show()
    editor.blocks = [
        {"id": "1", "text": "Hello", "translated": "你好", "rect": (20, 20, 120, 60), "apply": True},
        {"id": "2", "text": "Skip", "translated": "跳过", "rect": (200, 20, 100, 50), "apply": False},
    ]
    editor.ocr_revision = editor.doc.revision
    editor.refresh_blocks()
    editor.apply_translations()
    assert len(editor.doc.objects) == 1
    translated = editor.doc.render()
    assert translated.pixelColor(30, 40) != image.pixelColor(30, 40)
    assert translated.pixelColor(220, 30) == image.pixelColor(220, 30)
    obj = editor.doc.objects[0]
    source = deepcopy(obj["source_rect"])
    move_object(obj, QPointF(40, 70))
    editor.doc.changed_now()
    assert obj["source_rect"] == source
    assert editor.doc.render().pixelColor(30, 40).name() == "#ffffff"
    editor.undo()
    assert image_fingerprint(editor.doc.render()) == image_fingerprint(image)
    assert text_overflows({"kind": "text", "rect": (0, 0, 12, 10), "text": "长译文" * 30, "font_size": 24})
    editor.cancel()


def test_ocr_refresh_never_overwrites_new_response_with_old_fields(qt_app):
    editor = ImageEditor(fixture_image())
    editor.blocks = [{"id": "1", "text": "Old", "translated": "", "rect": (0, 0, 30, 30), "apply": True}]
    editor.refresh_blocks()
    editor.translated_text.setPlainText("旧译文")
    editor.blocks = [{"id": "1", "text": "New", "translated": "新译文", "rect": (0, 0, 30, 30), "apply": True}]
    editor.refresh_blocks()
    assert editor.blocks[0]["text"] == "New"
    assert editor.translated_text.toPlainText() == "新译文"
    editor.cancel()


def test_translation_response_previews_then_checkboxes_update_without_duplicates(qt_app):
    image = fixture_image(400, 200)
    editor = ImageEditor(image)
    editor.blocks = [
        {"id": "1", "text": "Hello", "translated": "", "rect": (20, 20, 120, 60), "apply": True},
        {"id": "2", "text": "World", "translated": "", "rect": (220, 20, 120, 60), "apply": True},
    ]
    editor.ocr_revision = editor.request_revision = editor.doc.revision
    editor.translation_ids = ["1", "2"]
    editor.refresh_blocks()
    exported = []
    editor.selected.connect(exported.append)
    editor.api_result("translate", {"translations": [{"id": "1", "text": "你好"}, {"id": "2", "text": "世界"}]})
    assert len(editor.doc.objects) == 2 and not exported
    assert editor.ocr_revision == editor.doc.revision
    editor.block_list.item(1).setCheckState(Qt.CheckState.Unchecked)
    editor.apply_translations()
    assert len(editor.doc.objects) == 1 and editor.doc.objects[0]["text"] == "你好"
    editor.translated_text.setPlainText("您好")
    editor.apply_translations()
    assert len(editor.doc.objects) == 1 and editor.doc.objects[0]["text"] == "您好"
    editor.block_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    editor.apply_translations()
    assert not editor.doc.objects
    editor.cancel()


def test_history_editor_entry_cancel_commit_and_pause(qt_app, tmp_path):
    from app import ClipboardApplication
    from test_history import FakeClipboard
    clipboard = FakeClipboard()
    controller = ClipboardApplication(qt_app, tmp_path, enable_hotkeys=False,
                                      enable_tray=False, clipboard=clipboard)
    original = fixture_image(120, 90)
    source_id = controller.storage.add_image(original)
    controller.window.refresh(select_id=source_id)
    controller.edit_image()
    assert controller.editor.isVisible()
    controller.editor.cancel()
    QTest.qWait(10)
    assert controller.editor is None and controller.storage.counts()["total"] == 1
    controller.edit_image()
    editor = controller.editor
    editor.doc.add([{"kind": "cover", "rect": (10, 10, 30, 30), "color": "#000000"}])
    editor.commit()
    QTest.qWait(10)
    assert clipboard.image().pixelColor(20, 20).name() == "#000000"
    assert controller.storage.counts()["total"] == 2
    assert image_fingerprint(QImage.fromData(controller.storage.get_record(source_id)["image"])) == image_fingerprint(original)
    controller.recorder.set_paused(True)
    controller.window.refresh(select_id=source_id)
    controller.edit_image()
    controller.editor.doc.add([{"kind": "cover", "rect": (10, 10, 30, 30), "color": "#ff0000"}])
    controller.editor.commit()
    QTest.qWait(10)
    assert clipboard.image().pixelColor(20, 20).name() == "#ff0000"
    assert controller.storage.counts()["total"] == 2
    controller.cleanup()
    controller.window.exiting = True
    controller.window.close()
