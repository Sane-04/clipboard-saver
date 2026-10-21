"""仅用本机元数据服务验证模型下拉、安全边界及取消。"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PySide6.QtTest import QTest

from api_settings import ApiSettings, SettingsDialog, SettingsStore, models_endpoint_url
from model_catalog import ModelCatalogClient, model_ids
from test_api import wait_for


@pytest.fixture
def models_server():
    state = {"status": 200, "body": {"data": [{"id": "beta"}, {"id": "alpha"}, {"id": "beta"}]},
             "delay": 0, "raw": None, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, body, raw, delay = state["status"], state["body"], state["raw"], state["delay"]
            state["requests"].append((self.path, self.headers.get("Authorization"),
                                      self.headers.get("Content-Length")))
            time.sleep(delay)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if status == 302:
                    self.send_header("Location", "http://127.0.0.1:1/should-never-receive-key")
                self.end_headers()
                self.wfile.write(raw if raw is not None else json.dumps(body).encode())
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state, f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()
    server.server_close()


def test_models_url_normalization_and_transport_validation():
    assert models_endpoint_url("https://example.test/v1/") == "https://example.test/v1/models"
    assert models_endpoint_url("https://example.test/v1/chat/completions/") == \
        "https://example.test/v1/models"
    for url in ("http://example.test/v1", "https://user:pass@example.test/v1",
                "https://example.test/v1?key=bad"):
        with pytest.raises(ValueError):
            models_endpoint_url(url)
    assert model_ids({"data": ["b", {"id": "a"}, {"id": "b"}]}) == ["a", "b"]


@pytest.mark.parametrize("body", [{}, {"data": {}}, {"data": [None]}, {"data": [{"id": ""}]}])
def test_malformed_catalog_does_not_become_selectable_models(body):
    with pytest.raises(ValueError):
        model_ids(body)


def test_real_async_get_only_sends_metadata_request(qt_app, models_server):
    state, url = models_server
    state["delay"] = .1
    client, result = ModelCatalogClient(), []
    client.succeeded.connect(result.append)
    assert client.request(url, "synthetic-catalog-key")
    assert not result and client.reply is not None
    wait_for(lambda: bool(result))
    assert result == [["alpha", "beta"]]
    assert state["requests"] == [("/v1/models", "Bearer synthetic-catalog-key", None)]


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 302])
def test_catalog_http_errors_and_redirect_are_safe(qt_app, models_server, status):
    state, url = models_server
    state["status"] = status
    client, errors, result = ModelCatalogClient(), [], []
    client.failed.connect(errors.append)
    client.succeeded.connect(result.append)
    client.request(url, "synthetic-key")
    wait_for(lambda: bool(errors))
    assert str(status) in errors[0] and not result
    assert len(state["requests"]) == 1


def test_dialog_keeps_current_model_and_custom_input_without_saving_on_fetch(qt_app, tmp_path, models_server):
    state, url = models_server
    store = SettingsStore(tmp_path)
    store.save(ApiSettings(base_url=url, key="synthetic", model="current-private-alias"))
    before = store.path.read_bytes()
    dialog = SettingsDialog(store)
    dialog.show()
    wait_for(lambda: dialog.catalog.reply is None and len(state["requests"]) == 1)
    assert dialog.model.currentText() == "current-private-alias"
    assert [dialog.model.itemText(i) for i in range(dialog.model.count())] == [
        "current-private-alias", "alpha", "beta"]
    assert store.path.read_bytes() == before
    dialog.model.setCurrentText("my-custom-model")
    dialog.save()
    assert SettingsStore(tmp_path).settings.model == "my-custom-model"
    assert SettingsStore(tmp_path).settings.key == "synthetic"


def test_editing_credentials_cancels_stale_catalog(qt_app, tmp_path, models_server):
    state, url = models_server
    state["delay"] = .2
    store = SettingsStore(tmp_path)
    store.settings = ApiSettings(base_url=url, key="synthetic", model="kept")
    dialog = SettingsDialog(store)
    dialog.show()
    wait_for(lambda: bool(state["requests"]))
    dialog.key.setText("changed-synthetic-key")
    assert dialog.catalog.reply is None
    QTest.qWait(300)
    assert dialog.model.count() == 1 and dialog.model.currentText() == "kept"
    assert "重新获取" in dialog.model_note.text()
    assert not store.path.exists()
    dialog.reject()


def test_close_and_timeout_leave_no_late_results(qt_app, tmp_path, models_server):
    state, url = models_server
    state["delay"] = .2
    store = SettingsStore(tmp_path)
    store.settings = ApiSettings(base_url=url, key="synthetic")
    dialog = SettingsDialog(store)
    dialog.show()
    wait_for(lambda: bool(state["requests"]))
    dialog.reject()
    QTest.qWait(250)
    assert dialog.catalog.reply is None and dialog.model.count() == 1
    client, errors, result = ModelCatalogClient(), [], []
    client.failed.connect(errors.append)
    client.succeeded.connect(result.append)
    client.request(url, "synthetic")
    client.timer.start(20)
    wait_for(lambda: bool(errors))
    QTest.qWait(250)
    assert "超时" in errors[0] and not result


def test_unavailable_models_keep_manual_model_and_settings(qt_app, tmp_path, models_server):
    state, url = models_server
    state["status"] = 404
    store = SettingsStore(tmp_path)
    store.settings = ApiSettings(base_url=url, key="synthetic", model="kept")
    dialog = SettingsDialog(store)
    dialog.fetch_models()
    wait_for(lambda: "404" in dialog.model_note.text())
    assert dialog.model.isEditable() and dialog.model.currentText() == "kept"
    assert not store.path.exists()
    dialog.reject()
