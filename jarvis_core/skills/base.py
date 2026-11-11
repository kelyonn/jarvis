"""Tool framework: registry, context, risk classification."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from jarvis_core.config import Config
from jarvis_core.errors import PermissionMissing, SkillError
from jarvis_core.frontend import Frontend, FrontendError
from jarvis_core.interfaces import ToolSchema
from jarvis_core.policy.audit import AuditLog

log = logging.getLogger(__name__)


class Risk(str, Enum):
    SAFE = "safe"
    CONFIRM = "confirm"  # needs a spoken yes (when safety.confirm_risky is on)
    BLOCKED = "blocked"  # never runs


@dataclass
class Decision:
    action: str  # "allow" | "confirm" | "block"
    prompt: str = ""
    reason: str = ""

    @staticmethod
    def allow() -> Decision:
        return Decision("allow")

    @staticmethod
    def confirm(prompt: str) -> Decision:
        return Decision("confirm", prompt=prompt)

    @staticmethod
    def block(reason: str) -> Decision:
        return Decision("block", reason=reason)


@dataclass
class CmdResult:
    code: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0


Runner = Callable[..., Awaitable[CmdResult]]


async def run_cmd(
    argv: list[str],
    *,
    timeout: float = 15.0,
    input: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> CmdResult:
    """Run a subprocess without a shell. Never raises for a non-zero exit."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError:
        return CmdResult(127, "", f"{argv[0]}: command not found")
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input.encode() if input is not None else None), timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return CmdResult(124, "", f"timed out after {timeout:.0f}s")
    return CmdResult(proc.returncode or 0, out.decode(errors="replace").strip(), err.decode(errors="replace").strip())


Announcer = Callable[..., Awaitable[Any]]


@dataclass
class ToolContext:
    cfg: Config
    frontend: Frontend | None = None
    runner: Runner = run_cmd
    audit: AuditLog = field(default_factory=AuditLog)
    announce: Announcer | None = None  # async (text, **kw): speak when Jarvis is free
    narrate: Callable[[str], Awaitable[None]] | None = None  # speak a progress line mid-turn
    clock: Callable[[], datetime] = lambda: datetime.now().astimezone()
    app_dirs: list[Path] = field(
        default_factory=lambda: [
            Path("/Applications"),
            Path("/System/Applications"),
            Path("/System/Applications/Utilities"),
            Path("/Applications/Utilities"),
            Path("/System/Library/CoreServices"),  # Finder, Siri, Screen Sharing ...
            Path("/System/Library/CoreServices/Applications"),  # Directory Utility, Network Utility ...
            Path("/System/Cryptexes/App/System/Applications"),  # Safari on recent macOS
            Path.home() / "Applications",
        ]
    )
    home_dir: Path = field(default_factory=Path.home)
    services: dict[str, Any] = field(default_factory=dict)  # memory, briefing, comms, ... (later phases)
    state: dict[str, Any] = field(default_factory=dict)  # per-skill scratch (timers, caches)

    # -- subprocess helpers ---------------------------------------------------
    async def run(self, argv: list[str], **kw: Any) -> CmdResult:
        return await self.runner(argv, **kw)

    async def osa(self, script: str, *args: str, timeout: float = 15.0) -> str:
        """Run AppleScript. Arguments are passed as ``argv`` (never interpolated into the script)."""
        res = await self.run(["osascript", "-e", script, *args], timeout=timeout)
        if not res.ok:
            msg = res.err or res.out or "AppleScript failed"
            low = msg.lower()
            if "not authorized" in low or "not allowed assistive" in low or "(-1743)" in msg:
                raise PermissionMissing("automation")
            if "(-1728)" in msg or "can’t get" in low or "can't get" in low:
                raise SkillError("I couldn't find that, sir.")
            if "(-600)" in msg or "isn’t running" in low or "isn't running" in low:
                raise SkillError("That application isn't running, sir.")
            raise SkillError(f"AppleScript error: {msg[:160]}")
        return res.out

    async def shell_request(self, op: str, args: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        """Ask JarvisShell (AX, input, screenshots, location)."""
        if self.frontend is None or not getattr(self.frontend, "connected", False):
            raise SkillError("That needs the Jarvis shell app, sir, and it isn't running.")
        try:
            return await self.frontend.request(op, args or {}, timeout=timeout)
        except FrontendError as e:
            msg = str(e)
            if "Accessibility" in msg:
                raise PermissionMissing("accessibility", "I need Accessibility permission for that, sir.") from e
            if "Screen Recording" in msg:
                raise PermissionMissing("screen", "I need Screen Recording permission for that, sir.") from e
            raise SkillError(msg) from e


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]
Classifier = Callable[[ToolContext, dict[str, Any]], Decision]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    risk: Risk = Risk.SAFE
    confirm_prompt: Callable[[dict[str, Any]], str] | None = None
    classify: Classifier | None = None  # overrides ``risk`` when set
    category: str = ""
    timeout_s: float = 30.0
    untrusted_output: bool = False  # output contains outside text; the agent wraps it
    final: bool = False  # on success the result is the whole spoken reply: skip the second model call that would paraphrase it

    def schema(self) -> ToolSchema:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }

    def _default_confirm(self, args: dict[str, Any]) -> str:
        """Spoken question for a risky tool with no prompt of its own: say what will be done, with the key details."""
        details = []
        for v in args.values():
            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).strip():
                s = " ".join(str(v).split())
                details.append(s if len(s) <= 40 else s[:37].rstrip() + "...")
            if len(details) == 2:
                break
        what = self.name.replace("_", " ")
        return f"Shall I go ahead with {what}, {' and '.join(details)}?" if details else f"Shall I go ahead with {what}?"

    def decide(self, ctx: ToolContext, args: dict[str, Any]) -> Decision:
        if self.classify:
            return self.classify(ctx, args)
        if self.risk == Risk.BLOCKED:
            return Decision.block(f"{self.name} is disabled")
        if self.risk == Risk.CONFIRM:
            prompt = self.confirm_prompt(args) if self.confirm_prompt else self._default_confirm(args)
            return Decision.confirm(prompt)
        return Decision.allow()


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self, categories: set[str] | None = None, exclude: set[str] | None = None) -> list[ToolSchema]:
        """Tool schemas to offer the model. ``exclude`` hides whole categories (capabilities the user switched off)."""
        return [
            t.schema()
            for t in self._tools.values()
            if (categories is None or t.category in categories) and not (exclude and t.category in exclude)
        ]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


