"""仅使用合成文字、本机模拟接口与隔离编辑器验证有限补译。"""

import json
from copy import deepcopy

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from api_client import parse_translation_batch
from storage import image_fingerprint
from translation_session import TranslationSession, text_batches
from test_api import server, wait_for
from test_delete_and_move import canvas_point, editor_with_blocks, start_translation


def returned(editor, translations):
    editor.client.reply = None
    editor.client.busy_changed.emit(False)
    editor.api_result("translate", {"translations": translations})


def sent_blocks(request):
    return json.loads(request[1].split("Input blocks:\n")[1])


@pytest.mark.parametrize("value", ["", " ", None])
def test_blank_result_retains_other_text_and_retries_only_failed(qt_app, tmp_path, value):
    editor = editor_with_blocks(qt_app, tmp_path)
    _, requests = start_translation(editor)
    original = image_fingerprint(editor.doc.render())
    returned(editor, [{"id": "1", "text": "第一"}, {"id": "2", "text": value}])
    assert len(requests) == 2 and sent_blocks(requests[1]) == [{"id": "2", "text": "Second"}]
    assert "previous response" in requests[1][1]
    assert editor.request_active and editor.source_text.isReadOnly()
    assert image_fingerprint(editor.doc.render()) == original
    returned(editor, [{"id": 2, "text": "第二"}])
    assert not editor.request_active and not editor.source_text.isReadOnly()
    assert {o["block_id"]: o["text"] for o in editor.doc.objects} == {"a": "第一", "b": "第二"}
    assert len(editor.doc.undo_states) == 1
    editor.undo()
    assert image_fingerprint(editor.doc.render()) == original
    editor.cancel()


def test_omitted_item_retries_and_can_arrive_out_of_order():
    session = TranslationSession([{"id": "3", "text": "One"}, {"id": "7", "text": "Two"}], "中文")
    session.next_prompt()
    session.accept({"translations": [{"id": "7", "text": "二"}]})
    assert '"id": "3"' in session.next_prompt()
    session.accept({"translations": [{"id": " 3 ", "text": "一"}]})
    assert session.next_prompt() is None and session.results == {"7": "二", "3": "一"}


