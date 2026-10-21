"""合成滚动图与注入采集后端验证；不读取真实桌面，不发送实际滚轮。"""
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMainWindow

from long_capture import LongScreenshotController
from long_stitch import VerticalStitcher
from local_ocr import recognize_image
from storage import image_fingerprint
from test_local_ocr import wait_for
from screenshot import ScreenshotController, SelectionOverlay


def source_image(width=300, height=1800):
    random = np.random.default_rng(814)
    array = random.integers(0, 255, (height, width, 3), dtype=np.uint8)
    return QImage(array.data, width, height, width*3, QImage.Format.Format_RGB888).copy()


def test_stitch_variable_scroll_is_pixel_exact_and_undo(qt_app):
    original = source_image()
    stitcher = VerticalStitcher(original.copy(0, 0, 300, 400))
    for top in (60, 180, 290, 450, 680, 820):
        result = stitcher.add(original.copy(0, top, 300, 400))
        assert result.status == "append" and result.height == top+400
    assert image_fingerprint(stitcher.render()) == image_fingerprint(original.copy(0, 0, 300, 1220))
    assert stitcher.undo() and stitcher.height == 1080
    assert image_fingerprint(stitcher.render()) == image_fingerprint(original.copy(0, 0, 300, 1080))
    assert stitcher.add(original.copy(0, 820, 300, 400)).status == "append"


def test_high_dpi_frames_render_every_physical_pixel(qt_app):
    original = source_image()
    original.setDevicePixelRatio(1.5)
    stitcher = VerticalStitcher(original.copy(0, 0, 300, 400))
    assert stitcher.add(original.copy(0, 150, 300, 400)).status == "append"
    result = stitcher.render()
    expected = original.copy(0, 0, 300, 550)
    assert image_fingerprint(result) == image_fingerprint(expected)
    assert result.devicePixelRatio() == 1


def test_duplicate_reverse_no_overlap_and_changed_size_keep_result(qt_app):
    original = source_image()
    stitcher = VerticalStitcher(original.copy(0, 200, 300, 400))
    before = image_fingerprint(stitcher.render())
    assert stitcher.add(original.copy(0, 200, 300, 400)).status == "duplicate"
    assert stitcher.add(original.copy(0, 100, 300, 400)).status == "unmatched"
    assert stitcher.add(original.copy(0, 1200, 300, 400)).status == "unmatched"
    assert stitcher.add(original.copy(0, 200, 280, 400)).status == "size_changed"
    assert image_fingerprint(stitcher.render()) == before


def test_repeating_texture_is_ambiguous(qt_app):
    small = source_image(300, 60)
    repeated = QImage(300, 1200, QImage.Format.Format_RGB32)
    painter = QPainter(repeated)
    for y in range(0, 1200, 60):
        painter.drawImage(0, y, small)
    painter.end()
    stitcher = VerticalStitcher(repeated.copy(0, 0, 300, 420))
    assert stitcher.add(repeated.copy(0, 20, 300, 420)).status == "ambiguous"
    assert len(stitcher.parts) == 1


def test_dynamic_small_cursor_change_tolerated_and_limits_never_truncate(qt_app):
    original = source_image()
    stitcher = VerticalStitcher(original.copy(0, 0, 300, 400), max_height=520)
    frame = original.copy(0, 100, 300, 400)
    frame.setPixelColor(25, 25, QColor("red"))
    assert stitcher.add(frame).status == "append"
    assert stitcher.add(original.copy(0, 200, 300, 400)).status == "limit"
    assert stitcher.height == 500
    with pytest.raises(ValueError):
        VerticalStitcher(original.copy(0, 0, 300, 400), max_pixels=1000)


class FakeSource:
    def __init__(self):
        self.original = source_image()
        self.top = self.scrolls = 0

    def capture(self):
        return self.original.copy(0, self.top, 300, 400)

    def scroll(self):
        self.scrolls += 1
        self.top += 60


def controller(qt_app):
    window = QMainWindow()
    window.show_history = window.show
    outputs = []
    source = FakeSource()
    current = LongScreenshotController(window, outputs.append, source_factory=lambda *args: source)
    current.begin((QRect(10, 10, 300, 400), (10, 10, 300, 400), 1, QRect(0, 0, 1000, 700)))
    wait_for(lambda: current.stitcher is not None)
    return current, source, outputs, window


