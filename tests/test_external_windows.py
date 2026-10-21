"""模拟后端验证选窗、随窗标记、身份及退出恢复，不枚举用户窗口。"""
from dataclasses import replace
from time import monotonic

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from external_windows import WindowTarget
from window_picker import WindowPinController
from ui import HistoryWindow


class FakeWindows:
    def __init__(self):
        self.targets = {
            10: WindowTarget(10, 100, 101, "Synthetic", "合成记事本", False),
            20: WindowTarget(20, 200, 201, "Synthetic", "合成浏览器", True),
        }
        self.hit = 10
        self.calls = []
        self.error = ""
        self.delayed = False
        self.bounds = (20, 30, 700, 500)
        self.positions = {}

    def current(self, target):
        value = self.targets.get(target.hwnd)
        if value is None or value.identity != target.identity:
            raise ValueError("目标窗口已关闭或身份改变，请重新选择。")
        return value

    def set_topmost(self, target, enabled):
        self.current(target)
        if self.error:
            raise OSError(self.error)
        self.calls.append((target.hwnd, enabled))
        if not self.delayed:
            self.targets[target.hwnd] = replace(self.targets[target.hwnd], topmost=enabled)

    def window_at(self, point, excluded=()):
        return self.targets.get(self.hit)

    def cursor(self):
        return 100, 100

    def rect(self, hwnd):
        return self.bounds

    def monitor_rect(self, hwnd):
        return 0, 0, 800, 800

    def position_badge(self, hwnd, target):
        self.positions[hwnd] = self.bounds
        return 300, 40


@pytest.fixture
def pins(qt_app):
    controller = WindowPinController(backend=FakeWindows())
    yield controller
    controller.close()


def test_selection_click_is_consumed_then_badge_unpins(pins):
    pins.start()
    assert pins.selecting and pins.candidate.hwnd == 10
    overlay = pins.overlays[0]
    assert not overlay.cursor().pixmap().isNull()
    QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
    assert not pins.backend.calls
    QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
    assert not pins.selecting and not pins.overlays
    entry = next(iter(pins.entries.values()))
    assert pins.backend.calls == [(10, True)] and entry.badge.isVisible()
    entry.badge.click()
    assert pins.backend.calls == [(10, True), (10, False)]
    assert not pins.entries and not pins.timer.isActive()


@pytest.mark.parametrize("method", ["escape", "right", "screen", "inactive"])
def test_cancel_never_changes_window_and_can_restart(pins, method):
    pins.start()
    overlay = pins.overlays[0]
    if method == "escape":
        QTest.keyClick(overlay, Qt.Key.Key_Escape)
    elif method == "right":
        QTest.mouseClick(overlay, Qt.MouseButton.RightButton)
    elif method == "screen":
        pins.screen_changed()
    else:
        pins.application_state_changed(Qt.ApplicationState.ApplicationInactive)
    assert not pins.selecting and not pins.overlays and not pins.backend.calls
    pins.start()
    assert pins.selecting


def test_invalid_point_and_drag_to_different_window_never_write(pins):
    pins.backend.hit = None
    pins.start()
    overlay = pins.overlays[0]
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton)
    assert pins.selecting and not pins.backend.calls
    pins.backend.hit = 10
    QTest.mousePress(overlay, Qt.MouseButton.LeftButton)
    pins.backend.hit = 20
    QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton)
    assert pins.selecting and not pins.backend.calls


def test_handle_reuse_and_closed_target_never_touch_replacement(pins):
    old = pins.backend.targets[10]
    pins.choose(old)
    pins.backend.targets[10] = replace(old, pid=900, thread=901, class_name="Reused", topmost=True)
    pins.tick()
    assert not pins.entries and pins.backend.calls == [(10, True)]
    pins.choose(old)
    assert not pins.entries and pins.backend.calls == [(10, True)]


def test_badge_tracks_geometry_hides_for_minimize_and_cloak(pins):
    pins.choose(pins.backend.targets[10])
    entry = next(iter(pins.entries.values()))
    pins.backend.bounds = (-900, 100, -200, 600)
    pins.tick()
    assert pins.backend.positions[int(entry.badge.winId())] == pins.backend.bounds
    for field in ("minimized", "cloaked"):
        pins.backend.targets[10] = replace(pins.backend.targets[10], **{field: True})
        pins.tick()
        assert not entry.badge.isVisible() and pins.entries
        pins.backend.targets[10] = replace(pins.backend.targets[10], **{field: False})
        pins.tick()
        assert entry.badge.isVisible()
    pins.backend.targets[10] = replace(pins.backend.targets[10], visible=False)
    pins.tick()
    assert not entry.badge.isVisible() and pins.entries
    pins.backend.targets[10] = replace(pins.backend.targets[10], visible=True)
    pins.tick()
    assert entry.badge.isVisible()


