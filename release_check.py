"""发布版自检：仅访问临时合成数据与本机模拟接口，不读取真实剪贴板。"""
import ctypes
import json
import sys
import traceback
from datetime import datetime
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from PySide6.QtCore import QDate, QEventLoop, QMimeData, QObject, QPoint, QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QImageReader, QPainter, QPixmap, QWheelEvent
from PySide6.QtNetwork import QSslSocket
from PySide6.QtTest import QTest

from app_paths import application_root, resource_root
from api_settings import ApiSettings
from data_locations import DataLocations, LOCATION_LABELS, LocationStore, database_digest, write_png
from local_ocr import MODEL_DIR
from screenshot import SelectionOverlay
from storage import HistoryStorage, image_fingerprint


class SyntheticClipboard(QObject):
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


def synthetic_image():
    image = QImage(800, 350, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    painter.setPen(QColor("#202633"))
    painter.setFont(QFont("Arial", 38))
    painter.drawText(QPoint(45, 105), "Hello world")
    painter.drawText(QPoint(45, 245), "Keep your ideas")
    painter.end()
    return image


def wait_for(predicate, milliseconds=45000):
    if predicate():
        return
    loop = QEventLoop()
    poll, timer = QTimer(), QTimer()
    poll.setInterval(20)
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    poll.start()
    timer.start(milliseconds)
    loop.exec()
    poll.stop()
    timer.stop()
    assert predicate(), "自检等待超时"


def ssl_modules():
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    process = kernel.GetCurrentProcess()
    api = ctypes.WinDLL("psapi")
    api.EnumProcessModulesEx.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    api.EnumProcessModulesEx.restype = wintypes.BOOL
    api.GetModuleFileNameExW.argtypes = [
        wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    api.GetModuleFileNameExW.restype = wintypes.DWORD
    modules, needed = (wintypes.HMODULE * 2048)(), wintypes.DWORD()
    assert api.EnumProcessModulesEx(process, modules, ctypes.sizeof(modules), ctypes.byref(needed), 3)
    assert needed.value <= ctypes.sizeof(modules)
    paths = []
    for module in modules[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
        name = ctypes.create_unicode_buffer(32768)
        if api.GetModuleFileNameExW(process, module, name, len(name)) and \
                Path(name.value).name.lower() in ("libssl-3-x64.dll", "libcrypto-3-x64.dll"):
            paths.append(name.value)
    return paths


class TranslationHandler(BaseHTTPRequestHandler):
    calls = 0
    mode = ""
    model_calls = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        assert self.path == "/v1/models"
        assert self.headers["Authorization"] == "Bearer synthetic-release-check"
        type(self).model_calls += 1
        body = json.dumps({"data": [{"id": "synthetic-model"}, {"id": "synthetic-vision"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/v1/chat/completions"
        assert self.headers["Authorization"] == "Bearer synthetic-release-check"
        prompt = payload["messages"][0]["content"][0]["text"]
        blocks = json.loads(prompt.split("Input blocks:\n", 1)[1])
        translations = [{"id": b["id"], "text": "你好，世界" if "Hello" in b["text"] else "留住灵感"}
                        for b in blocks]
        if self.mode == "always-empty" or self.mode == "recover" and "previous response" not in prompt:
            for block, translated in zip(blocks, translations):
                if "Keep" in block["text"]:
                    translated["text"] = ""
        type(self).calls += 1
        result = {"choices": [{"message": {"content": json.dumps({"translations": translations})}}]}
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_self_check(application, report_path):
    from app import ClipboardApplication
    from api_settings import SettingsStore

    report_path = Path(report_path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {"frozen": bool(getattr(sys, "frozen", False)), "executable": sys.executable,
              "application_root": str(application_root()), "resources": str(resource_root()),
              "models": str(MODEL_DIR), "synthetic_only": True, "system_clipboard_used": False,
              "platform": application.platformName(), "checks": {}}
    checks = report["checks"]
    controller = overlay = service = server = None
    try:
        with TemporaryDirectory(prefix="便携自检-", dir=report_path.parent) as temporary:
            scratch = Path(temporary)
            try:
                assert QSslSocket.supportsSsl()
                report["qt_openssl"] = QSslSocket.sslLibraryVersionString()
                import ssl
                report["python_openssl"] = ssl.OPENSSL_VERSION
                ssl.create_default_context()
                checks["ssl_available"] = True
                import requests
                from urllib3.util.ssl_ import IS_PYOPENSSL
                # 移除可选 Python TLS 后端后仍使用标准库的 SNI 和证书校验。
                assert ssl.HAS_SNI and not IS_PYOPENSSL
                assert requests.Session().verify is True
                checks["native_ssl_backend_retains_sni_and_certificate_verification"] = True
                for name in ("check.svg", "chevron-down.svg"):
                    reader = QImageReader(str(resource_root() / "assets" / name))
                    assert not reader.read().isNull(), reader.errorString()
                checks["svg_icons"] = True
                from long_capture import CaptureBar
                from long_stitch import VerticalStitcher
                import numpy as np
                pixels = np.random.default_rng(91).integers(0, 255, (1400, 240, 3), dtype=np.uint8)
                long_source = QImage(pixels.data, 240, 1400, 720, QImage.Format.Format_RGB888).copy()
                long_source.setDevicePixelRatio(1.5)
                stitcher = VerticalStitcher(long_source.copy(0, 0, 240, 400))
                for top in (100, 250, 440):
                    assert stitcher.add(long_source.copy(0, top, 240, 400)).status == "append"
                assert image_fingerprint(stitcher.render()) == image_fingerprint(long_source.copy(0, 0, 240, 840))
                bar = CaptureBar()
                assert bar.mode.currentData() == "manual"
                bar.deleteLater()
                checks["long_stitch_physical_pixels_and_manual_default"] = True
                short_stitcher = VerticalStitcher(long_source.copy(0, 0, 240, 80))
                for top in (25, 55, 90):
                    assert short_stitcher.add(long_source.copy(0, top, 240, 80)).status == "append"
                assert image_fingerprint(short_stitcher.render()) == image_fingerprint(
                    long_source.copy(0, 0, 240, 170))
                checks["short_viewport_overlap_is_pixel_exact"] = True
                from long_capture import LongScreenshotController
                from PySide6.QtWidgets import QMainWindow
                if application.platformName() == "windows":
                    import os
                    from external_windows import WindowsWindowBackend
                    from window_picker import WindowPinController
                    pin_probe = QMainWindow()
                    pin_controller = None
                    try:
                        pin_probe.setWindowTitle("合成文档窗口 · 置顶验收")
                        pin_probe.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
                        pin_probe.setGeometry(-20000, -20000, 420, 200)
                        pin_probe.show()
                        application.processEvents()
                        backend = WindowsWindowBackend(exclude_pid=0, allowed_pids={os.getpid()})
                        target_info = backend.query(int(pin_probe.winId()))
                        assert target_info is not None and not target_info.topmost
                        assert WindowsWindowBackend(exclude_pid=os.getpid()).query(int(pin_probe.winId())) is None
                        before_geometry = pin_probe.geometry()
                        pin_controller = WindowPinController(backend=backend)
                        pin_controller.choose(target_info)
                        wait_for(lambda: not pin_controller.pending)
                        assert backend.current(target_info).topmost
                        assert len(pin_controller.entries) == 1
                        next(iter(pin_controller.entries.values())).badge.click()
                        wait_for(lambda: not pin_controller.pending)
                        assert not backend.current(target_info).topmost
                        assert pin_probe.geometry() == before_geometry
                        pin_controller.choose(target_info)
                        wait_for(lambda: not pin_controller.pending)
                        pin_controller.close()
                        wait_for(lambda: not backend.current(target_info).topmost)
                        checks["window_pin_badge_cancel_identity_and_geometry"] = True
                        checks["window_pin_exit_restores_owned_topmost"] = True
                    finally:
                        if pin_controller is not None:
                            pin_controller.close()
                        pin_probe.close()
                class SyntheticScrollSource:
                    top = 0
                    scrolls = 0
                    def capture(self):
                        if long_controller.preview is not None:
                            bands = long_controller.outline.bands if long_controller.outline is not None else []
                            for widget in (long_controller.bar, long_controller.preview, *bands):
                                if widget is not None and not getattr(widget, "capture_excluded", False) and \
                                    (widget.geometry() if widget in bands else
                                    widget.frameGeometry().adjusted(-2, -2, 2, 2)).intersects(
                                        long_controller.payload[0]):
                                    assert not widget.isVisible(), "Preview leaked into capture"
                        return long_source.copy(0, self.top, 240, 400)
                    def scroll(self):
                        self.scrolls += 1
                        self.top += 50
                target = QMainWindow()
                target.show_history = target.show
                captured = []
                synthetic_source = SyntheticScrollSource()
                long_controller = LongScreenshotController(target, captured.append,
                    source_factory=lambda *args: synthetic_source)
                try:
                    long_controller.begin((QRect(0, 0, 240, 400), (0, 0, 240, 400), 1,
                                           QRect(0, 0, 900, 700)))
                    wait_for(lambda: long_controller.stitcher is not None)
                    long_controller.timer.stop()
                    assert long_controller.preview.isVisible()
                    report["capture_exclusion_available"] = all(
                        getattr(widget, "capture_excluded", False)
                        for widget in (long_controller.bar, long_controller.preview, *long_controller.outline.bands))
                    assert all(band.isVisible() for band in long_controller.outline.bands)
                    assert all(band.windowFlags() & Qt.WindowType.WindowTransparentForInput
                               for band in long_controller.outline.bands)
                    assert long_controller.preview.canvas.image_height == 400
                    long_controller.preview.move(8, 8)
                    synthetic_source.top = 150
                    long_controller.capture_frame(long_controller.serial)
                    wait_for(lambda: long_controller.stitcher.height == 550)
                    long_controller.timer.stop()
                    assert long_controller.preview.canvas.image_height == 550
                    assert long_controller.preview.canvas.latest_start == 400
                    assert image_fingerprint(long_controller.preview.live_image) == image_fingerprint(
                        synthetic_source.capture())
                    checks["preview_current_frame_matches_region"] = True
                    wait_for(lambda: long_controller.preview.scroll.verticalScrollBar().value() ==
                             long_controller.preview.scroll.verticalScrollBar().maximum())
                    assert synthetic_source.scrolls == 0 and not captured
                    long_controller.finish()
                    wait_for(lambda: long_controller.editor is not None)
                    assert long_controller.preview is None
                    assert long_controller.outline is None
                    assert long_controller.editor.scroll.y() > long_controller.editor.tools.geometry().bottom()
                    assert not captured
                    long_editor = long_controller.editor
                    long_before = image_fingerprint(long_editor.doc.render())
                    long_editor.set_zoom(1)
                    application.processEvents()
                    for delta, expected_scale in ((120, 1.2), (-120, 1)):
                        point = QPoint(90, 110)
                        event = QWheelEvent(QPointF(point), QPointF(long_editor.canvas.mapToGlobal(point)),
                            QPoint(), QPoint(0, delta), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.ControlModifier, Qt.ScrollPhase.NoScrollPhase, False)
                        application.sendEvent(long_editor.canvas, event)
                        application.processEvents()
                        assert abs(long_editor.zoom_factor-expected_scale) < 1e-6
                    assert image_fingerprint(long_editor.doc.render()) == long_before
                    checks["capture_outline_click_through_hidden_at_screen_edges_and_disposed"] = True
                    checks["capture_overlay_exclusion_or_safe_hidden_fallback"] = True
                    checks["ctrl_wheel_zoom_preserves_original_pixels"] = True
                    long_controller.editor.commit()
                    assert len(captured) == 1 and captured[0].height() == 550
                    checks["long_manual_controller_edit_then_commit"] = True
                    checks["long_live_preview_growth_follow_hidden_during_capture_and_cleanup"] = True
                    from screenshot import ScreenshotController, SelectionOverlay
                    from PySide6.QtGui import QPixmap, QPainter
                    synthetic_source.top = 0
                    captured.clear()
                    snapshot = QImage(600, 500, QImage.Format.Format_RGB32)
                    snapshot.fill(QColor("white"))
                    first = synthetic_source.capture()
                    first.setDevicePixelRatio(1)
                    painter = QPainter(snapshot)
                    painter.drawImage(30, 40, first)
                    painter.end()
                    ordinary = ScreenshotController(target, captured.append)
                    ordinary.screen = object()
                    ordinary.active = True
                    ordinary.overlay = SelectionOverlay(QPixmap.fromImage(snapshot), QRect(0, 0, 600, 500))
                    ordinary.overlay.doc.crop = (30, 40, 240, 400)
                    ordinary.overlay.doc.add([{"kind": "cover", "rect": (45, 60, 50, 20), "color": "#000000"}])
                    ordinary.overlay.long_capture_requested.connect(ordinary.start_long_capture)
                    ordinary.on_long_capture = long_controller.start_from_screenshot
                    ordinary.overlay.long_capture_button.click()
                    wait_for(lambda: long_controller.stitcher is not None and not long_controller.busy)
                    long_controller.timer.stop()
                    assert not long_controller.selector.active and long_controller.selector.overlay is None
                    assert long_controller.payload[1] == (30, 40, 240, 400)
                    synthetic_source.top = 100
                    long_controller.capture_frame(long_controller.serial)
                    wait_for(lambda: long_controller.stitcher.height == 500)
                    long_controller.timer.stop()
                    long_controller.finish()
                    wait_for(lambda: long_controller.editor is not None)
                    assert long_controller.editor.doc.objects[0]["rect"] == (15, 20, 50, 20)
                    long_controller.editor.long_capture_button.click()
                    wait_for(lambda: long_controller.stitcher is not None and not long_controller.busy)
                    assert long_controller.source is synthetic_source
                    long_controller.timer.stop()
                    assert long_controller.preview.canvas.image_height == 500
                    synthetic_source.top = 200
                    long_controller.capture_frame(long_controller.serial)
                    wait_for(lambda: long_controller.stitcher.height == 600)
                    long_controller.timer.stop()
                    long_controller.finish()
                    wait_for(lambda: long_controller.editor is not None)
                    assert not captured
                    long_controller.editor.commit()
                    assert len(captured) == 1 and captured[0].height() == 600
                    assert not ordinary.active and ordinary.overlay is None
                    checks["screenshot_toolbar_reuses_region_and_continues_with_layers"] = True
                finally:
                    long_controller.stop()
                    target.close()

                from history_settings import DateCleanupDialog
                stamp = lambda value: int(datetime.fromisoformat(value).timestamp())*1_000_000_000
                old_time = [stamp("2026-10-01T12:00:00")]
                date_store = HistoryStorage(scratch / "日期清理", clock=lambda: old_time[0])
                try:
                    old = date_store.add_text("仅旧日")
                    shared = date_store.add_text("跨日")
                    protected = date_store.add_image(long_source.copy(0, 0, 240, 400))
                    date_store.set_mark(protected, True, "保护")
                    old_time[0] = stamp("2026-10-02T00:00:00")
                    boundary = date_store.add_text("截止日")
                    date_store.touch(shared)
                    dialog = DateCleanupDialog(date_store, date_store.delete_before)
                    dialog.cutoff.setDate(QDate(2026, 10, 2))
                    assert dialog.stats["records"] == 1 and dialog.stats["days"] == 2
                    dialog.reject()
                    assert date_store.counts()["total"] == 4
                    result = date_store.delete_before("2026-10-02")
                    assert result["records"] == 1 and date_store.get_record(old) is None
                    assert all(date_store.get_record(value) is not None
                               for value in (shared, protected, boundary))
                    assert {row["id"] for row in date_store.list_records(start="2026-10-01")} == {protected}
                    assert date_store.delete_before("2026-10-02", True)["images"] == 1
                    assert not date_store.connection.execute("PRAGMA foreign_key_check").fetchall()
                    checks["date_cleanup_boundary_shared_content_protection_and_cancel"] = True
                finally:
                    date_store.close()

                initial = DataLocations.defaults(scratch / "初始数据", MODEL_DIR)
                route = LocationStore(scratch / "启动配置", initial)
                clipboard = SyntheticClipboard()
                controller = ClipboardApplication(application, initial.history,
                    enable_hotkeys=False, enable_tray=False, clipboard=clipboard,
                    location_store=route)
                assert controller.storage.counts()["total"] == 0
                assert not controller.settings_store.settings.key
                checks["clean_first_start"] = True
                controller.window.show_history()
                history_hwnd = int(controller.window.winId())
                controller.window.topmost_button.click()
                assert controller.window_pins.selecting
                controller.window_pins.cancel()
                history_target = controller.window_pins.backend.query(history_hwnd)
                controller.window_pins.choose(history_target)
                wait_for(lambda: not controller.window_pins.pending)
                if application.platformName() == "windows":
                    api = ctypes.WinDLL("user32")
                    api.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
                    api.GetWindowLongPtrW.restype = ctypes.c_ssize_t
                    assert api.GetWindowLongPtrW(history_hwnd, -20) & 8
                controller.window.hide_window()
                controller.window.show_history()
                if application.platformName() == "windows":
                    assert api.GetWindowLongPtrW(history_hwnd, -20) & 8
                controller.window_pins.unpin(history_target)
                wait_for(lambda: not controller.window_pins.pending)
                assert int(controller.window.winId()) == history_hwnd
                if application.platformName() == "windows":
                    assert not api.GetWindowLongPtrW(history_hwnd, -20) & 8
                checks["history_topmost_keeps_native_handle_and_hotkeys"] = True
                text = controller.storage.add_text("合成记录：便携版、日期和特殊标记。")
                controller.storage.set_mark(text, True, "发布自检")
                image = synthetic_image()
                record = controller.storage.add_image(image)
                controller.window.refresh(select_id=record)
                QTest.mouseDClick(controller.window.image_preview, Qt.MouseButton.LeftButton,
                                 pos=controller.window.image_preview.rect().center())
                assert len(controller.window.image_viewers) == 1
                viewer = controller.window.image_viewers[0]
                assert viewer.parentWidget() is None
                viewer.topmost_button.click()
                assert controller.window_pins.selecting
                controller.window_pins.cancel()
                viewer_target = controller.window_pins.backend.query(int(viewer.winId()))
                controller.window_pins.choose(viewer_target)
                wait_for(lambda: not controller.window_pins.pending)
                if application.platformName() == "windows":
                    assert api.GetWindowLongPtrW(int(viewer.winId()), -20) & 8
                    assert not api.GetWindowLongPtrW(history_hwnd, -20) & 8
                controller.window.close()
                assert not controller.window.isVisible() and viewer.isVisible()
                viewer.set_fit(False)
                assert viewer.surface.size() == image.size()
                application.processEvents()
                point = QPoint(200, 150)
                viewport = viewer.scroll.viewport()
                origin = QPointF(viewer.surface.mapTo(viewport, QPoint()))
                anchor_pixel = (QPointF(point) - origin) / viewer.zoom_factor
                for delta, scale in ((120, 1.2), (-120, 1)):
                    event = QWheelEvent(QPointF(point), QPointF(viewport.mapToGlobal(point)), QPoint(),
                        QPoint(0, delta), Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
                    application.sendEvent(viewport, event)
                    application.processEvents()
                    assert abs(viewer.zoom_factor - scale) < 1e-6
                    mapped_pixel = (QPointF(point) - QPointF(viewer.surface.mapTo(viewport, QPoint()))) / scale
                    assert (mapped_pixel - anchor_pixel).manhattanLength() < 2
                viewer.set_zoom(2)
                application.processEvents()
                viewer.scroll.verticalScrollBar().setValue(100)
                before_scroll = viewer.scroll.verticalScrollBar().value()
                event = QWheelEvent(QPointF(point), QPointF(viewport.mapToGlobal(point)), QPoint(),
                    QPoint(0, -120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                    Qt.ScrollPhase.NoScrollPhase, False)
                application.sendEvent(viewport, event)
                application.processEvents()
                assert viewer.scroll.verticalScrollBar().value() > before_scroll and viewer.zoom_factor == 2
                assert image_fingerprint(viewer.surface.image) == image_fingerprint(image)
                assert viewer_target.identity in controller.window_pins.entries
                assert viewer.grab().save(str(report_path.parent / (report_path.stem + "-图片预览缩放.png")))
                checks["image_viewer_ctrl_wheel_anchor_plain_scroll_and_original_pixels"] = True
                viewer.set_fit(False)
                assert viewer.grab().save(str(report_path.parent / (report_path.stem + "-独立图片窗口.png")))
                viewer.close()
                controller.window.show_history()
                assert not controller.window.image_viewers
                checks["double_click_independent_image_viewer_stays_when_history_closes"] = True
                checks["image_viewer_topmost_independent_and_original_pixels_retained"] = True

                from history_edit import TextRecordDialog
                before_text = controller.storage.get_record(text)
                before_days = [tuple(r) for r in controller.storage.connection.execute(
                    "SELECT * FROM clip_copy_days WHERE clip_id=?", (text,))]
                text_dialog = TextRecordDialog(before_text["text"], lambda value: controller.storage.update_text(
                    text, value, expected=before_text["fingerprint"]), controller.window)
                text_dialog.text.setPlainText("合成记录：已手动更新，日期和标记保留。")
                text_dialog.save()
                edited = controller.storage.get_record(text)
                assert edited["tag"] == "发布自检" and edited["marked"] == 1
                assert edited["updated_at"] == before_text["updated_at"]
                assert before_days == [tuple(r) for r in controller.storage.connection.execute(
                    "SELECT * FROM clip_copy_days WHERE clip_id=?", (text,))]
                probe = controller.storage.add_image(image.copy(0, 0, 120, 50))
                controller.window.refresh(select_id=probe)
                controller.edit_image()
                probe_editor = controller.editor
                probe_editor.doc.add([{"kind": "cover", "rect": (2, 2, 20, 20), "color": "#000000"}])
                probe_editor.save_mode.setCurrentIndex(probe_editor.save_mode.findData("update"))
                probe_editor.commit()
                wait_for(lambda: controller.editor is None, 3000)
                assert QImage.fromData(controller.storage.get_record(probe)["image"]).pixelColor(5, 5) == QColor("black")
                assert not clipboard.mimeData().hasImage() and not clipboard.mimeData().hasText()
                controller.storage.delete(probe)
                checks["manual_text_and_image_updates_keep_ids_dates_and_metadata_without_copying"] = True
                controller.window.refresh(select_id=record)
                controller.edit_image("text")
                editor = controller.editor
                service = editor.local_client.service
                wait_for(lambda: editor.request_active or bool(editor.blocks))
                wait_for(lambda: not editor.request_active)
                assert [b["text"] for b in editor.blocks] == ["Hello world", "Keep your ideas"], \
                    [b["text"] for b in editor.blocks]
                report["recognized_text"] = [b["text"] for b in editor.blocks]
                checks["real_rapidocr"] = True
                original = image_fingerprint(editor.doc.render())
                assert original == image_fingerprint(image)
                checks["extract_does_not_translate"] = True
                editor.full_text_button.click()
                full_text = editor.full_text_dialog
                assert full_text.source_text.toPlainText() == "Hello world\nKeep your ideas"
                assert full_text.source_text.textCursor().hasSelection()
                controller.recorder.set_paused(True)
                full_text.copy_button.click()
                assert clipboard.mimeData().text() == "Hello world\nKeep your ideas"
                full_text.block_list.item(0).setCheckState(Qt.CheckState.Unchecked)
                full_text.copy_button.click()
                assert clipboard.mimeData().text() == "Keep your ideas"
                assert all(block["apply"] for block in editor.blocks)
                full_text.scope_buttons[2].click()
                assert full_text.source_text.toPlainText() == "Hello world"
                full_text.scope_buttons[0].click()
                full_text.select_all_button.click()
                assert full_text.grab().save(str(report_path.parent / (report_path.stem + "-全文选择.png")))
                controller.recorder.set_paused(False)
                checks["ocr_full_text_select_all_and_independent_copy_scope"] = True
                full_text.close()

                server = ThreadingHTTPServer(("127.0.0.1", 0), TranslationHandler)
                TranslationHandler.calls, TranslationHandler.mode = 0, ""
                TranslationHandler.model_calls = 0
                Thread(target=server.serve_forever, daemon=True).start()
                controller.settings_store.save(ApiSettings(
                    base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    model="synthetic-model", key="synthetic-release-check"))
                from api_settings import SettingsDialog
                models_dialog = SettingsDialog(controller.settings_store, editor)
                models_dialog.show()
                wait_for(lambda: models_dialog.catalog.reply is None and models_dialog.model.count() == 2)
                assert TranslationHandler.model_calls == 1
                assert models_dialog.model.currentText() == "synthetic-model"
                assert models_dialog.model.findText("synthetic-vision") >= 0
                assert models_dialog.grab().save(str(report_path.parent / (report_path.stem + "-模型下拉设置.png")))
                models_dialog.reject()
                checks["models_dropdown_real_local_get_preserves_current_model"] = True
                edit_pixels, edit_revision = image_fingerprint(editor.doc.render()), editor.doc.revision
                editor_hwnd = int(editor.winId())
                editor.topmost_button.click()
                assert controller.window_pins.selecting
                controller.window_pins.cancel()
                editor_target = controller.window_pins.backend.query(editor_hwnd)
                controller.window_pins.choose(editor_target)
                wait_for(lambda: not controller.window_pins.pending)
                if application.platformName() == "windows":
                    assert api.GetWindowLongPtrW(editor_hwnd, -20) & 8
                controller.window_pins.unpin(editor_target)
                wait_for(lambda: not controller.window_pins.pending)
                assert int(editor.winId()) == editor_hwnd
                assert image_fingerprint(editor.doc.render()) == edit_pixels and editor.doc.revision == edit_revision
                checks["editor_topmost_preserves_handle_and_document"] = True
                editor.translate_button.click()
                wait_for(lambda: not editor.request_active)
                assert TranslationHandler.calls == 1
                assert len(editor.doc.objects) == 2, editor.panel_note.text()
                assert all(obj["kind"] == "translation" for obj in editor.doc.objects)
                assert image_fingerprint(editor.doc.render()) != original
                write_png(editor.doc.render(), report_path.parent / (report_path.stem + "-合成译文.png"))
                assert editor.grab().save(str(report_path.parent / (report_path.stem + "-编辑器.png")))
                checks["mock_api_translation_and_image_replacement"] = True
                editor.full_text_button.click()
                full_text = editor.full_text_dialog
                assert full_text.translated_text.toPlainText() == "\n".join(b["translated"] for b in editor.blocks)
                full_text.block_list.item(0).setCheckState(Qt.CheckState.Unchecked)
                full_text.tabs.setCurrentIndex(1)
                controller.recorder.set_paused(True)
                full_text.copy_button.click()
                assert clipboard.mimeData().text() == editor.blocks[1]["translated"]
                controller.recorder.set_paused(False)
                full_text.close()
                checks["full_text_translation_copy_respects_independent_selection"] = True
                editor.undo()
                assert image_fingerprint(editor.doc.render()) == original
                TranslationHandler.mode = "recover"
                editor.translate_button.click()
                wait_for(lambda: not editor.request_active)
                assert TranslationHandler.calls == 3 and len(editor.doc.objects) == 2
                assert not editor.translation_retry_ids
                checks["empty_translation_retried_once_and_merged_by_id"] = True

                editor.undo()
                assert image_fingerprint(editor.doc.render()) == original
                TranslationHandler.mode = "always-empty"
                editor.translate_button.click()
                wait_for(lambda: not editor.request_active)
                assert TranslationHandler.calls == 5 and len(editor.doc.objects) == 1
                failed = next(b for b in editor.blocks if b["id"] in editor.translation_retry_ids)
                rect = tuple(map(int, failed["rect"]))
                assert image_fingerprint(editor.doc.render().copy(*rect)) == image_fingerprint(image.copy(*rect))
                assert editor.translate_button.text() == "重试未完成" and "未完成" in editor.panel_note.text()
                assert editor.grab().save(str(report_path.parent / (report_path.stem + "-补译未完成.png")))
                checks["partial_translation_preserves_failed_pixels_and_marks_pending"] = True
                TranslationHandler.mode = ""
                editor.translate_button.click()
                wait_for(lambda: not editor.request_active)
                assert TranslationHandler.calls == 6 and len(editor.doc.objects) == 2
                assert not editor.translation_retry_ids
                checks["manual_retry_only_pending_blocks"] = True
                editor.commit()
                wait_for(lambda: controller.editor is None, 3000)
                assert controller.editor is None
                assert controller.storage.counts()["total"] == 3
                assert clipboard.mimeData().hasImage()
                checks["explicit_commit"] = True

                # 截图编辑器使用合成底图。拖选、松开、重新调整和标注都不抓真实桌面。
                overlay = SelectionOverlay(QPixmap.fromImage(image), QRect(0, 0, 800, 350))
                overlay.show()
                canvas = overlay.canvas
                QTest.mousePress(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(25, 25))
                QTest.mouseMove(canvas, QPoint(680, 300), 15)
                QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(680, 300))
                assert overlay.doc.crop and not overlay.finished
                crop = overlay.doc.crop
                overlay.doc.crop = (crop[0] + 10, crop[1] + 10, crop[2] - 20, crop[3] - 20)
                overlay.doc.add([{"kind": "cover", "rect": (55, 70, 75, 45), "color": "#000000"}])
                written = []
                overlay.selected.connect(written.append)
                overlay.commit()
                assert len(written) == 1
                assert written[0].pixelColor(25, 40) == QColor("#000000")
                checks["synthetic_screenshot_adjust_annotate_commit"] = True

                before = database_digest(controller.storage.connection)
                target = DataLocations(**{name: scratch / "自选位置 含空格" / name
                                          for name in LOCATION_LABELS}, auto_export=True)
                controller.apply_locations(target)
                assert database_digest(controller.storage.connection) == before
                assert SettingsStore(target.config).settings.key == "synthetic-release-check"
                assert LocationStore(route.root).locations == target
                checks["all_data_locations_and_dpapi"] = True
                assert controller.storage.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                controller.cleanup()
                controller = None
                with_store = HistoryStorage(target.history, backup_dir=target.backups)
                try:
                    assert database_digest(with_store.connection) == before
                    assert with_store.get_record(text)["tag"] == "发布自检"
                finally:
                    with_store.close()
                checks["persistence_and_history_integrity"] = True
                modules = ssl_modules()
                report["ssl_modules"] = modules
                assert len(modules) == 2, modules
                if report["frozen"]:
                    assert all(Path(path).is_relative_to(resource_root()) for path in modules)
                    import cv2
                    import numpy
                    import onnxruntime
                    for module in (cv2, numpy, onnxruntime):
                        assert Path(module.__file__).is_relative_to(resource_root()), module.__file__
                    checks["dependencies_loaded_from_bundle"] = True
                checks["matched_ssl_after_ocr"] = True
            finally:
                if controller:
                    controller.cleanup()
                if overlay and not overlay.finished:
                    overlay.cancel()
                if service:
                    service.shutdown()
                if server:
                    server.shutdown()
                    server.server_close()
        report["passed"] = True
    except Exception:
        report["passed"] = False
        report["error"] = traceback.format_exc()
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1
