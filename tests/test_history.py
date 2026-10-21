from PySide6.QtCore import QMimeData, QObject, QUrl, Signal
from PySide6.QtGui import QColor, QImage

from clipboard_capture import ClipboardRecorder
from storage import HistoryStorage, image_fingerprint


def picture(width=32, height=17):
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(QColor(11, 122, 99, 170))
    image.setPixelColor(3, 2, QColor(250, 34, 66, 255))
    return image


class FakeClipboard(QObject):
    dataChanged = Signal()

    def __init__(self):
        super().__init__()
        self.mime = QMimeData()

    def mimeData(self):
        return self.mime

    def image(self):
        return QImage(self.mime.imageData()) if self.mime.hasImage() else QImage()

    def setMimeData(self, mime):
        self.mime = mime
        self.dataChanged.emit()

    def put_text(self, text):
        mime = QMimeData()
        mime.setText(text)
        self.setMimeData(mime)


def test_text_exact_duplicate_and_restart(tmp_path):
    store = HistoryStorage(tmp_path)
    first = store.add_text("  中文\n    code()  \n")
    store.toggle_pin(first)
    store.add_text("第二条")
    assert store.add_text("  中文\n    code()  \n") == first
    assert store.get_record(first)["pinned"] == 1
    assert store.add_text("中文\n    code()") != first
    assert store.add_text(" \r\n ") is None
    store.close()
    store = HistoryStorage(tmp_path)
    assert store.get_record(first)["text"] == "  中文\n    code()  \n"
    assert store.counts()["total"] == 3
    store.close()


def test_image_pixels_transparency_and_dedup_after_restart(tmp_path):
    store = HistoryStorage(tmp_path)
    image = picture()
    record_id = store.add_image(image)
    equivalent = image.convertToFormat(QImage.Format.Format_ARGB32)
    equivalent.setText("source", "different metadata")
    assert store.add_image(equivalent) == record_id
    different = image.copy()
    different.setPixelColor(0, 0, QColor(12, 122, 99, 170))
    assert store.add_image(different) != record_id
    assert "image" not in store.list_records()[0]
    store.close()
    store = HistoryStorage(tmp_path)
    recovered = QImage.fromData(store.get_record(record_id)["image"])
    assert recovered.size() == image.size()
    assert image_fingerprint(recovered) == image_fingerprint(image)
    assert recovered.pixelColor(0, 0).alpha() == 170
    assert not QImage.fromData(store.thumbnail(record_id)).isNull()
    store.delete(record_id)
    assert store.get_record(record_id) is None
    assert store.thumbnail(record_id) == b""
    store.close()


def test_mixed_quota_keeps_pinned_and_unpin_prunes(tmp_path):
    store = HistoryStorage(tmp_path, limit=3)
    pinned = store.add_image(picture())
    store.toggle_pin(pinned)
    oldest = store.add_text("oldest")
    for text in ["second", "third", "fourth"]:
        store.add_text(text)
    assert store.get_record(oldest) is None
    assert store.counts() == dict(total=4, images=1, pinned=1)
    store.toggle_pin(pinned)
    assert store.counts()["total"] == 3
    store.close()


def test_search_literal_and_clear_preserves_pins(storage):
    literal = storage.add_text("100%_中文")
    storage.add_text("另一条")
    image_id = storage.add_image(picture())
    storage.toggle_pin(image_id)
    assert [r["id"] for r in storage.list_records("%_")] == [literal]
    assert len(storage.list_records(kind="image")) == 1
    assert len(storage.list_records(kind="text")) == 2
    storage.clear_unpinned()
    assert storage.counts() == dict(total=1, images=1, pinned=1)


def test_pause_no_backfill_and_own_copy_no_loop(qt_app, storage):
    clipboard = FakeClipboard()
    recorder = ClipboardRecorder(storage, clipboard)
    events = []
    recorder.recorded.connect(events.append)
    clipboard.put_text("第一条")
    first = events[-1]
    recorder.set_paused(True)
    clipboard.put_text("暂停时的文字")
    recorder.set_paused(False)
    assert storage.counts()["total"] == 1
    clipboard.put_text("第二条")
    assert recorder.copy_record(first)
    assert clipboard.mimeData().text() == "第一条"
    assert len(events) == 3
    assert storage.counts()["total"] == 2
    recorder.disconnect()


def test_image_priority_file_objects_and_screenshot_pause(qt_app, storage):
    clipboard = FakeClipboard()
    recorder = ClipboardRecorder(storage, clipboard)
    events = []
    recorder.recorded.connect(events.append)
    mixed = QMimeData()
    mixed.setImageData(picture())
    mixed.setText("https://example.test/image")
    clipboard.setMimeData(mixed)
    assert storage.counts() == dict(total=1, images=1, pinned=0)
    image_id = events[-1]
    file_mime = QMimeData()
    file_mime.setUrls([QUrl.fromLocalFile("C:/test/image.png")])
    file_mime.setText("C:/test/image.png")
    clipboard.setMimeData(file_mime)
    assert storage.counts()["total"] == 1
    assert recorder.copy_record(image_id)
    assert image_fingerprint(clipboard.image()) == image_fingerprint(picture())
    recorder.set_paused(True)
    recorder.place_screenshot(picture(40, 25))
    assert clipboard.image().width() == 40
    assert storage.counts()["total"] == 1
    recorder.set_paused(False)
    assert storage.counts()["total"] == 1
    recorder.place_screenshot(picture(45, 29))
    assert storage.counts()["total"] == 2
    assert len(events) == 3
    recorder.disconnect()
