"""Open other apps' SQLite databases read-only without disturbing them."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

COPY_MIN_INTERVAL_S = 60.0


class ReadOnlyDB:
    """A read-only handle on a live app database.

    First tries a direct ``mode=ro`` open (readers never block the app's writers in WAL mode). If
    that fails, it falls back to a private copy of the database plus its WAL, refreshed at most
    once a minute.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self._dir: Path | None = None
        self._copied_at = 0.0
        self._use_copy = False
        self._conn: sqlite3.Connection | None = None

    def exists(self) -> bool:
        return self.path.exists()

    def _copy(self) -> Path:
        if self._dir is None:
            self._dir = Path(tempfile.mkdtemp(prefix="jarvis-db-"))
        dst = self._dir / self.path.name
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self.path) + suffix)
            if src.exists():
                shutil.copy2(src, Path(str(dst) + suffix))
        self._copied_at = time.monotonic()
        return dst

    def _open(self) -> sqlite3.Connection:
        if not self._use_copy:
            try:
                conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=2.0)
                conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
                return conn
            except sqlite3.OperationalError as e:
                if "authorized" in str(e).lower() or "permission" in str(e).lower():
                    raise PermissionError(str(e)) from e
                log.debug("direct read-only open of %s failed (%s); using a copy", self.path, e)
                self._use_copy = True
        dst = self._copy()
        conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True, timeout=2.0)
        return conn

    def connect(self) -> sqlite3.Connection:
        """A fresh connection each call (cheap) so we always see the latest committed state."""
        if self._use_copy and time.monotonic() - self._copied_at > COPY_MIN_INTERVAL_S:
            self.close()
        if self._conn is None or self._use_copy is False:
            self.close()
            self._conn = self._open()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def cleanup(self) -> None:
        self.close()
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def columns(self, table: str) -> set[str]:
        try:
            rows = self.connect().execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error:
            return set()
        return {r[1] for r in rows}
