"""本地历史：文字原样保存，图片用 PNG 保存原图与缩略图。"""

import hashlib
import sqlite3
import struct
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from time import time_ns

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage


def image_fingerprint(image):
    # 用像素而非 PNG 文件字节去重；压缩方式和附带文字不应制造重复记录。
    normalized = image.convertToFormat(QImage.Format.Format_RGBA8888)
    digest = hashlib.sha256(b"image\0" + struct.pack(">II", image.width(), image.height()))
    digest.update(bytes(normalized.constBits()))
    digest.update(bytes(image.colorSpace().iccProfile()))
    return digest.hexdigest()


def encode_png(image):
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("图片无法保存为 PNG")
    buffer.close()
    return bytes(data)


def local_copy_day(timestamp_ns):
    # 先取整到秒，避免浮点数将 23:59:59.999999999 四舍五入成次日。
    return datetime.fromtimestamp(timestamp_ns // 1_000_000_000).date().isoformat()


class HistoryStorage:
    def __init__(self, data_dir, limit=None, *, clock=time_ns, backup_dir=None):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.backup_dir = Path(backup_dir).resolve() if backup_dir is not None else self.data_dir / "backups"
        self.connection = sqlite3.connect(self.data_dir / "clipboard.sqlite3", timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        saved = self.connection.execute(
            "SELECT value FROM history_settings WHERE name='max_records'").fetchone()
        self.limit = self.validate_limit(limit if limit is not None else int(saved[0]) if saved else 1000)

    def _migrate(self):
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 3:
            self.connection.close()
            raise RuntimeError("历史库版本高于当前程序，请使用较新的剪贴收藏。")
        if version == 3:
            return
        if version in (1, 2):
            backups = self.backup_dir
            backups.mkdir(parents=True, exist_ok=True)
            prefix = "before-date-migration" if version == 1 else "before-history-settings"
            with closing(sqlite3.connect(backups / f"{prefix}-{time_ns()}.sqlite3")) as target:
                self.connection.backup(target)
        # DDL 也明确进入事务，失败不能留下半张日期索引或重建空库。
        self.connection.execute("BEGIN IMMEDIATE")
        with self.connection:
            self.connection.execute("""
            CREATE TABLE IF NOT EXISTS clips (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('text', 'image')),
                fingerprint TEXT NOT NULL,
                text TEXT,
                image BLOB,
                thumbnail BLOB,
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0,
                UNIQUE(kind, fingerprint)
            )
            """)
            self.connection.execute("CREATE INDEX IF NOT EXISTS clips_order ON clips(pinned, updated_at DESC)")
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS clip_copy_days (
                    clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
                    copy_day TEXT NOT NULL,
                    last_copied_at INTEGER NOT NULL,
                    PRIMARY KEY(clip_id, copy_day)
                )
            """)
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS copy_days_order ON clip_copy_days(copy_day DESC,last_copied_at DESC)")
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS copy_days_latest ON clip_copy_days(clip_id,last_copied_at DESC)")
            # 旧库只有最后复制时间，迁移只回填这一日，不编造此前的复制事件。
            if version < 2:
                rows = self.connection.execute("SELECT id,updated_at FROM clips").fetchall()
                self.connection.executemany("INSERT OR IGNORE INTO clip_copy_days VALUES(?,?,?)",
                    [(row["id"], local_copy_day(row["updated_at"]), row["updated_at"]) for row in rows])
            # 直接增加元数据列，保持内容 ID、原图、固定状态和已有日期归档不变。
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(clips)")}
            if "marked" not in columns:
                self.connection.execute("ALTER TABLE clips ADD COLUMN marked INTEGER NOT NULL DEFAULT 0")
            if "tag" not in columns:
                self.connection.execute("ALTER TABLE clips ADD COLUMN tag TEXT NOT NULL DEFAULT ''")
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS history_settings (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                )
            """)
            self.connection.execute("PRAGMA user_version=3")

    def _record_day(self, record_id, timestamp):
        # 每内容每天一行；复制次数再多也不重复储存原图或无限追加事件。
        self.connection.execute("""
            INSERT INTO clip_copy_days(clip_id,copy_day,last_copied_at) VALUES(?,?,?)
            ON CONFLICT(clip_id,copy_day) DO UPDATE SET last_copied_at=excluded.last_copied_at
        """, (record_id, local_copy_day(timestamp), timestamp))

    def _upsert(self, kind, fingerprint, text=None, image=None, thumbnail=None, width=0, height=0):
        timestamp = self.clock()
        with self.connection:
            self.connection.execute("""
                INSERT INTO clips(kind, fingerprint, text, image, thumbnail, width, height, updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(kind, fingerprint) DO UPDATE SET updated_at=excluded.updated_at
            """, (kind, fingerprint, text, image, thumbnail, width, height, timestamp))
            record_id = self.connection.execute(
                "SELECT id FROM clips WHERE kind=? AND fingerprint=?", (kind, fingerprint)
            ).fetchone()[0]
            self._record_day(record_id, timestamp)
            self._prune()
        return record_id

    def _prune(self, limit=None):
        # 固定和特殊标记均独立于普通配额；手动删除仍然可以删除这些内容。
        self.connection.execute("""
            DELETE FROM clips WHERE id IN (
                SELECT id FROM clips WHERE pinned=0 AND marked=0
                ORDER BY updated_at DESC, id DESC LIMIT -1 OFFSET ?
            )
        """, (self.limit if limit is None else limit,))

    @staticmethod
    def validate_limit(limit):
        if type(limit) is not int or not 1 <= limit <= 1_000_000:
            raise ValueError("最大保留数量应为 1 到 1,000,000 之间的整数。")
        return limit

    def ordinary_count(self):
        return self.connection.execute(
            "SELECT count(*) FROM clips WHERE pinned=0 AND marked=0").fetchone()[0]

    def marked_count(self):
        return self.connection.execute("SELECT count(*) FROM clips WHERE marked=1").fetchone()[0]

    def set_limit(self, limit):
        limit = self.validate_limit(limit)
        # 配额与淘汰处于同一事务；磁盘/数据库写入失败时两者都保持原状。
        with self.connection:
            self.connection.execute("""
                INSERT INTO history_settings(name,value) VALUES('max_records',?)
                ON CONFLICT(name) DO UPDATE SET value=excluded.value
            """, (str(limit),))
            self._prune(limit)
        self.limit = limit

    def add_text(self, text):
        if not text.strip():
            return None
        fingerprint = hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()
        return self._upsert("text", fingerprint, text=text)

    def add_image(self, image):
        if image.isNull():
            return None
        original = image.copy()
        # 保存实际像素尺寸；设备缩放属于截图坐标，不应让粘贴的原图再被缩小。
        original.setDevicePixelRatio(1)
        fingerprint = image_fingerprint(original)
        existing = self.connection.execute(
            "SELECT id FROM clips WHERE kind='image' AND fingerprint=?", (fingerprint,)
        ).fetchone()
        if existing:
            self.touch(existing["id"])
            return existing["id"]
        thumb = original.scaled(140, 96, Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
        return self._upsert("image", fingerprint, image=encode_png(original),
                            thumbnail=encode_png(thumb), width=original.width(),
                            height=original.height())

    def _replace_content(self, record_id, kind, fingerprint, *, expected=None, text=None,
                         image=None, thumbnail=None, width=0, height=0):
        # 修改原条目不制造新的复制日期、不改保护/标签元数据；整个更新在同一事务。
        self.connection.execute("BEGIN IMMEDIATE")
        with self.connection:
            old = self.get_record(record_id)
            if old is None or old["kind"] != kind:
                raise ValueError("原记录已被删除或类型已改变。可另存为新记录。")
            if expected is not None and old["fingerprint"] != expected:
                raise ValueError("原记录在编辑期间已被更新，请重新打开，或另存为新记录。")
            if self.connection.execute(
                "SELECT id FROM clips WHERE kind=? AND fingerprint=? AND id<>?",
                (kind, fingerprint, record_id)).fetchone():
                raise ValueError("已有另一条相同内容，未覆盖任何记录。请直接使用已有记录或修改内容。")
            if old["fingerprint"] == fingerprint:
                return record_id
            self.connection.execute("""
                UPDATE clips SET fingerprint=?,text=?,image=?,thumbnail=?,width=?,height=? WHERE id=?
            """, (fingerprint, text, image, thumbnail, width, height, record_id))
        return record_id

    def update_text(self, record_id, text, *, expected=None):
        if not text.strip():
            raise ValueError("内容不能为空。要移除记录请使用删除。")
        fingerprint = hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()
        return self._replace_content(record_id, "text", fingerprint, expected=expected, text=text)

    def update_image(self, record_id, image, *, expected=None):
        if image.isNull():
            raise ValueError("不能用空图片覆盖原记录。")
        original = image.copy()
        original.setDevicePixelRatio(1)
        thumb = original.scaled(140, 96, Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
        return self._replace_content(
            record_id, "image", image_fingerprint(original), expected=expected,
            image=encode_png(original), thumbnail=encode_png(thumb),
            width=original.width(), height=original.height())

    def list_records(self, search="", kind="", start=None, end=None, marked_only=False):
        # 列表不读取原图 BLOB。文字只取摘要，完整内容在选中后读取。
        if start is not None or end is not None:
            start, end = start or end, end or start
            date.fromisoformat(start)
            date.fromisoformat(end)
            if end < start:
                raise ValueError("结束日期不能早于开始日期。")
            source = "clips c JOIN clip_copy_days d ON d.clip_id=c.id AND d.copy_day BETWEEN ? AND ?"
            selected = "d.copy_day AS copy_day,d.last_copied_at AS display_at"
            parameters = (start, end, kind, kind, search, search, search, int(marked_only))
        else:
            source = "clips c"
            selected = """(SELECT copy_day FROM clip_copy_days d WHERE d.clip_id=c.id
                           ORDER BY last_copied_at DESC,copy_day DESC LIMIT 1) AS copy_day,
                           c.updated_at AS display_at"""
            parameters = (kind, kind, search, search, search, int(marked_only))
        # 日期组先排序，固定只在匹配组内置顶；多日期行仍共用同一个内容 ID。
        rows = self.connection.execute(f"""
            SELECT c.id,c.kind,substr(coalesce(c.text,''),1,240) AS preview,
                   c.width,c.height,c.updated_at,c.pinned,c.marked,c.tag,{selected}
            FROM {source}
            WHERE (?='' OR kind=?)
              AND (?='' OR (kind='text' AND instr(lower(text),lower(?))>0)
                   OR (marked=1 AND instr(lower(tag),lower(?))>0))
              AND (?=0 OR marked=1)
            ORDER BY copy_day DESC,pinned DESC,display_at DESC,c.id DESC
        """, parameters).fetchall()
        return [dict(row) for row in rows]

    def get_record(self, record_id):
        row = self.connection.execute("SELECT * FROM clips WHERE id=?", (record_id,)).fetchone()
        return dict(row) if row else None

    def thumbnail(self, record_id):
        row = self.connection.execute("SELECT thumbnail FROM clips WHERE id=?", (record_id,)).fetchone()
        return bytes(row[0]) if row and row[0] else b""

    def counts(self):
        row = self.connection.execute("""
            SELECT count(*) AS total,
                   coalesce(sum(kind='image'),0) AS images,
                   coalesce(sum(pinned),0) AS pinned FROM clips
        """).fetchone()
        return dict(row)

    def touch(self, record_id):
        timestamp = self.clock()
        with self.connection:
            changed = self.connection.execute("UPDATE clips SET updated_at=? WHERE id=?", (timestamp, record_id))
            if changed.rowcount:
                self._record_day(record_id, timestamp)

    def toggle_pin(self, record_id):
        with self.connection:
            self.connection.execute("UPDATE clips SET pinned=1-pinned WHERE id=?", (record_id,))
            self._prune()

    def set_mark(self, record_id, marked, tag=""):
        tag = tag.strip() if marked else ""
        if len(tag) > 120:
            raise ValueError("标签最多 120 个字符。")
        with self.connection:
            self.connection.execute("UPDATE clips SET marked=?,tag=? WHERE id=?",
                                    (int(bool(marked)), tag, record_id))
            self._prune()

    def delete(self, record_id):
        self.delete_many([record_id])

    def delete_many(self, record_ids):
        # 同一内容可在多个日期被选中，只删除一份内容及其全部日期索引；批量操作同一事务。
        with self.connection:
            self.connection.executemany("DELETE FROM clips WHERE id=?",
                                        [(record_id,) for record_id in dict.fromkeys(record_ids)])

    def clear_unpinned(self):
        with self.connection:
            self.connection.execute("DELETE FROM clips WHERE pinned=0 AND marked=0")

    def cleanup_before_stats(self, cutoff, include_protected=False):
        cutoff = date.fromisoformat(cutoff).isoformat()
        eligible = "1" if include_protected else "c.pinned=0 AND c.marked=0"
        old_days = self.connection.execute(f"""
            SELECT count(*) FROM clip_copy_days d JOIN clips c ON c.id=d.clip_id
            WHERE d.copy_day<? AND ({eligible})
        """, (cutoff,)).fetchone()[0]
        # 同一内容有较新的复制日期时只删旧归档，不删除共享的原图/正文。
        orphaned = self.connection.execute(f"""
            SELECT count(*) AS records, coalesce(sum(c.kind='image'),0) AS images
            FROM clips c WHERE ({eligible})
              AND EXISTS (SELECT 1 FROM clip_copy_days d WHERE d.clip_id=c.id AND d.copy_day<?)
              AND NOT EXISTS (SELECT 1 FROM clip_copy_days d WHERE d.clip_id=c.id AND d.copy_day>=?)
        """, (cutoff, cutoff)).fetchone()
        protected = self.connection.execute("""
            SELECT count(*) FROM clip_copy_days d JOIN clips c ON c.id=d.clip_id
            WHERE d.copy_day<? AND (c.pinned=1 OR c.marked=1)
        """, (cutoff,)).fetchone()[0] if not include_protected else 0
        return {"days": old_days, "records": orphaned["records"], "images": orphaned["images"],
                "protected_days": protected}

    def delete_before(self, cutoff, include_protected=False):
        cutoff = date.fromisoformat(cutoff).isoformat()
        eligible = "1" if include_protected else "c.pinned=0 AND c.marked=0"
        # 计数、删除正文/原图及归档使用同一写事务，任何一步失败全部回滚。
        self.connection.execute("BEGIN IMMEDIATE")
        with self.connection:
            result = self.cleanup_before_stats(cutoff, include_protected)
            self.connection.execute(f"""
                DELETE FROM clips WHERE id IN (
                    SELECT c.id FROM clips c WHERE ({eligible})
                      AND EXISTS (SELECT 1 FROM clip_copy_days d WHERE d.clip_id=c.id AND d.copy_day<?)
                      AND NOT EXISTS (SELECT 1 FROM clip_copy_days d WHERE d.clip_id=c.id AND d.copy_day>=?)
                )
            """, (cutoff, cutoff))
            self.connection.execute(f"""
                DELETE FROM clip_copy_days WHERE copy_day<? AND clip_id IN (
                    SELECT c.id FROM clips c WHERE ({eligible})
                )
            """, (cutoff,))
        return result

    def close(self):
        self.connection.close()
