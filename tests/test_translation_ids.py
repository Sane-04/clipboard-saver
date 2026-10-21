"""翻译请求编号与本地块精确映射；响应来自隔离模拟，不上传用户正文。"""
import json
from copy import deepcopy

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

from api_client import parse_translations, translation_prompt
from image_document import estimate_foreground
from storage import image_fingerprint
from test_delete_and_move import editor_with_blocks, finish_translation, start_translation


def test_prompt_template_uses_actual_ids_and_preserves_nonlinguistic_text():
    blocks = [{"id": "3", "text": "Hello"}, {"id": "7", "text": "123."}]
    prompt = translation_prompt(blocks, "简体中文")
    assert '"id": "1"' not in prompt
    assert '"id": "3"' in prompt and '"id": "7"' in prompt
    assert "never renumber" in prompt and "nonempty" in prompt


@pytest.mark.parametrize("ids", [(" 1 ", 2), (1.0, "2"), (1, 2.0)])
def test_safe_id_representation_and_out_of_order_return(ids):
    response = {"translations": [{"id": ids[1], "text": "第二"}, {"id": ids[0], "text": "第一"}]}
    assert parse_translations(response, ["1", "2"]) == {"2": "第二", "1": "第一"}


@pytest.mark.parametrize("invalid", [True, None, [], {}, 1.5, "other", "0", "3"])
def test_unknown_ids_are_never_guessed_by_position(invalid):
    with pytest.raises(ValueError, match="不属于"):
        parse_translations({"translations": [{"id": invalid, "text": "不能错配"}]}, ["1"])


def test_duplicate_missing_and_empty_have_specific_errors():
    with pytest.raises(ValueError, match="重复"):
        parse_translations({"translations": [{"id": "1", "text": "一"}, {"id": 1, "text": "二"}]}, ["1"])
    with pytest.raises(ValueError, match="所有"):
        parse_translations({"translations": [{"id": "1", "text": "一"}]}, ["1", "2"])
    with pytest.raises(ValueError, match="空译文"):
        parse_translations({"translations": [{"id": "1", "text": " "}]}, ["1"], {"1": "Hello"})
    with pytest.raises(ValueError, match="空译文"):
        parse_translations({"translations": [{"id": "1", "text": ""}]}, ["1"], {"1": "你好"})


@pytest.mark.parametrize("original", ["1.", "12345", "—", "•", "2026/10/07", "￥99.00"])
def test_empty_translation_of_numbers_or_punctuation_preserves_original(original):
    assert parse_translations({"translations": [{"id": "1", "text": ""}]},
                              ["1"], {"1": original}) == {"1": original}


def test_selected_nonconsecutive_ids_use_wire_snapshot_and_map_back(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    editor.blocks[0]["id"], editor.blocks[1]["id"] = "3", "7"
    editor.blocks[0]["apply"] = False
    editor.refresh_blocks()
    reply, requests = start_translation(editor)
    prompt = requests[0][1]
    sent = json.loads(prompt.split("Input blocks:\n")[1])
    assert sent == [{"id": "1", "text": "Second"}]
    assert editor.translation_ids == ["7"] and editor.translation_id_map == {"1": "7"}
    finish_translation(editor)
    layer = editor.doc.objects[0]
    assert layer["block_id"] == "7" and layer["text"] == "第二"
    assert editor.blocks[0]["translated"] == "" and not reply.aborted
    editor.cancel()


def test_out_of_order_wire_ids_map_to_right_layers_and_keep_manual_positions(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    editor.blocks[0]["id"], editor.blocks[1]["id"] = "3", "7"
    editor.refresh_blocks()
    start_translation(editor)
    editor.client.reply = None
    editor.client.busy_changed.emit(False)
    editor.api_result("translate", {"translations": [{"id": " 2 ", "text": "第二"},
                                                    {"id": 1.0, "text": "第一"}]})
    layers = {o["block_id"]: o for o in editor.doc.objects}
    assert layers["3"]["text"] == "第一" and layers["7"]["text"] == "第二"
    assert layers["3"]["rect"] == pytest.approx(editor.blocks[0]["rect"])
    assert layers["7"]["rect"] == pytest.approx(editor.blocks[1]["rect"])
    editor.cancel()


def test_invalid_return_keeps_existing_preview_unchanged(qt_app, tmp_path):
    editor = editor_with_blocks(qt_app, tmp_path)
    start_translation(editor)
    finish_translation(editor)
    before = deepcopy(editor.doc.objects)
    pixels = image_fingerprint(editor.doc.render())
    start_translation(editor)
    editor.client.reply = None
    editor.client.busy_changed.emit(False)
    editor.api_result("translate", {"translations": [{"id": "1", "text": "有效"},
                                                    {"id": "999", "text": "无效"}]})
    assert editor.doc.objects == before and image_fingerprint(editor.doc.render()) == pixels
    assert "不属于" in editor.status.text()
    editor.cancel()


@pytest.mark.parametrize("background, ink", [("#ffffff", "#183e32"), ("#202020", "#eeeeee")])
def test_sparse_glyph_is_not_mistaken_for_background(background, ink):
    image = QImage(80, 60, QImage.Format.Format_RGBA8888)
    image.fill(QColor(background))
    for x in range(12, 15):
        for y in range(12, 42):
            image.setPixelColor(x, y, QColor(ink))
    assert estimate_foreground(image, QRectF(0, 0, 80, 60), background) == ink


@pytest.mark.parametrize("background, expected", [("#ffffff", "#202020"), ("#202020", "#ffffff")])
def test_empty_source_box_uses_visible_contrast(background, expected):
    image = QImage(40, 30, QImage.Format.Format_RGBA8888)
    image.fill(QColor(background))
    assert estimate_foreground(image, QRectF(0, 0, 40, 30), background) == expected
