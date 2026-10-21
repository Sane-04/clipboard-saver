"""历史管理升级使用隔离数据库；验证数据行为及真实 Qt 操作入口。"""
import sqlite3

import pytest
from PySide6.QtCore import QItemSelectionModel, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMenu

from app import ClipboardApplication
from history_settings import HistorySettingsDialog, SpecialMarkDialog
from storage import HistoryStorage
from test_dates import stamp
from test_history import FakeClipboard, picture
from ui import DateRangeDialog, HistoryWindow


def test_saved_quota_prunes_oldest_and_keeps_marked_and_pinned_after_restart(tmp_path):
    now = [stamp("2026-10-01T10:00:00")]
    store = HistoryStorage(tmp_path, clock=lambda: now[0])
    pin = store.add_text("固定内容")
    store.toggle_pin(pin)
    marked = store.add_image(picture())
    store.set_mark(marked, True, "学习")
    normal = []
    for i in range(5):
        now[0] += 1
        normal.append(store.add_text(f"普通 {i}"))
    store.set_limit(2)
    assert store.limit == 2 and store.ordinary_count() == 2
    assert all(store.get_record(value) is None for value in normal[:3])
    assert store.get_record(pin)["pinned"] and store.get_record(marked)["tag"] == "学习"
    assert store.counts()["total"] == 4
    assert not store.connection.execute("PRAGMA foreign_key_check").fetchall()
    store.close()
    store = HistoryStorage(tmp_path)
    assert store.limit == 2 and store.marked_count() == 1
    store.add_text("重开后复制")
    assert store.ordinary_count() == 2 and store.get_record(marked)
    store.close()


def test_quota_and_records_roll_back_together_on_database_failure(tmp_path):
    store = HistoryStorage(tmp_path)
    ids = [store.add_text(f"记录 {i}") for i in range(4)]
    store.set_limit(4)
    store.connection.execute("""
        CREATE TRIGGER refuse_delete BEFORE DELETE ON clips
        BEGIN SELECT RAISE(ABORT, 'simulated disk/database failure'); END
    """)
    with pytest.raises(sqlite3.Error):
        store.set_limit(1)
    assert store.limit == 4 and all(store.get_record(i) for i in ids)
    assert store.connection.execute("SELECT value FROM history_settings").fetchone()[0] == "4"
    store.close()
    store = HistoryStorage(tmp_path)
    assert store.limit == 4 and store.counts()["total"] == 4
    store.close()


@pytest.mark.parametrize("value", [0, -1, 1_000_001, True, 3.5])
def test_invalid_quota_does_not_mutate_history(tmp_path, value):
    store = HistoryStorage(tmp_path)
    record = store.add_text("保留")
    with pytest.raises(ValueError):
        store.set_limit(value)
    assert store.limit == 1000 and store.get_record(record)
    store.close()


def test_mark_metadata_survives_recopy_and_tag_search_combines_with_dates(tmp_path):
    now = [stamp("2026-10-01T10:00:00")]
    store = HistoryStorage(tmp_path, clock=lambda: now[0])
    image = picture()
    record = store.add_image(image)
    store.set_mark(record, True, "  Work%_报销  ")
    text = store.add_text("工作备忘")
    now[0] = stamp("2026-10-02T10:00:00")
    assert store.add_image(image) == record
    assert store.get_record(record)["tag"] == "Work%_报销"
    assert [r["id"] for r in store.list_records("work%_", marked_only=True)] == [record]
    assert not store.list_records("Work", kind="text", marked_only=True)
    assert len(store.list_records("报销", "image", "2026-10-01", "2026-10-02", True)) == 2
    assert not store.list_records("报销", start="2026-10-03", marked_only=True)
    assert store.list_records("备忘")[0]["id"] == text
    store.set_mark(record, True, "")
    assert store.marked_count() == 1 and store.list_records(marked_only=True)[0]["tag"] == ""
    store.set_mark(record, False, "应被清空")
    assert store.get_record(record)["tag"] == "" and not store.list_records(marked_only=True)
    store.close()


