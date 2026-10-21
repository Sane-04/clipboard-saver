"""异步读取配置接口的模型元数据；不发送图片、历史或推理请求。"""
import json

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


def model_ids(data):
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("接口未返回模型列表，可继续手动填写模型名。")
    result = []
    for item in items:
        value = item.get("id") if isinstance(item, dict) else item if isinstance(item, str) else None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("接口返回的模型列表格式不正确，可继续手动填写。")
        result.append(value.strip())
    return sorted(set(result), key=str.casefold)


class ModelCatalogClient(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.manager = QNetworkAccessManager(self)
        self.reply = None
        self.serial = 0
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.timeout)

    def cancel(self):
        self.serial += 1
        self.timer.stop()
        if self.reply is not None:
            reply, self.reply = self.reply, None
            reply.abort()
            reply.deleteLater()
            self.busy_changed.emit(False)

    def timeout(self):
        self.cancel()
        self.failed.emit("读取模型列表超时，可重试或手动填写模型名。")

    def request(self, base_url, key, timeout=15):
        from api_settings import models_endpoint_url
        self.cancel()
        try:
            url = models_endpoint_url(base_url)
            if not key.strip():
                raise ValueError("请先填写 API Key，再读取模型列表。")
        except ValueError as error:
            self.failed.emit(str(error))
            return False
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"Authorization", ("Bearer " + key.strip()).encode("utf-8"))
        request.setRawHeader(b"Accept", b"application/json")
        # 列表请求也不跟随重定向，密钥只能发送到用户当前指定的接口。
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        self.reply = reply = self.manager.get(request)
        serial = self.serial
        reply.finished.connect(lambda: self.finished(reply, serial))
        self.timer.start(timeout * 1000)
        self.busy_changed.emit(True)
        return True

    def finished(self, reply, serial):
        if serial != self.serial or reply is not self.reply:
            return
        self.timer.stop()
        self.reply = None
        raw = bytes(reply.readAll())
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
        error = reply.error()
        reply.deleteLater()
        self.busy_changed.emit(False)
        if status >= 300:
            reason = {401: "密钥认证失败", 403: "账户无权限", 404: "接口未提供模型列表",
                      429: "请求限流"}.get(status, "服务暂时不可用或不支持模型列表")
            self.failed.emit(f"读取模型列表失败（HTTP {status}）：{reason}。可继续手动填写。")
            return
        if error != QNetworkReply.NetworkError.NoError:
            self.failed.emit("模型列表网络请求失败，请检查地址、网络和证书。可手动填写模型名。")
            return
        try:
            ids = model_ids(json.loads(raw))
        except (ValueError, TypeError) as error:
            self.failed.emit(str(error) if isinstance(error, ValueError) and not isinstance(
                error, json.JSONDecodeError) else "接口未返回有效模型列表，可继续手动填写。")
            return
        self.succeeded.emit(ids)
