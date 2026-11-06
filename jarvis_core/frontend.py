"""Frontend abstraction: where audio comes from/goes to and where the orb lives.

Implementations: :class:`StandaloneFrontend` (Python mic/speaker, no UI),
:class:`jarvis_core.ipc.frontend.IPCFrontend` (JarvisShell over WebSocket), and
:class:`FakeFrontend` (tests).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

log = logging.getLogger(__name__)


class FrontendEvents(Protocol):
    def on_audio(self, frame: bytes) -> None: ...
    def on_hotkey(self, action: str) -> None: ...
    def on_lifecycle(self, event: str) -> None: ...
    def on_device_usage(self, mic_in_use_by_other: bool, camera_in_use: bool, app_hint: str) -> None: ...
    def on_orb_click(self, action: str) -> None: ...
    def on_text_input(self, text: str) -> None: ...
    def on_connected(self, info: dict[str, Any]) -> None: ...
    def on_disconnected(self) -> None: ...


class FrontendError(RuntimeError):
    """A request to the frontend failed or timed out."""


class Frontend:
    """Base class with playback-completion bookkeeping."""

    supports_aec = False
    has_ui = False
    connected = True

    def __init__(self) -> None:
        self.events: FrontendEvents | None = None
        self._waiters: dict[str, asyncio.Future[str]] = {}

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    # -- UI -----------------------------------------------------------------
    async def set_state(self, name: str, caption: str | None = None) -> None: ...

    async def caption(self, text: str, final: bool = False) -> None: ...

    async def orb_queue(self, items: list[dict[str, Any]]) -> None: ...

    async def rich(self, text: str) -> None:
        """Formatted (markdown) version of the reply for the chat card: code, working, tables, steps."""

    async def hide_ui(self) -> None:
        """Close the conversation card and clear its history (the orb comes back with the next activity)."""

    async def notify(self, title: str, body: str) -> None: ...

    async def earcon(self, name: str) -> None: ...

    async def permission_prompt(self, which: str) -> None: ...

    # -- audio out ----------------------------------------------------------
    async def play_begin(self, utt: str, interruptible: bool = True) -> None: ...

    async def play_chunk(self, utt: str, pcm16: bytes, sample_rate: int) -> None: ...

    async def play_end(self, utt: str) -> None: ...

    async def stop_audio(self) -> None:
        for utt in list(self._waiters):
            self.resolve_playback(utt, "interrupted")

    # -- mic control ---------------------------------------------------------
    async def set_mic_enabled(self, enabled: bool) -> None: ...

    async def set_capture(self, open_: bool) -> None:
        """Open or close the capture device itself. Default: nothing (frontends that own the mic always listen)."""

    # -- request/response (AX, screenshots, input, location) -----------------
    async def request(self, op: str, args: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        raise FrontendError(f"{type(self).__name__} does not support '{op}'")

    # -- playback bookkeeping -----------------------------------------------
    def expect_playback(self, utt: str) -> asyncio.Future[str]:
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters[utt] = fut
        return fut

    def resolve_playback(self, utt: str, event: str) -> None:
        fut = self._waiters.pop(utt, None)
        if fut and not fut.done():
            fut.set_result(event)

    async def wait_playback_done(self, utt: str, timeout: float = 120.0) -> str:
        fut = self._waiters.get(utt)
        if fut is None:
            return "finished"
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            self._waiters.pop(utt, None)
            return "timeout"


class FakeFrontend(Frontend):
    """Records everything; playback 'finishes' instantly (or on demand) for tests."""

    supports_aec = True
    has_ui = True

    def __init__(self, auto_finish: bool = True) -> None:
        super().__init__()
        self.auto_finish = auto_finish
        self.log: list[tuple] = []
        self.capture_log: list[bool] = []
        self.states: list[str] = []
        self.captions: list[str] = []
        self.rich_blocks: list[str] = []
        self.played: dict[str, bytearray] = {}
        self.earcons: list[str] = []
        self.notifications: list[tuple[str, str]] = []
        self.orb_items: list[dict[str, Any]] = []
        self.requests: list[tuple[str, dict]] = []
        self.request_handler = None

    async def set_capture(self, open_):
        self.capture_log.append(open_)

    async def set_state(self, name, caption=None):
        self.states.append(name)
        self.log.append(("state", name))

    async def caption(self, text, final=False):
        self.captions.append(text)

    async def orb_queue(self, items):
        self.orb_items = list(items)

    async def hide_ui(self):
        self.log.append(("hide_ui",))

    async def rich(self, text):
        self.rich_blocks.append(text)

    async def notify(self, title, body):
        self.notifications.append((title, body))

    async def earcon(self, name):
        self.earcons.append(name)

    async def play_begin(self, utt, interruptible=True):
        self.played[utt] = bytearray()
        self.expect_playback(utt)
        self.log.append(("play_begin", utt))

    async def play_chunk(self, utt, pcm16, sample_rate):
        self.played.setdefault(utt, bytearray()).extend(pcm16)

    async def play_end(self, utt):
        self.log.append(("play_end", utt))
        if self.auto_finish:
            self.resolve_playback(utt, "finished")

    async def stop_audio(self):
        self.log.append(("stop_audio",))
        await super().stop_audio()

    async def request(self, op, args=None, timeout=10.0):
        self.requests.append((op, args or {}))
        if self.request_handler:
            res = self.request_handler(op, args or {})
            if asyncio.iscoroutine(res):
                res = await res
            return res
        return {"ok": True}
