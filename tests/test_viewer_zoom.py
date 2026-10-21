"""只读预览的滚轮方向、锚点、滚动和复位；只使用合成图片。"""
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QWheelEvent

from image_viewer import ImageViewer
from storage import image_fingerprint


def make_viewer(qt_app, width=1800, height=2600):
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(QColor(35, 90, 135, 180))
    viewer = ImageViewer(image, original_size=True)
    viewer.resize(700, 540)
    viewer.show()
    qt_app.processEvents()
    return viewer


def wheel(qt_app, viewer, delta=120, *, control=True, target=None, point=None, pixel_delta=0, process=True):
    target = target or viewer.scroll.viewport()
    point = point or QPoint(190, 180)
    event = QWheelEvent(QPointF(point), QPointF(target.mapToGlobal(point)),
                        QPoint(0, pixel_delta), QPoint(0, delta), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.ControlModifier if control else Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    qt_app.sendEvent(target, event)
    if process:
        qt_app.processEvents()
    return event


def pixel_at(viewer, position):
    origin = QPointF(viewer.surface.mapTo(viewer.scroll.viewport(), QPoint()))
    return (QPointF(position) - origin) / viewer.zoom_factor


def test_ctrl_wheel_both_directions_keep_mouse_pixel_and_raw_image(qt_app):
    viewer = make_viewer(qt_app)
    before = image_fingerprint(viewer.surface.image)
    position = QPoint(190, 180)
    horizontal, vertical = viewer.scroll.horizontalScrollBar(), viewer.scroll.verticalScrollBar()
    horizontal.setValue(200)
    vertical.setValue(500)
    pixel = pixel_at(viewer, position)
    wheel(qt_app, viewer, point=position)
    assert viewer.zoom_factor == pytest.approx(1.2) and not viewer.fit
    assert (pixel_at(viewer, position) - pixel).manhattanLength() < 1.5
    wheel(qt_app, viewer, -120, point=position)
    assert viewer.zoom_factor == pytest.approx(1)
    assert (pixel_at(viewer, position) - pixel).manhattanLength() < 2
    assert image_fingerprint(viewer.surface.image) == before
    assert "100.0%" in viewer.statusBar().currentMessage()
    viewer.close()


@pytest.mark.parametrize("on_surface", [False, True])
def test_plain_wheel_scrolls_without_changing_zoom(qt_app, on_surface):
    viewer = make_viewer(qt_app)
    bar = viewer.scroll.verticalScrollBar()
    bar.setValue(600)
    before = bar.value()
    target = viewer.surface if on_surface else viewer.scroll.viewport()
    point = viewer.surface.mapFrom(viewer.scroll.viewport(), QPoint(190, 180)) if on_surface else None
    wheel(qt_app, viewer, -120, control=False, target=target, point=point)
    assert bar.value() > before and viewer.zoom_factor == 1
    viewer.close()


def test_wheel_over_surface_and_blank_viewport_and_zero_delta(qt_app):
    viewer = make_viewer(qt_app, 180, 100)
    wheel(qt_app, viewer, target=viewer.surface, point=QPoint(70, 40))
    assert viewer.zoom_factor == pytest.approx(1.2)
    wheel(qt_app, viewer, point=QPoint(10, 10))
    assert viewer.zoom_factor == pytest.approx(1.44)
    wheel(qt_app, viewer, 0, pixel_delta=-60)
    assert viewer.zoom_factor == pytest.approx(1.2)
    wheel(qt_app, viewer, 0)
    assert viewer.zoom_factor == pytest.approx(1.2)
    viewer.close()


def test_resize_keeps_manual_scale_and_fit_and_original_buttons_reset(qt_app):
    viewer = make_viewer(qt_app)
    wheel(qt_app, viewer)
    viewer.resize(760, 610)
    qt_app.processEvents()
    assert viewer.zoom_factor == pytest.approx(1.2)
    viewer.set_fit(True)
    qt_app.processEvents()
    assert viewer.fit and viewer.zoom_factor == pytest.approx(viewer.fit_factor())
    assert viewer.surface.width() <= viewer.scroll.viewport().width()
    assert viewer.surface.height() <= viewer.scroll.viewport().height()
    viewer.resize(850, 660)
    qt_app.processEvents()
    assert viewer.zoom_factor == pytest.approx(viewer.fit_factor())
    viewer.set_fit(False)
    qt_app.processEvents()
    assert not viewer.fit and viewer.zoom_factor == 1
    assert viewer.surface.size() == viewer.surface.image.size()
    viewer.close()


def test_fast_events_bounds_and_reset_invalidate_queued_anchor(qt_app):
    viewer = make_viewer(qt_app)
    for _ in range(30):
        wheel(qt_app, viewer)
    assert viewer.zoom_factor == 8
    viewer.set_zoom(.00001)
    assert viewer.zoom_factor == .01
    viewer.set_zoom(3, pixel=QPointF(700, 900), position=QPointF(190, 180))
    viewer.set_fit(True)
    qt_app.processEvents()
    assert viewer.fit and viewer.zoom_factor == pytest.approx(viewer.fit_factor())
    assert viewer.scroll.horizontalScrollBar().value() == viewer.scroll.verticalScrollBar().value() == 0
    viewer.close()


def test_long_transparent_image_zoom_keeps_pixels_and_independent_pin(qt_app):
    viewer = make_viewer(qt_app, 100, 18000)
    before = image_fingerprint(viewer.surface.image)
    viewer.set_fit(True)
    qt_app.processEvents()
    initial = viewer.zoom_factor
    requests = []
    viewer.topmost_button.requested.connect(lambda: requests.append(True))
    viewer.topmost_button.click()
    wheel(qt_app, viewer)
    assert viewer.zoom_factor > initial and requests == [True]
    viewer.set_zoom(2)
    qt_app.processEvents()
    viewer.scroll.verticalScrollBar().setValue(15000)
    assert not viewer.scroll.viewport().grab().isNull()
    assert image_fingerprint(viewer.surface.image) == before
    viewer.close()


def test_burst_wheel_events_keep_latest_anchor(qt_app):
    viewer = make_viewer(qt_app)
    viewer.scroll.horizontalScrollBar().setValue(200)
    viewer.scroll.verticalScrollBar().setValue(500)
    point = QPoint(190, 180)
    before = pixel_at(viewer, point)
    for _ in range(6):
        wheel(qt_app, viewer, point=point, process=False)
    qt_app.processEvents()
    assert viewer.zoom_factor == pytest.approx(1.2 ** 6)
    assert (pixel_at(viewer, point) - before).manhattanLength() < 3
    viewer.close()
