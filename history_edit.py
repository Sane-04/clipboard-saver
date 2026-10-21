"""历史文字的显式编辑/保存，取消不写库，不改系统剪贴板。"""
import sqlite3

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout

from ui_theme import STYLE


class TextRecordDialog(QDialog):
    def __init__(self, text, save_handler, parent=None):
        super().__init__(parent)
        self.save_handler = save_handler
        self.setWindowTitle("编辑已保存的文字 · 剪贴收藏")
        self.setStyleSheet(STYLE)
        self.resize(650, 450)
        layout = QVBoxLayout(self)
        note = QLabel("保存到原记录，各日期中的同一条内容同步更新。\n固定、特殊标记、标签和日期保留。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.text = QPlainTextEdit()
        self.text.setPlainText(text)
        self.initial_display = self.text.toPlainText()
        layout.addWidget(self.text, 1)
        self.error = QLabel()
        self.error.setObjectName("error")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                  QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存修改")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self):
        value = self.text.toPlainText()
        if value == self.initial_display:
            # Qt 会把 CRLF 显示为 LF；没有实际改字时不写回，保留原始换行字节。
            self.accept()
            return
        try:
            self.save_handler(value)
        except ValueError as error:
            self.error.setText(str(error))
            self.error.show()
            return
        except (sqlite3.Error, OSError, RuntimeError):
            self.error.setText("修改未保存，请检查数据目录与磁盘空间。编辑内容已保留。")
            self.error.show()
            return
        self.accept()
