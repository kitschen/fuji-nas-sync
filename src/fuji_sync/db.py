"""SQLite state: registered cameras and the list of already transferred files."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    camera_id      TEXT PRIMARY KEY,      -- name the camera reports, e.g. FUJIFILM-X-E2-BD35
    name           TEXT NOT NULL,         -- display name, used for the NAS folder
    model          TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    mark_existing  INTEGER NOT NULL DEFAULT 0,  -- next transfer only records the files on the card as copied
    registered_at  REAL NOT NULL,
    last_seen_at   REAL,
    last_sync_at   REAL,
    last_error     TEXT,
    full_scan      INTEGER NOT NULL DEFAULT 0,  -- next transfer reads the whole card
    last_full_scan_at REAL,
    card_known     INTEGER,  -- files on the card that were in the list after the last scan
    card_free      INTEGER   -- free bytes on the card then, plus the size of the files not yet copied
);
CREATE TABLE IF NOT EXISTS transferred (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id      TEXT NOT NULL,
    filename       TEXT NOT NULL,         -- e.g. DSCF2090.JPG
    captured       TEXT NOT NULL,         -- camera local time, ISO; distinguishes reused file numbers
    size           INTEGER NOT NULL,
    nas_path       TEXT NOT NULL,         -- empty when only marked as copied
    transferred_at REAL NOT NULL,
    UNIQUE (camera_id, filename, captured, size)
);
"""

# Columns added after the first release: (name, definition) for ALTER TABLE on existing databases
CAMERA_COLUMNS = [
    ("full_scan", "INTEGER NOT NULL DEFAULT 0"),
    ("last_full_scan_at", "REAL"),
    ("card_known", "INTEGER"),
    ("card_free", "INTEGER"),
]

FileKey = tuple[str, str, int]  # filename, captured, size


@dataclass
class Camera:
    camera_id: str
    name: str
    model: str
    enabled: bool
    mark_existing: bool
    registered_at: float
    last_seen_at: float | None
    last_sync_at: float | None
    last_error: str | None
    full_scan: bool = False
    last_full_scan_at: float | None = None
    card_known: int | None = None
    card_free: int | None = None


@dataclass
class TransferredFile:
    id: int
    camera_id: str
    filename: str
    captured: str
    size: int
    nas_path: str
    transferred_at: float