def test_second_empty_preserves_pixels_marks_failure_and_manual_retry_is_only_pending(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    before = editor.doc.render()
    _, requests = start_translation(editor)
    returned(editor, [{"id": "1", "text": "第一"}, {"id": "2", "text": ""}])
    returned(editor, [{"id": "2", "text": ""}])
    assert len(requests) == 2 and not editor.request_active
    assert [o["block_id"] for o in editor.doc.objects] == ["a"]
    rect = editor.blocks[1]["rect"]
    after = editor.doc.render()
    assert image_fingerprint(before.copy(*map(int, rect))) == image_fingerprint(after.copy(*map(int, rect)))
    assert "未完成" in editor.block_list.item(1).text()
    assert editor.translate_button.text() == "重试未完成"
    assert "1 块未完成" in editor.status.text()
    editor.block_list.setCurrentRow(1)
    assert "未完成" in editor.block_list.item(1).text()
    editor.translate()
    assert sent_blocks(requests[-1]) == [{"id": "1", "text": "Second"}]
    returned(editor, [{"id": "1", "text": "第二"}])
    assert {o["block_id"]: o["text"] for o in editor.doc.objects} == {"a": "第一", "b": "第二"}
    assert not editor.translation_retry_ids and "未完成" not in editor.block_list.item(1).text()
    editor.cancel()


def test_all_empty_stops_after_one_retry_and_never_erases_image(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    before = image_fingerprint(editor.doc.render())
    _, requests = start_translation(editor)
    returned(editor, [])
    returned(editor, [])
    assert len(requests) == 2 and not editor.request_active
    assert not editor.doc.objects and image_fingerprint(editor.doc.render()) == before
    assert editor.translation_retry_ids == {"a", "b"}
    assert all("未完成" in editor.block_list.item(i).text() for i in range(2))
    editor.cancel()


def test_cancel_retry_ignores_late_response(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    before = image_fingerprint(editor.doc.render())
    reply, requests = start_translation(editor)
    returned(editor, [{"id": "1", "text": "第一"}])
    editor.cancel_api()
    assert reply.aborted and len(requests) == 2 and not editor.request_active
    editor.api_result("translate", {"translations": [{"id": "2", "text": "迟到"}]})
    assert image_fingerprint(editor.doc.render()) == before
    assert not editor.doc.objects and not editor.source_text.isReadOnly()
    editor.cancel()


def test_failed_retranslation_retains_existing_layer_and_manual_edit_removes_marker(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    _, requests = start_translation(editor)
    returned(editor, [{"id": "1", "text": "第一"}, {"id": "2", "text": "原有译文"}])
    existing = deepcopy(editor.doc.objects[1])
    editor.translate()
    returned(editor, [{"id": "1", "text": "新版第一"}, {"id": "2", "text": ""}])
    returned(editor, [{"id": "2", "text": ""}])
    assert next(o for o in editor.doc.objects if o["block_id"] == "b") == existing
    editor.block_list.setCurrentRow(1)
    editor.translated_text.setPlainText("手动译文")
    assert not editor.translation_retry_ids and "未完成" not in editor.block_list.item(1).text()
    editor.apply_translations()
    assert next(o for o in editor.doc.objects if o["block_id"] == "b")["text"] == "手动译文"
    editor.cancel()


def test_unknown_or_duplicate_ids_are_still_rejected_without_retry():
    for response in ([{"id": "99", "text": "未知"}],
                     [{"id": "1", "text": ""}, {"id": 1, "text": "重复"}]):
        session = TranslationSession([{"id": "1", "text": "Hello"}], "中文")
        session.next_prompt()
        with pytest.raises(ValueError):
            session.accept({"translations": response})
        assert not session.results and not session.queue


def test_batch_limits_and_retry_budget_for_many_items():
    blocks = [{"id": str(i), "text": "Synthetic text " * 20} for i in range(100)]
    assert all(len(batch) <= 32 and sum(len(b["text"]) for b in batch) <= 6000
               for batch in text_batches(blocks))
    session = TranslationSession(blocks, "中文")
    attempts = {}
    while session.next_prompt() is not None:
        for block in session.current:
            attempts[block["id"]] = attempts.get(block["id"], 0) + 1
        session.accept({"translations": []})
    assert set(attempts.values()) == {2} and session.failed == set(session.blocks)


def test_missing_numbers_and_punctuation_are_preserved_without_retry():
    valid, pending, _ = parse_translation_batch(
        {"translations": [{"id": "1", "text": "你好"}]}, ["1", "2", "3"],
        {"1": "Hello", "2": "123.", "3": "•"})
    assert valid == {"1": "你好", "2": "123.", "3": "•"} and not pending


def test_retry_completion_during_drag_uses_latest_position(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    start_translation(editor)
    returned(editor, [{"id": "1", "text": "第一"}])
    rect = editor.blocks[1]["rect"]
    start = canvas_point(editor, (rect[0] + rect[2] / 2, rect[1] + rect[3] / 2))
    end = canvas_point(editor, (rect[0] + rect[2] / 2 + 20, rect[1] + rect[3] / 2 + 10))
    QTest.mousePress(editor.canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(editor.canvas, end, 10)
    returned(editor, [{"id": "2", "text": "第二"}])
    assert editor.pending_translation_preview and not editor.doc.objects
    QTest.mouseRelease(editor.canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    assert not editor.pending_translation_preview
    assert next(o for o in editor.doc.objects if o["block_id"] == "b")["rect"] == editor.blocks[1]["rect"]
    editor.cancel()


def test_network_failure_during_retry_keeps_completed_translation(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    start_translation(editor)
    returned(editor, [{"id": "1", "text": "第一"}])
    editor.client.reply = None
    editor.api_error("模拟接口 HTTP 429")
    assert not editor.request_active and not editor.source_text.isReadOnly()
    assert [o["block_id"] for o in editor.doc.objects] == ["a"]
    assert editor.translation_retry_ids == {"b"} and "429" in editor.panel_note.text()
    editor.cancel()


def test_edit_successful_translation_while_another_block_is_pending(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    start_translation(editor)
    returned(editor, [{"id": "1", "text": "第一"}])
    returned(editor, [])
    editor.block_list.setCurrentRow(0)
    editor.translated_text.setPlainText("校对后的第一")
    editor.apply_button.click()
    assert [o["text"] for o in editor.doc.objects] == ["校对后的第一"]
    assert editor.translation_retry_ids == {"b"} and "未完成" in editor.panel_note.text()
    editor.cancel()


def test_changed_target_language_translates_all_selected_again(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    _, requests = start_translation(editor)
    returned(editor, [{"id": "1", "text": "第一"}])
    returned(editor, [])
    editor.language.setCurrentText("English")
    assert editor.translate_button.text() == "翻译所选"
    editor.translate()
    assert len(sent_blocks(requests[-1])) == 2 and "English" in requests[-1][1]
    editor.cancel()


def test_real_local_http_retry_preserves_nonconsecutive_mapping(qt_app, tmp_path, server):
    state, url = server
    editor = editor_with_blocks(qt_app, tmp_path)
    editor.store.settings.base_url, editor.store.settings.key = url, "local-dummy"
    editor.blocks[0]["id"], editor.blocks[1]["id"] = "3", "7"
    state["bodies"] = [{"translations": [{"id": "1", "text": "第一"}]},
                       {"translations": [{"id": "2", "text": "第二"}]}]
    editor.translate()
    wait_for(lambda: not editor.request_active)
    assert len(state["requests"]) == 2
    assert {o["block_id"]: o["text"] for o in editor.doc.objects} == {"3": "第一", "7": "第二"}
    assert all(len(r["messages"][0]["content"]) == 1 for r in state["requests"])
    editor.cancel()
