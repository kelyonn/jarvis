"""``jarvisd brief``: build and print (or speak) a briefing from the terminal."""

from __future__ import annotations

import asyncio
import sys

from jarvis_core.briefing import composer
from jarvis_core.briefing.env import Env
from jarvis_core.briefing.service import BriefingService
from jarvis_core.briefing.store import Store
from jarvis_core.config import load_config
from jarvis_core.errors import LLMAuthError, LLMUnavailable
from jarvis_core.skills.base import ToolContext


def build_service(cfg, *, llm=None, ctx: ToolContext | None = None, services: dict | None = None) -> tuple[BriefingService, Env]:
    store = Store()
    ctx = ctx or ToolContext(cfg=cfg)
    env = Env(cfg=cfg, http=Env.make_client(), store=store, ctx=ctx, services=ctx.services if services is None else services)
    return BriefingService(cfg, env, llm), env


async def run_brief(*, mode: str = "auto", speak: bool = True) -> int:
    cfg = load_config()
    llm = None
    try:
        from jarvis_core.brain.llm import build_llm

        llm = build_llm(cfg)
    except Exception:
        llm = None
    svc, env = build_service(cfg, llm=llm)
    try:
        from jarvis_core.comms.setup import build_comms

        build_comms(cfg, env.ctx, env.store, llm)
    except Exception:
        pass
    try:
        data = await svc.build(mode)
        try:
            text = await composer.compose_text(llm, cfg, data)
        except (LLMAuthError, LLMUnavailable):
            text = composer.template_brief(data, cfg)
        svc._remember(data)
        sys.stdout.write(text + "\n")
        failed = data.failed()
        if failed:
            sys.stderr.write(f"(unavailable: {', '.join(failed)})\n")
        if speak:
            proc = await asyncio.create_subprocess_exec("say", "-v", cfg.voice.say_voice, text)
            await proc.wait()
        return 0
    finally:
        await env.http.aclose()
