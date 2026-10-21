from threading import Event
from types import SimpleNamespace

import numpy as np
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtTest import QTest

from api_settings import ApiSettings, SettingsStore
from image_document import ImageDocument
from image_editor import ImageEditor
from local_ocr import LocalOcrClient, LocalOcrService, image_to_bgr, output_to_data


def wait_for(predicate, timeout=2500):
    from PySide6.QtCore import QEventLoop, QTimer
    if predicate():
        return
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(20)
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    poll.start()
    deadline.start(timeout)
    # 使用与真实程序相同的 Qt 事件循环，给后台 Python 模型导入释放 GIL。
    loop.exec()
    poll.stop()
    deadline.stop()
    assert predicate()


def fixture_image():
    image = QImage(240, 100, QImage.Format.Format_RGBA8888)
    image.fill(QColor("white"))
    return image


def output(text="Hello"):
    return SimpleNamespace(boxes=np.array([[[10, 10], [90, 10], [90, 40], [10, 40]]]),
                           txts=(text,), scores=(.99,))


def test_local_rgb_stride_transparency_and_box_contract():
    image = QImage(7, 3, QImage.Format.Format_RGBA8888)
    image.fill(QColor(230, 70, 20, 255))
    image.setPixelColor(2, 1, QColor(0, 0, 0, 0))
    bgr = image_to_bgr(image)
    assert bgr.shape == (3, 7, 3)
    assert bgr[0, 0].tolist() == [20, 70, 230]
    assert bgr[1, 2].tolist() == [255, 255, 255]
    data = output_to_data(output(), 100, 50)
    assert data["blocks"][0]["bbox"] == [.1, .2, .9, .8]
    assert data["blocks"][0]["confidence"] == .99
    empty = output_to_data(SimpleNamespace(boxes=None, txts=None, scores=None), 100, 50)
    assert not empty["blocks"]


def test_model_reused_redacted_crop_async_and_no_api_key(qt_app):
    arrays, created = [], []

    def factory():
        created.append(True)
        def engine(array):
            arrays.append(array.copy())
            return output()
        return engine

    service = LocalOcrService(engine_factory=factory)
    image = fixture_image()
    doc = ImageDocument(image, (20, 10, 120, 60))
    doc.add([{"kind": "cover", "rect": (30, 20, 25, 25), "color": "#000000"}])
    client = LocalOcrClient(service=service)
    results = []
    client.succeeded.connect(lambda kind, data: results.append(data))
    client.request(doc.render())
    assert not results
    wait_for(lambda: len(results) == 1)
    client.request(doc.render())
    wait_for(lambda: len(results) == 2)
    assert len(created) == 1
    assert arrays[0].shape == (60, 120, 3)
    assert arrays[0][15, 15].tolist() == [0, 0, 0]
    service.shutdown()


def test_cancel_and_timeout_ignore_late_results(qt_app):
    entered, release = Event(), Event()
    def engine(array):
        entered.set()
        release.wait(2)
        return output()
    service = LocalOcrService(engine_factory=lambda: engine)
    client = LocalOcrClient(service=service)
    results, errors = [], []
    client.succeeded.connect(lambda *args: results.append(args))
    client.failed.connect(errors.append)
    client.request(fixture_image())
    wait_for(entered.is_set)
    client.cancel()
    release.set()
    QTest.qWait(60)
    assert not results and not client.busy
    entered.clear()
    release.clear()
    client.request(fixture_image())
    wait_for(entered.is_set)
    client.timer.start(25)
    wait_for(lambda: bool(errors))
    release.set()
    QTest.qWait(60)
    assert not results and "超时" in errors[0]
    service.shutdown()


def test_editor_local_result_maps_offset_and_edit_cancels_without_upload(qt_app):
    release = Event()
    def engine(array):
        release.wait(2)
        return output()
    service = LocalOcrService(engine_factory=lambda: engine)
    editor = ImageEditor(fixture_image(), local_service=service)
    editor.doc.crop = (20, 10, 120, 60)
    api_calls = []
    editor.client.request = lambda *args: api_calls.append(args)
    editor.extract()
    assert editor.local_client.busy and not api_calls
    editor.doc.add([{"kind": "cover", "rect": (0, 0, 10, 10), "color": "#000000"}])
    assert not editor.local_client.busy
    release.set()
    QTest.qWait(60)
    assert not editor.blocks
    editor.extract()
    wait_for(lambda: bool(editor.blocks))
    assert editor.blocks[0]["rect"] == (30, 20, 80, 30)
    assert not api_calls and "本地识别" in editor.status.text()
    editor.cancel()
    service.shutdown()


def test_failure_never_falls_back_to_api(qt_app):
    def missing():
        raise ImportError("test component missing")
    service = LocalOcrService(engine_factory=missing)
    editor = ImageEditor(fixture_image(), local_service=service)
    api_calls = []
    editor.client.request = lambda *args: api_calls.append(args)
    editor.extract()
    wait_for(lambda: "缺失" in editor.status.text())
    assert not api_calls and not editor.blocks
    editor.cancel()
    service.shutdown()


def test_ocr_provider_saved_without_changing_api(qt_app, tmp_path):
    store = SettingsStore(tmp_path)
    store.save(ApiSettings(key="dummy-only", ocr_provider="api"))
    loaded = SettingsStore(tmp_path)
    assert loaded.settings.ocr_provider == "api"
    loaded.settings.ocr_provider = "local"
    loaded.save(loaded.settings)
    again = SettingsStore(tmp_path)
    assert again.settings.ocr_provider == "local"
    assert again.settings.key == "dummy-only"
    assert again.settings.base_url == "https://s2api.top/v1"
    assert again.settings.model == "gpt-5.6-luna"


def test_real_cpu_recognition_and_ui_heartbeat(qt_app):
    from pathlib import Path
    # 固定的合成样图，避免 Windows offscreen 插件的缺失字体影响识别断言。
    image = QImage(str(Path(__file__).parent / "fixtures" / "ocr_en.png"))
    from PySide6.QtCore import QTimer
    ticks, results, errors = [], [], []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(True))
    service = LocalOcrService()
    client = LocalOcrClient(service=service)
    client.succeeded.connect(lambda kind, data: results.append(data))
    client.failed.connect(errors.append)
    timer.start()
    client.request(image)
    future = service.jobs[client.job_id][1]
    wait_for(lambda: bool(results) or bool(errors), timeout=10000)
    assert not errors, repr(future.exception())
    timer.stop()
    assert len(ticks) >= 2  # 模型加载和推理期间，主 Qt 事件循环仍在运行。
    assert [b["text"] for b in results[0]["blocks"]] == ["Hello world", "Keep your ideas"]
    assert results[0]["blocks"][0]["bbox"]
    service.shutdown()
