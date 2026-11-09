"""Terminal command execution with allow / confirm / block classification."""

from __future__ import annotations

from typing import Any

from jarvis_core.errors import SkillError
from jarvis_core.policy.protected import command_targets_protected
from jarvis_core.policy.shell_policy import classify_command, split_argv
from jarvis_core.skills.base import Decision, S, Tool, ToolContext, ToolRegistry, obj

MAX_OUT = 3000
SPOKEN_OUT = 600  # longer output is not read out: it opens in TextEdit and only the start is returned


async def _open_in_textedit(ctx: ToolContext, text: str) -> str:
    """Long output is for reading, not listening: save it (owner-only) and open it in TextEdit."""
    import os
    import time

    from jarvis_core import paths

    folder = paths.run_dir()
    for old in folder.glob("output-*.txt"):  # keep the folder tidy
        try:
            if time.time() - old.stat().st_mtime > 86400:
                old.unlink()
        except OSError:
            pass
    path = folder / f"output-{int(time.time())}.txt"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text[:200_000])
    res = await ctx.run(["open", "-a", "TextEdit", str(path)])
    return "I've opened the full output in TextEdit" if res.ok else f"the full output is saved at {path}"


def _classify(ctx: ToolContext, a: dict[str, Any]) -> Decision:
    if a.get("directory"):
        from jarvis_core.policy.shell_policy import is_protected_path
        from jarvis_core.skills.files import resolve_path

        try:
            if is_protected_path(resolve_path(ctx, a["directory"]), ctx.home_dir):
                return Decision.block("that folder holds secrets")
        except Exception:  # noqa: BLE001 - an unresolvable folder is checked again when the command runs
            pass
    if command_targets_protected(a.get("command", ""), ctx.cfg):
        return Decision.block("that app is off limits and is never closed")
    return classify_command(a.get("command", ""), ctx.cfg.safety.terminal_allowlist_no_confirm)


async def _run(ctx: ToolContext, a: dict[str, Any]) -> str:
    cmd = a["command"].strip()
    cwd = str(ctx.home_dir)
    if a.get("directory"):
        from jarvis_core.skills.files import resolve_path

        d = resolve_path(ctx, a["directory"])
        if not d.is_dir():
            raise SkillError("That directory doesn't exist, sir.")
        cwd = str(d)
    argv = split_argv(cmd)
    if argv:
        res = await ctx.run(argv, timeout=30, cwd=cwd)
    else:
        res = await ctx.run(["/bin/zsh", "-c", cmd], timeout=30, cwd=cwd)
    out = (res.out + ("\n" + res.err if res.err else "")).strip()
    status = "" if res.ok else f"[exit code {res.code}]\n"
    if len(out) > SPOKEN_OUT:
        shown = await _open_in_textedit(ctx, out)
        head = out[:SPOKEN_OUT].rsplit("\n", 1)[0]
        return (status + head + f"\n[{len(out.splitlines())} lines in all; {shown}]").strip()
    return (status + out) or "Done, no output."


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "run_command",
        "Run a terminal command in the user's home folder. Safe read-only commands run immediately; "
        "anything else asks the user first; dangerous commands are refused. This is macOS: run Python with python3 (there is no 'python'), "
        "and give one complete command rather than several attempts.",
        obj({"command": S("The shell command"), "directory": S("Optional working directory")}, ["command"]),
        _run, classify=_classify, category="terminal", untrusted_output=True, timeout_s=40))