def test_default_manual_capture_undo_edit_then_explicit_commit(qt_app):
    current, source, outputs, window = controller(qt_app)
    try:
        assert current.bar.mode.currentData() == "manual"
        current.timer.stop()
        source.top = 100
        current.capture_frame(current.serial)
        # 工作线程先追加像素，Qt随后才接收完成信号并启用撤回按钮。
        # 应等到用户实际可操作的状态，不能只看共享长图高度。
        wait_for(lambda: current.stitcher.height == 500 and not current.busy)
        current.timer.stop()
        assert not source.scrolls and not outputs
        current.undo()
        wait_for(lambda: not current.busy)
        assert current.stitcher.height == 400 and current.paused
        current.finish()
        wait_for(lambda: current.editor is not None)
        assert not outputs
        assert current.editor.canvas.minimumHeight() >= 400
        current.editor.commit()
        assert len(outputs) == 1 and outputs[0].height() == 400 and not current.active
    finally:
        current.stop()
        window.close()


def test_auto_requires_explicit_choice_and_resume(qt_app):
    current, source, outputs, window = controller(qt_app)
    try:
        current.timer.stop()
        current.bar.mode.setCurrentIndex(1)
        assert current.paused and not source.scrolls
        current.toggle_pause()
        wait_for(lambda: source.scrolls > 0 and current.stitcher.height > 400)
        assert not outputs
    finally:
        current.cancel()
        window.close()


def test_cancel_discards_and_stale_capture_callback_cannot_reopen(qt_app):
    current, source, outputs, window = controller(qt_app)
    token = current.serial
    current.cancel()
    current.capture_frame(token)
    current.initialize(token, (QRect(), (), 1, QRect()))
    QTest.qWait(200)
    assert not current.active and not outputs and current.editor is None
    window.close()


def test_unmatched_manual_scroll_waits_for_overlap_without_adding(qt_app):
    current, source, outputs, window = controller(qt_app)
    try:
        current.timer.stop()
        source.top = 1000
        current.capture_frame(current.serial)
        wait_for(lambda: current.recovering)
        assert current.stitcher.height == 400
        assert "重叠不足" in current.bar.note.text() and not source.scrolls
    finally:
        current.cancel()
        window.close()


