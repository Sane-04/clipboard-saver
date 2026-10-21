"""分类数据位置、可发现的引导配置，以及不覆盖用户文件的迁移。"""
import hashlib
from contextlib import closing
import json
import shutil
import sqlite3
import traceback
from dataclasses import dataclass, fields
from pathlib import Path
from time import time_ns
from uuid import uuid4

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)
from ui_theme import STYLE

LOCATION_LABELS = {
    "history": "历史记录（文字与图片）",
    "config": "接口与快捷键配置",
    "models": "本地 OCR 模型",
    "backups": "数据备份",
    "logs": "运行日志",
    "exports": "截图与成品导出",
}
MODEL_FILES = ("PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx",
               "ch_ppocr_mobile_v2.0_cls_mobile.onnx")


@dataclass(frozen=True)
class DataLocations:
    history: Path
    config: Path
    models: Path
    backups: Path
    logs: Path
    exports: Path
    auto_export: bool = False

    @classmethod
    def defaults(cls, data_dir, model_dir=None):
        root = Path(data_dir).resolve()
        return cls(root, root, Path(model_dir).resolve() if model_dir else root / "ocr-models",
                   root / "backups", root, root / "exports")

    @classmethod
    def from_values(cls, values):
        paths = {}
        for name, label in LOCATION_LABELS.items():
            value = values.get(name)
            if not isinstance(value, (str, Path)) or not str(value).strip():
                raise ValueError(f"请选择“{label}”的文件夹。")
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError(f"“{label}”需要完整的文件夹路径。")
            if path.exists() and not path.is_dir():
                raise ValueError(f"“{label}”选择的是文件，请选择文件夹。")
            paths[name] = path.resolve()
        auto_export = values.get("auto_export", False)
        if type(auto_export) is not bool:
            raise ValueError("自动导出设置需要是开启或关闭。")
        return cls(**paths, auto_export=auto_export)

    def values(self):
        return {field.name: str(getattr(self, field.name)) if field.name != "auto_export"
                else self.auto_export for field in fields(self)}


class LocationStore(QObject):
    changed = Signal(object)

    def __init__(self, root, defaults=None):
        super().__init__()
        self.root = Path(root).resolve()
        self.path = self.root / "data-locations.json"
        self.defaults = defaults or DataLocations.defaults(self.root / "data")
        self.locations, self.error, self.apply_handler = self.defaults, "", None
        if self.path.exists():
            try:
                self.locations = DataLocations.from_values(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, AttributeError) as error:
                # 不悄悄回到空默认库；位置配置损坏时停止启动，原文件保留。
                raise RuntimeError("数据位置配置无法读取，原文件已保留。请检查 data-locations.json。") from error

    def save(self, locations):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".data-locations-{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(locations.values(), ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.locations = locations

    def apply(self, values):
        locations = DataLocations.from_values(values)
        if self.apply_handler:
            self.apply_handler(locations)
        else:
            self.save(locations)
            self.changed.emit(locations)


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def database_digest(connection):
    digest = hashlib.sha256()
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    digest.update(str(version).encode())
    for table, order in (("clips", "id"), ("clip_copy_days", "clip_id,copy_day"), ("history_settings", "name")):
        digest.update(table.encode())
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            for value in row:
                data = value if isinstance(value, bytes) else repr(value).encode("utf-8")
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
    return digest.hexdigest()


