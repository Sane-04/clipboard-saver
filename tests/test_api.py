import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from api_client import ApiClient, parse_ocr, parse_translations, response_json
from api_settings import ApiSettings, SettingsDialog, SettingsStore, endpoint_url
from image_document import ImageDocument
from image_editor import ImageEditor


def wait_for(predicate, timeout=2500):
    deadline = time.monotonic() + timeout / 1000
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(20)
    assert predicate(), "异步请求未按预期结束"


@pytest.fixture
def server():
    state = {"status": 200, "delay": 0, "body": {"blocks": []}, "raw": None, "requests": [], "bodies": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            state["requests"].append(json.loads(data))
            body = state["bodies"].pop(0) if state["bodies"] else state["body"]
            time.sleep(state["delay"])
            data = state["raw"] if state["raw"] is not None else json.dumps(
                {"choices": [{"message": {"content": json.dumps(body)}}]}).encode()
            try:
                self.send_response(state["status"])
                self.send_header("Content-Type", "application/json")
                if state["status"] == 302:
                    self.send_header("Location", "http://127.0.0.1:1/never")
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{http.server_port}/v1"
    http.shutdown()
    http.server_close()


def test_dpapi_roundtrip_mask_clear_and_corrupt_preserved(qt_app, tmp_path):
    store = SettingsStore(tmp_path)
    fake_key = "test-only-dummy-credential"
    store.save(ApiSettings(key=fake_key))
    assert fake_key not in store.path.read_text(encoding="utf-8")
    loaded = SettingsStore(tmp_path)
    assert loaded.settings.key == fake_key
    dialog = SettingsDialog(loaded)
    assert dialog.key.echoMode() == dialog.key.EchoMode.Password
    dialog.key.clear()
    dialog.save()
    assert not SettingsStore(tmp_path).settings.key
    store.path.write_text("{broken", encoding="utf-8")
    assert SettingsStore(tmp_path).error
    assert store.path.read_text(encoding="utf-8") == "{broken"


def test_endpoints_and_data_validation():
    assert endpoint_url("https://example.com/v1/") == "https://example.com/v1/chat/completions"
    assert endpoint_url("https://example.com/v1/chat/completions") == "https://example.com/v1/chat/completions"
    with pytest.raises(ValueError):
        endpoint_url("http://remote.example/v1")
    blocks = parse_ocr({"blocks": [{"id": "1", "text": "Hello", "bbox": [.1, .2, .5, .4]}]}, (200, 100), (30, 40))
    assert blocks[0]["rect"] == (50, 60, 80, 20)
    assert parse_ocr({"blocks": [{"text": "x", "bbox": None}]}, (20, 10))[0]["rect"] is None
    for box in [[0, 0, 2, 1], [0, 0, 0, .5], [0, 0, float("nan"), 1]]:
        with pytest.raises(ValueError):
            parse_ocr({"blocks": [{"text": "x", "bbox": box}]}, (20, 10))
    with pytest.raises(ValueError):
        parse_translations({"translations": [{"id": "other", "text": "x"}]}, ["1"])
    assert response_json('```json\n{"ok":true}\n```') == {"ok": True}


def test_async_request_sends_only_rendered_crop_and_redaction(qt_app, server):
    state, url = server
    image = QImage(300, 100, QImage.Format.Format_RGBA8888)
    image.fill(QColor("#ffffff"))
    image.setPixelColor(40, 30, QColor("#ff0000"))
    doc = ImageDocument(image, (20, 10, 100, 60))
    doc.add([{"kind": "cover", "rect": (30, 20, 30, 30), "color": "#000000"}])
    client = ApiClient()
    results = []
    client.succeeded.connect(lambda kind, data: results.append(data))
    client.request(ApiSettings(base_url=url, key="dummy"), "ocr", "test", doc.render())
    QTest.qWait(10)
    assert not results  # 完成依赖事件循环，不是阻塞 UI 的同步调用。
    wait_for(lambda: bool(results))
    payload = state["requests"][0]
    value = payload["messages"][0]["content"][1]["image_url"]["url"]
    assert value.startswith("data:" + "image/png;base64,")
    sent = QImage.fromData(base64.b64decode(value.split(",", 1)[1]))
    assert (sent.width(), sent.height()) == (100, 60)
    assert sent.pixelColor(20, 20).name() == "#000000"
    assert payload["model"] == "gpt-5.6-luna"


@pytest.mark.parametrize("status", [401, 429, 500, 302])
def test_http_errors_and_redirect_not_followed(qt_app, server, status):
    state, url = server
    state["status"] = status
    client, errors, results = ApiClient(), [], []
    client.failed.connect(errors.append)
    client.succeeded.connect(lambda *args: results.append(args))
    client.request(ApiSettings(base_url=url, key="dummy"), "ocr", "test")
    wait_for(lambda: bool(errors))
    assert str(status) in errors[0]
    assert len(state["requests"]) == 1 and not results


def test_malformed_cancel_timeout_and_editor_revision(qt_app, server):
    state, url = server
    client, errors, results = ApiClient(), [], []
    client.failed.connect(errors.append)
    client.succeeded.connect(lambda *args: results.append(args))
    settings = ApiSettings(base_url=url, key="dummy")
    state["raw"] = b"{bad"
    client.request(settings, "ocr", "test")
    wait_for(lambda: bool(errors))
    state["raw"], state["delay"] = None, .25
    client.request(settings, "ocr", "test")
    QTest.qWait(40)
    client.cancel()
    QTest.qWait(350)
    assert not results
    errors.clear()
    client.request(settings, "ocr", "test")
    client.timer.start(35)
    wait_for(lambda: bool(errors))
    QTest.qWait(350)
    assert "超时" in errors[0] and not results
    image = QImage(200, 100, QImage.Format.Format_RGBA8888)
    image.fill(QColor("white"))
    editor = ImageEditor(image)
    editor.request_revision = editor.doc.revision
    editor.request_size, editor.request_offset = (200, 100), (0, 0)
    editor.doc.add([{"kind": "cover", "rect": (0, 0, 30, 30), "color": "#000000"}])
    editor.api_result("ocr", {"blocks": [{"text": "stale", "bbox": [0, 0, .5, .5]}]})
    assert not editor.blocks
    editor.cancel()
