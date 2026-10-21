"""可修改的接口配置；真实密钥仅使用当前 Windows 用户的 DPAPI 保存。"""

import base64
import ctypes
import json
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit
from ui_theme import STYLE

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QVBoxLayout,
)


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data, decrypt=False):
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("Windows 无法读取或加密此用户的 API 密钥。")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        # DPAPI 输出由 LocalAlloc 分配，必须用 LocalFree 释放。
        kernel = ctypes.WinDLL("kernel32")
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(target.data)


def endpoint_url(base_url):
    url = base_url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.netloc or parts.username or parts.password:
        raise ValueError("请输入有效的 http/https API 地址，地址中不能包含用户名或密码。")
    if parts.query or parts.fragment:
        raise ValueError("API 地址不能包含查询参数或锚点。")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("远程接口需要 https，避免密钥以明文传输。")
    return url if parts.path.endswith("/chat/completions") else url + "/chat/completions"


def models_endpoint_url(base_url):
    # 同时兼容 Base URL 与已填写完整 chat/completions 的旧设置。
    return endpoint_url(base_url).removesuffix("/chat/completions") + "/models"


@dataclass
class ApiSettings:
    base_url: str = "https://s2api.top/v1"
    model: str = "gpt-5.6-luna"
    timeout: int = 90
    language: str = "简体中文"
    key: str = ""
    ocr_provider: str = "local"


class SettingsStore:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "api-settings.json"
        self.settings = ApiSettings()
        self.error = ""
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.settings = ApiSettings(
                    base_url=saved["base_url"], model=saved["model"],
                    timeout=int(saved["timeout"]), language=saved["language"],
                    key=dpapi(base64.b64decode(saved["protected_key"]), True).decode("utf-8")
                    if saved.get("protected_key") else "",
                    ocr_provider=saved.get("ocr_provider", "local"))
            except (OSError, ValueError, KeyError, TypeError):
                self.error = "API 配置读取失败，原文件已保留。请在 API 设置中重新保存配置。"

    def save(self, settings):
        endpoint_url(settings.base_url)
        if not settings.model.strip() or not settings.language.strip():
            raise ValueError("模型和目标语言不能为空。")
        if settings.ocr_provider not in ("local", "api"):
            raise ValueError("请选择本地 OCR 或 API OCR。")
        saved = asdict(settings)
        saved.pop("key")
        saved["protected_key"] = base64.b64encode(dpapi(settings.key.encode("utf-8"))).decode("ascii") \
            if settings.key else ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        # 单文件原子替换：保存失败不覆盖旧配置，也不把密钥明文写到磁盘。
        temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self.settings = settings
        self.error = ""


class SettingsDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("文字识别与 API 设置 · 剪贴收藏")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)
        title = QLabel("文字识别与翻译")
        title.setObjectName("heading")
        layout.addWidget(title)
        note = QLabel("RapidOCR 在本机免费识别。点击翻译时发送文本；\n"
                      "选择 API 识别时发送当前选区图片。密钥在本机加密保存。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        form.setSpacing(12)
        settings = store.settings
        self.ocr_provider = QComboBox()
        self.ocr_provider.addItem("RapidOCR · 本地免费识别", "local")
        self.ocr_provider.addItem("API · 图文模型识别", "api")
        self.ocr_provider.setCurrentIndex(self.ocr_provider.findData(settings.ocr_provider))
        form.addRow("文字识别", self.ocr_provider)
        self.base = QLineEdit(settings.base_url)
        self.model = QComboBox()
        self.model.setObjectName("modelChoice")
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.addItem(settings.model)
        self.model.setMaxVisibleItems(14)
        self.model.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.model.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.key = QLineEdit(settings.key)
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.language = QLineEdit(settings.language)
        self.timeout = QSpinBox()
        self.timeout.setRange(10, 300)
        self.timeout.setValue(settings.timeout)
        self.timeout.setSuffix(" 秒")
        model_row = QHBoxLayout()
        model_row.addWidget(self.model, 1)
        self.refresh_models = QPushButton("获取模型")
        self.refresh_models.clicked.connect(self.toggle_models_request)
        model_row.addWidget(self.refresh_models)
        for label, field in [("Base URL", self.base), ("API Key", self.key)]:
            form.addRow(label, field)
        form.addRow("模型", model_row)
        for label, field in [
                             ("默认目标语言", self.language), ("请求超时", self.timeout)]:
            form.addRow(label, field)
        layout.addLayout(form)
        self.model_note = QLabel("可从接口获取模型，也可以直接输入模型名。")
        self.model_note.setObjectName("muted")
        self.model_note.setWordWrap(True)
        layout.addWidget(self.model_note)
        from model_catalog import ModelCatalogClient
        self.catalog = ModelCatalogClient(self)
        self.catalog.succeeded.connect(self.models_loaded)
        self.catalog.failed.connect(self.model_note.setText)
        self.catalog.busy_changed.connect(
            lambda busy: self.refresh_models.setText("取消读取" if busy else "刷新模型"))
        self.base.textChanged.connect(self.invalidate_models)
        self.key.textChanged.connect(self.invalidate_models)
        self.auto_fetched = False
        clear = QPushButton("清除密钥")
        clear.setObjectName("quiet")
        clear.clicked.connect(self.key.clear)
        layout.addWidget(clear)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                  QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if store.error:
            layout.addWidget(QLabel(store.error))

    def save(self):
        try:
            self.store.save(ApiSettings(self.base.text().strip(), self.model.currentText().strip(),
                                        self.timeout.value(), self.language.text().strip(),
                                        self.key.text().strip(), self.ocr_provider.currentData()))
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "设置未保存", str(error))
            return
        self.accept()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.auto_fetched and self.key.text().strip():
            self.auto_fetched = True
            QTimer.singleShot(0, self.fetch_models_if_visible)

    def fetch_models_if_visible(self):
        if self.isVisible():
            self.fetch_models()

    def fetch_models(self):
        self.model_note.setText("正在从当前接口读取模型列表…")
        self.catalog.request(self.base.text(), self.key.text(), min(self.timeout.value(), 20))

    def toggle_models_request(self):
        if self.catalog.reply is not None:
            self.catalog.cancel()
            self.model_note.setText("已取消读取，当前模型保持，可手动填写。")
        else:
            self.fetch_models()

    def invalidate_models(self, *_):
        # 编辑地址或密钥时，旧接口的在途结果与候选列表立即失效；当前填写值保留。
        self.catalog.cancel()
        current = self.model.currentText()
        self.model.clear()
        if current:
            self.model.addItem(current)
        self.model.setCurrentText(current)
        self.model_note.setText("接口配置已改变，请重新获取模型；也可直接填写模型名。")

    def models_loaded(self, ids):
        current = self.model.currentText()
        self.model.clear()
        if current and current not in ids:
            self.model.addItem(current)
            self.model.setItemData(0, "当前填写的模型，未包含在接口返回的列表中。", Qt.ItemDataRole.ToolTipRole)
        self.model.addItems(ids)
        self.model.setCurrentText(current)
        self.model_note.setText(
            f"接口返回 {len(ids)} 个模型，可下拉选择或输入关键词。模型是否支持图片由接口决定。"
            if ids else "接口返回空模型列表，可继续手动填写模型名。")

    def done(self, result):
        self.catalog.cancel()
        super().done(result)
