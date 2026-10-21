"""全文选择窗口；汇总识别结果，不改变文字块及图片排版。"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPlainTextEdit,
    QPushButton, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)


class TextResultsDialog(QDialog):
    text_copied = Signal(str)

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("剪贴收藏 · 全部文字")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint |
                            Qt.WindowType.WindowCloseButtonHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumSize(560, 340)
        available = self.screen().availableGeometry()
        self.resize(min(850, available.width()-40), min(580, available.height()-60))
        self.valid = False
        self.session = None
        self.snapshot = None
        self.records, self.included = [], {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        self.note = QLabel()
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        split = QSplitter(Qt.Orientation.Horizontal)
        scope = QWidget()
        scope_layout = QVBoxLayout(scope)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        scope_layout.setSpacing(8)
        scope_layout.addWidget(QLabel("勾选要复制的文字"))
        scope_actions = QHBoxLayout()
        scope_actions.setSpacing(4)
        self.scope_buttons = []
        for title, mode in (("全选", "all"), ("全不选", "none"), ("反选", "invert")):
            button = QPushButton(title)
            button.setObjectName("quiet")
            button.clicked.connect(lambda checked=False, value=mode: self.select_scope(value))
            scope_actions.addWidget(button)
            self.scope_buttons.append(button)
        scope_layout.addLayout(scope_actions)
        self.block_list = QListWidget()
        self.block_list.itemChanged.connect(self.scope_changed)
        scope_layout.addWidget(self.block_list, 1)
        split.addWidget(scope)
        self.tabs = QTabWidget()
        self.source_text, self.translated_text = QPlainTextEdit(), QPlainTextEdit()
        for title, field in (("原文", self.source_text), ("译文", self.translated_text)):
            field.setReadOnly(True)
            field.setPlaceholderText("暂无文字")
            self.tabs.addTab(field, title)
        self.tabs.currentChanged.connect(self.update_buttons)
        split.addWidget(self.tabs)
        split.setChildrenCollapsible(False)
        split.setSizes([240, 540])
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        hint = QLabel("Ctrl+A 全选 · Ctrl+C 复制 · 可跨行拖选；校对文字请回到识别块。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.select_all_button = QPushButton("全选文字")
        self.select_all_button.clicked.connect(self.select_all)
        row.addWidget(self.select_all_button)
        row.addStretch()
        self.copy_button = QPushButton("复制全部原文")
        self.copy_button.setObjectName("primary")
        self.copy_button.clicked.connect(self.copy_all)
        row.addWidget(self.copy_button)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        row.addWidget(close)
        layout.addLayout(row)

    def set_results(self, blocks, valid, session):
        records = [(block["id"], block["text"], block["translated"]) for block in blocks] if valid else []
        snapshot = (valid, session, records)
        if snapshot == self.snapshot:
            return
        self.snapshot = snapshot
        self.valid = valid
        if session != self.session:
            # 重新识别可能复用相同 ID，新会话重新全选，不能误用上一张图的复制范围。
            self.session, self.included = session, {}
        self.records = records
        current = self.block_list.currentItem()
        current_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        scroll = self.block_list.verticalScrollBar().value()
        self.block_list.blockSignals(True)
        self.block_list.clear()
        for block_id, text, _ in records:
            included = self.included.setdefault(block_id, True)
            item = QListWidgetItem(" ".join(text.split())[:80])
            item.setData(Qt.ItemDataRole.UserRole, block_id)
            item.setToolTip(text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if included else Qt.CheckState.Unchecked)
            self.block_list.addItem(item)
            if block_id == current_id:
                self.block_list.setCurrentItem(item)
        self.block_list.blockSignals(False)
        self.block_list.verticalScrollBar().setValue(scroll)
        self.block_list.setEnabled(valid)
        for button in self.scope_buttons:
            button.setEnabled(valid and bool(records))
        self.update_preview()

    def scope_changed(self, item):
        self.included[item.data(Qt.ItemDataRole.UserRole)] = item.checkState() == Qt.CheckState.Checked
        self.update_preview()

    def select_scope(self, mode):
        self.block_list.blockSignals(True)
        for row in range(self.block_list.count()):
            item = self.block_list.item(row)
            block_id = item.data(Qt.ItemDataRole.UserRole)
            included = not self.included[block_id] if mode == "invert" else mode == "all"
            self.included[block_id] = included
            item.setCheckState(Qt.CheckState.Checked if included else Qt.CheckState.Unchecked)
        self.block_list.blockSignals(False)
        self.update_preview()

    def update_preview(self):
        selected = [record for record in self.records if self.included[record[0]]]
        # 复制范围独立于翻译勾选，保留识别顺序与块内的换行/空格。
        source = "\n".join(record[1] for record in selected)
        translated = "\n".join(record[2] for record in selected if record[2].strip())
        for field, text in ((self.source_text, source), (self.translated_text, translated)):
            # 状态/选框刷新很频繁，相同文字不能重置用户的拖选、光标和滚动位置。
            if field.toPlainText() != text:
                field.setPlainText(text)
        self.note.setText(
            f"已选 {len(selected)} / {len(self.records)} 个文字块 · 仅控制复制，不改变翻译范围。"
            if self.valid else "图片已改变，请回到编辑器重新提取文字。")
        self.update_buttons()

    def update_buttons(self):
        field = self.tabs.currentWidget()
        enabled = self.valid and bool(field.toPlainText())
        self.select_all_button.setEnabled(enabled)
        self.copy_button.setEnabled(enabled)
        all_selected = all(self.included[record[0]] for record in self.records)
        self.copy_button.setText(("复制全部" if all_selected else "复制勾选") +
                                 ("原文" if field is self.source_text else "译文"))

    def select_all(self):
        field = self.tabs.currentWidget()
        field.setFocus()
        field.selectAll()

    def copy_all(self):
        field = self.tabs.currentWidget()
        text = field.toPlainText()
        if self.valid and text:
            self.text_copied.emit(text)

