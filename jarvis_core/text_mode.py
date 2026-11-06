"""``jarvisd text``: talk to Jarvis from the terminal (same brain, no voice)."""

from __future__ import annotations

import asyncio
import sys

from jarvis_core.brain.llm import build_llm
from jarvis_core.config import Config
from jarvis_core.errors import LLMAuthError, LLMUnavailable
from jarvis_core.skills.base import ToolContext


async def run_text_mode(cfg: Config, *, once: str | None = None, llm=None) -> int:
    from jarvis_core.assistant import Assistant

    llm = llm or build_llm(cfg)
    ctx = ToolContext(cfg=cfg)

    async def _print_announcement(text: str, **_: object) -> None:  # timers, alerts: shown instead of spoken
        sys.stdout.write(f"\njarvis> {text}\nyou> ")
        sys.stdout.flush()

    ctx.announce = _print_announcement
    closers = attach_services(cfg, ctx, llm)
    assistant = Assistant.create(cfg, llm=llm, text_only=True, ctx=ctx)

    async def one(text: str) -> None:
        sys.stdout.write("jarvis> ")
        sys.stdout.flush()
        try:
            async for chunk in assistant.respond_stream(text):
                sys.stdout.write(chunk)
                sys.stdout.flush()
        except LLMAuthError:
            sys.stdout.write("I have no API key yet, sir. Run `jarvisd set-key`.")
        except LLMUnavailable as e:
            sys.stdout.write(f"I'm having trouble reaching my servers, sir. ({e})")
        sys.stdout.write("\n")

    try:
        return await _run(one, once)
    finally:
        for c in closers:
            try:
                res = c()
                if hasattr(res, "__await__"):
                    await res
            except Exception:
                pass


async def _run(one, once: str | None) -> int:
    if once is not None:
        await one(once)
        return 0

    print("Jarvis text mode. Ctrl-D or 'exit' to quit.")
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input("you> "))
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        line = line.strip()
        if not line:
            continue
        if line.lower() in {"exit", "quit"}:
            return 0
        await one(line)


def attach_services(cfg: Config, ctx: ToolContext, llm) -> list:
    """The daemon-free services (briefings, memory, messaging) so text mode has the same tools as the voice assistant."""
    from jarvis_core.briefing.env import Env
    from jarvis_core.briefing.service import BriefingService
    from jarvis_core.briefing.store import Store
    from jarvis_core.comms.setup import build_comms
    from jarvis_core.memory.store import SQLiteMemory

    closers: list = []
    store = Store()
    env = Env(cfg=cfg, http=Env.make_client(), store=store, ctx=ctx, services=ctx.services)
    ctx.services.update(briefing=BriefingService(cfg, env, llm), store=store)
    closers.append(env.http.aclose)
    if cfg.memory.enabled:
        mem = SQLiteMemory()
        ctx.services["memory"] = mem
        closers.append(mem.close)
    try:
        build_comms(cfg, ctx, store, llm)
    except Exception:
        pass
    return closers
