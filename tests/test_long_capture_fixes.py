"""短视口、恢复接续、实时画面与自身窗口排除的行为验证。"""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QMainWindow
from long_capture import LongScreenshotController
from long_stitch import VerticalStitcher
from storage import image_fingerprint
from test_long_capture import controller, source_image
from test_local_ocr import wait_for


@pytest.mark.parametrize("height,step", [(40, 25), (60, 42), (80, 60), (120, 94), (160, 124), (200, 154)])
def test_short_viewports_preserve_pixels_with_small_overlap(qt_app, height, step):
    image = source_image(220, 1400)
    stitcher = VerticalStitcher(image.copy(0, 0, 220, height))
    for top in range(step, step*5, step):
        assert stitcher.add(image.copy(0, top, 220, height)).status == "append"
    assert image_fingerprint(stitcher.render()) == image_fingerprint(
        image.copy(0, 0, 220, step*4+height))
    before = image_fingerprint(stitcher.render())
    assert stitcher.add(image.copy(0, 1300, 220, height)).status in ("unmatched", "size_changed")
    assert image_fingerprint(stitcher.render()) == before


def test_recovery_live_frame_updates_while_accepted_image_is_preserved(qt_app):
    current, source, outputs, window = controller(qt_app)
    try:
        current.timer.stop()
        before = image_fingerprint(current.stitcher.render())
        source.top = 900
        current.capture_frame(current.serial)
        wait_for(lambda: current.recovering)
        current.timer.stop()
        assert image_fingerprint(current.preview.live_image) == image_fingerprint(source.capture())
        assert image_fingerprint(current.stitcher.render()) == before
        assert "尚未接入" in current.preview.live_title.text()
        assert not current.paused and current.preview.canvas.viewport_start is None
        source.top = 120
        current.sample()
        wait_for(lambda: current.stitcher.height == 520)
        current.timer.stop()
        assert not current.recovering
        assert current.preview.canvas.viewport_start == 120
        assert image_fingerprint(current.preview.live_image) == image_fingerprint(source.capture())
        assert not outputs
    finally:
        current.stop()
        window.close()


def test_continuation_reuses_original_target_and_recovers_without_manual_resume(qt_app):
    current, source, outputs, window = controller(qt_app)
    try:
        current.timer.stop()
        source.top = 100
        current.capture_frame(current.serial)
        wait_for(lambda: current.stitcher.height == 500)
        current.timer.stop()
        current.finish()
        wait_for(lambda: current.editor is not None)
        editor = current.editor
        before = image_fingerprint(editor.doc.base)
        def wrong_factory(*args):
            raise AssertionError("Continuing must keep original target identity")
        current.source_factory = wrong_factory
        source.top = 1000
        editor.long_capture_button.click()
        wait_for(lambda: current.recovering)
        current.timer.stop()
        assert current.source is source and image_fingerprint(current.stitcher.render()) == before
        assert current.preview.live_image.height() == 400 and not current.paused
        source.top = 200
        current.sample()
        wait_for(lambda: current.stitcher.height == 600)
        current.timer.stop()
        current.finish()
        wait_for(lambda: current.editor is not None)
        assert current.editor.long_capture_button.width() >= current.editor.long_capture_button.sizeHint().width()
        current.editor.long_capture_button.click()
        wait_for(lambda: current.stitcher is not None and not current.busy)
        current.timer.stop()
        assert current.source is source
        current.cancel()
        assert current.editor.doc.base.height() == 600 and not outputs
    finally:
        current.stop()
        window.close()


def test_capture_excluded_overlays_never_hide_when_inside_selection(qt_app):
    current, source, outputs, window = controller(qt_app)
    try:
        current.timer.stop()
        current.bar.move(20, 30)
        current.preview.move(30, 40)
        widgets = (current.bar, current.preview, *current.outline.bands)
        for widget in widgets:
            widget.capture_excluded = True
        assert not current.hide_overlaps()
        assert all(widget.isVisible() for widget in widgets)
        current.restore_overlays()
        assert all(widget.isVisible() for widget in widgets)
    finally:
        current.stop()
        window.close()
