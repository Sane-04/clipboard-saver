"""全文与可选复制范围的交互；所有图文为合成，剪贴板仅使用 offscreen 平台。"""
from copy import deepcopy

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest

from storage import image_fingerprint
from test_delete_and_move import editor_with_blocks, start_translation, finish_translation


@pytest.fixture
def editor(qt_app, tmp_path):
    instance = editor_with_blocks(qt_app, tmp_path)
    yield instance
    instance.cancel()
    qt_app.processEvents()


def selected_text(field):
    return field.textCursor().selectedText().replace("\u2029", "\n")


def test_open_selects_all_blocks_with_spaces_and_ctrl_a_c(qt_app, editor):
    editor.source_text.setPlainText("  第一行\n    保留缩进")
    editor.block_list.setCurrentRow(1)
    editor.source_text.setPlainText("第二块 · café")
    original = image_fingerprint(editor.doc.render())
    editor.full_text_button.click()
    qt_app.processEvents()
    dialog = editor.full_text_dialog
    expected = "  第一行\n    保留缩进\n第二块 · café"
    assert selected_text(dialog.source_text) == expected
    dialog.source_text.moveCursor(QTextCursor.MoveOperation.End)
    QTest.keyClick(dialog.source_text, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert selected_text(dialog.source_text) == expected
    assert qt_app.platformName() == "offscreen"
    QTest.keyClick(dialog.source_text, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert qt_app.clipboard().text() == expected
    copied = []
    editor.text_copied.connect(copied.append)
    dialog.copy_button.click()
    assert copied == [expected]
    assert image_fingerprint(editor.doc.render()) == original
    assert not editor.doc.objects


def test_copy_scope_excludes_unwanted_rows_without_changing_translation(editor):
    editor.block_list.item(1).setCheckState(Qt.CheckState.Unchecked)
    before = deepcopy(editor.blocks)
    editor.full_text_button.click()
    dialog = editor.full_text_dialog
    assert dialog.source_text.toPlainText() == "First\nSecond"
    dialog.block_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert dialog.source_text.toPlainText() == "Second"
    copied = []
    editor.text_copied.connect(copied.append)
    dialog.copy_button.click()
    assert copied == ["Second"] and editor.blocks == before
    dialog.scope_buttons[1].click()
    assert not dialog.copy_button.isEnabled() and not dialog.source_text.toPlainText()
    dialog.scope_buttons[2].click()
    assert dialog.source_text.toPlainText() == "First\nSecond"
    dialog.block_list.item(1).setCheckState(Qt.CheckState.Unchecked)
    dialog.scope_buttons[0].click()
    assert dialog.source_text.toPlainText() == "First\nSecond"
    assert editor.blocks == before


def test_refresh_and_switch_block_preserve_cross_line_partial_selection(editor):
    editor.full_text_button.click()
    dialog = editor.full_text_dialog
    cursor = dialog.source_text.textCursor()
    cursor.setPosition(2)
    cursor.setPosition(10, QTextCursor.MoveMode.KeepAnchor)
    dialog.source_text.setTextCursor(cursor)
    expected = selected_text(dialog.source_text)
    editor.refresh_text_controls()
    editor.block_list.setCurrentRow(1)
    editor.refresh_text_controls()
    assert selected_text(dialog.source_text) == expected == "rst\nSeco"
    editor.full_text_button.click()
    assert editor.full_text_dialog is dialog
    assert selected_text(dialog.source_text) == "First\nSecond"


def test_corrections_delete_undo_and_new_ocr_keep_correct_scope(editor):
    editor.full_text_button.click()
    dialog = editor.full_text_dialog
    dialog.block_list.item(1).setCheckState(Qt.CheckState.Unchecked)
    editor.source_text.setPlainText("Corrected first")
    assert dialog.source_text.toPlainText() == "Corrected first"
    editor.delete_object()
    assert dialog.source_text.toPlainText() == ""
    editor.undo()
    assert dialog.source_text.toPlainText() == "Corrected first"
    editor.request_revision = editor.doc.revision
    editor.api_result("ocr", {"blocks": [
        {"id": "a", "text": "New first", "bbox": [.1, .1, .3, .2]},
        {"id": "b", "text": "New second", "bbox": [.4, .4, .7, .5]},
    ]})
    assert dialog.source_text.toPlainText() == "New first\nNew second"
    assert all(dialog.block_list.item(i).checkState() == Qt.CheckState.Checked for i in range(2))
    assert [block["text"] for block in editor.blocks] == ["New first", "New second"]


def test_translations_refresh_without_overwriting_original_or_copy_scope(editor):
    editor.full_text_button.click()
    dialog = editor.full_text_dialog
    dialog.block_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    original = dialog.source_text.toPlainText()
    start_translation(editor)
    finish_translation(editor)
    assert dialog.source_text.toPlainText() == original == "Second"
    assert dialog.translated_text.toPlainText() == "第二"
    copied = []
    editor.text_copied.connect(copied.append)
    dialog.tabs.setCurrentIndex(1)
    dialog.select_all_button.click()
    assert selected_text(dialog.translated_text) == "第二"
    dialog.copy_button.click()
    assert copied == ["第二"]
    assert all(block["apply"] for block in editor.blocks)


def test_invalid_result_cannot_copy_and_editor_close_closes_full_text(qt_app, editor):
    editor.full_text_button.click()
    dialog = editor.full_text_dialog
    copied = []
    editor.text_copied.connect(copied.append)
    editor.doc.add([{"kind": "cover", "rect": (30, 20, 30, 20), "color": "#000000"}])
    assert not editor.full_text_button.isEnabled()
    assert not dialog.source_text.toPlainText() and not dialog.translated_text.toPlainText()
    dialog.copy_all()
    assert not dialog.copy_button.isEnabled() and not copied
    editor.cancel()
    assert editor.full_text_dialog is None
    qt_app.processEvents()


def test_full_text_close_reopens_cleanly_and_empty_translation_never_copies(editor):
    editor.full_text_button.click()
    dialog = editor.full_text_dialog
    dialog.tabs.setCurrentIndex(1)
    copied = []
    editor.text_copied.connect(copied.append)
    dialog.copy_all()
    assert not dialog.copy_button.isEnabled() and not copied
    dialog.close()
    assert editor.full_text_dialog is None
    editor.full_text_button.click()
    assert editor.full_text_dialog is not dialog
    assert selected_text(editor.full_text_dialog.source_text) == "First\nSecond"
