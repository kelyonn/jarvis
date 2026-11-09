"""Tool-calling agent: LLM loop + risk policy + spoken confirmation."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jarvis_core.brain.compose import COMPOSE_TOOLS, compose_reply
from jarvis_core.brain.history import ConversationHistory
from jarvis_core.brain.modes import TurnPlan, deliberation_messages, plan_turn
from jarvis_core.brain.persona import build_system_prompt, wrap_untrusted
from jarvis_core.brain.thinking import ThinkFilter, strip_think
from jarvis_core.brain.toolselect import STICKY_TURNS, fix_asr, match_categories, select_categories
from jarvis_core.config import Config
from jarvis_core.errors import PermissionMissing, ShortcutMissing, SkillError
from jarvis_core.interfaces import LLM, TextDelta, ToolCallEvent
from jarvis_core.skills.base import Tool, ToolContext, ToolRegistry, validate_args
from jarvis_core.voice.turn import parse_confirmation

log = logging.getLogger(__name__)

CONFIRM_TTL_S = 90.0
MAX_RESULT_CHARS = 6000

ContextProvider = Callable[[str], dict[str, Any]]


_PAGE_OPENERS = frozenset({"open_url", "open_site", "search_in_browser"})
_PAGE_OPENER_LIMIT = 3  # pages opened in a single turn before Jarvis stops and asks

_NUDGE = (
    "You replied without calling a tool. If the request needs an action or live information (open or close an app, "
    "timer, reminder, note, search, files, settings, music, weather, messages...), call the matching tool now. Never "
    "say you did, are doing or will do something unless a tool has actually run. Only answer in words when no tool is needed."
)

_CLAIM = re.compile(
    r"^\W*(?:(?:right|ok|okay|very good|certainly|of course|sure|alright|understood)\W+)*"
    r"(?:(?:i(?:'ll| will|'m| am)\s+)?(?:opening|launching|searching|checking|setting|creating|starting|turning|playing|looking|"
    r"fetching|closing|taking|adding|running|saving|finding|writing|sending|navigating|connecting|working on|pulling up|"
    r"looking up|getting|reading|muting|pausing|skipping|switching|quitting|locking|dimming|brightening|shuffling|queuing|"
    r"adding|removing|moving|snapping|resizing|minimi[sz]ing|maximi[sz]ing|enabling|disabling|activating|launching|"
    r"booking|scheduling|reminding|calling|texting|messaging|drafting|searching|downloading|uploading|connecting)\b"
    r"|on it\b|one moment\b|just a moment\b|let me\b|i'll\b|i will\b|"
    r"(?:note|reminder|timer|alarm|event|file|folder|message|draft) (?:created|set|saved|added|sent|started|deleted)\b|"
    r"(?:opened|created|started|turned|saved|added|closed|sent|set)\b)",
    re.I,
)


_COMMAND_VERBS = frozenset(
    "set open close quit play pause resume turn remind make start stop text message send call search find take write save add "
    "remove delete move snap put launch switch mute unmute lock schedule book draft reply read connect disconnect show check "
    "skip shuffle create".split()
)
_LEAD_WORDS = frozenset("please can could would you jarvis jarvis hey hi okay ok just then now and also".split())
_YES_DRAFT = re.compile(
    r"^\W*(?:(?:yes|yeah|yep|yup|ok|okay|sure|please|go ahead|alright)\W+)*(?:do it|go ahead|put it in|put it there|insert it|"
    r"send it(?: to (?:her|him|them))?|send that|send|yes|yeah|yep|yup|ok|okay|sure|please do)\W*$", re.I)
_REFUSAL = re.compile(
    r"\b(?:can't|cannot|can not|unable|sorry|don't have|do not have|no way to|not able|afraid|couldn't|could not|didn't|"
    r"did not|isn't|is not|wasn't|not found|no such|failed|error|problem|trouble)\b", re.I)


def _is_command(user_text: str) -> bool:
    """Does the request start with an action verb ("set a timer...", "open Safari", "please play...")?"""
    words = re.findall(r"[a-z']+", (user_text or "").lower())
    while words and words[0] in _LEAD_WORDS:
        words.pop(0)
    return bool(words) and words[0] in _COMMAND_VERBS


def _answers_command_with_talk(user_text: str, reply: str) -> bool:
    """A command was given, no tool ran, and the reply is a statement that sounds like it was done ("Right, twenty seconds it
    is. Starting now."). Questions back and honest refusals are fine."""
    r = reply.strip()
    return _is_command(user_text) and bool(r) and not r.endswith("?") and not _REFUSAL.search(r)


def _claims_action(text: str) -> bool:
    """True when the reply opens like "Opening Safari now" / "Reminder set": a claim that something is being done."""
    return bool(_CLAIM.match(text.strip()))


@dataclass
class Pending:
    calls: list[ToolCallEvent]  # remaining calls; the first awaits the user's yes/no
    prompt: str
    created: float
    action: Callable[[], Awaitable[str]] | None = None  # runs on yes instead of ``calls`` (e.g. making a shortcut)


class Agent:
    def __init__(
        self,
        llm: LLM,
        cfg: Config,
        registry: ToolRegistry,
        history: ConversationHistory,
        ctx: ToolContext,
        *,
        context_provider: ContextProvider | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.llm = llm
        self.cfg = cfg
        self.registry = registry
        self.history = history
        self.ctx = ctx
        self.context_provider = context_provider
        self._clock = clock
        self.pending: Pending | None = None
        self.interrupted = False  # the user cut off the previous reply; set by the daemon, consumed by one turn
        self.tools_used: list[str] = []
        self._recent_categories: list[set[str]] = []  # categories used in the last few turns
        self.plan: TurnPlan | None = None  # how this turn is being handled (kind, reasoning technique, length, sampling)
        self._last_kind = "chat"

    # ------------------------------------------------------------------ public

    @property
    def awaiting_confirmation(self) -> bool:
        return self.pending is not None and self._clock() - self.pending.created <= CONFIRM_TTL_S

    async def respond(self, user_text: str) -> AsyncIterator[str]:
        try:
            async for chunk in self._respond(user_text):
                yield chunk
        finally:
            self.interrupted = False

    async def _respond(self, user_text: str) -> AsyncIterator[str]:
        self.tools_used = []
        self.plan = None
        if self.pending is not None:
            p, self.pending = self.pending, None
            expired = self._clock() - p.created > CONFIRM_TTL_S
            answer = None if expired else parse_confirmation(user_text)
            if answer is True and p.action is not None:
                self.ctx.audit.write("confirmed", prompt=p.prompt)
                try:
                    reply = await p.action()
                except SkillError as e:
                    reply = str(e)
                self.history.add({"role": "assistant", "content": reply})
                yield reply
                return
            if answer is True:
                self.ctx.audit.write("confirmed", prompt=p.prompt)
                prompt = await self._run_calls(p.calls, confirmed_first=True)
                if prompt:
                    yield prompt
                    return
                async for chunk in self._loop():
                    yield chunk
                return
            self._cancel_calls(p.calls)
            self.ctx.audit.write("declined" if answer is False else "confirm_ignored", prompt=p.prompt)
            if answer is False:
                reply = "Understood. I've left it alone."
                self.history.add({"role": "assistant", "content": reply})
                yield reply
                return
            # Unclear or expired: treat the utterance as a fresh request.
        user_text = fix_asr(user_text)
        if self.ctx.state.get("draft") is not None and _YES_DRAFT.match(user_text):
            # A draft is waiting and the user said yes (or "send it"): put it in the app ourselves rather than trusting the
            # model to call insert_draft, which it sometimes only claims to have done.
            tool = self.registry.get("insert_draft")
            if tool is not None:
                self.history.add({"role": "user", "content": user_text})
                self.tools_used = ["insert_draft"]
                result = re.sub(r"</?untrusted[^>]*>\s*", "", await self._invoke(tool, {})).strip()
                if result.startswith("Error:"):
                    result = result.removeprefix("Error:").strip()
                elif re.match(r"^\W*send\b", user_text, re.I):
                    result += " I never send for you, so press Return when you're ready."
                self.history.add({"role": "assistant", "content": result})
                yield result
                return
        self.history.add({"role": "user", "content": user_text})
        async for chunk in self._loop(user_text):
            yield chunk

    def reset(self) -> None:
        self.pending = None

    async def try_direct(self, user_text: str, call: Any) -> str | None:
        """Run a router-matched, safe tool call without the LLM. ``None`` means "let the agent handle it"."""
        self.plan = None
        tool = self.registry.get(call.tool)
        if tool is None:
            return None
        try:
            args = validate_args(tool, call.args)
        except SkillError:
            return None
        if tool.decide(self.ctx, args).action != "allow":
            return None
        self.tools_used = []
        result = await self._invoke(tool, args)
        offer = self._offer_shortcut(result)
        if offer:
            self.history.add({"role": "user", "content": user_text})
            self.history.add({"role": "assistant", "content": offer})
            return offer
        # Directly spoken results are read to the user, not fed to a model: drop the prompt-injection wrapper.
        result = re.sub(r"</?untrusted[^>]*>\s*", "", result).strip()
        if result.startswith("Error:"):
            return None
        reply = call.render_reply(result)
        if call.tool in COMPOSE_TOOLS and call.reply_template.strip() == "{result}":
            composed = await compose_reply(self.llm, self.cfg, user_text, result)
            reply = composed or reply.split("\n\n[details]\n")[0]
        self.history.add({"role": "user", "content": user_text})
        self.history.add({"role": "assistant", "content": reply})
        return reply

    # ---------------------------------------------------------------- internals

    def _system_prompt(self, user_text: str, plan: TurnPlan | None = None, notes: str = "") -> str:
        extra = self.context_provider(user_text) if self.context_provider else {}
        turns = sum(1 for m in self.history.messages() if m.get("role") == "user")
        return build_system_prompt(
            self.cfg, **extra, plan=plan, user_text=user_text, turn_index=max(0, turns - 1), interrupted=self.interrupted, notes=notes,
        )

    async def _deliberate(self, user_text: str, plan: TurnPlan) -> str:
        """A private first pass for requests that benefit from thinking: working notes that are never spoken or shown.

        The technique (chain, least-to-most, tree or graph of thoughts) comes from the turn's plan. Any failure or timeout just
        means the answer is given without notes."""
        extra = self.context_provider(user_text) if self.context_provider else {}
        facts = "; ".join(extra.get("memory_snippets") or [])
        context = f"Location: {extra.get('city', 'unknown')}. " + (f"Known about the user: {facts}." if facts else "")
        msgs = deliberation_messages(plan, user_text, context=context, recent=self.history.messages()[:-1])
        temp = 0.2 if plan.kind in ("math", "code", "troubleshoot") else 0.4
        t0 = time.monotonic()
        try:
            raw = await asyncio.wait_for(
                self.llm.complete(msgs, role="fast", temperature=temp, max_tokens=int(plan.think_words * 2.4) + 80),
                timeout=float(self.cfg.llm.think_timeout_s),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - thinking is optional
            log.info("deliberation skipped: %s", e)
            return ""
        notes = strip_think(raw)[:2500]
        log.info("deliberated (%s) in %.1fs, %d chars", plan.technique, time.monotonic() - t0, len(notes))
        return notes

    async def _visible(self, stream: AsyncIterator[Any], tf: ThinkFilter) -> AsyncIterator[Any]:
        """The model's events with its private <think> blocks removed from the text."""
        async for ev in stream:
            if isinstance(ev, TextDelta):
                vis = tf.feed(ev.text)
                if vis:
                    yield TextDelta(vis)
            else:
                yield ev
        tail = tf.flush()
        if tail:
            yield TextDelta(tail)
        if tf.thought:
            log.debug("thought (%s): %s", self.plan.technique if self.plan else "?", tf.thought[:600])

    _SEND_CLAIM = re.compile(r"\b(?:sent|delivered|posted|messaged)\b|\b(?:she|he|they)(?:'ll| will) (?:see|get|receive) it\b", re.I)
    _NEGATED = re.compile(r"\b(?:never|not|haven'?t|hasn'?t|didn'?t|won'?t|can'?t|couldn'?t|no)\b|n't", re.I)
    # what the user asked for -> the tool category that has to have run for "done" to be true
    _NEEDS = (
        ("notes", re.compile(r"\b(?:create|make|add|write|take|jot|save)\b.*\bnotes?\b|\bnote (?:down|that)\b", re.I)),
        ("reminders", re.compile(r"\bremind(?:er)? me\b|\b(?:create|add|set|make)\b.*\breminder\b|\btell me to\b", re.I)),
        ("timers", re.compile(r"\b(?:set|start)\b.*\b(?:timer|alarm)\b", re.I)),
    )

    def _reconcile(self, user_text: str, text: str) -> str | None:
        """Catch a reply that says something was sent, saved or created when no tool did it. ``None`` means the reply is fine."""
        if not text:
            return None
        used = {t.category for n in self.tools_used if (t := self.registry.get(n)) is not None}
        for m in self._SEND_CLAIM.finditer(text):
            if not self._NEGATED.search(text[max(0, m.start() - 30): m.start()]):
                return ("I never send messages myself. The draft is sitting in the compose box, not sent. "
                        "Press Return when you're ready to send it.")
        if _REFUSAL.search(text) or text.rstrip().endswith("?"):
            return None
        for cat, rx in self._NEEDS:
            if rx.search(user_text or "") and cat not in used and cat in (match_categories(user_text) | {cat}):
                what = {"notes": "the note", "reminders": "the reminder", "timers": "the timer"}[cat]
                return f"I haven't actually made {what}: that didn't go through. Say it again and I'll redo it."
        return None

    def _offered_categories(self, user_text: str) -> set[str] | None:
        if not self.cfg.llm.tool_selection:
            return None
        sticky = set().union(*self._recent_categories[-STICKY_TURNS:]) if self._recent_categories else set()
        sel = select_categories(user_text, sticky)
        if sel is not None:  # tools already used this turn stay available for the next step
            sel |= {t.category for n in self.tools_used if (t := self.registry.get(n)) is not None}
        return sel

    def _note_categories(self) -> None:
        used = {t.category for n in self.tools_used if (t := self.registry.get(n)) is not None}
        self._recent_categories.append(used)
        del self._recent_categories[:-STICKY_TURNS]

    async def _loop(self, user_text: str = "") -> AsyncIterator[str]:
        tools = self.registry.schemas(self._offered_categories(user_text))
        nudged = False
        failed_rounds = 0
        filler_said = False
        call_counts: dict[str, int] = {}
        hold_all = _is_command(user_text) and bool(tools)  # a command's answer is judged whole before it is spoken
        if user_text:
            self.plan = plan_turn(user_text, self._last_kind, reasoning=self.cfg.llm.reasoning, max_think_words=self.cfg.llm.max_think_words)
            log.info("turn plan: %s/%s technique=%s think=%d", self.plan.kind, self.plan.complexity, self.plan.technique, self.plan.think_words)
        plan = self.plan
        base_plan = plan
        notes = await self._deliberate(user_text, plan) if user_text and plan is not None and plan.thinks else ""
        for _ in range(max(1, self.cfg.llm.max_tool_iterations)):
            messages = [{"role": "system", "content": self._system_prompt(user_text, plan, notes)}, *self.history.messages()]
            if nudged:
                messages.append({"role": "system", "content": _NUDGE})
            text_parts: list[str] = []
            calls: list[ToolCallEvent] = []
            # Speech must not start with "Opening X now" when no tool is actually called, so while the opening words
            # look like a claim of action we hold the text back until we know whether a tool call follows.
            held: list[str] = []
            flushed = False
            tf = ThinkFilter()
            sampling = {"temperature": plan.temperature, "max_tokens": plan.max_tokens} if plan is not None else {}
            async for ev in self._visible(self.llm.stream_chat(messages, tools, role="chat", **sampling), tf):
                if isinstance(ev, TextDelta):
                    text_parts.append(ev.text)
                    if flushed:
                        yield ev.text
                        continue
                    held.append(ev.text)
                    head = "".join(held)
                    if len(head.split()) >= 6 and not _claims_action(head) and not hold_all:
                        flushed = True
                        yield head
                elif isinstance(ev, ToolCallEvent):
                    calls.append(ev)
            text = "".join(text_parts).strip()
            if not calls:
                claimed = not self.tools_used and bool(tools) and (_claims_action(text) or _answers_command_with_talk(user_text, text))
                if claimed and not nudged:
                    nudged = True  # retry once, telling the model to act instead of describing
                    continue
                fixed = None if claimed else self._reconcile(user_text, text)
                if claimed:
                    text = "I'm sorry, I wasn't able to do that one. Could you put it another way?"
                    yield text
                elif fixed:
                    text = fixed  # the reply claimed something no tool did: say what really happened instead
                    yield (" " if flushed else "") + text
                elif not flushed and text:
                    yield "".join(held)
                if not text and self.tools_used:
                    text = "Done."
                    yield text
                self.history.add({"role": "assistant", "content": text})
                self._note_categories()
                if base_plan is not None:
                    self._last_kind = base_plan.kind
                return
            # Tool calls follow: anything held back was just narration, so it is dropped rather than spoken.
            if flushed:
                pass
            self.history.add({
                "role": "assistant",
                "content": (text if flushed else None) or None,
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                    for c in calls
                ],
            })
            looping = False
            for c in calls:
                sig = f"{c.name}:{json.dumps(c.arguments, sort_keys=True, default=str)}"
                call_counts[sig] = call_counts.get(sig, 0) + 1
                looping = looping or call_counts[sig] >= 3  # the same call a third time is a loop, not progress
                call_counts[c.name] = call_counts.get(c.name, 0) + 1
                # opening pages one after another with different URLs is flailing too: stop after a few in one turn
                looping = looping or (c.name in _PAGE_OPENERS and call_counts[c.name] > _PAGE_OPENER_LIMIT)
            if looping:
                break
            run = asyncio.create_task(self._run_calls(calls))
            try:
                limit = float(self.cfg.llm.filler_after_s)
                if limit > 0 and not filler_said:
                    done, _ = await asyncio.wait({run}, timeout=limit)
                    if not done:  # slow tool: fill the silence in the same utterance, so nothing is cut off
                        filler_said = True
                        yield self._filler_for(calls) + " "
                prompt = await run
            finally:
                if not run.done():
                    run.cancel()
            if prompt:
                self._note_categories()
                yield prompt
                return
            # Tools that fail twice in a row will not start working on a third try: say why and stop.
            results = [m["content"] for m in self.history.messages()[-len(calls):] if m.get("role") == "tool"]
            if results and all(str(r).startswith("Error:") for r in results):
                failed_rounds += 1
                if failed_rounds >= 2:
                    why = str(results[-1]).removeprefix("Error:").strip() or "it didn't work"
                    msg = f"I couldn't do that. {why}"
                    self.history.add({"role": "assistant", "content": msg})
                    self._note_categories()
                    yield msg
                    return
            else:
                failed_rounds = 0
        self._note_categories()
        async for chunk in self._wrap_up_unfinished():
            yield chunk

    async def _wrap_up_unfinished(self) -> AsyncIterator[str]:
        """Out of steps (or going in circles): say what got done and what is left, rather than a canned apology."""
        messages = [{"role": "system", "content": self._system_prompt("", None)}, *self.history.messages(), {
            "role": "system",
            "content": (
                "You could not finish the request in the steps allowed. Without calling any tool, tell the user in two short "
                "spoken sentences what you completed and what is still left, then ask whether to carry on. Do not apologise "
                "at length and do not mention step limits or tools."),
        }]
        text_parts: list[str] = []
        try:
            async for ev in self.llm.stream_chat(messages, None, role="chat"):
                if isinstance(ev, TextDelta):
                    text_parts.append(ev.text)
                    yield ev.text
        except Exception:  # noqa: BLE001 - fall back to a plain sentence
            log.debug("wrap-up failed", exc_info=True)
        if not "".join(text_parts).strip():
            msg = "I got part of the way through that. Shall I carry on?"
            text_parts = [msg]
            yield msg
        self.history.add({"role": "assistant", "content": "".join(text_parts).strip()})

    _FILLERS = {
        "briefing": ["Checking now.", "Let me have a look.", "One moment."],
        "web": ["Let me look that up.", "Searching now.", "One moment."],
        "messaging": ["Reading them now.", "One moment."],
        "vision": ["Taking a look.", "One moment."],
        "media": ["On it.", "One moment."],
    }

    def _filler_for(self, calls: list[ToolCallEvent]) -> str:
        tool = self.registry.get(calls[0].name) if calls else None
        options = self._FILLERS.get(getattr(tool, "category", ""), ["One moment.", "Working on it.", "Just a moment."])
        return random.choice(options)

    def _cancel_calls(self, calls: list[ToolCallEvent]) -> None:
        for c in calls:
            self.history.add({"role": "tool", "tool_call_id": c.id, "content": "Cancelled: the user did not confirm."})

    async def _run_calls(self, calls: list[ToolCallEvent], *, confirmed_first: bool = False) -> str | None:
        """Execute calls in order. Returns a spoken confirmation prompt if one is needed (and parks the rest)."""
        for i, call in enumerate(calls):
            tool = self.registry.get(call.name)
            if tool is None:
                self._tool_msg(call, f"Error: unknown tool {call.name}")
                continue
            try:
                args = validate_args(tool, call.arguments)
            except SkillError as e:
                self._tool_msg(call, f"Error: {e}")
                continue
            decision = tool.decide(self.ctx, args)
            if decision.action == "block":
                self.ctx.audit.write("blocked", tool=tool.name, args=args, reason=decision.reason)
                self._tool_msg(call, f"Blocked by safety policy: {decision.reason}. Tell the user plainly.")
                continue
            needs_confirm = (
                decision.action == "confirm"
                and self.cfg.safety.confirm_risky
                and not (i == 0 and confirmed_first)
            )
            if needs_confirm:
                # Keep the *validated* args so what the user approved is what runs.
                parked = [ToolCallEvent(call.id, call.name, args), *calls[i + 1:]]
                self.pending = Pending(parked, decision.prompt, self._clock())
                self.ctx.audit.write("confirm_requested", tool=tool.name, args=args, prompt=decision.prompt)
                return decision.prompt
            result = await self._invoke(tool, args)
            self._tool_msg(call, result)
            offer = self._offer_shortcut(result)
            if offer:
                self._cancel_calls(calls[i + 1:])
                return offer
        return None

    def _offer_shortcut(self, result: str) -> str | None:
        """If the last tool needed a shortcut Jarvis can build, park "make it?" as the pending question."""
        from jarvis_core.adapters import shortcuts as shortcuts_kit

        name = shortcuts_kit.take_wanted(self.ctx)
        if not name:
            return None
        done = "" if result.startswith("Error:") else re.sub(r"</?untrusted[^>]*>\s*", "", result).strip() + " "
        prompt = f"{done}The '{name}' shortcut isn't set up yet, sir. Shall I make it for you?"
        self.pending = Pending([], prompt, self._clock(), action=lambda: shortcuts_kit.install(self.ctx, name))
        self.ctx.audit.write("confirm_requested", tool="make_shortcut", args={"name": name}, prompt=prompt)
        return prompt

    def _tool_msg(self, call: ToolCallEvent, content: str) -> None:
        self.history.add({"role": "tool", "tool_call_id": call.id, "content": content})

    async def _invoke(self, tool: Tool, args: dict[str, Any]) -> str:
        t0 = time.monotonic()
        ok = True
        try:
            raw = await asyncio.wait_for(tool.handler(self.ctx, args), tool.timeout_s)
            out = raw if isinstance(raw, str) else json.dumps(raw, default=str)
        except PermissionMissing as e:
            ok = False
            out = f"Error: {e}"
            fe = self.ctx.frontend
            if fe is not None:
                try:
                    await fe.permission_prompt(e.which)
                except Exception:  # pragma: no cover - UI nicety only
                    log.debug("permission prompt failed", exc_info=True)
        except ShortcutMissing as e:
            ok = False
            out = f"Error: {e}"
            if e.buildable:
                from jarvis_core.adapters import shortcuts as shortcuts_kit

                shortcuts_kit.want(self.ctx, e.name)
        except SkillError as e:
            ok = False
            out = f"Error: {e}"
        except asyncio.TimeoutError:
            ok = False
            out = f"Error: {tool.name} timed out."
        except Exception as e:
            ok = False
            log.exception("tool %s crashed", tool.name)
            out = f"Error: {tool.name} failed unexpectedly ({type(e).__name__})."
        out = out[:MAX_RESULT_CHARS]
        self.tools_used.append(tool.name)
        self.ctx.audit.write(
            "tool", tool=tool.name, args=args, ok=ok, ms=int((time.monotonic() - t0) * 1000), result=out[:200]
        )
        if tool.untrusted_output and ok:
            out = wrap_untrusted(out, source=tool.name)
        return out
