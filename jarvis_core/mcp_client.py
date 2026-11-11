"""Tools from MCP servers, joined to Jarvis's own.

Servers are listed in ``~/.jarvis/mcp.json``::

    {"servers": {
        "playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]},
        "notes": {"command": "uvx", "args": ["some-mcp"], "env": {"TOKEN": "..."}, "trust": "ask", "block": ["delete_all"]}
    }}

Each server's tools are registered like any other tool, named ``mcp_<server>_<tool>``, in the ``mcp`` category (so
``safety.disabled_capabilities = ["mcp"]`` turns them all off). Every call goes through the same gate and audit log.

``trust`` decides what needs a spoken yes:

* ``"readonly"`` (default): tools the server marks read-only run freely; everything else asks first.
* ``"ask"``: every tool asks first.
* ``"allow"``: nothing asks. Only for servers you fully trust.

Anything listed in ``block`` never runs. Output from a server is treated as untrusted text, like a web page.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis_core import paths
from jarvis_core.errors import SkillError
from jarvis_core.skills.base import Risk, Tool, ToolContext, ToolRegistry

log = logging.getLogger(__name__)

CATEGORY = "mcp"
START_TIMEOUT_S = 25.0
CALL_TIMEOUT_S = 60.0
MAX_NAME = 64
_TRUST = {"readonly", "ask", "allow"}


@dataclass
class ServerSpec:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    trust: str = "readonly"
    block: set[str] = field(default_factory=set)
    enabled: bool = True


def config_file() -> Path:
    return paths.home() / "mcp.json"


def load_specs(path: Path | None = None) -> list[ServerSpec]:
    """Read the server list. A missing file means no servers; a broken entry is skipped, never fatal."""
    path = path or config_file()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        log.warning("mcp.json could not be read: %s", e)
        return []
    out: list[ServerSpec] = []
    servers = data.get("servers") if isinstance(data, dict) else None
    for name, raw in (servers or {}).items():
        if not isinstance(raw, dict) or not isinstance(raw.get("command"), str) or not raw["command"].strip():
            log.warning("mcp server %r skipped: it needs a 'command'", name)
            continue
        trust = str(raw.get("trust", "readonly")).lower()
        if trust not in _TRUST:
            log.warning("mcp server %r: unknown trust %r, using 'ask'", name, trust)
            trust = "ask"
        out.append(ServerSpec(
            name=_slug(str(name)),
            command=raw["command"],
            args=[str(a) for a in raw.get("args", [])],
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
            trust=trust,
            block={str(b) for b in raw.get("block", [])},
            enabled=bool(raw.get("enabled", True)),
        ))
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_") or "server"


def tool_name(server: str, tool: str) -> str:
    return f"mcp_{server}_{_slug(tool)}"[:MAX_NAME]


def risk_for(spec: ServerSpec, tool: str, read_only: bool) -> Risk:
    if tool in spec.block:
        return Risk.BLOCKED
    if spec.trust == "allow":
        return Risk.SAFE
    if spec.trust == "readonly" and read_only:
        return Risk.SAFE
    return Risk.CONFIRM


def _text_of(result: Any) -> str:
    parts = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
        else:
            parts.append(f"[{getattr(item, 'type', 'content')}]")
    return "\n".join(parts).strip()


def make_tool(spec: ServerSpec, session: Any, mcp_tool: Any) -> Tool:
    """A Jarvis tool that forwards to one MCP tool."""
    annotations = getattr(mcp_tool, "annotations", None)
    read_only = bool(getattr(annotations, "readOnlyHint", False))
    schema = dict(getattr(mcp_tool, "inputSchema", None) or {})
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": {}, "required": []}
    schema.setdefault("properties", {})
    schema.setdefault("required", [])
    name = getattr(mcp_tool, "name", "")
    desc = (getattr(mcp_tool, "description", None) or name).strip()

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> str:
        try:
            result = await asyncio.wait_for(session.call_tool(name, args), CALL_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise SkillError(f"{spec.name} took too long, sir.") from e
        except SkillError:
            raise
        except Exception as e:
            log.warning("mcp %s/%s failed: %s", spec.name, name, e)
            raise SkillError(f"{spec.name} couldn't do that: {str(e)[:120]}") from e
        text = _text_of(result)
        if getattr(result, "isError", False):
            raise SkillError(text[:200] or f"{spec.name} reported an error.")
        return text or "Done."

    return Tool(
        tool_name(spec.name, name),
        f"[{spec.name}] {desc}"[:600],
        schema,
        handler,
        risk=risk_for(spec, name, read_only),
        confirm_prompt=lambda a, s=spec.name, n=name: f"Shall I use {s} to {n.replace('_', ' ')}"
        + (f" with {', '.join(str(v)[:30] for v in list(a.values())[:2])}" if a else "") + "?",
        category=CATEGORY,
        timeout_s=CALL_TIMEOUT_S + 5,
        untrusted_output=True,
    )


class MCPManager:
    """Starts each configured server in its own long-lived task and registers its tools."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.started: dict[str, int] = {}  # server -> number of tools registered
        self.failed: dict[str, str] = {}

    async def start(self, registry: ToolRegistry, specs: list[ServerSpec] | None = None) -> None:
        specs = load_specs() if specs is None else specs
        waits = []
        for spec in specs:
            if not spec.enabled:
                continue
            ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._tasks.append(asyncio.create_task(self._run(spec, registry, ready), name=f"mcp-{spec.name}"))
            waits.append(ready)
        if waits:
            await asyncio.gather(*waits, return_exceptions=True)

    async def _run(self, spec: ServerSpec, registry: ToolRegistry, ready: asyncio.Future[None]) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        try:
            params = StdioServerParameters(command=spec.command, args=spec.args, env=spec.env or None)
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), START_TIMEOUT_S)
                listing = await asyncio.wait_for(session.list_tools(), START_TIMEOUT_S)
                count = 0
                for t in listing.tools:
                    tool = make_tool(spec, session, t)
                    if tool.name in registry:
                        log.warning("mcp tool name clash, skipping %s", tool.name)
                        continue
                    registry.register(tool)
                    count += 1
                self.started[spec.name] = count
                log.info("mcp server %s ready with %d tool(s)", spec.name, count)
                if not ready.done():
                    ready.set_result(None)
                await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # a broken server must never take Jarvis down
            self.failed[spec.name] = f"{type(e).__name__}: {e}"[:200]
            log.warning("mcp server %s could not start: %s", spec.name, self.failed[spec.name])
        finally:
            if not ready.done():
                ready.set_result(None)

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            try:
                await asyncio.wait_for(t, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                t.cancel()
        self._tasks.clear()
