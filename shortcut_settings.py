"""独立快捷键配置；不改写 API 密钥文件，保存成功后通知现有窗口。"""
import json
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QKeySequenceEdit, QLabel, QMessageBox,
    QPushButton, QVBoxLayout,
)
from ui_theme import STYLE

DEFAULT_SHORTCUTS = {
    "history": "Ctrl+Shift+V",
    "screenshot": "Ctrl+Alt+X",
    "long_screenshot": "Ctrl+Alt+L",
    "commit": "Ctrl+Return",
    "undo": "Ctrl+Z",
    "redo": "Ctrl+Y",
    "delete": "Delete",
}
SHORTCUT_LABELS = {
    "history": "调出历史",
    "screenshot": "框选截图",
    "long_screenshot": "滚动长截图",
    "commit": "编辑完成",
    "undo": "撤销编辑",
    "redo": "重做编辑",
    "delete": "删除选中对象",
}


def global_key(sequence):
    """Qt 的单组合键转换为 Windows RegisterHotKey 使用的修饰键与虚拟键。"""
    combination = QKeySequence(sequence)[0]
    modifiers = combination.keyboardModifiers()
    key = combination.key().value
    windows_modifiers = 0
    for qt_modifier, flag in ((Qt.KeyboardModifier.AltModifier, 1),
                              (Qt.KeyboardModifier.ControlModifier, 2),
                              (Qt.KeyboardModifier.ShiftModifier, 4),
                              (Qt.KeyboardModifier.MetaModifier, 8)):
        if modifiers & qt_modifier:
            windows_modifiers |= flag
    if Qt.Key.Key_F1.value <= key <= Qt.Key.Key_F24.value:
        virtual_key = 0x70 + key-Qt.Key.Key_F1.value
    elif ord("A") <= key <= ord("Z") or ord("0") <= key <= ord("9"):
        virtual_key = key
    else:
        other_keys = {
            Qt.Key.Key_Space.value: 0x20, Qt.Key.Key_Tab.value: 0x09,
            Qt.Key.Key_Return.value: 0x0D, Qt.Key.Key_Enter.value: 0x0D,
            Qt.Key.Key_Backspace.value: 0x08, Qt.Key.Key_Delete.value: 0x2E,
            Qt.Key.Key_Insert.value: 0x2D, Qt.Key.Key_Home.value: 0x24,
            Qt.Key.Key_End.value: 0x23, Qt.Key.Key_PageUp.value: 0x21,
            Qt.Key.Key_PageDown.value: 0x22, Qt.Key.Key_Left.value: 0x25,
            Qt.Key.Key_Up.value: 0x26, Qt.Key.Key_Right.value: 0x27,
            Qt.Key.Key_Down.value: 0x28,
        }
        if key not in other_keys:
            raise ValueError("全局快捷键请使用字母、数字、F1–F24、空格或导航键。")
        virtual_key = other_keys[key]
    if not windows_modifiers & (1 | 2 | 8) and not Qt.Key.Key_F1.value <= key <= Qt.Key.Key_F24.value:
        raise ValueError("全局快捷键需包含 Ctrl、Alt 或 Win，或使用 F1–F24。")
    return windows_modifiers, virtual_key


def validate_shortcuts(values):
    values = dict(values)
    if "long_screenshot" not in values:
        # 升级只补新字段；若旧用户已占用 Ctrl+Alt+L，选择备用键，不重置旧六项。
        existing = {QKeySequence(value).toString(QKeySequence.SequenceFormat.PortableText)
                    for value in values.values() if isinstance(value, str)}
        values["long_screenshot"] = next(key for key in
            ("Ctrl+Alt+L", "Ctrl+Alt+Shift+L", "Ctrl+Alt+Shift+F24", "Ctrl+Alt+Shift+F23",
             "Ctrl+Alt+Shift+F22", "Ctrl+Alt+Shift+F21", "Ctrl+Alt+Shift+F20", "Ctrl+Alt+Shift+F19")
            if key not in existing)
    normalized, used = {}, set()
    for name, default in DEFAULT_SHORTCUTS.items():
        sequence = QKeySequence(values.get(name, default))
        if sequence.isEmpty() or sequence.count() != 1 or sequence[0].key() == Qt.Key.Key_unknown:
            raise ValueError(f"“{SHORTCUT_LABELS[name]}”需要一个有效的组合键。")
        text = sequence.toString(QKeySequence.SequenceFormat.PortableText)
        if text == "Esc":
            raise ValueError("Esc 保留用于收起窗口和取消截图，请使用其他组合键。")
        comparable = text.replace("Enter", "Return")
        if comparable in used:
            raise ValueError(f"快捷键 {text} 重复，请为各操作设置不同组合键。")
        used.add(comparable)
        if name in ("history", "screenshot", "long_screenshot"):
            global_key(text)
        normalized[name] = text
    keys = [global_key(normalized[name]) for name in ("history", "screenshot", "long_screenshot")]
    if len(set(keys)) != len(keys):
        raise ValueError("调出历史、普通截图和长截图不能使用相同的全局快捷键。")
    return normalized


class ShortcutStore(QObject):
    changed = Signal(object)

    def __init__(self, data_dir, parent=None):
        super().__init__(parent)
        self.path = Path(data_dir) / "shortcuts.json"
        self.bindings = dict(DEFAULT_SHORTCUTS)
        self.error = ""
        self.apply_handler = None
        if self.path.exists():
            try:
                self.bindings = validate_shortcuts(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, AttributeError):
                self.error = "快捷键配置无法读取，暂用默认值；原文件已保留。"

    def label(self, name):
        return QKeySequence(self.bindings[name]).toString(QKeySequence.SequenceFormat.NativeText)

    def save(self, values):
        values = validate_shortcuts(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self.bindings = values
        self.error = ""
        self.changed.emit(dict(values))

    def apply(self, values):
        values = validate_shortcuts(values)
        if self.apply_handler:
            self.apply_handler(values)
        else:
            self.save(values)


class ShortcutSettingsDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("快捷键设置 · 剪贴收藏")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)
        title = QLabel("快捷键")
        title.setObjectName("heading")
        layout.addWidget(title)
        note = QLabel("点击输入框，再按你想使用的组合键。\n"
                      "前三项在后台生效，其余用于图片编辑；保存后立即生效。")
        note.setObjectName("muted")
        layout.addWidget(note)
        form = QFormLayout()
        form.setSpacing(10)
        self.fields = {}
        for name, text in SHORTCUT_LABELS.items():
            field = QKeySequenceEdit(QKeySequence(store.bindings[name]))
            field.setMaximumSequenceLength(1)
            field.setClearButtonEnabled(True)
            self.fields[name] = field
            form.addRow(text, field)
        layout.addLayout(form)
        restore = QPushButton("恢复默认值")
        restore.setObjectName("quiet")
        restore.clicked.connect(self.restore_defaults)
        layout.addWidget(restore)
        hint = QLabel("Esc 固定用于取消。删除对象在画布或文字块列表中生效，输入文字时仍可正常删除字符。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                  QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def restore_defaults(self):
        for name, field in self.fields.items():
            field.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS[name]))

    def save(self):
        try:
            self.store.apply({name: field.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
                              for name, field in self.fields.items()})
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "快捷键未保存", str(error))
            return
        self.accept()