def test_occlusion_hides_badge_external_unpin_removes_it(pins):
    pins.choose(pins.backend.targets[10])
    entry = next(iter(pins.entries.values()))
    pins.backend.hit = 20
    pins.tick()
    assert not entry.badge.isVisible()
    pins.backend.hit = 10
    pins.tick()
    assert entry.badge.isVisible()
    pins.backend.targets[10] = replace(pins.backend.targets[10], topmost=False)
    pins.tick()
    assert not pins.entries


def test_multiple_targets_and_exit_restore_only_new_pins(pins):
    pins.choose(pins.backend.targets[10])
    pins.backend.hit = 20
    pins.choose(pins.backend.targets[20])
    assert len(pins.entries) == 2
    pins.close()
    assert not pins.backend.targets[10].topmost and pins.backend.targets[20].topmost
    assert pins.backend.calls == [(10, True), (10, False)]
    assert not pins.entries and not pins.overlays


def test_already_topmost_selection_adds_badge_and_click_explicitly_unpins(pins):
    pins.backend.hit = 20
    pins.choose(pins.backend.targets[20])
    assert not pins.backend.calls
    next(iter(pins.entries.values())).badge.click()
    assert pins.backend.calls == [(20, False)] and not pins.entries


def test_async_confirm_and_repeated_click_send_once(pins):
    pins.backend.delayed = True
    target = pins.backend.targets[10]
    pins.choose(target)
    pins.choose(target)
    assert pins.pending and pins.backend.calls == [(10, True)]
    pins.backend.targets[10] = replace(target, topmost=True)
    pins.tick()
    assert not pins.pending and next(iter(pins.entries.values())).badge.isVisible()


def test_late_confirmation_and_unpin_error_keep_cancel_route(pins):
    pins.backend.delayed = True
    target = pins.backend.targets[10]
    pins.choose(target)
    key = target.identity
    pins.pending[key] = True, monotonic()-1
    errors = []
    pins.failed.connect(errors.append)
    pins.tick()
    assert errors and "未能确认" in errors[0] and pins.entries
    pins.backend.targets[10] = replace(target, topmost=True)
    pins.tick()
    entry = pins.entries[key]
    assert entry.badge.isVisible()
    pins.backend.error = "模拟权限拒绝"
    entry.badge.click()
    assert pins.entries and entry.badge.isEnabled() and "权限" in errors[-1]
    assert pins.backend.calls == [(10, True)]


def test_initial_error_leaves_no_badge_or_pending(pins):
    pins.backend.error = "模拟权限拒绝"
    pins.choose(pins.backend.targets[10])
    assert not pins.pending and not pins.entries and not pins.backend.calls


def test_only_existing_buttons_start_picker_no_old_menu(qt_app, pins, storage):
    qt_app._window_pin_controller = pins
    window = HistoryWindow(storage)
    try:
        window.topmost_button.click()
        assert pins.selecting and not window.topmost_button.isCheckable()
        assert not hasattr(window, "external_pin_action")
        assert not pins.backend.calls
    finally:
        pins.cancel()
        window.exiting = True
        window.close()


def test_capture_blocks_picker(pins):
    pins.can_start = lambda: False
    pins.start()
    assert not pins.selecting and not pins.overlays and not pins.backend.calls


def test_capture_temporarily_hides_existing_badges_then_restores(pins):
    pins.choose(pins.backend.targets[10])
    entry = next(iter(pins.entries.values()))
    pins.can_start = lambda: False
    pins.tick()
    assert not entry.badge.isVisible() and pins.entries
    pins.can_start = lambda: True
    pins.tick()
    assert entry.badge.isVisible() and pins.backend.calls == [(10, True)]


def test_badge_display_failure_retains_target_for_recovery_and_exit(pins):
    original = pins.backend.position_badge
    def unavailable(*_):
        raise OSError("模拟显示器暂时不可用")
    pins.backend.position_badge = unavailable
    pins.choose(pins.backend.targets[10])
    assert pins.backend.targets[10].topmost and pins.entries
    pins.backend.position_badge = original
    pins.tick()
    assert next(iter(pins.entries.values())).badge.isVisible()
    pins.close()
    assert not pins.backend.targets[10].topmost
