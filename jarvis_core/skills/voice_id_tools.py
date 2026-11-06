"""Tools for teaching Jarvis your voice and for switching voice recognition off."""

from __future__ import annotations

from typing import Any

from jarvis_core.errors import SkillError
from jarvis_core.skills.base import Tool, ToolContext, ToolRegistry, obj


def _daemon(ctx: ToolContext):
    d = ctx.services.get("daemon")
    if d is None or d.verifier is None:
        raise SkillError("Voice recognition isn't available, sir.")
    return d


async def _learn(ctx: ToolContext, a: dict[str, Any]) -> str:
    from jarvis_core.voice.enroll import begin

    return begin(_daemon(ctx))  # the rest happens turn by turn: Jarvis asks, you answer, it moves on


async def _forget(ctx: ToolContext, a: dict[str, Any]) -> str:
    d = _daemon(ctx)
    d.verifier.forget()
    return "Done, sir. I've forgotten your voice and I'll respond to anyone again."


async def _status(ctx: ToolContext, a: dict[str, Any]) -> str:
    d = _daemon(ctx)
    if not d.verifier.enrolled:
        return "I haven't learned your voice yet, sir. Say learn my voice to teach me."
    score = f" The last voice I heard matched at {d.last_speaker_score:.0%}." if d.last_speaker_score else ""
    return f"I only act on your voice at the moment.{score}"


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "learn_my_voice",
        "Teach Jarvis the owner's voice (voice recognition): reads six phrases and listens. Afterwards only the owner's "
        "voice is acted on. Use for 'learn my voice', 'recognise my voice', 'set up voice recognition'.",
        obj(), _learn, category="system"))
    reg.register(Tool(
        "forget_my_voice", "Delete the stored voiceprint so Jarvis responds to any voice again.",
        obj(), _forget, category="system"))
    reg.register(Tool(
        "voice_recognition_status", "Whether Jarvis has learned the owner's voice.", obj(), _status, category="system"))
