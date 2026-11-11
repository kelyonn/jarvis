"""Developer hook: run a tool, or a whole assistant turn, inside the live daemon and read the result back.

Create (or touch) ``~/.jarvis/dev`` to switch it on for 12 hours, then write ``~/.jarvis/run/devhook.json`` with ``{"tool": "list_windows", "args": {}}`` or ``{"turn": "what's the weather"}``;
the daemon runs it with the real Shell, permissions and services attached and writes ``devhook_result.json``.
Only tools whose policy says "allow" run; anything that would need a confirmation is refused.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from jarvis_core import paths

log = logging.getLogger(__name__)


async def run_request(assistant: Any, req: dict[str, Any]) -> dict[str, Any]:
    t0 = time.monotonic()
    if "osa" in req:  # run AppleScript as Jarvis.app (it holds the Accessibility and Automation grants)
        try:
            out = await assistant.ctx.osa(str(req["osa"]), timeout=float(req.get("timeout", 20)))
            return {"ok": True, "result": out[-3000:]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:600]}
    if "shell" in req:  # a raw request to the Shell, e.g. {"shell": "ax.tree", "args": {"app": "Clock"}}
        try:
            data = await assistant.ctx.shell_request(str(req["shell"]), req.get("args") or {}, timeout=float(req.get("timeout", 20)))
            full = json.dumps(data, default=str)
            if req.get("save"):  # keep the whole answer (a big AX tree) in a file; the result below is only a preview
                from pathlib import Path

                Path(str(req["save"])).write_text(full)
            return {"ok": True, "result": full[:9000]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:600]}
    if "profile" in req:
        import cProfile
        import io
        import pstats

        pr = cProfile.Profile()
        pr.enable()
        await asyncio.sleep(float(req["profile"]))
        pr.disable()
        buf = io.StringIO()
        pstats.Stats(pr, stream=buf).sort_stats("tottime").print_stats(18)
        return {"ok": True, "result": buf.getvalue()[-2500:]}
    if "turn" in req:
        reply = "".join([c async for c in assistant.respond_stream(str(req["turn"]))])
        return {"reply": reply, "seconds": round(time.monotonic() - t0, 2)}
    name = str(req.get("tool") or "")
    registry = assistant.registry
    tool = registry.get(name)
    if tool is None:
        return {"error": f"unknown tool {name}"}
    args = req.get("args") or {}
    from jarvis_core.policy import gate

    decision = gate.check(tool, assistant.ctx, args)
    if decision.action != "allow":
        return {"refused": f"{name} needs confirmation or is blocked"}
    try:
        out = await asyncio.wait_for(tool.handler(assistant.ctx, args), tool.timeout_s)
        return {"ok": True, "result": str(out)[:2000], "seconds": round(time.monotonic() - t0, 2)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:500]}


DEV_MODE_HOURS = 12


def _dev_mode_on() -> bool:
    """The hook runs AppleScript with Jarvis.app's privacy grants, so a forgotten switch must not stay on forever."""
    flag = paths.home() / "dev"
    try:
        return time.time() - flag.stat().st_mtime < DEV_MODE_HOURS * 3600
    except OSError:
        return False


async def watch_devhook(assistant: Any, interval: float = 1.0) -> None:
    req_path = paths.run_dir() / "devhook.json"
    out_path = paths.run_dir() / "devhook_result.json"
    while True:
        await asyncio.sleep(interval)
        if not req_path.exists():
            continue
        if not _dev_mode_on():  # off unless ~/.jarvis/dev exists and was touched in the last 12 hours
            req_path.unlink(missing_ok=True)
            continue
        try:
            req = json.loads(req_path.read_text() or "{}")
        except (OSError, ValueError):
            req = {}
        req_path.unlink(missing_ok=True)
        try:
            res = await run_request(assistant, req)
        except Exception as e:  # noqa: BLE001
            res = {"error": f"{type(e).__name__}: {e}"[:500]}
        out_path.write_text(json.dumps({"request": req, **res}, indent=2))
