"""SQLite persistence for the briefing system: HTTP cache, delivery log, delta snapshots."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis_core import paths


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else paths.state_db()
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, fetched_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, slot TEXT NOT NULL,
                kind TEXT NOT NULL, delivered_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots (name TEXT PRIMARY KEY, value TEXT NOT NULL, saved_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS seen (kind TEXT NOT NULL, item_id TEXT NOT NULL, seen_at REAL NOT NULL,
                PRIMARY KEY (kind, item_id));
            CREATE TABLE IF NOT EXISTS alert_log (key TEXT PRIMARY KEY, fired_at REAL NOT NULL);
            """
        )
        self._db.commit()
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- cache -----------------------------------------------------------------
    def cache_get(self, key: str, max_age_s: float | None = None) -> tuple[Any, float] | None:
        with self._lock:
            row = self._db.execute("SELECT value, fetched_at FROM cache WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        if max_age_s is not None and time.time() - row[1] > max_age_s:
            return None
        return json.loads(row[0]), row[1]

    def cache_put(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO cache(key,value,fetched_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, fetched_at=excluded.fetched_at",
                (key, json.dumps(value, default=str), time.time()),
            )
            self._db.commit()

    # -- deliveries ------------------------------------------------------------------
    def mark_delivered(self, day: date, slot: str, kind: str, when: datetime | None = None) -> None:
        ts = (when or datetime.now()).timestamp()
        with self._lock:
            self._db.execute(
                "INSERT INTO deliveries(day,slot,kind,delivered_at) VALUES(?,?,?,?)", (day.isoformat(), slot, kind, ts)
            )
            self._db.commit()

    def delivered(self, day: date, slot: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM deliveries WHERE day=? AND slot=?", (day.isoformat(), slot)).fetchone()
        return row is not None

    def last_delivery(self) -> datetime | None:
        with self._lock:
            row = self._db.execute("SELECT MAX(delivered_at) FROM deliveries").fetchone()
        return datetime.fromtimestamp(row[0]).astimezone() if row and row[0] else None

    # -- snapshots (delta basis) --------------------------------------------------------
    def snapshot_get(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM snapshots WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def snapshot_put(self, name: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO snapshots(name,value,saved_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value, saved_at=excluded.saved_at",
                (name, json.dumps(value, default=str), time.time()),
            )
            self._db.commit()

    # -- seen items -------------------------------------------------------------------------
    def seen(self, kind: str, item_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM seen WHERE kind=? AND item_id=?", (kind, item_id)).fetchone() is not None

    def mark_seen(self, kind: str, item_ids: list[str]) -> None:
        now = time.time()
        with self._lock:
            self._db.executemany(
                "INSERT OR IGNORE INTO seen(kind,item_id,seen_at) VALUES(?,?,?)", [(kind, i, now) for i in item_ids]
            )
            self._db.execute("DELETE FROM seen WHERE seen_at < ?", (now - 14 * 86400,))
            self._db.commit()

    # -- alert cooldowns --------------------------------------------------------------------------
    def alert_fired_at(self, key: str) -> float | None:
        with self._lock:
            row = self._db.execute("SELECT fired_at FROM alert_log WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def alert_mark(self, key: str, when: float | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO alert_log(key,fired_at) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET fired_at=excluded.fired_at",
                (key, when or time.time()),
            )
            self._db.commit()


def briefing_day(now: datetime, rollover_hour: int) -> date:
    """The 'day' a moment belongs to; 02:00 still counts as yesterday when rollover is 04:00."""
    return (now - timedelta(hours=rollover_hour)).date()
