"""统一的剪贴板采集与写回入口，截图和历史复用均走这里。"""

import sqlite3
import uuid

from PySide6.QtCore import QMimeData, QObject, Signal
from PySide6.QtGui import QGuiApplication, QImage

OWN_FORMAT = "application/x-clipboard-saver-origin"


class ClipboardRecorder(QObject):
    recorded = Signal(int)
    failed = Signal(str)
    paused_changed = Signal(bool)

    def __init__(self, storage, clipboard=None, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.clipboard = clipboard if clipboard is not None else QGuiApplication.clipboard()
        self.paused = False
        self.origin = uuid.uuid4().hex.encode("ascii")
        self.clipboard.dataChanged.connect(self.capture)

    def set_paused(self, paused):
        self.paused = paused
        self.paused_changed.emit(paused)
        # 恢复时不读取剪贴板，避免补录暂停期间的最后一条私人内容。

    def capture(self):
        if self.paused:
            return
        mime = self.clipboard.mimeData()
        if mime is None or bytes(mime.data(OWN_FORMAT)) == self.origin:
            return
        if mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()):
            return
        try:
            if mime.hasImage():
                image = self.clipboard.image().copy()
                if image.isNull():
                    return
                record_id = self.storage.add_image(image)
            elif mime.hasText():
                record_id = self.storage.add_text(mime.text())
            else:
                return
            if record_id is not None:
                self.recorded.emit(record_id)
        except (sqlite3.Error, OSError, RuntimeError, ValueError):
            self.failed.emit("内容未能保存。请检查数据目录是否可写、磁盘空间是否充足。")

    def _write(self, text=None, image=None):
        mime = QMimeData()
        if image is not None:
            mime.setImageData(image)
        else:
            mime.setText(text)
        # setImage()/setText() 本身也是 setMimeData() 的包装。
        # 加一个私有格式过滤自己的通知，正常文字/图像格式仍可供其他软件粘贴。
        mime.setData(OWN_FORMAT, self.origin)
        self.clipboard.setMimeData(mime)

    def copy_record(self, record_id):
        record = self.storage.get_record(record_id)
        if record is None:
            return False
        if record["kind"] == "image":
            image = QImage.fromData(record["image"])
            if image.isNull():
                raise RuntimeError("历史图片无法读取")
            self._write(image=image)
        else:
            self._write(text=record["text"])
        # 暂停期间仍可取回旧内容，但不增加复制日期或改动归档顺序。
        if not self.paused:
            self.storage.touch(record_id)
        self.recorded.emit(record_id)
        return True

    def place_screenshot(self, image):
        self._write(image=image)
        if not self.paused:
            # 主动截图只保存一次，私有格式已经过滤了写回引发的 dataChanged。
            try:
                record_id = self.storage.add_image(image)
                if record_id is not None:
                    self.recorded.emit(record_id)
            except (sqlite3.Error, OSError, RuntimeError, ValueError):
                self.failed.emit("截图已复制，但未能保存到历史。请检查数据目录与磁盘空间。")

    def place_text(self, text):
        self._write(text=text)
        if not self.paused:
            try:
                record_id = self.storage.add_text(text)
                if record_id is not None:
                    self.recorded.emit(record_id)
            except (sqlite3.Error, OSError, RuntimeError, ValueError):
                self.failed.emit("文字已复制，但未能保存到历史。")

    def disconnect(self):
        self.clipboard.dataChanged.disconnect(self.capture)