class LocationMigration:
    """只处理本工具的已知文件；创建的副本可回滚，原目录始终保留。"""
    def __init__(self, storage, old, new):
        self.storage, self.old, self.new = storage, old, new
        self.created, self.staged = [], []

    def _stage_file(self, source, target):
        source, target = Path(source), Path(target)
        if source.resolve() == target.resolve() or not source.is_file():
            return
        if target.exists():
            if target.is_file() and file_digest(source) == file_digest(target):
                return
            raise ValueError(f"目标已有不同的数据：{target.name}。请选择空文件夹，避免覆盖。")
        temporary = target.with_name(f".clipboard-migrate-{uuid4().hex}.tmp")
        self.staged.append((temporary, target))
        shutil.copy2(source, temporary)

    def prepare(self):
        try:
            for name in LOCATION_LABELS:
                directory = getattr(self.new, name)
                directory.mkdir(parents=True, exist_ok=True)
                # 在每个用户选择的目录实际检查写入，退出检查仅删除自己创建的临时文件。
                probe = directory / f".clipboard-write-{uuid4().hex}.tmp"
                try:
                    with probe.open("xb") as stream:
                        stream.write(b"clipboard-saver")
                finally:
                    probe.unlink(missing_ok=True)
            self._history()
            for name in ("api-settings.json", "shortcuts.json"):
                self._stage_file(self.old.config / name, self.new.config / name)
            for name in MODEL_FILES:
                self._stage_file(self.old.models / name, self.new.models / name)
            if self.old.models != self.new.models:
                staged_targets = {target for _, target in self.staged}
                if any(not (self.new.models / name).is_file() and self.new.models / name not in staged_targets
                       for name in MODEL_FILES):
                    raise ValueError("本地 OCR 模型不完整，原位置已保留。请先准备模型或选择完整的模型目录。")
            for pattern in ("before-*.sqlite3", "before-*-api-settings.json"):
                for source in self.old.backups.glob(pattern):
                    self._stage_file(source, self.new.backups / source.name)
            for name in ("last-error.txt", "launch-error.txt"):
                self._stage_file(self.old.logs / name, self.new.logs / name)
            # 数据库快照用 SQLite backup，不能复制仍在写入的文件。
            if any(getattr(self.old, name) != getattr(self.new, name) for name in LOCATION_LABELS):
                self.old.backups.mkdir(parents=True, exist_ok=True)
                backup = self.old.backups / f"before-location-change-{time_ns()}.sqlite3"
                with closing(sqlite3.connect(backup)) as target:
                    self.storage.connection.backup(target)
                self._stage_file(backup, self.new.backups / backup.name)
            for temporary, target in self.staged:
                # 排他创建：检查后的竞争也不覆盖其他进程刚写入的文件。
                with target.open("xb") as destination:
                    self.created.append(target)
                    with temporary.open("rb") as source:
                        shutil.copyfileobj(source, destination)
                temporary.unlink()
        except Exception:
            self.rollback()
            raise

    def _history(self):
        target = self.new.history / "clipboard.sqlite3"
        source = self.old.history / "clipboard.sqlite3"
        if source.resolve() == target.resolve():
            return
        if target.exists():
            try:
                with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as existing:
                    if existing.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or \
                            database_digest(existing) != database_digest(self.storage.connection):
                        raise ValueError("目标已有不同的历史记录，请选择空文件夹，避免覆盖。")
            except sqlite3.Error as error:
                raise ValueError("目标历史文件无法读取，请选择空文件夹。") from error
            return
        temporary = target.with_name(f".clipboard-migrate-{uuid4().hex}.tmp")
        self.staged.append((temporary, target))
        with closing(sqlite3.connect(temporary)) as connection:
            self.storage.connection.backup(connection)
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("历史迁移校验失败，原数据已保留。")

    def rollback(self):
        for temporary, _ in self.staged:
            temporary.unlink(missing_ok=True)
        for target in reversed(self.created):
            target.unlink(missing_ok=True)
        self.created.clear()


def write_png(image, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".clipboard-export-{uuid4().hex}.tmp"
    try:
        if not image.save(str(temporary), "PNG"):
            raise OSError("图片导出失败。")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def export_png(image, directory):
    return write_png(image, Path(directory) / f"剪贴收藏-{time_ns()}.png")


def write_exception_log(locations, exc_type, exc_value, exc_traceback):
    locations.logs.mkdir(parents=True, exist_ok=True)
    target = locations.logs / "last-error.txt"
    with target.open("w", encoding="utf-8") as log:
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=log)
    return target


class LocationSettingsDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("数据保存位置")
        self.setStyleSheet(STYLE)
        self.resize(780, 630)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        title = QLabel("选择各类数据的保存文件夹")
        title.setObjectName("panelTitle")
        layout.addWidget(title)
        note = QLabel("保存时复制当前历史、配置和模型到新位置，保留原文件。\n"
                      "目标已有不同数据时会停止迁移。导出位置用于之后另存或自动保存的 PNG。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body, form = QWidget(), QFormLayout()
        form.setSpacing(12)
        self.fields = {}
        for name, label in LOCATION_LABELS.items():
            field = QLineEdit(str(getattr(store.locations, name)))
            field.setToolTip(field.text())
            field.textChanged.connect(field.setToolTip)
            button = QPushButton("选择…")
            button.clicked.connect(lambda checked=False, key=name: self.browse(key))
            row = QHBoxLayout()
            row.addWidget(field, 1)
            row.addWidget(button)
            form.addRow(label, row)
            self.fields[name] = field
        body.setLayout(form)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.auto_export = QCheckBox("完成截图或图片编辑时，额外自动保存一份 PNG")
        self.auto_export.setChecked(store.locations.auto_export)
        layout.addWidget(self.auto_export)
        hint = QLabel("自动导出默认关闭，暂停记录时也暂停自动导出。\n"
                      "图片编辑器中的“另存为 PNG”可随时手动保存。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        restore = QPushButton("填入默认位置")
        restore.setObjectName("quiet")
        restore.clicked.connect(self.restore_defaults)
        layout.addWidget(restore)
        self.error = QLabel()
        self.error.setObjectName("error")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("迁移并保存")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primary")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def browse(self, name):
        directory = QFileDialog.getExistingDirectory(self, LOCATION_LABELS[name], self.fields[name].text())
        if directory:
            self.fields[name].setText(directory)
            self.fields[name].setToolTip(directory)

    def restore_defaults(self):
        for name, field in self.fields.items():
            field.setText(str(getattr(self.store.defaults, name)))
        self.auto_export.setChecked(False)

    def save(self):
        self.buttons.setEnabled(False)
        try:
            self.store.apply({**{name: field.text().strip() for name, field in self.fields.items()},
                              "auto_export": self.auto_export.isChecked()})
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
            self.error.setText(str(error) or "位置未保存，请检查文件夹权限和磁盘空间。")
            self.error.show()
        else:
            self.accept()
        finally:
            self.buttons.setEnabled(True)
