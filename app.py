"""剪贴收藏入口。普通启动使用项目 data/，测试可指定独立 --data-dir。"""

from startup_runtime import prepare_runtime
try:
    prepare_runtime()
except Exception:
    if __name__ == "__main__":
        import ctypes
        import sys
        ctypes.windll.user32.MessageBoxW(
            None, "剪贴收藏的运行环境无法初始化。\n请完整解压便携包并保留 _internal 文件夹。"
            if getattr(sys, "frozen", False) else
            "剪贴收藏的运行环境无法初始化。\n请使用原启动入口，或运行“调试启动.cmd”检查 sane 环境。",
            "剪贴收藏", 0x10,
        )
    raise

import argparse
import ctypes
import sqlite3
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from clipboard_capture import ClipboardRecorder
from api_settings import SettingsDialog, SettingsStore
from image_editor import ImageEditor
from screenshot import ScreenshotController
from long_capture import LongScreenshotController
from storage import HistoryStorage
from ui import HistoryWindow, application_icon
from windows_integration import HotkeyFilter, SingleInstance, foreground
from shortcut_settings import ShortcutSettingsDialog, ShortcutStore
from history_settings import DateCleanupDialog, HistorySettingsDialog, SpecialMarkDialog
from data_locations import (
    DataLocations, LocationMigration, LocationSettingsDialog, LocationStore, export_png, write_exception_log,
)
from local_ocr import MODEL_DIR
from app_paths import application_root
from startup_settings import StartupManager
from history_edit import TextRecordDialog
from window_picker import WindowPinController

ROOT = application_root()


