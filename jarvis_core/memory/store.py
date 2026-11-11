"""SQLite fact store. Small data, so search is dependable word matching with a few offline helpers (no embeddings, no network): stemming, a general synonym lexicon, and word pairings learned from the person's own facts."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from rapidfuzz import fuzz

from jarvis_core import paths
from jarvis_core.interfaces import Fact

KINDS = ("fact", "person", "preference", "style", "loop", "episode")
MAX_FACTS = 2000
LEN_EXP = 0.5  # how hard long facts are marked down; shorter means long facts compete better
_STOP = frozenset(
    "the a an and or of to in on at for with is are was were be been am i you he she it we they my me your our "
    "that this those these do does did have has had will would can could should what who whom how when where why "
    "about from by as not no yes please jarvis jarvis sir tell say often out up long much many kind really actually just".split()
)
_WORD = re.compile(r"[a-z0-9]+")
# Spellings and short forms that mean the same thing, folded to one token so "my bro" finds "my brother".
_ALIASES = {
    "bro": "brother", "sis": "sister", "mum": "mom", "mummy": "mom", "mommy": "mom", "mother": "mom", "papa": "dad",
    "father": "dad", "daddy": "dad", "colour": "color", "favourite": "favorite", "grandma": "grandmother",
    "grandpa": "grandfather", "gf": "girlfriend", "bf": "boyfriend", "hubby": "husband",
}


# General-purpose concept groups: words that point at the same kind of fact. A question word finds facts using any of its
# group mates, at a discount. Kept generic on purpose (no names, no one person's facts).
_CONCEPTS = [
    "birthday birth born dob age birthdate",
    "study studying college university school student campus degree",
    "workout gym train training exercise fitness lift",
    "employer company job working intern internship role position hire hiring career employment",
    "married marry marriage wife husband spouse partner kids children wedding",
    "injury injuries injured ligament hurt pain sprain surgery",
    "startup founder cofounder venture company business",
    "salary pay paid stipend ctc lpa money earn income wage compensation",
    "phone mobile number call cell contact",
    "email mail address inbox gmail",
    "live living lives city home reside stay based location",
    "program programming code coding stack develop developer",
    "wake wakes waking morning alarm routine schedule",
    "still active running alive dead die died shut closed closure stopped operating",
    "final capstone thesis project",
    "duration length hours minutes",
    "offer offers weighing deciding options choosing choose",
    "competitor competitors rival rivals alternative",
    "store storage ssd disk drive keep stored",
    "board boards platform portal jobboard",
    "eat eating food meal diet nutrition cutting",
    "prefer preference favorite taste",
    "goal goals dream aim target ambition plan",
    "mom mother dad father parent parents family sibling brother sister",
    "mba business masters postgraduate",
    "interview round assessment test screening",
]


def _stem(w: str) -> str:
    """Light suffix folding so run/running/runs and train/training/trained meet."""
    if len(w) > 5 and w.endswith("ied"):
        return w[:-3] + "y"
    for suf in ("ing", "ed", "ly", "es"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


_PHRASES = (("work out", "workout"), ("working out", "workout"), ("final year", "capstone"), ("date of birth", "birthday"),
            ("job board", "jobboard"), ("job site", "jobboard"))


# The basics about a person (name, college, city, age). They go into every prompt, because a question like "who am I?" shares
# no words with the facts that answer it, so searching by the question alone finds nothing.
CORE_RE = re.compile(r"\b(?:my name is|i go by|i live in|i'm an? [\w ]*student|i'm \d+ as of|i was born|i'm in sem)\b", re.I)
FACT_KINDS = ("fact", "person", "preference", "loop")  # things about the user, as opposed to episodes (chat summaries) and style cards


def tokens(text: str) -> set[str]:
    out = set()
    text = text.lower()
    for a, b in _PHRASES:
        text = text.replace(a, b)
    for w in _WORD.findall(text):
        if w in _STOP or len(w) < 2:
            continue
        w = w[:-1] if len(w) > 3 and w.endswith("s") else w  # crude plural folding
        out.add(_stem(_ALIASES.get(w, w)))
    return out


_CONCEPT_OF: dict[str, set[int]] = {}
_CONCEPT_WORDS: list[set[str]] = []
for _i, _g in enumerate(_CONCEPTS):
    _ws = tokens(_g)
    _CONCEPT_WORDS.append(_ws)
    for _w in _ws:
        _CONCEPT_OF.setdefault(_w, set()).add(_i)


def _concept_mates(t: str) -> set[str]:
    out: set[str] = set()
    for i in _CONCEPT_OF.get(t, ()):
        out |= _CONCEPT_WORDS[i]
    out.discard(t)
    return out


def _near(t: str, u: str) -> bool:
    """Close spellings only: short words must be almost identical, so "lunch" does not become "launch"."""
    if len(u) < 5 or u[0] != t[0]:
        return False
    if min(len(t), len(u)) >= 7:
        return fuzz.ratio(t, u) >= 80
    return len(t) == len(u) and sum(a != b for a, b in zip(t, u)) <= 1  # one wrong letter, same length


def _norm(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


def _associations(pool, bags, df, q) -> dict[str, dict[str, float]]:
    """For each question word, the few words that keep showing up in the same facts as it (so "startup" leads to the name
    of the startup). Learned from the person's own facts, so it needs no model and gets better as they say more."""
    out: dict[str, dict[str, float]] = {}
    n = len(pool) or 1
    for t in q:
        if df.get(t, 0) < 2:
            continue
        co: dict[str, int] = {}
        for bag in bags.values():
            if t in bag:
                for u in bag:
                    if u != t:
                        co[u] = co.get(u, 0) + 1
        ranked = []
        for u, c in co.items():
            if c >= 2 and df[u] >= 3 and u not in _CONCEPT_OF:
                pmi = math.log(c * n / (df[t] * df[u]))
                if pmi > 1.0 and c / df[u] >= 0.5:
                    ranked.append((pmi * c / df[u], u))
        ranked.sort(reverse=True)
        if ranked:
            out[t] = {u: 0.45 for _, u in ranked[:4]}
    return out


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

    def replace(self, fact_id: str, fact: Fact) -> None:
        """Overwrite an existing fact's wording and details in place (the newer statement wins)."""
        norm = _norm(fact.text)
        if not norm:
            raise ValueError("empty fact")
        with self._lock:
            self._db.execute(
                "UPDATE facts SET text=?, norm=?, meta=?, created_at=?, confidence=? WHERE id=?",
                (fact.text.strip(), norm, json.dumps(fact.meta), fact.created_at or time.time(), fact.confidence, fact_id))
            self._db.commit()

    def search(
        self, query: str, k: int = 5, *, kinds: tuple[str, ...] | None = None, since: float | None = None
    ) -> list[Fact]:
        """Best matches for ``query``. Words are weighted by rarity (a name counts for more than "likes"), near-spellings
        count a little, and newer facts win ties. ``kinds`` and ``since`` narrow the search (for example episodes from
        the last week)."""
        q = tokens(query)
        if not q:
            return []
        pool = [f for f in self.list() if f.kind != "style"]  # style cards are fetched explicitly by the drafter
        bags = {f.id: tokens(f.text) | tokens(" ".join(str(v) for v in f.meta.values())) for f in pool}
        n = len(pool) or 1
        df: dict[str, int] = {}
        for bag in bags.values():
            for t in bag:
                df[t] = df.get(t, 0) + 1
        weight = {t: math.log(1 + n / (1 + df.get(t, 0))) for t in q}
        total = sum(weight.values()) or 1.0
        # Other words that stand in for each question word: same-concept words and words this person's own facts pair it with.
        assoc = _associations(pool, bags, df, q)
        stand_in: dict[str, dict[str, float]] = {}
        for t in q:
            alts = {u: 0.6 for u in _concept_mates(t)}
            for u, p in assoc.get(t, {}).items():
                alts[u] = max(alts.get(u, 0.0), p)
            stand_in[t] = alts
        scored: list[tuple[float, Fact]] = []
        now = time.time()
        for f in pool:
            if (kinds and f.kind not in kinds) or (since is not None and f.created_at < since):
                continue
            ft = bags[f.id]
            hit = 0.0
            for t in q:
                if t in ft:
                    hit += weight[t]
                    continue
                best = 0.0
                if len(t) >= 5 and any(_near(t, u) for u in ft):
                    best = max(best, 0.7)  # a near-spelling ("colur", "Arjan"): counts, but less than the real word
                for u, factor in stand_in[t].items():
                    if factor > best and u in ft:
                        best = factor
                hit += best * weight[t]
            if hit <= 0:
                continue
            score = hit / (len(ft) or 1) ** LEN_EXP + 0.15 * hit / total
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
        core = sorted((f for f in self.list() if f.kind in FACT_KINDS and CORE_RE.search(f.text)), key=lambda f: f.created_at)
        out = [f.text for f in core[:5]]
        out += [f.text for f in self.search(query, k, kinds=FACT_KINDS) if f.text not in out]  # never a log of past chats
        for f in self.list("preference")[:3]:
            if f.text not in out:
                out.append(f.text)
        return out[: k + 8]

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