def test_long_ocr_owned_overlap_coordinates_and_order(qt_app):
    image = QImage(400, 3500, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    calls = []
    line_positions = [100, 1170, 1230, 1700, 2290, 3200]
    def engine(array):
        top = len(calls)*1120
        calls.append(array.shape)
        boxes, texts = [], []
        for y in line_positions:
            if top <= y and y+35 <= top+array.shape[0]:
                boxes.append([[20, y-top], [200, y-top], [200, y-top+35], [20, y-top+35]])
                texts.append(f"line {y}")
        return SimpleNamespace(boxes=np.array(boxes), txts=texts, scores=[.99]*len(texts))
    data = recognize_image(engine, image, Event())
    assert data["tiled"] and len(calls) == 3
    assert [block["text"] for block in data["blocks"]] == [f"line {y}" for y in line_positions]
    assert [round(block["bbox"][1]*3500) for block in data["blocks"]] == line_positions
    assert [block["id"] for block in data["blocks"]] == list(map(str, range(1, 7)))


def test_old_shortcuts_preserved_when_new_default_is_already_taken(qt_app):
    from shortcut_settings import DEFAULT_SHORTCUTS, validate_shortcuts
    previous = {name: value for name, value in DEFAULT_SHORTCUTS.items() if name != "long_screenshot"}
    previous["screenshot"] = "Ctrl+Alt+L"
    loaded = validate_shortcuts(previous)
    assert loaded["screenshot"] == "Ctrl+Alt+L"
    assert loaded["long_screenshot"] == "Ctrl+Alt+Shift+L"
    from PySide6.QtGui import QKeySequence
    assert all(QKeySequence(loaded[name]) == QKeySequence(value) for name, value in previous.items())


def screenshot_to_long(qt_app, *, ratio=1):
    window = QMainWindow()
    window.show_history = window.show
    outputs, source = [], FakeSource()
    ordinary = ScreenshotController(window, outputs.append)
    snapshot = QImage(600, 700, QImage.Format.Format_RGB32)
    snapshot.fill(QColor("white"))
    painter = QPainter(snapshot)
    painter.drawImage(40, 60, source.capture())
    painter.end()
    snapshot.setDevicePixelRatio(ratio)
    ordinary.screen = object()  # 必须使用原屏幕，不根据当前鼠标重新选择。
    ordinary.overlay = SelectionOverlay(QPixmap.fromImage(snapshot),
                                         QRect(-400, -100, round(600/ratio), round(700/ratio)))
    ordinary.active = True
    ordinary.overlay.doc.crop = (40, 60, 300, 400)
    ordinary.overlay.doc.add([{"kind": "cover", "rect": (65, 90, 60, 25), "color": "#000000"}])
    ordinary.overlay.show_tools()
    ordinary.overlay.long_capture_requested.connect(ordinary.start_long_capture)
    seen = []
    current = LongScreenshotController(window, outputs.append,
        source_factory=lambda *args: (seen.append(args), source)[1])
    ordinary.on_long_capture = current.start_from_screenshot
    return current, ordinary, source, outputs, window, seen


@pytest.mark.parametrize("ratio", [1, 1.25, 1.5, 2])
def test_screenshot_toolbar_reuses_region_original_pixels_and_editable_layers(qt_app, ratio):
    current, ordinary, source, outputs, window, seen = screenshot_to_long(qt_app, ratio=ratio)
    original = ordinary.overlay
    before = image_fingerprint(original.doc.render())
    try:
        button = original.long_capture_button
        assert button.isEnabled() and not button.isHidden()
        assert button.text() == "长截图" and not button.icon().isNull()
        assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        button.click()
        wait_for(lambda: current.stitcher is not None and not current.busy)
        current.timer.stop()
        assert seen[0][0] is ordinary.screen
        logical, pixels, captured_ratio, geometry = seen[0][1:]
        assert pixels == (40, 60, 300, 400) and captured_ratio == ratio
        assert geometry == QRect(-400, -100, round(600/ratio), round(700/ratio))
        assert current.selector.overlay is None and not current.selector.active
        assert not original.isVisible() and not original.finished
        assert not outputs and not source.scrolls
        source.top = 100
        current.capture_frame(current.serial)
        wait_for(lambda: current.stitcher.height == 500)
        current.timer.stop()
        current.finish()
        wait_for(lambda: current.editor is not None)
        editor = current.editor
        assert not ordinary.active and ordinary.overlay is None
        assert not outputs and editor.doc.objects[0]["rect"] == (25, 30, 60, 25)
        assert image_fingerprint(editor.doc.render().copy(0, 0, 300, 400)) == before
        assert image_fingerprint(editor.doc.base) == image_fingerprint(source.original.copy(0, 0, 300, 500))
        assert editor.long_capture_button.text() == "继续长截图"
        editor.commit()
        assert len(outputs) == 1 and outputs[0].height() == 500
    finally:
        current.stop()
        ordinary.stop()
        window.close()


def test_cancel_first_conversion_returns_exact_original_editor(qt_app):
    current, ordinary, source, outputs, window, seen = screenshot_to_long(qt_app)
    editor = ordinary.overlay
    before = editor.doc.state()
    try:
        editor.long_capture_button.click()
        wait_for(lambda: current.stitcher is not None and not current.busy)
        current.timer.stop()
        current.cancel()
        assert ordinary.active and ordinary.overlay is editor
        assert editor.isVisible() and not editor.finished
        assert editor.doc.state() == before and not outputs
        editor.long_capture_button.click()
        wait_for(lambda: current.stitcher is not None and not current.busy)
        assert current.selector.overlay is None and len(seen) == 2
    finally:
        current.stop()
        ordinary.stop()
        window.close()


def test_continue_long_capture_appends_without_reselection_and_cancel_restores(qt_app):
    current, source, outputs, window = controller(qt_app)
    try:
        current.timer.stop()
        source.top = 100
        current.capture_frame(current.serial)
        wait_for(lambda: current.stitcher.height == 500)
        current.timer.stop()
        current.finish()
        wait_for(lambda: current.editor is not None)
        original = current.editor
        original.doc.add([{"kind": "cover", "rect": (25, 30, 60, 25), "color": "#000000"}])
        before = image_fingerprint(original.doc.render())
        original.long_capture_button.click()
        wait_for(lambda: current.stitcher is not None and not current.busy)
        current.timer.stop()
        assert current.stitcher.height == 500 and current.selector.overlay is None
        source.top = 200
        current.capture_frame(current.serial)
        wait_for(lambda: current.stitcher.height == 600)
        current.timer.stop()
        current.cancel()
        assert current.editor is original and current.active and original.isVisible()
        assert image_fingerprint(original.doc.render()) == before and not outputs
        # 取消追加不消耗旧基准；恢复页面至原重叠后仍可接续同一长图。
        source.top = 100
        original.long_capture_button.click()
        wait_for(lambda: current.stitcher is not None and not current.busy)
        current.timer.stop()
        source.top = 200
        current.capture_frame(current.serial)
        wait_for(lambda: current.stitcher.height == 600)
        current.timer.stop()
        current.finish()
        wait_for(lambda: current.editor is not None)
        assert current.editor.doc.objects and current.editor.doc.base.height() == 600
        assert image_fingerprint(current.editor.doc.render().copy(0, 0, 300, 500)) == before
        current.editor.commit()
        assert len(outputs) == 1 and outputs[0].height() == 600
    finally:
        current.stop()
        window.close()


def test_saved_image_has_no_screen_region_and_cannot_discard_it_for_reselection(qt_app):
    from image_editor import ImageEditor
    editor = ImageEditor(source_image())
    requests = []
    editor.long_capture_requested.connect(lambda: requests.append(True))
    try:
        assert not editor.long_capture_button.isEnabled()
        editor.request_long_capture()
        assert not requests and not editor.finished
    finally:
        editor.cancel()


def test_failed_target_initialization_keeps_current_screenshot_and_unsaved_layers(qt_app):
    current, ordinary, source, outputs, window, seen = screenshot_to_long(qt_app)
    editor = ordinary.overlay
    errors = []
    current.failed.connect(errors.append)
    def rejected(*args):
        raise ValueError("合成目标暂不可用")
    current.source_factory = rejected
    before = editor.doc.state()
    try:
        editor.long_capture_button.click()
        wait_for(lambda: bool(errors))
        assert ordinary.active and ordinary.overlay is editor and editor.isVisible()
        assert not editor.finished and editor.doc.state() == before and not outputs
        assert not current.active and not current.selector.active
    finally:
        current.stop()
        ordinary.stop()
        window.close()


def test_narrow_editor_toolbar_keeps_commit_and_long_button_visible(qt_app):
    from image_editor import ImageEditor
    from PySide6.QtCore import QPoint
    editor = ImageEditor(source_image(), long_capture_enabled=True)
    try:
        editor.resize(editor.minimumWidth(), editor.minimumHeight())
        editor.show()
        QTest.qWait(20)
        for button in (editor.long_capture_button, editor.commit_button):
            assert button.mapTo(editor.tools, QPoint()).x()+button.width() <= editor.tools.width()
    finally:
        editor.cancel()


def test_preview_updates_accepted_segments_and_follows_without_full_image_render(qt_app, monkeypatch):
    current, source, outputs, window = controller(qt_app)
    try:
        current.timer.stop()
        preview = current.preview
        assert preview.isVisible() and current.bar.preview_button.isChecked()
        assert preview.canvas.image_height == 400 and preview.follow.isChecked()
        initial_part = preview.canvas.parts[0]
        def unexpected_render(*args):
            raise AssertionError("Capture preview must not render the entire full-size long image")
        with monkeypatch.context() as patch:
            patch.setattr(current.stitcher, "render", unexpected_render)
            for top in (100, 220, 360):
                source.top = top
                current.capture_frame(current.serial)
                wait_for(lambda: not current.busy and preview.canvas.image_height == 400+top)
                current.timer.stop()
            wait_for(lambda: preview.scroll.verticalScrollBar().value() ==
                     preview.scroll.verticalScrollBar().maximum())
        assert preview.canvas.parts[0] is initial_part
        assert preview.canvas.latest_start == 620
        assert "760" in preview.dimensions.text() and "+140" in preview.dimensions.text()
        assert not outputs and image_fingerprint(current.stitcher.render()) == \
            image_fingerprint(source.original.copy(0, 0, 300, 760))
        current.undo()
        wait_for(lambda: not current.busy)
        assert preview.canvas.image_height == 620 and preview.canvas.latest_start is None
        assert len(preview.canvas.parts) == len(current.stitcher.parts)
    finally:
        current.stop()
        window.close()


def test_preview_manual_browse_stops_follow_and_button_resumes(qt_app):
    from PySide6.QtWidgets import QAbstractSlider
    current, source, outputs, window = controller(qt_app)
    try:
        current.timer.stop()
        source.top = 260
        current.capture_frame(current.serial)
        wait_for(lambda: current.preview.canvas.image_height == 660)
        current.timer.stop()
        preview = current.preview
        wait_for(lambda: preview.scroll.verticalScrollBar().maximum() > 0)
        bar = preview.scroll.verticalScrollBar()
        bar.triggerAction(QAbstractSlider.SliderAction.SliderToMinimum)
        assert not preview.follow.isChecked() and bar.value() == 0
        source.top = 400
        current.capture_frame(current.serial)
        wait_for(lambda: preview.canvas.image_height == 800)
        current.timer.stop()
        QTest.qWait(40)
        assert bar.value() == 0 and not preview.follow.isChecked()
        preview.follow.click()
        wait_for(lambda: bar.value() == bar.maximum())
        preview.resize(420, 300)
        wait_for(lambda: bar.value() == bar.maximum())
        assert preview.canvas.image_height == 800 and not outputs
    finally:
        current.stop()
        window.close()


def test_preview_close_only_hides_and_bar_reopens_it(qt_app):
    current, source, outputs, window = controller(qt_app)
    try:
        current.timer.stop()
        preview = current.preview
        preview.close()
        assert current.active and not preview.isVisible() and not current.bar.preview_button.isChecked()
        current.bar.preview_button.click()
        assert preview.isVisible() and not outputs
        token = current.serial
        current.cancel()
        current.capture_frame(token)
        assert current.preview is None and not current.active
    finally:
        current.stop()
        window.close()


def test_preview_duplicate_unmatched_and_pause_keep_only_accepted_content(qt_app):
    current, source, outputs, window = controller(qt_app)
    try:
        current.timer.stop()
        preview = current.preview
        initial = preview.canvas.parts[0]
        current.capture_frame(current.serial)
        QTest.qWait(280)
        current.timer.stop()
        assert preview.canvas.image_height == 400 and preview.canvas.parts[0] is initial
        source.top = 1000
        current.capture_frame(current.serial)
        wait_for(lambda: current.recovering)
        assert preview.isVisible() and preview.canvas.image_height == 400 and not outputs
    finally:
        current.stop()
        window.close()


def test_preview_moved_into_capture_region_is_hidden_for_every_frame(qt_app):
    current, source, outputs, window = controller(qt_app)
    checked = []
    original_capture = source.capture
    def guarded():
        selection = current.payload[0]
        for widget in (current.bar, current.preview):
            if widget is not None and widget.frameGeometry().adjusted(-2, -2, 2, 2).intersects(selection):
                assert not widget.isVisible()
        checked.append(True)
        return original_capture()
    source.capture = guarded
    try:
        current.timer.stop()
        current.preview.move(12, 12)
        current.bar.move(10, 10)
        source.top = 100
        current.capture_frame(current.serial)
        wait_for(lambda: current.preview.canvas.image_height == 500)
        current.timer.stop()
        assert len(checked) == 2 and current.preview.isVisible()
        assert current.bar.isVisible() and not outputs
        # 预览恢复不触碰拼接基准或给成品添加蓝线。
        assert image_fingerprint(current.stitcher.render()) == \
            image_fingerprint(source.original.copy(0, 0, 300, 500))
    finally:
        current.stop()
        window.close()


def test_preview_shows_existing_redaction_without_modifying_raw_stitch_pixels(qt_app):
    current, ordinary, source, outputs, window, seen = screenshot_to_long(qt_app)
    editor = ordinary.overlay
    expected = image_fingerprint(editor.doc.render())
    try:
        editor.long_capture_button.click()
        wait_for(lambda: current.stitcher is not None and not current.busy)
        current.timer.stop()
        preview = current.preview
        assert image_fingerprint(preview.canvas.parts[0]) == expected
        assert image_fingerprint(current.stitcher.parts[0]) == image_fingerprint(source.capture())
        source.top = 100
        current.capture_frame(current.serial)
        wait_for(lambda: preview.canvas.image_height == 500)
        current.timer.stop()
        assert preview.canvas.parts[-1].height() == 100 and not outputs
        current.finish()
        wait_for(lambda: current.editor is not None)
        assert current.preview is None and current.editor.doc.objects
    finally:
        current.stop()
        ordinary.stop()
        window.close()
