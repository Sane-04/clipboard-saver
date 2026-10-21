"""历史保留与特殊标记的设置界面，写入由应用层统一处理。"""
import sqlite3

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox, QDateEdit, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
)

from ui_theme import STYLE


class HistorySettingsDialog(QDialog):
    def __init__(self, storage, apply_handler, parent=None):
        super().__init__(parent)
        self.storage, self.apply_handler = storage, apply_handler
        self.setWindowTitle("历史保留设置")
        self.setStyleSheet(STYLE)
        self.resize(450, 285)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        title = QLabel("控制历史记录的保留数量")
        title.setObjectName("panelTitle")
        layout.addWidget(title)
        self.limit = QSpinBox()
        self.limit.setRange(1, 1_000_000)
        self.limit.setGroupSeparatorShown(True)
        self.limit.setSuffix(" 条")
        self.limit.setValue(storage.limit)
        form = QFormLayout()
        form.addRow("最多保留普通记录", self.limit)
        layout.addLayout(form)
        note = QLabel("文字与图片合计计数，相同内容只占一条。\n"
                      "固定记录和特殊标记记录额外保留，不占普通配额。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.impact = QLabel()
        self.impact.setWordWrap(True)
        layout.addWidget(self.impact)
        self.limit.valueChanged.connect(self.update_impact)
        self.update_impact()
        self.error = QLabel()
        self.error.setObjectName("error")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        layout.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def update_impact(self, *_):
        removed = max(0, self.storage.ordinary_count() - self.limit.value())
        self.impact.setText(f"保存后立即清理最旧的 {removed} 条普通记录，无法撤销。" if removed else
                            "保存后立即生效，以后超过上限时自动清理最旧的普通记录。")
        self.impact.setObjectName("error" if removed else "muted")
        self.impact.setStyleSheet("color: #b44242;" if removed else "color: #747d8d;")

    def save(self):
        try:
            self.apply_handler(self.limit.value())
        except (sqlite3.Error, OSError, RuntimeError, ValueError):
            self.error.setText("设置未保存，原配额与记录保持不变。请检查数据目录和磁盘空间。")
            self.error.show()
            return
        self.accept()


class DateCleanupDialog(QDialog):
    def __init__(self, storage, apply_handler, parent=None):
        super().__init__(parent)
        self.storage, self.apply_handler = storage, apply_handler
        self.setWindowTitle("按日期删除历史数据")
        self.setStyleSheet(STYLE)
        self.resize(490, 350)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        title = QLabel("清理指定日期以前的数据")
        title.setObjectName("panelTitle")
        layout.addWidget(title)
        self.cutoff = QDateEdit(QDate.currentDate())
        self.cutoff.setCalendarPopup(True)
        self.cutoff.setDisplayFormat("yyyy-MM-dd")
        form = QFormLayout()
        form.addRow("截止日期（不含当天）", self.cutoff)
        layout.addLayout(form)
        self.include_protected = QCheckBox("同时删除固定和特殊标记记录")
        layout.addWidget(self.include_protected)
        note = QLabel("默认保留固定和特殊标记记录。\n"
                      "同一内容在截止日期当天或以后复制过，会保留内容，只清理旧日期归档。\n"
                      "删除包括历史中的文字和原图，不影响设置、OCR 模型、备份或另存的 PNG。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.impact = QLabel()
        self.impact.setWordWrap(True)
        layout.addWidget(self.impact)
        self.error = QLabel()
        self.error.setObjectName("error")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        layout.addStretch()
        buttons = QDialogButtonBox()
        self.delete_button = QPushButton("删除日期以前的数据…")
        self.delete_button.setObjectName("primary")
        buttons.addButton(self.delete_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        self.delete_button.clicked.connect(self.delete)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.cutoff.dateChanged.connect(self.update_impact)
        self.include_protected.toggled.connect(self.update_impact)
        self.update_impact()

    def selected_cutoff(self):
        return self.cutoff.date().toString("yyyy-MM-dd")

    def update_impact(self, *_):
        self.stats = self.storage.cleanup_before_stats(
            self.selected_cutoff(), self.include_protected.isChecked())
        self.impact.setText(
            f'将清理 {self.stats["days"]} 条日期归档，删除 {self.stats["records"]} 条内容'
            f'（含 {self.stats["images"]} 张图片）。\n'
            f'保留 {self.stats["protected_days"]} 条固定 / 特殊标记日期归档。')
        self.delete_button.setEnabled(bool(self.stats["days"]))

    def delete(self):
        self.update_impact()
        if not self.stats["days"]:
            return
        cutoff, include = self.selected_cutoff(), self.include_protected.isChecked()
        message = (f'删除 {cutoff} 以前的数据（不含 {cutoff} 当天）？\n\n'
                   f'清理 {self.stats["days"]} 条日期归档，删除 {self.stats["records"]} 条内容'
                   f'（含 {self.stats["images"]} 张图片）。\n'
                   + ("包含固定和特殊标记记录。\n" if include else "固定和特殊标记记录会保留。\n")
                   + "较新日期的内容会保留。删除后无法撤销。")
        answer = QMessageBox.question(
            self, "确认按日期删除", message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.result_stats = self.apply_handler(cutoff, include)
        except (sqlite3.Error, OSError, RuntimeError, ValueError):
            self.error.setText("删除未完成，原记录保持不变。请检查数据目录和磁盘空间。")
            self.error.show()
            return
        self.accept()


class SpecialMarkDialog(QDialog):
    def __init__(self, record, parent=None):
        super().__init__(parent)
        self.setWindowTitle("特殊标记与标签")
        self.setStyleSheet(STYLE)
        self.resize(440, 260)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        self.marked = QCheckBox("特殊标记此记录")
        self.marked.setChecked(bool(record["marked"]))
        layout.addWidget(self.marked)
        self.tag = QLineEdit(record["tag"])
        self.tag.setMaxLength(120)
        self.tag.setPlaceholderText("例如：工作资料、报销、学习笔记（可留空）")
        form = QFormLayout()
        form.addRow("标签", self.tag)
        layout.addLayout(form)
        self.tag.setEnabled(self.marked.isChecked())
        self.marked.toggled.connect(self.tag.setEnabled)
        note = QLabel("标签可在搜索框中查找；“仅特殊标记”可单独查看。\n"
                      "标记记录不会被自动清理，仍可手动删除。\n"
                      "取消标记后恢复普通保留规则，未固定的旧记录可能被淘汰。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