class ClipboardApplication:
    def __init__(self, application, data_dir, instance=None, *, enable_hotkeys=True,
                 enable_tray=True, clipboard=None, location_store=None, start_paused=False,
                 startup_manager=None):
        self.application = application
        self.instance = instance
        self.location_store = location_store or LocationStore(
            data_dir, DataLocations.defaults(data_dir, MODEL_DIR))
        self.locations = self.location_store.locations
        self.previous_model_dir = getattr(application, "_clipboard_ocr_model_dir", None)
        application._clipboard_ocr_model_dir = self.locations.models
        self.shortcut_store = ShortcutStore(self.locations.config)
        self.storage = HistoryStorage(self.locations.history, backup_dir=self.locations.backups)
        self.window = HistoryWindow(self.storage, self.shortcut_store)
        self.startup_manager = startup_manager or StartupManager(ROOT)
        self.recorder = ClipboardRecorder(self.storage, clipboard, self.window)
        self.recorder.set_paused(start_paused)
        self.settings_store = SettingsStore(self.locations.config)
        self.settings_store.export_dir = self.locations.exports
        self.editor = None
        self.screenshots = ScreenshotController(self.window, self.place_image, self.window,
                                                self.settings_store, self.recorder.place_text, self.shortcut_store)
        self.long_screenshots = LongScreenshotController(self.window, self.place_image, self.window,
                                                        self.settings_store, self.recorder.place_text, self.shortcut_store)
        self.screenshots.on_long_capture = self.long_screenshots.start_from_screenshot
        self.tray = None
        self.hotkeys = None
        self.closed = False
        self.hidden_tip_shown = False
        self.hotkey_warning = ""
        self.last_error = ""
        self.window_pins = WindowPinController(
            self.window, can_start=lambda: not (self.screenshots.active or self.long_screenshots.active))
        self.application._window_pin_controller = self.window_pins
        self.window_pins.failed.connect(self.report_error)
        self.window.copy_requested.connect(self.copy_record)
        self.window.pin_requested.connect(lambda record_id: self.edit(self.storage.toggle_pin, record_id))
        self.window.delete_requested.connect(lambda record_id: self.edit(self.storage.delete, record_id))
        self.window.delete_many_requested.connect(lambda ids: self.edit(self.storage.delete_many, ids))
        self.window.clear_requested.connect(lambda: self.edit(self.storage.clear_unpinned))
        self.window.screenshot_requested.connect(self.start_screenshot)
        self.window.long_screenshot_requested.connect(self.start_long_screenshot)
        self.window.pause_requested.connect(self.toggle_pause)
        self.window.hidden.connect(self.on_hidden)
        self.window.settings_requested.connect(self.open_settings)
        self.window.shortcuts_requested.connect(self.open_shortcut_settings)
        self.window.history_settings_requested.connect(self.open_history_settings)
        self.window.date_cleanup_requested.connect(self.open_date_cleanup)
        self.window.locations_requested.connect(self.open_location_settings)
        self.window.autostart_requested.connect(self.set_autostart)
        self.window.mark_requested.connect(self.open_special_mark)
        self.window.image_edit_requested.connect(self.edit_image)
        self.window.text_edit_requested.connect(self.edit_saved_text)
        self.recorder.recorded.connect(lambda record_id: self.window.refresh(select_id=record_id))
        self.recorder.failed.connect(self.report_error)
        self.recorder.paused_changed.connect(self.update_pause)
        self.screenshots.failed.connect(self.report_error)
        self.long_screenshots.failed.connect(self.report_error)
        if instance is not None:
            instance.show_requested.connect(self.show_history)
        if enable_tray:
            self.make_tray()
        if enable_hotkeys:
            self.hotkeys = HotkeyFilter(application, self.window, self.show_history, self.start_screenshot,
                                       self.shortcut_store.bindings, long_screenshot=self.start_long_screenshot)
            self.hotkey_warning = "  ".join(self.hotkeys.errors)
            self.window.set_error(self.hotkey_warning)
        self.application.aboutToQuit.connect(self.cleanup)
        self.shortcut_store.apply_handler = self.apply_shortcuts
        self.location_store.apply_handler = self.apply_locations
        self.shortcut_store.changed.connect(self.update_shortcut_labels)
        self.update_shortcut_labels()
        self.update_pause(self.recorder.paused)
        if self.settings_store.error:
            self.report_error(self.settings_store.error)
        if self.shortcut_store.error:
            self.report_error(self.shortcut_store.error)
        self.refresh_autostart()

    def refresh_autostart(self):
        try:
            enabled = self.startup_manager.is_enabled()
        except OSError:
            enabled = False
            self.report_error("无法读取 Windows 自启动设置，请检查当前用户的注册表权限。")
        self.window.autostart_action.setChecked(enabled)
        if self.tray:
            self.autostart_action.setChecked(enabled)

    def start_screenshot(self):
        self.window_pins.cancel()
        if self.long_screenshots.active:
            self.report_error("请先完成或取消当前长截图。")
            return
        self.screenshots.start()

    def start_long_screenshot(self):
        self.window_pins.cancel()
        if self.editor is not None or self.screenshots.active or QApplication.activeModalWidget() is not None:
            self.report_error("请先完成或取消当前编辑/设置，再开始长截图。")
            return
        self.long_screenshots.start()

    def set_autostart(self, enabled):
        try:
            self.startup_manager.set_enabled(enabled)
        except (OSError, ValueError) as error:
            self.report_error(f"自启动设置未完成：{error}")
        self.refresh_autostart()

    def open_settings(self):
        SettingsDialog(self.settings_store, self.window).exec()

    def open_shortcut_settings(self):
        ShortcutSettingsDialog(self.shortcut_store, self.window).exec()

    def open_history_settings(self):
        dialog = HistorySettingsDialog(self.storage, self.apply_history_limit, self.window)
        self.recorder.recorded.connect(dialog.update_impact)
        dialog.exec()
        self.recorder.recorded.disconnect(dialog.update_impact)

    def apply_history_limit(self, limit):
        self.storage.set_limit(limit)
        self.window.refresh()

    def open_date_cleanup(self):
        dialog = DateCleanupDialog(self.storage, self.apply_date_cleanup, self.window)
        self.recorder.recorded.connect(dialog.update_impact)
        try:
            dialog.exec()
        finally:
            self.recorder.recorded.disconnect(dialog.update_impact)

    def apply_date_cleanup(self, cutoff, include_protected):
        result = self.storage.delete_before(cutoff, include_protected)
        self.window.refresh()
        return result

    def open_location_settings(self):
        if self.editor is not None or self.screenshots.active or self.long_screenshots.active:
            QMessageBox.information(self.window, "先结束当前编辑", "请先完成或取消当前图片编辑，再设置数据保存位置。")
            return
        LocationSettingsDialog(self.location_store, self.window).exec()

    def apply_locations(self, locations):
        if self.editor is not None or self.screenshots.active or self.long_screenshots.active:
            raise ValueError("请先完成或取消当前图片编辑，再迁移数据。")
        if locations == self.locations:
            return
        migration = LocationMigration(self.storage, self.locations, locations)
        old_storage, new_storage = self.storage, None
        migration.prepare()
        try:
            new_storage = HistoryStorage(locations.history, limit=old_storage.limit, backup_dir=locations.backups)
            settings = SettingsStore(locations.config)
            shortcuts = ShortcutStore(locations.config)
            if settings.error or shortcuts.error or settings.settings != self.settings_store.settings or \
                    shortcuts.bindings != self.shortcut_store.bindings:
                raise ValueError("目标配置校验失败，原数据和位置已保留。")
            self.location_store.save(locations)
        except Exception:
            if new_storage is not None:
                new_storage.close()
            migration.rollback()
            raise
        # 保存成功后一次性更新所有读取/写入者，原配置对象及信号连接继续使用。
        self.locations = locations
        self.storage = self.recorder.storage = new_storage
        self.window.storage = self.window.model.storage = self.window.delegate.storage = new_storage
        self.settings_store.path = locations.config / "api-settings.json"
        self.settings_store.export_dir = locations.exports
        self.shortcut_store.path = locations.config / "shortcuts.json"
        self.application._clipboard_ocr_model_dir = locations.models
        old_storage.close()
        self.window.refresh()
        self.location_store.changed.emit(locations)

    def place_image(self, image):
        self.recorder.place_screenshot(image)
        if self.locations.auto_export and not self.recorder.paused:
            try:
                export_png(image, self.locations.exports)
            except (OSError, RuntimeError):
                self.report_error("图片已复制，自动 PNG 导出未完成。请检查导出文件夹和磁盘空间。")

    def open_special_mark(self, record_id):
        record = self.storage.get_record(record_id)
        if record is None:
            return
        # 新标记默认选中，用户可保留空标签；取消窗口不修改历史。
        dialog = SpecialMarkDialog(record, self.window)
        if not record["marked"]:
            dialog.marked.setChecked(True)
        if dialog.exec() == dialog.DialogCode.Accepted:
            self.edit(self.storage.set_mark, record_id, dialog.marked.isChecked(), dialog.tag.text())

    def apply_shortcuts(self, values):
        if self.hotkeys:
            self.hotkeys.apply(values, persist=lambda: self.shortcut_store.save(values))
        else:
            self.shortcut_store.save(values)
        self.hotkey_warning = ""
        self.window.set_error(self.last_error)

    def update_shortcut_labels(self, *_):
        long_label = f'滚动长截图 · {self.shortcut_store.label("long_screenshot")}'
        self.window.long_screenshot_action.setText(long_label)
        if self.tray:
            self.show_action.setText(f'打开历史 · {self.shortcut_store.label("history")}')
            self.screenshot_action.setText(f'框选截图 · {self.shortcut_store.label("screenshot")}')
            self.long_screenshot_action.setText(long_label)

    def edit_image(self, mode="edit"):
        if self.window.current_image.isNull():
            return
        if self.editor is not None:
            foreground(self.editor, self.editor.showNormal)
            if mode == "text":
                self.editor.open_text_panel()
            return
        # 保存目标绑定打开时的记录快照，后续列表选中变化不会把编辑结果写到别条记录。
        source_id = self.window.current_id
        record = self.storage.get_record(source_id)
        if record is None or record["kind"] != "image":
            self.window.refresh()
            return
        expected = record["fingerprint"]
        def update_original(image):
            result = self.storage.update_image(source_id, image, expected=expected)
            self.window.refresh(select_id=result)
            return result
        self.editor = ImageEditor(self.window.current_image, self.settings_store, self.window,
                                  shortcut_store=self.shortcut_store, update_handler=update_original)
        self.editor.selected.connect(self.place_image)
        self.editor.text_copied.connect(self.recorder.place_text)
        self.editor.destroyed.connect(self.editor_closed)
        self.editor.show()
        foreground(self.editor, self.editor.showNormal)
        if mode == "text":
            QTimer.singleShot(0, self.editor.open_text_panel)

    def editor_closed(self):
        self.editor = None

    def edit_saved_text(self, record_id):
        record = self.storage.get_record(record_id)
        if record is None or record["kind"] != "text":
            self.window.refresh()
            return
        def save(text):
            self.storage.update_text(record_id, text, expected=record["fingerprint"])
            self.window.refresh(select_id=record_id)
        TextRecordDialog(record["text"], save, self.window).exec()

    def make_tray(self):
        self.tray = QSystemTrayIcon(application_icon(), self.window)
        self.tray.setToolTip("剪贴收藏 · 正在记录")
        self.menu = QMenu()
        self.show_action = QAction(self.menu)
        self.show_action.triggered.connect(self.show_history)
        self.screenshot_action = QAction(self.menu)
        self.screenshot_action.triggered.connect(self.start_screenshot)
        self.long_screenshot_action = QAction("滚动长截图", self.menu)
        self.long_screenshot_action.triggered.connect(self.start_long_screenshot)
        self.pause_action = QAction("暂停记录", self.menu)
        self.pause_action.triggered.connect(self.toggle_pause)
        exit_action = QAction("退出剪贴收藏", self.menu)
        exit_action.triggered.connect(self.quit)
        self.menu.addAction(self.show_action)
        self.menu.addAction(self.screenshot_action)
        self.menu.addAction(self.long_screenshot_action)
        self.menu.addAction(self.pause_action)
        self.menu.addAction("快捷键设置", self.open_shortcut_settings)
        self.menu.addAction("历史保留设置", self.open_history_settings)
        self.menu.addAction("删除指定日期以前的数据…", self.open_date_cleanup)
        self.menu.addAction("数据保存位置", self.open_location_settings)
        self.autostart_action = self.menu.addAction("登录 Windows 后自动启动")
        self.autostart_action.setCheckable(True)
        self.autostart_action.triggered.connect(self.set_autostart)
        self.menu.aboutToShow.connect(self.refresh_autostart)
        self.menu.addSeparator()
        self.menu.addAction(exit_action)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()

    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_history()

    def show_history(self):
        self.window_pins.cancel()
        if not (self.screenshots.active or self.long_screenshots.active):
            foreground(self.window)

    def on_hidden(self):
        if self.tray and not self.hidden_tip_shown:
            self.hidden_tip_shown = True
            self.tray.showMessage("剪贴收藏仍在记录",
                                  f'{self.shortcut_store.label("history")} 调出历史，'
                                  f'{self.shortcut_store.label("screenshot")} 截图。\n'
                                  "真正退出请右键托盘图标选择“退出”。",
                                  QSystemTrayIcon.MessageIcon.Information, 3000)

    def toggle_pause(self):
        self.recorder.set_paused(not self.recorder.paused)

    def update_pause(self, paused):
        self.window.set_paused(paused)
        if self.tray:
            self.pause_action.setText("恢复记录" if paused else "暂停记录")
            self.tray.setToolTip("剪贴收藏 · 已暂停" if paused else "剪贴收藏 · 正在记录")

    def report_error(self, message):
        self.last_error = message
        self.window.set_error("  ".join(filter(None, [message, self.hotkey_warning])))
        if self.tray:
            self.tray.showMessage("剪贴收藏", message, QSystemTrayIcon.MessageIcon.Warning, 4000)

    def handle_exception(self, exc_type, exc_value, exc_traceback):
        try:
            target = write_exception_log(self.locations, exc_type, exc_value, exc_traceback)
        except OSError:
            self.report_error("程序遇到错误，日志目录暂不可写。请检查保存位置和磁盘连接。")
        else:
            self.report_error(f"程序遇到错误，详情已写入 {target}。")

    def edit(self, operation, *args):
        try:
            operation(*args)
            self.window.refresh()
        except (sqlite3.Error, OSError, RuntimeError):
            self.report_error("操作未完成，请检查数据目录与磁盘空间。")

    def copy_record(self, record_id):
        try:
            if self.recorder.copy_record(record_id):
                self.window.hide()
        except (sqlite3.Error, OSError, RuntimeError):
            self.report_error("未能取回这条记录，请重试或检查数据文件。")

    def cleanup(self):
        if self.closed:
            return
        self.closed = True
        self.window_pins.close()
        self.screenshots.stop()
        self.long_screenshots.stop()
        self.window.close_image_viewers()
        if self.editor is not None:
            self.editor.cancel()
        if self.hotkeys:
            self.hotkeys.close()
        self.recorder.disconnect()
        if self.tray:
            self.tray.hide()
        self.storage.close()
        if self.previous_model_dir is None:
            if hasattr(self.application, "_clipboard_ocr_model_dir"):
                del self.application._clipboard_ocr_model_dir
        else:
            self.application._clipboard_ocr_model_dir = self.previous_model_dir
        if self.instance:
            self.instance.close()

    def quit(self):
        self.window.exiting = True
        self.cleanup()
        self.window.close()
        self.application.quit()


