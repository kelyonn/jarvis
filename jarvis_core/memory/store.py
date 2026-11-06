"""SQLite fact store. Small data, so search is a simple, dependable token-overlap score (no embeddings, no network)."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from jarvis_core import paths
from jarvis_core.interfaces import Fact

KINDS = ("fact", "person", "preference", "style", "loop", "episode")
MAX_FACTS = 2000
_STOP = frozenset(
    "the a an and or of to in on at for with is are was were be been am i you he she it we they my me your our "
    "that this those these do does did have has had will would can could should what who whom how when where why "
    "about from by as not no yes please jarvis jarvis sir tell say".split()
)
_WORD = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(text.lower()):
        if w in _STOP or len(w) < 2:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)  # crude plural folding
    return out


def _norm(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


class SQLiteMemory:
    """Implements :class:`jarvis_core.interfaces.MemoryStore`."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else paths.memory_db()
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS facts ("
            "id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, norm TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}',"
            "created_at REAL NOT NULL, confidence REAL NOT NULL DEFAULT 1.0)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS facts_kind ON facts(kind, created_at)")
        self._db.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _row(r: tuple) -> Fact:
        return Fact(id=r[0], kind=r[1], text=r[2], meta=json.loads(r[3] or "{}"), created_at=r[4], confidence=r[5])

    _COLS = "id, kind, text, meta, created_at, confidence"

    # ------------------------------------------------------------ MemoryStore

    def add(self, fact: Fact) -> str:
        """Insert a fact. An identical fact (same kind and wording) is refreshed instead of duplicated."""
        norm = _norm(fact.text)
        if not norm:
            raise ValueError("empty fact")
        with self._lock:
            row = self._db.execute("SELECT id FROM facts WHERE kind=? AND norm=?", (fact.kind, norm)).fetchone()
            now = fact.created_at or time.time()
            if row:
                self._db.execute("UPDATE facts SET created_at=?, meta=?, confidence=max(confidence, ?) WHERE id=?",
                                 (now, json.dumps(fact.meta), fact.confidence, row[0]))
                self._db.commit()
                return row[0]
            fid = fact.id or uuid.uuid4().hex[:12]
            self._db.execute("INSERT OR REPLACE INTO facts VALUES (?,?,?,?,?,?,?)",
                             (fid, fact.kind, fact.text.strip(), norm, json.dumps(fact.meta), now, fact.confidence))
            over = self._db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] - MAX_FACTS
            if over > 0:  # drop the oldest low-value facts, never people or preferences
                self._db.execute(
                    "DELETE FROM facts WHERE id IN (SELECT id FROM facts WHERE kind IN ('episode','fact','loop','style') "
                    "ORDER BY created_at LIMIT ?)", (over,))
            self._db.commit()
            return fid

    def search(self, query: str, k: int = 5) -> list[Fact]:
        q = tokens(query)
        if not q:
            return []
        scored: list[tuple[float, Fact]] = []
        now = time.time()
        for f in self.list():
            if f.kind == "style":
                continue  # style cards are fetched explicitly by the drafter
            ft = tokens(f.text) | tokens(" ".join(str(v) for v in f.meta.values()))
            hit = q & ft
            if not hit:
                continue
            score = len(hit) / math.sqrt(len(ft) or 1) + 0.15 * len(hit) / len(q)
            score += 0.05 * math.exp(-(now - f.created_at) / (30 * 86400))  # tie-break towards recent
            scored.append((score, f))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [f for _, f in scored[:k]]

    def list(self, kind: str | None = None) -> list[Fact]:
        with self._lock:
            if kind:
                rows = self._db.execute(f"SELECT {self._COLS} FROM facts WHERE kind=? ORDER BY created_at DESC", (kind,))
            else:
                rows = self._db.execute(f"SELECT {self._COLS} FROM facts ORDER BY created_at DESC")
            return [self._row(r) for r in rows.fetchall()]

    def delete(self, fact_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM facts WHERE id=?", (fact_id,))
            self._db.commit()

    # ------------------------------------------------------------------ extras

    def get(self, fact_id: str) -> Fact | None:
        with self._lock:
            r = self._db.execute(f"SELECT {self._COLS} FROM facts WHERE id=?", (fact_id,)).fetchone()
        return self._row(r) if r else None

    def last(self, kinds: tuple[str, ...] = ("fact", "person", "preference", "loop")) -> Fact | None:
        marks = ",".join("?" * len(kinds))
        with self._lock:
            r = self._db.execute(
                f"SELECT {self._COLS} FROM facts WHERE kind IN ({marks}) ORDER BY created_at DESC LIMIT 1", kinds).fetchone()
        return self._row(r) if r else None

    def about(self, name: str) -> list[Fact]:
        """Every fact mentioning ``name`` (whole word, case-insensitive), style cards excluded."""
        pat = re.compile(rf"\b{re.escape(name.strip())}\b", re.I)
        out = []
        for f in self.list():
            if f.kind == "style":
                continue
            blob = f.text + " " + " ".join(str(v) for v in f.meta.values())
            if pat.search(blob):
                out.append(f)
        return out

    def delete_many(self, facts: list[Fact]) -> int:
        with self._lock:
            for f in facts:
                self._db.execute("DELETE FROM facts WHERE id=?", (f.id,))
            self._db.commit()
        return len(facts)

    def clear(self) -> int:
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            self._db.execute("DELETE FROM facts")
            self._db.commit()
            self._db.execute("VACUUM")
        return n

    def snippets(self, query: str, k: int = 5) -> list[str]:
        """Prompt context: best matches plus the standing preferences."""
        out = [f.text for f in self.search(query, k)]
        for f in self.list("preference")[:3]:
            if f.text not in out:
                out.append(f.text)
        return out[: k + 3]

    def recent_episodes(self, n: int = 3, within_days: float = 14.0) -> list[str]:
        """The last few exchanges (oldest first), dated, for picking a conversation back up."""
        cutoff = time.time() - within_days * 86400
        out = [f for f in self.list("episode")[:n] if f.created_at >= cutoff]
        return [f"Earlier conversation: {f.text}" for f in reversed(out)]

    def upsert_style(self, app: str, text: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM facts WHERE kind='style' AND json_extract(meta,'$.app')=?", (app,))
            self._db.commit()
        self.add(Fact(kind="style", text=text, meta={"app": app}))

    def close(self) -> None:
        with self._lock:
            self._db.close()
