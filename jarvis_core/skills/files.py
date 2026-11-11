"""Files: find, read, list, create folders, move to Trash."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from jarvis_core import cards, paths
from jarvis_core.errors import SkillError
from jarvis_core.policy.shell_policy import is_protected_path
from jarvis_core.skills.base import Decision, I, Risk, S, Tool, ToolContext, ToolRegistry, obj

_FOLDER_WORDS = {
    "desktop": "Desktop", "downloads": "Downloads", "documents": "Documents", "pictures": "Pictures",
    "movies": "Movies", "music": "Music", "home": "",
}

MAX_READ = 6000
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".ts", ".yaml", ".yml", ".toml",
                 ".html", ".css", ".swift", ".sh", ".rtf", ".xml", ".ini", ".cfg", ".tsv"}


def resolve_path(ctx: ToolContext, raw: str) -> Path:
    raw = raw.strip().strip("\"'")
    key = raw.lower().strip("/").removeprefix("my ").removeprefix("the ")
    if key in _FOLDER_WORDS:
        return ctx.home_dir / _FOLDER_WORDS[key]
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = ctx.home_dir / p
    return p


CHANGES_KEY = "file_changes"
MAX_CHANGES = 10
UNDO_WINDOW_S = 7 * 86400  # an undo offered after a restart must be recent: old paths may mean something else by now
LOG_MAX_LINES = 200
LOG_KEEP = 100


def _log_path() -> Path:
    return paths.home() / "changes.jsonl"


def _valid(c: Any) -> bool:
    return (
        isinstance(c, dict) and c.get("op") in ("move", "trash") and isinstance(c.get("src"), str)
        and isinstance(c.get("dst" if c.get("op") == "move" else "trash"), str) and isinstance(c.get("id"), str)
        and isinstance(c.get("t"), (int, float))
    )


def _read_log() -> tuple[list[dict[str, Any]], set[str]]:
    """Records still undoable (newest last) from the private log: recent, well-formed and not already undone."""
    records: list[dict[str, Any]] = []
    undone: set[str] = set()
    try:
        lines = _log_path().read_text().splitlines()
    except OSError:
        return [], set()
    cutoff = time.time() - UNDO_WINDOW_S
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a half-written or damaged line must never block undo
        if isinstance(row, dict) and isinstance(row.get("undone"), str):
            undone.add(row["undone"])
        elif _valid(row) and row["t"] >= cutoff:
            records.append(row)
    return [r for r in records if r["id"] not in undone], undone


def _history(ctx: ToolContext) -> list[dict[str, Any]]:
    """The undo history. The first time it is needed in a session (after a restart) it is reloaded from the log."""
    if CHANGES_KEY not in ctx.state:
        ctx.state[CHANGES_KEY] = _read_log()[0][-MAX_CHANGES:]
    return ctx.state[CHANGES_KEY]


def _append(row: dict[str, Any]) -> None:
    try:
        fd = os.open(_log_path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass  # the log is a courtesy; undo works from memory


def _trim_log() -> None:
    """Keep the log small: once it passes LOG_MAX_LINES, rewrite it atomically with only the live records."""
    try:
        if len(_log_path().read_text().splitlines()) <= LOG_MAX_LINES:
            return
        live = _read_log()[0][-LOG_KEEP:]
        tmp = _log_path().with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in live))
        os.replace(tmp, _log_path())
    except OSError:
        pass


def _record(ctx: ToolContext, change: dict[str, Any]) -> None:
    """Remember a move or trash so it can be undone: the last 10 in memory, and paths only (never contents) in a private log."""
    hist = _history(ctx)
    change = {**change, "t": round(time.time(), 1), "id": os.urandom(6).hex()}
    hist.append(change)
    del hist[:-MAX_CHANGES]
    _append(change)
    _trim_log()


def _tilde(ctx: ToolContext, p: str | Path) -> str:
    return str(p).replace(str(ctx.home_dir), "~", 1)


def _guard(ctx: ToolContext, p: Path) -> Path:
    if is_protected_path(p, ctx.home_dir):
        raise SkillError("That location holds secrets, sir. I keep out of it.")
    return p


async def _last_used(ctx: ToolContext, paths_: list[str]) -> dict[str, float]:
    """``kMDItemLastUsedDate`` for each path (when you last opened it), via one mdls call."""
    from datetime import datetime

    if not paths_:
        return {}
    res = await ctx.run(["mdls", "-name", "kMDItemLastUsedDate", "-raw", "-nullMarker", "(null)", *paths_], timeout=8)
    if not res.ok:
        return {}
    out: dict[str, float] = {}
    for path, value in zip(paths_, res.out.split("\x00"), strict=False):
        value = value.strip()
        if not value or value == "(null)":
            continue
        try:
            out[path] = datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z").timestamp()
        except ValueError:
            continue
    return out


async def _find_files(ctx: ToolContext, a: dict[str, Any]) -> str:
    query = a["query"].strip()
    folder = resolve_path(ctx, a.get("folder") or "~")
    argv = ["mdfind", "-onlyin", str(folder), "-name", query]
    res = await ctx.run(argv, timeout=10)
    lines = [ln for ln in res.out.splitlines() if ln and not is_protected_path(Path(ln), ctx.home_dir)]
    if not lines:
        return f"No files matching '{query}'."
    # What you opened most recently first (Spotlight's last-used date), falling back to when the file changed.
    def mtime(p: str) -> float:
        try:
            return Path(p).stat().st_mtime
        except OSError:
            return 0.0

    used = await _last_used(ctx, lines[:40])
    lines.sort(key=lambda p: max(used.get(p, 0.0), mtime(p)), reverse=True)
    limit = int(a.get("limit") or 5)
    cards.post(ctx, cards.files_card(lines[:limit], str(ctx.home_dir)))
    return "\n".join(lines[:limit]) + (f"\n(and {len(lines) - limit} more)" if len(lines) > limit else "")


async def _list_dir(ctx: ToolContext, a: dict[str, Any]) -> str:
    p = _guard(ctx, resolve_path(ctx, a.get("path") or "~"))
    if not p.is_dir():
        raise SkillError("That isn't a folder, sir.")
    entries = sorted(p.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
    names = [e.name + ("/" if e.is_dir() else "") for e in entries if not e.name.startswith(".")]
    limit = int(a.get("limit") or 15)
    return ", ".join(names[:limit]) + (f" (and {len(names) - limit} more)" if len(names) > limit else "") or "Empty folder."


async def _read_file(ctx: ToolContext, a: dict[str, Any]) -> str:
    p = _guard(ctx, resolve_path(ctx, a["path"]))
    if not p.is_file():
        raise SkillError("I can't find that file, sir.")
    if p.suffix.lower() not in TEXT_SUFFIXES:
        raise SkillError("I can only read plain text files, sir.")
    data = p.read_bytes()[: MAX_READ * 4].decode("utf-8", errors="replace")
    return data[:MAX_READ] + ("\n[truncated]" if len(data) > MAX_READ else "")


async def _file_info(ctx: ToolContext, a: dict[str, Any]) -> str:
    p = _guard(ctx, resolve_path(ctx, a["path"]))
    if not p.exists():
        raise SkillError("That doesn't exist, sir.")
    st = p.stat()
    import datetime as dt

    mod = dt.datetime.fromtimestamp(st.st_mtime).strftime("%d %b %Y %H:%M")
    kind = "folder" if p.is_dir() else "file"
    return f"{p.name}: {kind}, {st.st_size} bytes, modified {mod}, at {p}"


async def _reveal(ctx: ToolContext, a: dict[str, Any]) -> str:
    p = _guard(ctx, resolve_path(ctx, a["path"]))
    if not p.exists():
        raise SkillError("That doesn't exist, sir.")
    res = await ctx.run(["open", "-R", str(p)])
    if not res.ok:
        raise SkillError("I couldn't reveal that, sir.")
    return f"Showing {p.name} in Finder."


MAX_WRITE = 200_000
SAVE_FOLDER = ("Desktop", "Jarvis")  # where a bare file name is saved


def _write_target(ctx: ToolContext, raw: str) -> Path:
    """A bare name like 'reverse.py' goes to ~/Desktop/Jarvis; anything with a folder is taken as written."""
    raw = raw.strip().strip("\"'")
    if "/" not in raw and not raw.startswith("~"):
        return ctx.home_dir.joinpath(*SAVE_FOLDER, raw)
    return resolve_path(ctx, raw)


def _unique(p: Path) -> Path:
    """Never overwrite: reverse.py -> reverse_2.py -> reverse_3.py."""
    if not p.exists():
        return p
    for n in range(2, 100):
        q = p.with_name(f"{p.stem}_{n}{p.suffix}")
        if not q.exists():
            return q
    raise SkillError("There are too many files with that name, sir.")


async def _write_file(ctx: ToolContext, a: dict[str, Any]) -> str:
    name = (a.get("path") or "").strip()
    if not name:
        raise SkillError("What should I call the file, sir?")
    content = a.get("content")
    if content is None or not str(content).strip():
        raise SkillError("There was nothing to write, sir.")
    text = str(content)
    if len(text) > MAX_WRITE:
        raise SkillError("That's too large to save in one go, sir.")
    p = _unique(_guard(ctx, _write_target(ctx, name)))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    except OSError as e:
        raise SkillError(f"I couldn't save that file, sir. {e.strerror or ''}".strip()) from e
    ctx.audit.write("file_written", path=str(p), chars=len(text))
    where = str(p).replace(str(ctx.home_dir), "~")
    return f"Saved {len(text.splitlines())} lines to {where}."


async def _make_folder(ctx: ToolContext, a: dict[str, Any]) -> str:
    p = _guard(ctx, resolve_path(ctx, a["path"]))
    p.mkdir(parents=True, exist_ok=True)
    return f"Created {p}."


async def _trash(ctx: ToolContext, a: dict[str, Any]) -> str:
    p = _guard(ctx, resolve_path(ctx, a["path"]))
    if not p.exists():
        raise SkillError("That doesn't exist, sir.")
    if p == ctx.home_dir or p.parent == p:
        raise SkillError("I won't trash that, sir.")
    # Finder's Trash keeps "Put Back" working; fall back to a manual move.
    try:
        await ctx.osa('on run argv\ntell application "Finder" to delete (POSIX file (item 1 of argv) as alias)\nend run', str(p))
    except SkillError:
        trash = ctx.home_dir / ".Trash"
        trash.mkdir(exist_ok=True)
        shutil.move(str(p), str(trash / p.name))
    _record(ctx, {"op": "trash", "src": str(p), "trash": str(ctx.home_dir / ".Trash" / p.name)})
    return f"Moved {p.name} to the Trash."


async def _move(ctx: ToolContext, a: dict[str, Any]) -> str:
    src = _guard(ctx, resolve_path(ctx, a["source"]))
    dst = _guard(ctx, resolve_path(ctx, a["destination"]))
    if not src.exists():
        raise SkillError("The source doesn't exist, sir.")
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise SkillError("Something already exists at the destination, sir.")
    shutil.move(str(src), str(dst))
    _record(ctx, {"op": "move", "src": str(src), "dst": str(dst)})
    return f"Moved {src.name} to {dst.parent}."


def _last_change(ctx: ToolContext) -> dict[str, Any] | None:
    hist = _history(ctx)
    return hist[-1] if hist else None


def _undo_paths(ctx: ToolContext, c: dict[str, Any]) -> tuple[Path, Path]:
    """(where the item is now, where it goes back to)."""
    return (Path(c["dst"]), Path(c["src"])) if c["op"] == "move" else (Path(c["trash"]), Path(c["src"]))


def _classify_undo(ctx: ToolContext, a: dict[str, Any]) -> Decision:
    c = _last_change(ctx)
    if c is None:
        return Decision.allow()  # the handler says plainly that there is nothing to undo
    now, back = _undo_paths(ctx, c)
    return Decision.confirm(f"Move {now.name} back to {_tilde(ctx, back.parent)}?")


async def _undo(ctx: ToolContext, a: dict[str, Any]) -> str:
    c = _last_change(ctx)
    if c is None:
        raise SkillError("I haven't moved or trashed anything that I can undo, sir.")
    now, back = _undo_paths(ctx, c)
    _guard(ctx, now)
    _guard(ctx, back)
    if not (now.exists() or now.is_symlink()):
        where = "the Trash" if c["op"] == "trash" else _tilde(ctx, now.parent)
        raise SkillError(f"I can't undo that, sir: {now.name} is no longer in {where}.")
    if back.exists() or back.is_symlink():
        raise SkillError(f"I can't undo that, sir: something already exists at {_tilde(ctx, back)}.")
    if not back.parent.is_dir():
        raise SkillError(f"I can't undo that, sir: {_tilde(ctx, back.parent)} no longer exists.")
    shutil.move(str(now), str(back))
    _history(ctx).pop()
    _append({"undone": c["id"], "t": round(time.time(), 1)})
    return f"Put {back.name} back in {_tilde(ctx, back.parent)}."


def register(reg: ToolRegistry) -> None:
    path = {"path": S("Path, or a folder word such as 'downloads'")}
    reg.register(Tool(
        "find_files", "Find files by name using Spotlight; newest first.",
        obj({"query": S("File name fragment"), "folder": S("Folder to search, default home"), "limit": I("Max results")}, ["query"]),
        _find_files, category="files"))
    reg.register(Tool("list_folder", "List the newest items in a folder.", obj({**path, "limit": I("Max items")}), _list_dir, category="files"))
    reg.register(Tool("read_text_file", "Read a plain text file (truncated).", obj(path, ["path"]), _read_file, category="files", untrusted_output=True))
    reg.register(Tool("file_info", "Size and modified time of a file or folder.", obj(path, ["path"]), _file_info, category="files"))
    reg.register(Tool("reveal_in_finder", "Show a file in Finder.", obj(path, ["path"]), _reveal, category="files"))
    reg.register(Tool(
        "write_file",
        "Save text, code, a script or notes to a file. Use this whenever the user asks you to write, save or put code or a "
        "document 'in a file': put the content here and tell them where it is, instead of reading it out. A bare name such as "
        "'reverse_linked_list.py' goes to ~/Desktop/Jarvis. Never overwrites: a clashing name gets _2, _3. Follow with open_path "
        "(optionally with_app) to open it.",
        obj({"path": S("File name (with extension) or a full path"), "content": S("The complete file contents")},
            ["path", "content"]),
        _write_file, category="files", timeout_s=20))
    reg.register(Tool("make_folder", "Create a folder.", obj(path, ["path"]), _make_folder, category="files"))
    reg.register(Tool(
        "move_to_trash", "Move a file or folder to the Trash.", obj(path, ["path"]), _trash,
        risk=Risk.CONFIRM, confirm_prompt=lambda a: f"Move {a.get('path')} to the Trash?", category="files"))
    reg.register(Tool(
        "move_file", "Move or rename a file or folder.",
        obj({"source": S("Current path"), "destination": S("New path or destination folder")}, ["source", "destination"]),
        _move, risk=Risk.CONFIRM,
        confirm_prompt=lambda a: f"Move {a.get('source')} to {a.get('destination')}?", category="files"))
    reg.register(Tool(
        "undo_last_change",
        "Undo the last file move or trash ('undo that', 'put it back'): moves the item back where it was. Never overwrites anything.",
        obj(), _undo, classify=_classify_undo, category="files"))