# -- JSON-schema helpers -------------------------------------------------------


def S(desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **extra}


def N(desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": "number", "description": desc, **extra}


def I(desc: str, **extra: Any) -> dict[str, Any]:  # noqa: E741
    return {"type": "integer", "description": desc, **extra}


def B(desc: str) -> dict[str, Any]:
    return {"type": "boolean", "description": desc}


def E(desc: str, values: list[str]) -> dict[str, Any]:
    return {"type": "string", "description": desc, "enum": values}


def obj(props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props or {}, "required": required or []}


def validate_args(tool: Tool, args: Any) -> dict[str, Any]:
    """Coerce and sanity-check model-supplied arguments against the tool schema."""
    if not isinstance(args, dict):
        raise SkillError(f"arguments for {tool.name} must be an object")
    props = tool.parameters.get("properties", {})
    for req in tool.parameters.get("required", []):
        if req not in args or args[req] in (None, ""):
            raise SkillError(f"missing '{req}' for {tool.name}")
    out: dict[str, Any] = {}
    for k, v in args.items():
        spec = props.get(k)
        if spec is None:
            continue  # drop hallucinated parameters
        t = spec.get("type")
        try:
            if t == "integer" and not isinstance(v, bool):
                v = int(float(v))
            elif t == "number" and not isinstance(v, bool):
                v = float(v)
            elif t == "boolean" and isinstance(v, str):
                v = v.strip().lower() in {"true", "yes", "1", "on"}
            elif t == "string" and not isinstance(v, str):
                v = str(v)
        except (TypeError, ValueError) as e:
            raise SkillError(f"bad value for '{k}' in {tool.name}") from e
        if "enum" in spec and v not in spec["enum"]:
            raise SkillError(f"'{k}' must be one of {', '.join(spec['enum'])}")
        out[k] = v
    return out


def monotonic() -> float:
    return time.monotonic()
