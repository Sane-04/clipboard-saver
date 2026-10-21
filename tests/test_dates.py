"""日期归档与分组选择的行为验证，所有数据使用隔离库。"""
import sqlite3
from datetime import date, datetime

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from clipboard_capture import ClipboardRecorder
from storage import HistoryStorage, local_copy_day
from test_history import FakeClipboard, picture
from ui import DateRangeDialog, HistoryWindow


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp()) * 1_000_000_000


def test_midnight_precision_and_cross_day_dedup(tmp_path):
    now = [stamp("2026-12-31T23:59:59") + 999_999_999]
    store = HistoryStorage(tmp_path, clock=lambda: now[0])
    first = store.add_text("跨年中文")
    store.add_text("跨年中文")
    assert local_copy_day(now[0]) == "2026-12-31"
    now[0] = stamp("2027-01-01T00:00:00")
    assert store.add_text("跨年中文") == first
    assert store.counts()["total"] == 1
    assert [r["copy_day"] for r in store.list_records()] == ["2027-01-01"]
    ranged = store.list_records("中文", "text", "2026-12-31", "2027-01-01")
    assert [r["copy_day"] for r in ranged] == ["2027-01-01", "2026-12-31"]
    assert {r["id"] for r in ranged} == {first}
    assert len(store.list_records(start="2026-12-31")) == 1
    store.close()


def test_image_shared_across_days_and_filters(tmp_path):
    now = [stamp("2026-10-01T10:00:00")]
    store = HistoryStorage(tmp_path, clock=lambda: now[0])
    image_id = store.add_image(picture())
    text_id = store.add_text("需要查询的文字")
    store.toggle_pin(text_id)
    now[0] = stamp("2026-10-02T10:00:00")
    assert store.add_image(picture()) == image_id
    assert len(store.list_records(kind="image", start="2026-10-01", end="2026-10-02")) == 2
    assert len(store.list_records()) == 2
    assert store.connection.execute("SELECT count(image) FROM clips").fetchone()[0] == 1
    assert store.list_records("查询", "text", "2026-10-01", "2026-10-01")[0]["id"] == text_id
    assert not store.list_records("查询", "text", "2026-10-02", "2026-10-02")
    assert not store.list_records("查询", "image", "2026-10-01", "2026-10-02")
    with pytest.raises(ValueError):
        store.list_records(start="2026-10-02", end="2026-10-01")
    store.close()


def test_version_one_migration_preserves_content_and_is_once(tmp_path):
    old = HistoryStorage(tmp_path, clock=lambda: stamp("2026-09-30T20:10:00"))
    record = old.add_image(picture())
    old.toggle_pin(record)
    before = old.get_record(record)
    old.close()
    with sqlite3.connect(tmp_path / "clipboard.sqlite3") as con:
        con.execute("DROP TABLE clip_copy_days")
        con.execute("PRAGMA user_version=1")
    migrated = HistoryStorage(tmp_path)
    assert migrated.get_record(record) == before
    assert migrated.list_records()[0]["copy_day"] == "2026-09-30"
    assert migrated.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    migrated.close()
    assert len(list((tmp_path / "backups").glob("before-date-migration-*.sqlite3"))) == 1
    reopened = HistoryStorage(tmp_path)
    assert reopened.connection.execute("PRAGMA user_version").fetchone()[0] == 3
    assert len(reopened.list_records()) == 1
    reopened.close()
    assert len(list((tmp_path / "backups").glob("before-date-migration-*.sqlite3"))) == 1


def test_delete_clear_and_quota_cascade_daily_index(tmp_path):
    store = HistoryStorage(tmp_path, limit=2)
    pin = store.add_text("固定")
    store.toggle_pin(pin)
    oldest = store.add_text("淘汰")
    store.add_text("新一")
    store.add_text("新二")
    assert store.get_record(oldest) is None
    assert store.connection.execute(
        "SELECT count(*) FROM clip_copy_days WHERE clip_id=?", (oldest,)).fetchone()[0] == 0
    store.clear_unpinned()
    assert [r["id"] for r in store.list_records()] == [pin]
    store.delete(pin)
    assert store.connection.execute("SELECT count(*) FROM clip_copy_days").fetchone()[0] == 0
    store.close()