def main():
    parser = argparse.ArgumentParser(description="剪贴收藏：本地文字和图片历史、Ctrl+Alt+X 截图")
    parser.add_argument("--data-dir", type=Path, help="独立的数据目录，不使用正式位置设置")
    parser.add_argument("--hidden", action="store_true", help="启动后仅显示托盘")
    parser.add_argument("--paused", action="store_true", help="以暂停记录模式启动")
    parser.add_argument("--autostart", action="store_true", help="登录自启动：仅托盘，已有实例时安静退出")
    parser.add_argument("--self-check", type=Path, help="仅用合成数据做运行检查，将结果写入指定 JSON")
    args = parser.parse_args()
    application = QApplication(sys.argv[:1])
    application.setApplicationName("剪贴收藏")
    application.setOrganizationName("ClipboardSaverLocal")
    application.setFont(QFont("Microsoft YaHei UI", 10))
    application.setStyle("Fusion")
    application.setQuitOnLastWindowClosed(False)
    application.setWindowIcon(application_icon())
    if args.self_check is not None:
        # 验收分支先于正式存储与剪贴板初始化，任何测试都不会访问用户历史。
        from release_check import run_self_check
        return run_self_check(application, args.self_check)
    location_store = LocationStore(args.data_dir, DataLocations.defaults(args.data_dir, MODEL_DIR)) \
        if args.data_dir is not None else LocationStore(ROOT, DataLocations.defaults(ROOT / "data", MODEL_DIR))
    if location_store.path.exists() and not (location_store.locations.history / "clipboard.sqlite3").is_file():
        raise RuntimeError("保存的历史文件暂不可用，请检查数据位置、磁盘连接和访问权限。原设置已保留。")
    # 实例身份属于启动配置，不随历史位置迁移，避免迁移后同时出现两份采集进程。
    instance = SingleInstance(args.data_dir if args.data_dir is not None else ROOT / "data",
                              show_existing=not args.autostart)
    if not instance.primary:
        return 0
    if getattr(sys, "frozen", False):
        # 安装包用同名互斥量（AppMutex）判断程序是否在运行，升级/卸载前提示先退出；句柄随进程释放。
        application._installer_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "ClipboardSaver.Running")
    controller = ClipboardApplication(application, location_store.locations.history, instance,
                                      location_store=location_store, start_paused=args.paused,
                                      startup_manager=StartupManager(ROOT, data_dir=args.data_dir))

    def exception_hook(exc_type, exc_value, exc_traceback):
        # 不记录剪贴板内容或局部变量；只提供程序堆栈以排查故障。
        controller.handle_exception(exc_type, exc_value, exc_traceback)

    sys.excepthook = exception_hook
    if not (args.hidden or args.autostart):
        controller.show_history()
    return application.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # pythonw 没有控制台，启动失败也要给出可见的错误及调试入口。
        if getattr(sys, "frozen", False):
            from time import time_ns
            try:
                log = ROOT / "data" / f"startup-error-{time_ns()}.txt"
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(traceback.format_exc(), encoding="utf-8")
                message = f"剪贴收藏启动失败。\n错误详情：{log}\n请检查解压是否完整、所在目录是否可写。"
            except OSError:
                message = "剪贴收藏启动失败。\n请完整解压到可写文件夹，例如文档目录，再重试。"
        else:
            message = "剪贴收藏启动失败。\n请运行“调试启动.cmd”查看错误详情。"
        ctypes.windll.user32.MessageBoxW(None, message, "剪贴收藏", 0x10)
        traceback.print_exc()
        raise