def default_name(camera_id: str, model: str) -> str:
    """FUJIFILM-X-E2-BD35 -> X-E2 BD35"""
    suffix = camera_id.rsplit("-", 1)[-1] if "-" in camera_id else ""
    if model:
        return f"{model} {suffix}".strip() if suffix and suffix != model else model
    return camera_id.removeprefix("FUJIFILM-") or "Camera"


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(cameras)")}
            for name, definition in CAMERA_COLUMNS:
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE cameras ADD COLUMN {name} {definition}")

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock, self._conn:
            return self._conn.execute(sql, params).rowcount

    # Cameras

    def register_camera(self, camera_id: str, model: str, mark_existing: bool = False) -> Camera:
        """Add a camera, or refresh the model of a known one (its settings are kept)."""
        existing = self.get_camera(camera_id)
        if existing:
            if model and model != existing.model:
                self._execute("UPDATE cameras SET model = ? WHERE camera_id = ?", (model, camera_id))
        else:
            self._execute(
                """INSERT INTO cameras (camera_id, name, model, enabled, mark_existing, registered_at)
                   VALUES (?, ?, ?, 1, ?, ?)""",
                (camera_id, default_name(camera_id, model), model, int(mark_existing), time.time()),
            )
        return self.get_camera(camera_id)

    def list_cameras(self) -> list[Camera]:
        return [self._camera(r) for r in self._query("SELECT * FROM cameras ORDER BY registered_at")]

    def get_camera(self, camera_id: str) -> Camera | None:
        rows = self._query("SELECT * FROM cameras WHERE camera_id = ?", (camera_id,))
        return self._camera(rows[0]) if rows else None

    def update_camera(self, camera_id: str, *, name: str | None = None, enabled: bool | None = None,
                      mark_existing: bool | None = None, full_scan: bool | None = None) -> bool:
        changed = 0
        for column, value in (("name", name), ("enabled", enabled), ("mark_existing", mark_existing),
                              ("full_scan", full_scan)):
            if value is not None:
                value = int(value) if isinstance(value, bool) else value
                changed += self._execute(f"UPDATE cameras SET {column} = ? WHERE camera_id = ?", (value, camera_id))
        return changed > 0

    def record_camera_seen(self, camera_id: str) -> None:
        self._execute("UPDATE cameras SET last_seen_at = ? WHERE camera_id = ?", (time.time(), camera_id))

    def record_camera_sync(self, camera_id: str, error: str | None) -> None:
        self._execute(
            "UPDATE cameras SET last_sync_at = ?, last_error = ? WHERE camera_id = ?", (time.time(), error, camera_id)
        )

    def record_card(self, camera_id: str, known: int, free: int | None, full_scan: bool) -> None:
        """What the next quick scan checks against (see sync.SyncService._scan)."""
        self._execute("UPDATE cameras SET card_known = ?, card_free = ? WHERE camera_id = ?", (known, free, camera_id))
        if full_scan:
            self._execute("UPDATE cameras SET last_full_scan_at = ?, full_scan = 0 WHERE camera_id = ?",
                          (time.time(), camera_id))

    def delete_camera(self, camera_id: str) -> bool:
        return self._execute("DELETE FROM cameras WHERE camera_id = ?", (camera_id,)) > 0

    @staticmethod
    def _camera(row: sqlite3.Row) -> Camera:
        return Camera(**{**dict(row), "enabled": bool(row["enabled"]), "mark_existing": bool(row["mark_existing"]),
                         "full_scan": bool(row["full_scan"])})

    # Transferred files

    def transferred_keys(self, camera_id: str) -> set[FileKey]:
        rows = self._query("SELECT filename, captured, size FROM transferred WHERE camera_id = ?", (camera_id,))
        return {(r["filename"], r["captured"], r["size"]) for r in rows}

    def add_transferred(self, camera_id: str, filename: str, captured: str, size: int, nas_path: str) -> None:
        self._execute(
            """INSERT INTO transferred (camera_id, filename, captured, size, nas_path, transferred_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(camera_id, filename, captured, size) DO UPDATE SET
                   nas_path=excluded.nas_path, transferred_at=excluded.transferred_at""",
            (camera_id, filename, captured, size, nas_path, time.time()),
        )

    def add_transferred_many(self, camera_id: str, keys: list[FileKey]) -> None:
        """Mark files as copied without a NAS path (the "mark existing" first transfer)."""
        now = time.time()
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT OR IGNORE INTO transferred (camera_id, filename, captured, size, nas_path, transferred_at)
                   VALUES (?, ?, ?, ?, '', ?)""",
                [(camera_id, f, c, s, now) for f, c, s in keys],
            )

    def list_transferred(
        self, camera_id: str | None = None, search: str = "", limit: int = 500, offset: int = 0
    ) -> tuple[list[TransferredFile], int]:
        where, params = ["1=1"], []
        if camera_id:
            where.append("camera_id = ?")
            params.append(camera_id)
        if search:
            where.append("(filename LIKE ? OR nas_path LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        clause = " AND ".join(where)
        total = self._query(f"SELECT COUNT(*) AS n FROM transferred WHERE {clause}", tuple(params))[0]["n"]
        rows = self._query(
            f"SELECT * FROM transferred WHERE {clause} ORDER BY transferred_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [TransferredFile(**dict(r)) for r in rows], total

    def delete_transferred(self, file_id: int) -> bool:
        return self._execute("DELETE FROM transferred WHERE id = ?", (file_id,)) > 0

    def reset_transferred(self, camera_id: str | None = None) -> int:
        if camera_id:
            return self._execute("DELETE FROM transferred WHERE camera_id = ?", (camera_id,))
        return self._execute("DELETE FROM transferred")
