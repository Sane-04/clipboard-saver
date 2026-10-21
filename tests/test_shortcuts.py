import ctypes
from ctypes import wintypes

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from image_editor import ImageEditor
from shortcut_settings import DEFAULT_SHORTCUTS, ShortcutSettingsDialog, ShortcutStore, validate_shortcuts
from test_editor import fixture_image
from windows_integration import HotkeyFilter, WM_HOTKEY


class FakeHotkeys:
    def __init__(self):
        self.registrations = {}
        self.occupied = set()
        self.calls = []

    def RegisterHotKey(self, hwnd, key_id, modifiers, key):
        spec = modifiers & ~0x4000, key
        self.calls.append(("register", key_id, spec))
        if spec in self.occupied or spec in self.registrations.values():
            return False
        self.registrations[hwnd, key_id] = spec
        return True

    def UnregisterHotKey(self, hwnd, key_id):
        self.calls.append(("unregister", key_id))
        self.registrations.pop((hwnd, key_id), None)
        return True


def manager(qt_app):
    window, backend, events = QWidget(), FakeHotkeys(), []
    keys = HotkeyFilter(qt_app, window, lambda: events.append("history"),
                        lambda: events.append("screenshot"), backend=backend)
    return window, backend, events, keys


def trigger(keys, key_id):
    message = wintypes.MSG()
    message.message, message.wParam = WM_HOTKEY, key_id
    assert keys.nativeEventFilter(b"windows_dispatcher_MSG", ctypes.addressof(message)) is True


def test_shortcuts_roundtrip_keeps_api_file_and_default_missing_fields(qt_app, tmp_path):
    api = tmp_path / "api-settings.json"
    api.write_bytes(b"encrypted-settings-placeholder")
    store = ShortcutStore(tmp_path)
    values = dict(DEFAULT_SHORTCUTS, history="Alt+F9", screenshot="Ctrl+Alt+S")
    store.save(values)
    assert ShortcutStore(tmp_path).bindings == validate_shortcuts(values)
    assert api.read_bytes() == b"encrypted-settings-placeholder"
    assert validate_shortcuts({"history": "Alt+F9"})["delete"] == "Del"


@pytest.mark.parametrize("patch", [
    {"history": "A"}, {"commit": ""}, {"delete": "Esc"},
    {"history": "Ctrl+K, Ctrl+H"},
    {"history": "Ctrl+Alt+X"}, {"undo": "Ctrl+Y"},
    {"history": "Ctrl+Enter"}, {"screenshot": "Ctrl+Shift+V"},
])
def test_invalid_or_duplicate_shortcuts_are_not_saved(qt_app, tmp_path, patch):
    store = ShortcutStore(tmp_path)
    with pytest.raises(ValueError):
        store.apply(dict(DEFAULT_SHORTCUTS, **patch))
    assert store.bindings == DEFAULT_SHORTCUTS and not store.path.exists()


def test_settings_dialog_saves_six_fields_and_restores_defaults(qt_app, tmp_path):
    store = ShortcutStore(tmp_path)
    dialog = ShortcutSettingsDialog(store)
    assert set(dialog.fields) == set(DEFAULT_SHORTCUTS)
    dialog.fields["history"].setKeySequence(QKeySequence("Alt+F9"))
    dialog.save()
    assert store.bindings["history"] == "Alt+F9"
    assert ShortcutStore(tmp_path).bindings["history"] == "Alt+F9"
    dialog = ShortcutSettingsDialog(store)
    dialog.restore_defaults()
    assert dialog.fields["history"].keySequence() == QKeySequence(DEFAULT_SHORTCUTS["history"])
    dialog.close()


def test_global_keys_change_and_old_keys_are_released_only_after_persist(qt_app):
    window, backend, events, keys = manager(qt_app)
    old = dict(backend.registrations)
    persisted = []
    values = dict(DEFAULT_SHORTCUTS, history="Alt+F9", screenshot="Alt+F10")

    def persist():
        assert all(backend.registrations.get(k) == v for k, v in old.items())
        persisted.append(True)

    keys.apply(values, persist)
    assert persisted == [True] and len(backend.registrations) == 2
    assert not set(old) & set(backend.registrations)
    for key_id, sequence in keys.sequences.items():
        trigger(keys, key_id)
    assert events == ["history", "screenshot"]
    keys.close()
    assert not backend.registrations
    window.close()


def test_swapping_global_keys_reuses_registrations_and_swaps_actions(qt_app):
    window, backend, events, keys = manager(qt_app)
    old, call_count = dict(backend.registrations), len(backend.calls)
    keys.apply(dict(DEFAULT_SHORTCUTS, history=DEFAULT_SHORTCUTS["screenshot"],
                    screenshot=DEFAULT_SHORTCUTS["history"]))
    assert backend.registrations == old and len(backend.calls) == call_count
    trigger(keys, 1)
    trigger(keys, 2)
    assert events == ["screenshot", "history"]
    keys.close()
    window.close()