def test_pause_history_copy_does_not_add_day(qt_app, tmp_path):
    now = [stamp("2026-10-01T10:00:00")]
    store = HistoryStorage(tmp_path, clock=lambda: now[0])
    record = store.add_text("旧内容")
    recorder = ClipboardRecorder(store, FakeClipboard())
    recorder.set_paused(True)
    now[0] = stamp("2026-10-02T10:00:00")
    assert recorder.copy_record(record)
    assert not store.list_records(start="2026-10-02")
    recorder.set_paused(False)
    recorder.copy_record(record)
    assert len(store.list_records(start="2026-10-01", end="2026-10-02")) == 2
    recorder.disconnect()
    store.close()


def test_group_headers_and_preserve_multi_day_selection(qt_app, tmp_path):
    now = [stamp("2026-10-01T10:00:00")]
    store = HistoryStorage(tmp_path, clock=lambda: now[0])
    record = store.add_text("两日相同")
    now[0] = stamp("2026-10-02T10:00:00")
    store.touch(record)
    window = HistoryWindow(store)
    window.set_date_mode("custom", "2026-10-01", "2026-10-02")
    assert not window.model.flags(window.model.index(0, 0)) & Qt.ItemFlag.ItemIsSelectable
    window.move_selection(1)
    assert window.current_key == (record, "2026-10-01")
    window.refresh(select_id=record)
    assert window.current_key == (record, "2026-10-01")
    now[0] = stamp("2026-10-03T10:00:00")
    new_record = store.add_text("范围外")
    window.refresh(select_id=new_record)
    assert window.current_key == (record, "2026-10-01")
    assert window.date_mode == "custom"
    window.search.setText("无匹配")
    assert window.current_id is None and not window.copy_button.isEnabled()
    window.exiting = True
    window.close()
    store.close()


def test_date_dialog_invalid_range_keeps_open(qt_app):
    dialog = DateRangeDialog("2026-10-02", "2026-10-01")
    dialog.apply()
    assert dialog.range is None
    assert not dialog.error.isHidden()
    dialog.mode.setCurrentIndex(0)
    dialog.apply()
    assert dialog.range == ("2026-10-02", "2026-10-02")


def test_relative_ranges_recalculate_and_custom_stays(qt_app, storage, monkeypatch):
    import ui
    class TestDate(date):
        current = date(2026, 10, 7)
        @classmethod
        def today(cls):
            return cls.current
    monkeypatch.setattr(ui, "date", TestDate)
    window = HistoryWindow(storage)
    window.set_date_mode("week")
    assert (window.date_start, window.date_end) == ("2026-10-01", "2026-10-07")
    TestDate.current = date(2026, 10, 8)
    window.check_day()
    assert (window.date_start, window.date_end) == ("2026-10-02", "2026-10-08")
    window.set_date_mode("custom", "2026-09-30", "2026-10-01")
    TestDate.current = date(2026, 10, 9)
    window.check_day()
    assert (window.date_start, window.date_end) == ("2026-09-30", "2026-10-01")
    window.exiting = True
    window.close()


def test_reused_sqlite_id_does_not_show_old_thumbnail(qt_app, storage):
    first = storage.add_image(picture())
    window = HistoryWindow(storage)
    old_color = window.delegate.thumbnail(first).pixelColor(0, 0)
    storage.delete(first)
    other = picture()
    other.fill(QColor("red"))
    second = storage.add_image(other)
    assert first == second
    window.refresh(select_id=second)
    assert window.delegate.thumbnail(second).pixelColor(0, 0).name() == "#ff0000"
    assert window.delegate.thumbnail(second).pixelColor(0, 0) != old_color
    window.exiting = True
    window.close()