def test_unmark_obeys_quota_but_pin_protects_and_manual_delete_can_remove_mark(tmp_path):
    store = HistoryStorage(tmp_path, limit=1)
    old = store.add_text("重要的旧记录")
    store.set_mark(old, True, "重要")
    store.add_text("最近的普通记录")
    store.toggle_pin(old)
    store.set_mark(old, False)
    assert store.get_record(old)
    store.set_mark(old, True, "重要")
    store.toggle_pin(old)
    store.clear_unpinned()
    assert store.counts()["total"] == 1 and store.get_record(old)
    store.add_text("新的普通记录")
    store.set_mark(old, False)
    assert store.get_record(old) is None
    current = store.list_records()[0]["id"]
    store.set_mark(current, True, "可以手动删")
    store.delete(current)
    assert store.marked_count() == 0 and store.counts()["total"] == 0
    store.close()


def test_real_version_two_migration_preserves_bytes_and_dates_and_backs_up_once(tmp_path):
    initial = HistoryStorage(tmp_path, clock=lambda: stamp("2026-10-01T10:00:00"))
    record = initial.add_image(picture())
    initial.toggle_pin(record)
    before = initial.get_record(record)
    dates = [tuple(row) for row in initial.connection.execute("SELECT * FROM clip_copy_days")]
    initial.close()
    with sqlite3.connect(tmp_path / "clipboard.sqlite3") as con:
        con.execute("ALTER TABLE clips DROP COLUMN marked")
        con.execute("ALTER TABLE clips DROP COLUMN tag")
        con.execute("DROP TABLE history_settings")
        con.execute("PRAGMA user_version=2")
    migrated = HistoryStorage(tmp_path)
    after = migrated.get_record(record)
    assert all(after[key] == value for key, value in before.items())
    assert [tuple(row) for row in migrated.connection.execute("SELECT * FROM clip_copy_days")] == dates
    assert migrated.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    migrated.close()
    backups = list((tmp_path / "backups").glob("before-history-settings-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 2
        assert con.execute("SELECT image FROM clips WHERE id=?", (record,)).fetchone()[0] == before["image"]
    reopened = HistoryStorage(tmp_path)
    reopened.close()
    assert len(list((tmp_path / "backups").glob("before-history-settings-*.sqlite3"))) == 1


def test_settings_show_actual_cleanup_and_cancel_does_not_save(qt_app, storage):
    for i in range(4):
        storage.add_text(f"普通 {i}")
    dialog = HistorySettingsDialog(storage, storage.set_limit)
    dialog.limit.setValue(2)
    assert "2 条" in dialog.impact.text()
    dialog.reject()
    assert storage.counts()["total"] == 4 and storage.limit == 1000
    dialog = HistorySettingsDialog(storage, storage.set_limit)
    dialog.limit.setValue(2)
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert storage.counts()["total"] == 2 and storage.limit == 2


def test_settings_failure_keeps_dialog_open_and_original_data(qt_app, storage):
    record = storage.add_text("原记录")
    def fail(_):
        raise sqlite3.OperationalError("simulated write failure")
    dialog = HistorySettingsDialog(storage, fail)
    dialog.limit.setValue(1)
    dialog.save()
    assert dialog.result() != QDialog.DialogCode.Accepted and not dialog.error.isHidden()
    assert storage.get_record(record) and storage.limit == 1000


def test_date_dialog_all_dates_clears_a_previous_range(qt_app, storage):
    dialog = DateRangeDialog("2026-10-01", "2026-10-02")
    dialog.mode.setCurrentText("全部日期")
    assert dialog.start.isHidden() and dialog.end.isHidden()
    dialog.apply()
    assert dialog.range == (None, None)
    assert DateRangeDialog().mode.currentText() == "全部日期"
    window = HistoryWindow(storage)
    window.set_date_mode("custom", "2026-10-01", "2026-10-02")
    def select_all():
        current = QApplication.activeModalWidget()
        assert isinstance(current, DateRangeDialog)
        current.mode.setCurrentText("全部日期")
        current.apply()
    QTimer.singleShot(0, select_all)
    window.open_dates()
    assert window.date_mode == "all" and window.date_start is None and window.date_end is None
    assert window.date_button.text() == "全部日期"
    window.exiting = True
    window.close()


def test_history_mark_filter_and_tag_preview(qt_app, storage):
    marked = storage.add_image(picture())
    storage.set_mark(marked, True, "报销")
    storage.add_text("另一个普通记录")
    window = HistoryWindow(storage)
    window.marked_filter.click()
    assert window.marked_only and window.current_id == marked
    assert window.mark_button.text() == "编辑标记" and "报销" in window.tag_label.text()
    window.search.setText("报销")
    assert window.current_id == marked
    window.set_kind("text")
    assert window.current_id is None and not window.mark_button.isEnabled()
    window.set_kind("image")
    assert window.current_id == marked
    window.exiting = True
    window.close()


def test_application_mark_settings_and_saved_quota_work_through_real_dialogs(qt_app, tmp_path):
    controller = ClipboardApplication(qt_app, tmp_path, enable_hotkeys=False,
                                      enable_tray=False, clipboard=FakeClipboard())
    record = controller.storage.add_text("需要标记的内容")
    controller.window.refresh(select_id=record)
    def mark():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, SpecialMarkDialog) and dialog.marked.isChecked()
        dialog.tag.setText("项目资料")
        dialog.accept()
    QTimer.singleShot(0, mark)
    controller.window.mark_button.click()
    assert controller.storage.get_record(record)["tag"] == "项目资料"
    assert "项目资料" in controller.window.tag_label.text()
    def save_limit():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, HistorySettingsDialog)
        dialog.limit.setValue(12)
        dialog.save()
    QTimer.singleShot(0, save_limit)
    controller.open_history_settings()
    assert controller.storage.limit == 12
    controller.cleanup()
    controller.window.exiting = True
    controller.window.close()