def test_conflict_rolls_back_new_keys_and_keeps_old_actions(qt_app):
    window, backend, events, keys = manager(qt_app)
    old = dict(backend.registrations)
    backend.occupied.add((1, 0x79))  # Alt+F10
    with pytest.raises(ValueError):
        keys.apply(dict(DEFAULT_SHORTCUTS, history="Alt+F9", screenshot="Alt+F10"))
    assert backend.registrations == old and keys.registered == {1, 2}
    trigger(keys, 1)
    trigger(keys, 2)
    assert events == ["history", "screenshot"]
    keys.close()
    window.close()


def test_persistence_failure_keeps_old_keys_and_settings(qt_app, tmp_path):
    window, backend, events, keys = manager(qt_app)
    old = dict(backend.registrations)
    store = ShortcutStore(tmp_path)

    def fail():
        raise OSError("simulated write failure")

    with pytest.raises(OSError):
        keys.apply(dict(DEFAULT_SHORTCUTS, history="Alt+F9", screenshot="Alt+F10"), fail)
    assert backend.registrations == old and store.bindings == DEFAULT_SHORTCUTS
    assert keys.sequences[1] == DEFAULT_SHORTCUTS["history"]
    keys.close()
    window.close()


def test_capture_registered_key_in_settings_does_not_trigger_global_action(qt_app, tmp_path):
    window, backend, events, keys = manager(qt_app)
    dialog = ShortcutSettingsDialog(ShortcutStore(tmp_path))
    dialog.show()
    dialog.activateWindow()
    dialog.fields["screenshot"].setFocus()
    qt_app.processEvents()
    trigger(keys, 1)
    assert not events
    assert dialog.fields["screenshot"].keySequence() == QKeySequence(DEFAULT_SHORTCUTS["history"])
    dialog.close()
    keys.close()
    window.close()


def test_application_updates_history_labels_and_existing_editor(qt_app, tmp_path):
    from app import ClipboardApplication
    from test_history import FakeClipboard
    controller = ClipboardApplication(qt_app, tmp_path, enable_hotkeys=False,
                                      enable_tray=False, clipboard=FakeClipboard())
    image_id = controller.storage.add_image(fixture_image())
    controller.window.refresh(select_id=image_id)
    controller.edit_image()
    controller.shortcut_store.apply(dict(DEFAULT_SHORTCUTS, history="Alt+F9", screenshot="Alt+F10",
                                        undo="Alt+Z"))
    assert "Alt+F10" in controller.window.screenshot_button.text()
    assert "Alt+F9" in controller.window.shortcut_hint.text()
    assert "Alt+Z" in controller.editor.history_buttons["undo"].toolTip()
    assert ShortcutStore(tmp_path).bindings["screenshot"] == "Alt+F10"
    controller.cleanup()
    controller.window.exiting = True
    controller.window.close()


def test_existing_editor_shortcuts_update_and_delete_does_not_edit_text(qt_app, tmp_path):
    store = ShortcutStore(tmp_path)
    editor = ImageEditor(fixture_image(), shortcut_store=store)
    editor.show()
    editor.activateWindow()
    qt_app.processEvents()
    editor.doc.add([{"kind": "cover", "rect": (30, 30, 30, 30), "color": "#000000"}])
    store.save(dict(DEFAULT_SHORTCUTS, undo="Alt+Z", redo="Alt+Y", delete="Backspace",
                    commit="Ctrl+Alt+Return"))
    editor.canvas.setFocus()
    qt_app.processEvents()
    QTest.keyClick(editor.canvas, Qt.Key.Key_Z, Qt.KeyboardModifier.AltModifier)
    assert not editor.doc.objects
    QTest.keyClick(editor.canvas, Qt.Key.Key_Y, Qt.KeyboardModifier.AltModifier)
    assert len(editor.doc.objects) == 1
    editor.show_panel()
    editor.source_text.setPlainText("abc")
    editor.source_text.moveCursor(editor.source_text.textCursor().MoveOperation.End)
    editor.source_text.setFocus()
    qt_app.processEvents()
    QTest.keyClick(editor.source_text, Qt.Key.Key_Backspace)
    assert editor.source_text.toPlainText() == "ab" and len(editor.doc.objects) == 1
    editor.canvas.selected = 0
    editor.canvas.setFocus()
    qt_app.processEvents()
    QTest.keyClick(editor.canvas, Qt.Key.Key_Backspace)
    assert not editor.doc.objects
    exported = []
    editor.selected.connect(exported.append)
    QTest.keyClick(editor.canvas, Qt.Key.Key_Return,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    assert len(exported) == 1
