"""Triggers macOS permission prompts from *inside* the running Jarvis (so they're attributed to Jarvis.app).

Drop ``~/.jarvis/run/probe.json`` (``{"automation": true, "shell": ["screen", "location"]}``). The daemon notices it,
launches and pokes every app Jarvis automates, asks the shell to raise its own prompts, writes
``probe_result.json`` and deletes the request. Running these from a terminal would grant the *terminal* instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from jarvis_core import paths
from jarvis_core.frontend import Frontend

log = logging.getLogger(__name__)

ALWAYS_RUNNING = {"System Events", "Finder"}
AUTOMATED_APPS = ["System Events", "Finder", "Notes", "Reminders", "Calendar", "Music", "Spotify", "Safari", "Google Chrome"]
PROMPT_WAIT_S = 90  # the user has to click Allow; osascript blocks until they do


async def _run(argv: list[str], timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "timed out waiting for an answer"
    return proc.returncode or 0, (out or err).decode(errors="replace").strip()


async def probe_app(app: str) -> str:
    code, _ = await _run(["osascript", "-e", f'id of application "{app}"'], 15)
    if code != 0:
        return "not installed"
    if app not in ALWAYS_RUNNING:
        await _run(["open", "-g", "-a", app], 20)  # launch in the background, no window stealing
        await asyncio.sleep(2.0)
    code, out = await _run(["osascript", "-e", f'tell application "{app}" to get name'], PROMPT_WAIT_S)
    if code == 0:
        return "allowed"
    if "-1743" in out or "not authorized" in out.lower():
        return "denied"
    return f"error: {out[:80]}"


PROTECTED_FILES = {
    "iMessage": "~/Library/Messages/chat.db",
    "WhatsApp": "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite",
    "Focus state": "~/Library/DoNotDisturb/DB/Assertions.json",
    "Contacts": "~/Library/Application Support/AddressBook",
}


def check_files() -> dict[str, str]:
    """Can *this* process (and so Jarvis.app) open the protected data? Read-only; nothing is parsed or kept."""
    out: dict[str, str] = {}
    for label, raw in PROTECTED_FILES.items():
        p = Path(raw).expanduser()
        if not p.exists():
            out[label] = "not present"
            continue
        try:
            if p.is_dir():
                next(iter(p.iterdir()), None)
            else:
                with p.open("rb") as f:
                    f.read(16)
            out[label] = "readable"
        except PermissionError:
            out[label] = "BLOCKED (needs Full Disk Access)"
        except OSError as e:
            out[label] = f"error: {e.strerror}"
    return out


async def run_probe(request: dict[str, Any], frontend: Frontend) -> dict[str, Any]:
    result: dict[str, Any] = {"automation": {}, "shell": {}}
    if request.get("files"):
        result["files"] = check_files()
    if request.get("bluetooth"):
        code, out = await _run(["blueutil", "--power"], 20)
        result["bluetooth"] = f"allowed (power={out})" if code == 0 else f"BLOCKED (needs Bluetooth permission): {out[:90]}"
    for which in request.get("shell", []):
        try:
            await frontend.permission_prompt(which)
            result["shell"][which] = "prompted"
        except Exception as e:  # noqa: BLE001
            result["shell"][which] = f"error: {e}"
        await asyncio.sleep(1.0)
    if request.get("automation"):
        for app in AUTOMATED_APPS:
            result["automation"][app] = await probe_app(app)
            log.info("automation probe %s: %s", app, result["automation"][app])
    return result


async def watch_probe(frontend: Frontend, interval: float = 2.0) -> None:
    """Poll for a probe request; cheap enough to leave running."""
    req_path = paths.run_dir() / "probe.json"
    out_path = paths.run_dir() / "probe_result.json"
    while True:
        await asyncio.sleep(interval)
        if not req_path.exists():
            continue
        try:
            request = json.loads(req_path.read_text() or "{}")
        except (OSError, ValueError):
            request = {}
        req_path.unlink(missing_ok=True)
        log.info("permission probe requested: %s", request)
        try:
            out_path.write_text(json.dumps(await run_probe(request, frontend), indent=2))
        except Exception:
            log.exception("permission probe failed")
