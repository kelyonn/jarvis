"""Undo for file moves and trashing: restores only when safe, never overwrites, keeps a private paths-only log."""

from __future__ import annotations

import json
import stat

import pytest
from fakes import FakeRunner
from test_skills import make_ctx

from jarvis_core import paths
from jarvis_core.errors import SkillError
from jarvis_core.skills import files as F
from jarvis_core.skills.base import CmdResult
from jarvis_core.skills.registry import build_registry


def rig(tmp_path, apps_dir, runner=None):
    ctx = make_ctx(tmp_path, apps_dir, runner or FakeRunner())
    reg = build_registry()
    return ctx, reg.get("move_file"), reg.get("move_to_trash"), reg.get("undo_last_change")


def touch(p, text="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


async def test_move_then_undo(tmp_path, apps_dir):
    ctx, move, _, undo = rig(tmp_path, apps_dir)
    src = touch(ctx.home_dir / "Documents" / "report.pdf", "data")
    (ctx.home_dir / "Archive").mkdir()
    await move.handler(ctx, {"source": str(src), "destination": str(ctx.home_dir / "Archive")})
    assert not src.exists()
    d = undo.decide(ctx, {})
    assert d.action == "confirm" and d.prompt == "Move report.pdf back to ~/Documents?"
    out = await undo.handler(ctx, {})
    assert out == "Put report.pdf back in ~/Documents." and src.read_text() == "data"
    assert not (ctx.home_dir / "Archive" / "report.pdf").exists()
    with pytest.raises(SkillError, match="anything that I can undo"):
        await undo.handler(ctx, {})  # the record was popped


async def test_nothing_recorded(tmp_path, apps_dir):
    ctx, _, _, undo = rig(tmp_path, apps_dir)
    assert undo.decide(ctx, {}).action == "allow"
    with pytest.raises(SkillError, match="anything that I can undo"):
        await undo.handler(ctx, {})


async def test_destination_vanished(tmp_path, apps_dir):
    ctx, move, _, undo = rig(tmp_path, apps_dir)
    src = touch(ctx.home_dir / "a.txt")
    await move.handler(ctx, {"source": str(src), "destination": str(ctx.home_dir / "b.txt")})
    (ctx.home_dir / "b.txt").unlink()
    with pytest.raises(SkillError, match="no longer in"):
        await undo.handler(ctx, {})


async def test_source_now_occupied_is_never_overwritten(tmp_path, apps_dir):
    ctx, move, _, undo = rig(tmp_path, apps_dir)
    src = touch(ctx.home_dir / "a.txt", "old")
    await move.handler(ctx, {"source": str(src), "destination": str(ctx.home_dir / "b.txt")})
    touch(src, "new file in its place")
    with pytest.raises(SkillError, match="already exists"):
        await undo.handler(ctx, {})
    assert src.read_text() == "new file in its place" and (ctx.home_dir / "b.txt").read_text() == "old"


async def test_original_folder_gone(tmp_path, apps_dir):
    ctx, move, _, undo = rig(tmp_path, apps_dir)
    src = touch(ctx.home_dir / "Docs" / "a.txt")
    await move.handler(ctx, {"source": str(src), "destination": str(ctx.home_dir / "a.txt")})
    src.parent.rmdir()
    with pytest.raises(SkillError, match="no longer exists"):
        await undo.handler(ctx, {})


async def test_protected_paths_are_refused(tmp_path, apps_dir):
    ctx, _, _, undo = rig(tmp_path, apps_dir)
    secret = touch(ctx.home_dir / ".ssh" / "id_rsa")
    ctx.state[F.CHANGES_KEY] = [{"op": "move", "src": str(ctx.home_dir / "x"), "dst": str(secret)}]
    with pytest.raises(SkillError, match="secrets"):
        await undo.handler(ctx, {})
    assert secret.exists()


async def test_manual_trash_undo(tmp_path, apps_dir):
    ctx, _, trash, undo = rig(tmp_path, apps_dir, FakeRunner({("osascript",): CmdResult(1, "", "boom")}))
    f = touch(ctx.home_dir / "Desktop" / "notes.txt", "keep me")
    await trash.handler(ctx, {"path": str(f)})
    assert not f.exists() and (ctx.home_dir / ".Trash" / "notes.txt").exists()
    assert undo.decide(ctx, {}).prompt == "Move notes.txt back to ~/Desktop?"
    await undo.handler(ctx, {})
    assert f.read_text() == "keep me" and not (ctx.home_dir / ".Trash" / "notes.txt").exists()


async def test_finder_trash_undo_from_the_trash_folder(tmp_path, apps_dir):
    ctx, _, trash, undo = rig(tmp_path, apps_dir)  # Finder "succeeds" but the fake does not move anything
    f = touch(ctx.home_dir / "Desktop" / "pic.png", "img")
    await trash.handler(ctx, {"path": str(f)})
    with pytest.raises(SkillError, match="no longer in the Trash"):  # not in ~/.Trash: say so plainly
        await undo.handler(ctx, {})
    (ctx.home_dir / ".Trash").mkdir(exist_ok=True)
    f.rename(ctx.home_dir / ".Trash" / "pic.png")
    await undo.handler(ctx, {})
    assert f.read_text() == "img"


async def test_history_is_bounded_and_undo_goes_newest_first(tmp_path, apps_dir):
    ctx, move, _, undo = rig(tmp_path, apps_dir)
    for i in range(12):
        src = touch(ctx.home_dir / f"f{i}.txt", str(i))
        await move.handler(ctx, {"source": str(src), "destination": str(ctx.home_dir / f"g{i}.txt")})
    assert len(ctx.state[F.CHANGES_KEY]) == F.MAX_CHANGES
    await undo.handler(ctx, {})
    assert (ctx.home_dir / "f11.txt").read_text() == "11" and not (ctx.home_dir / "g11.txt").exists()


async def test_log_is_private_and_holds_paths_only(tmp_path, apps_dir):
    ctx, move, _, _ = rig(tmp_path, apps_dir)
    src = touch(ctx.home_dir / "secret-plans.txt", "TOP SECRET CONTENT")
    await move.handler(ctx, {"source": str(src), "destination": str(ctx.home_dir / "b.txt")})
    log = paths.home() / "changes.jsonl"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    text = log.read_text()
    row = json.loads(text.splitlines()[0])
    assert row["op"] == "move" and row["src"].endswith("secret-plans.txt") and "t" in row
    assert "TOP SECRET" not in text


# ------------------------------------------------------------------ surviving a restart


def restart(tmp_path, apps_dir):
    """A fresh session on the same data folder: nothing in memory, only the log on disk."""
    return rig(tmp_path, apps_dir)


async def test_undo_still_works_after_a_restart(tmp_path, apps_dir):
    ctx, move, _, undo = rig(tmp_path, apps_dir)
    src = touch(ctx.home_dir / "Documents" / "report.pdf", "data")
    (ctx.home_dir / "Archive").mkdir()
    await move.handler(ctx, {"source": str(src), "destination": str(ctx.home_dir / "Archive")})
    ctx2, _, _, undo2 = restart(tmp_path, apps_dir)
    assert "file_changes" not in ctx2.state
    assert undo2.decide(ctx2, {}).prompt == "Move report.pdf back to ~/Documents?"
    assert await undo2.handler(ctx2, {}) == "Put report.pdf back in ~/Documents."
    assert src.read_text() == "data"


async def test_an_undone_change_is_not_offered_again_after_a_restart(tmp_path, apps_dir):
    ctx, move, _, undo = rig(tmp_path, apps_dir)
    src = touch(ctx.home_dir / "a.txt")
    (ctx.home_dir / "B").mkdir()
    await move.handler(ctx, {"source": str(src), "destination": str(ctx.home_dir / "B")})
    await undo.handler(ctx, {})
    ctx2, _, _, undo2 = restart(tmp_path, apps_dir)
    with pytest.raises(SkillError, match="haven't moved or trashed"):
        await undo2.handler(ctx2, {})


async def test_after_a_restart_undo_goes_newest_first_and_skips_undone_ones(tmp_path, apps_dir):
    ctx, move, _, undo = rig(tmp_path, apps_dir)
    (ctx.home_dir / "Dest").mkdir()
    for name in ("one.txt", "two.txt", "three.txt"):
        await move.handler(ctx, {"source": str(touch(ctx.home_dir / name)), "destination": str(ctx.home_dir / "Dest")})
    await undo.handler(ctx, {})  # three.txt is back
    ctx2, _, _, undo2 = restart(tmp_path, apps_dir)
    assert undo2.decide(ctx2, {}).prompt == "Move two.txt back to ~?"


async def test_a_new_change_after_a_restart_keeps_the_older_ones_undoable(tmp_path, apps_dir):
    ctx, move, _, _ = rig(tmp_path, apps_dir)
    (ctx.home_dir / "Dest").mkdir()
    await move.handler(ctx, {"source": str(touch(ctx.home_dir / "old.txt")), "destination": str(ctx.home_dir / "Dest")})
    ctx2, move2, _, undo2 = restart(tmp_path, apps_dir)
    await move2.handler(ctx2, {"source": str(touch(ctx2.home_dir / "new.txt")), "destination": str(ctx2.home_dir / "Dest")})
    await undo2.handler(ctx2, {})
    assert undo2.decide(ctx2, {}).prompt == "Move old.txt back to ~?"


async def test_corrupt_log_lines_are_ignored(tmp_path, apps_dir):
    ctx, move, _, _ = rig(tmp_path, apps_dir)
    (ctx.home_dir / "Dest").mkdir()
    await move.handler(ctx, {"source": str(touch(ctx.home_dir / "keep.txt")), "destination": str(ctx.home_dir / "Dest")})
    with (paths.home() / "changes.jsonl").open("a") as f:
        f.write('{"op": "move", "src": \n{not json\n[1, 2]\n{"op": "rm", "src": "/", "id": "x", "t": 1}\n{"op": "move", "src": 5}\n')
    ctx2, _, _, undo2 = restart(tmp_path, apps_dir)
    assert undo2.decide(ctx2, {}).prompt == "Move keep.txt back to ~?"


async def test_old_changes_are_not_restored_after_a_restart(tmp_path, apps_dir):
    import time

    ctx, move, _, _ = rig(tmp_path, apps_dir)
    (ctx.home_dir / "Dest").mkdir()
    await move.handler(ctx, {"source": str(touch(ctx.home_dir / "stale.txt")), "destination": str(ctx.home_dir / "Dest")})
    log = paths.home() / "changes.jsonl"
    row = json.loads(log.read_text().splitlines()[0])
    row["t"] = time.time() - 8 * 86400
    log.write_text(json.dumps(row) + "\n")
    ctx2, _, _, undo2 = restart(tmp_path, apps_dir)
    with pytest.raises(SkillError, match="haven't moved or trashed"):
        await undo2.handler(ctx2, {})


async def test_a_restored_change_still_meets_every_safety_refusal(tmp_path, apps_dir):
    ctx, move, _, _ = rig(tmp_path, apps_dir)
    (ctx.home_dir / "Dest").mkdir()
    src = touch(ctx.home_dir / "f.txt", "orig")
    await move.handler(ctx, {"source": str(src), "destination": str(ctx.home_dir / "Dest")})
    touch(src, "someone else's file")  # the original spot is taken
    ctx2, _, _, undo2 = restart(tmp_path, apps_dir)
    with pytest.raises(SkillError, match="already exists"):
        await undo2.handler(ctx2, {})
    assert src.read_text() == "someone else's file"
    # a record that points at a protected place is refused, however it got into the log
    forged = {"op": "move", "src": str(ctx.home_dir / ".ssh" / "id_rsa"), "dst": str(ctx.home_dir / "Dest" / "x"),
              "id": "forged", "t": __import__("time").time()}
    with (paths.home() / "changes.jsonl").open("a") as f:
        f.write(json.dumps(forged) + "\n")
    touch(ctx.home_dir / "Dest" / "x")
    ctx3, _, _, undo3 = restart(tmp_path, apps_dir)
    with pytest.raises(SkillError):
        await undo3.handler(ctx3, {})
    assert not (ctx.home_dir / ".ssh" / "id_rsa").exists()


async def test_the_log_is_trimmed_and_stays_private(tmp_path, apps_dir):
    ctx, move, _, _ = rig(tmp_path, apps_dir)
    log = paths.home() / "changes.jsonl"
    (ctx.home_dir / "Dest").mkdir()
    for i in range(F.LOG_MAX_LINES + 5):
        await move.handler(ctx, {"source": str(touch(ctx.home_dir / f"f{i}.txt")), "destination": str(ctx.home_dir / "Dest")})
    lines = log.read_text().splitlines()
    assert len(lines) <= F.LOG_MAX_LINES
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert not log.with_suffix(".tmp").exists()
    last = json.loads(lines[-1])
    assert last["src"].endswith(f"f{F.LOG_MAX_LINES + 4}.txt")  # the newest survived the trim
    ctx2, _, _, undo2 = restart(tmp_path, apps_dir)
    assert f"f{F.LOG_MAX_LINES + 4}.txt" in undo2.decide(ctx2, {}).prompt


async def test_the_log_holds_paths_only_and_an_id(tmp_path, apps_dir):
    ctx, move, _, _ = rig(tmp_path, apps_dir)
    (ctx.home_dir / "Dest").mkdir()
    await move.handler(ctx, {"source": str(touch(ctx.home_dir / "secret.txt", "TOPSECRET")), "destination": str(ctx.home_dir / "Dest")})
    text = (paths.home() / "changes.jsonl").read_text()
    assert "TOPSECRET" not in text and set(json.loads(text)) == {"op", "src", "dst", "t", "id"}
