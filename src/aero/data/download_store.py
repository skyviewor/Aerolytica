"""CDS download tracking — SQLite store for cds_downloads table."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path.cwd() / "aero_downloads.db"


class CDSDownloadStore:
    def __init__(self, db_path: Path | str | None = None, table: str = "cds_downloads"):
        self._db_path = Path(db_path) if db_path else DEFAULT_DB
        self._table = table
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                source           TEXT NOT NULL DEFAULT 'cds',
                request_id       TEXT,
                dataset_id       TEXT,
                variables        TEXT NOT NULL DEFAULT '[]',
                year             INTEGER,
                month            INTEGER,
                day              INTEGER,
                pressure_level   INTEGER,
                area             TEXT,
                data_format      TEXT DEFAULT 'netcdf',
                file_path        TEXT NOT NULL,
                file_size        INTEGER,
                download_url     TEXT,
                status           TEXT NOT NULL DEFAULT 'submitted',
                total_bytes      INTEGER,
                downloaded_bytes INTEGER DEFAULT 0,
                error_msg        TEXT,
                notes            TEXT,
                submitted_at     TEXT NOT NULL,
                completed_at     TEXT,
                updated_at       TEXT NOT NULL
            )
        """)
        indexes = [
            f"CREATE INDEX IF NOT EXISTS idx_{self._table}_status ON {self._table}(status)",
            f"CREATE INDEX IF NOT EXISTS idx_{self._table}_dataset ON {self._table}(dataset_id)",
            f"CREATE INDEX IF NOT EXISTS idx_{self._table}_source ON {self._table}(source)",
            f"CREATE INDEX IF NOT EXISTS idx_{self._table}_updated ON {self._table}(updated_at DESC)",
        ]
        for idx in indexes:
            conn.execute(idx)
        conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def insert(self, **kwargs) -> int:
        now = self._now()
        kwargs.setdefault("submitted_at", now)
        kwargs.setdefault("updated_at", now)
        kwargs.setdefault("status", "submitted")
        kwargs.setdefault("source", "cds")
        if "variables" in kwargs and isinstance(kwargs["variables"], list):
            kwargs["variables"] = json.dumps(kwargs["variables"], ensure_ascii=False)
        if "area" in kwargs and isinstance(kwargs["area"], (list, dict)):
            kwargs["area"] = json.dumps(kwargs["area"])
        columns = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        conn = self._connect()
        conn.execute(
            f"INSERT INTO {self._table} ({columns}) VALUES ({placeholders})",
            list(kwargs.values()),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def update(self, where: dict, **kwargs) -> int:
        if not kwargs:
            return 0
        kwargs["updated_at"] = self._now()
        if "variables" in kwargs and isinstance(kwargs["variables"], list):
            kwargs["variables"] = json.dumps(kwargs["variables"], ensure_ascii=False)
        if "area" in kwargs and isinstance(kwargs["area"], (list, dict)):
            kwargs["area"] = json.dumps(kwargs["area"])
        conds = " AND ".join(f"{k}=?" for k in where)
        sets = ", ".join(f"{k}=?" for k in kwargs)
        conn = self._connect()
        cursor = conn.execute(
            f"UPDATE {self._table} SET {sets} WHERE {conds}",
            list(kwargs.values()) + list(where.values()),
        )
        conn.commit()
        return cursor.rowcount

    def update_by_id(self, row_id: int, **kwargs) -> int:
        return self.update({"id": row_id}, **kwargs)

    def update_by_request_id(self, request_id: str, **kwargs) -> int:
        return self.update({"request_id": request_id}, **kwargs)

    def get(self, request_id: str | None = None, row_id: int | None = None) -> dict | None:
        conn = self._connect()
        if row_id is not None:
            row = conn.execute(f"SELECT * FROM {self._table} WHERE id=?", (row_id,)).fetchone()
        elif request_id:
            row = conn.execute(
                f"SELECT * FROM {self._table} WHERE request_id=?", (request_id,)
            ).fetchone()
        else:
            return None
        if row is None:
            return None
        return self._row_to_dict(row)

    def get_by_file_path(self, file_path: str) -> dict | None:
        conn = self._connect()
        row = conn.execute(
            f"""
            SELECT * FROM {self._table}
            WHERE file_path=?
            ORDER BY
              CASE WHEN status='completed_with_file' THEN 0 ELSE 1 END,
              updated_at DESC
            LIMIT 1
            """,
            (file_path,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_all(self, limit: int = 20, offset: int = 0) -> list[dict]:
        conn = self._connect()
        rows = conn.execute(
            f"SELECT * FROM {self._table} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_by_status(self, status: str, limit: int = 20) -> list[dict]:
        conn = self._connect()
        rows = conn.execute(
            f"SELECT * FROM {self._table} WHERE status=? ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_incomplete(self) -> list[dict]:
        incomplete = ("submitted", "queued", "running", "downloading", "download_failed", "error")
        placeholders = ", ".join("?" for _ in incomplete)
        conn = self._connect()
        rows = conn.execute(
            f"SELECT * FROM {self._table} WHERE status IN ({placeholders}) "
            f"ORDER BY updated_at DESC",
            list(incomplete),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_stats(self) -> dict:
        conn = self._connect()
        stats = {}
        for s in ("completed_with_file", "download_failed", "downloading",
                   "queued", "running", "submitted", "error", "confirmed", "verified"):
            count = conn.execute(
                f"SELECT COUNT(*) FROM {self._table} WHERE status=?", (s,)
            ).fetchone()[0]
            if count > 0:
                stats[s] = count
        total = conn.execute(f"SELECT COUNT(*) FROM {self._table}").fetchone()[0]
        stats["total"] = total
        return stats

    def delete_by_ids(self, ids: list[int]) -> int:
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        conn = self._connect()
        cursor = conn.execute(
            f"DELETE FROM {self._table} WHERE id IN ({placeholders})", ids
        )
        conn.commit()
        return cursor.rowcount

    def delete_by_status(self, status: str) -> int:
        conn = self._connect()
        cursor = conn.execute(f"DELETE FROM {self._table} WHERE status=?", (status,))
        conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        for field in ("variables", "area"):
            if d.get(field) and isinstance(d[field], str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d