def test_right_click_selected_row_targets_that_row_without_losing_multi_selection(qt_app, storage):
    first, second = storage.add_text("第一条"), storage.add_text("第二条")
    window = HistoryWindow(storage)
    window.show()
    qt_app.processEvents()
    indexes = {r["id"]: window.model.index(i, 0) for i, r in enumerate(window.model.rows)
               if not r.get("header")}
    window.list_view.setCurrentIndex(indexes[first])
    window.list_view.selectionModel().select(indexes[second], QItemSelectionModel.SelectionFlag.Select)
    marks = []
    window.mark_requested.connect(marks.append)
    def mark_clicked_row():
        assert window.current_id == second
        assert set(window.selected_record_ids()) == {first, second}
        menu = QApplication.activePopupWidget()
        next(action for action in menu.actions() if "特殊标记" in action.text()).trigger()
        menu.close()
    QTimer.singleShot(0, mark_clicked_row)
    window.open_record_menu(window.list_view.visualRect(indexes[second]).center())
    assert marks == [second]
    window.exiting = True
    window.close()


def test_more_settings_actions_emit_qt_signals_instead_of_calling_signal_objects(qt_app, storage):
    window = HistoryWindow(storage)
    events = []
    window.shortcuts_requested.connect(lambda: events.append("shortcuts"))
    window.history_settings_requested.connect(lambda: events.append("history"))
    window.settings_requested.connect(lambda: events.append("api"))
    actions = {action.text(): action for menu in window.findChildren(QMenu) for action in menu.actions()}
    actions["快捷键设置"].trigger()
    actions["历史保留设置"].trigger()
    actions["文字识别与 API 设置"].trigger()
    assert events == ["shortcuts", "history", "api"]
    window.exiting = True
    window.close()
