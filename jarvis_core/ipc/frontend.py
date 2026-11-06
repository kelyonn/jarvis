"""IPCFrontend: the Frontend implementation that talks to JarvisShell."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from typing import Any

from jarvis_core.frontend import Frontend, FrontendError
from jarvis_core.ipc import protocol as P
from jarvis_core.ipc.server import IPCServer

log = logging.getLogger(__name__)


class IPCFrontend(Frontend):
    # The Shell captures with a plain input (no voice-processing I/O): that unit ducks and degrades every other
    # app's audio system-wide, so voice barge-in is off and the mic is closed while Jarvis talks.
    supports_aec = False
    has_ui = True

    def __init__(
        self,
        ipc_file: str | None = None,
        *,
        shell_lost_timeout_s: float = 90.0,
        on_shell_lost: Callable[[], None] | None = None,
        server: IPCServer | None = None,
    ) -> None:
        super().__init__()
        self.server = server or IPCServer(
            on_control=self._on_control,
            on_mic=self._on_mic,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
            ipc_file=ipc_file,
        )
        if server is not None:
            server._on_control = self._on_control
            server._on_mic = self._on_mic
            server._on_connect = self._on_connect
            server._on_disconnect = self._on_disconnect
        self.connected = False
        self.permissions: dict[str, Any] = {}
        self.last_location: dict[str, Any] | None = None
        self.on_shell_lost = on_shell_lost
        self._lost_timeout = shell_lost_timeout_s
        self._lost_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._last_state: tuple[str, str | None] = ("booting", None)
        self._last_queue: list[dict[str, Any]] = []
        self._mic_enabled = True
        self._capture_open = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self.server.start()
        self._lost_task = asyncio.create_task(self._lost_timer(initial=True))

    async def stop(self) -> None:
        if self._lost_task:
            self._lost_task.cancel()
        await self.server.stop()

    # -- inbound ------------------------------------------------------------

    def _on_mic(self, frame: bytes) -> None:
        if self.events and self._mic_enabled:
            self.events.on_audio(frame)

    def _write_shell_status(self, info: dict[str, Any]) -> None:
        """Leave the Shell's permission report where ``jarvisd doctor`` can read it."""
        try:
            import json
            import time

            from jarvis_core import paths

            (paths.run_dir() / "shell_status.json").write_text(json.dumps({
                "ts": time.time(), "shell_version": info.get("shell_version", ""),
                "permissions": self.permissions}))
        except Exception:
            log.debug("could not write shell status", exc_info=True)

    def _on_connect(self, info: dict[str, Any]) -> None:
        self.connected = True
        self.permissions = dict(info.get("permissions") or {})
        self._write_shell_status(info)
        if self._lost_task:
            self._lost_task.cancel()
            self._lost_task = None
        if self.events:
            self.events.on_connected(info)
        asyncio.get_running_loop().create_task(self._replay())

    def _on_disconnect(self) -> None:
        self.connected = False
        for utt in list(self._waiters):
            self.resolve_playback(utt, "interrupted")
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(FrontendError("JarvisShell disconnected"))
        self._pending.clear()
        if self.events:
            self.events.on_disconnected()
        self._lost_task = asyncio.get_running_loop().create_task(self._lost_timer())

    async def _lost_timer(self, initial: bool = False) -> None:
        await asyncio.sleep(self._lost_timeout)
        log.error("JarvisShell has been gone for %.0fs; shutting down", self._lost_timeout)
        if self.on_shell_lost:
            self.on_shell_lost()

    async def _replay(self) -> None:
        name, caption = self._last_state
        await self.set_state(name, caption)
        if self._last_queue:
            await self.orb_queue(self._last_queue)
        await self.set_mic_enabled(self._mic_enabled)
        await self.set_capture(self._capture_open)

    async def _on_control(self, c: P.Control) -> None:
        d = c.data
        ev = self.events
        t = c.type
        if t == P.HOTKEY and ev and d.get("action") in P.HOTKEY_ACTIONS:
            ev.on_hotkey(d["action"])
        elif t == P.LIFECYCLE and ev and d.get("event") in P.LIFECYCLE_EVENTS:
            ev.on_lifecycle(d["event"])
        elif t == P.DEVICE_USAGE and ev:
            ev.on_device_usage(bool(d.get("mic_in_use_by_other")), bool(d.get("camera_in_use")),
                               str(d.get("app_hint", "")))
        elif t == P.PLAYBACK:
            self.resolve_playback(str(d.get("utterance_id", "")), str(d.get("event", "finished")))
        elif t == P.AX_RESULT:
            fut = self._pending.pop(str(d.get("request_id", "")), None)
            if fut and not fut.done():
                if d.get("ok"):
                    fut.set_result(d.get("data") or {})
                else:
                    fut.set_exception(FrontendError(str(d.get("error", "request failed"))))
        elif t == P.LOCATION:
            self.last_location = d
        elif t == P.TEXT_INPUT and ev:
            ev.on_text_input(str(d.get("text") or ""))
        elif t in (P.ORB_CLICK, P.COMMAND) and ev:
            ev.on_orb_click(str(d.get("action") or d.get("name") or ""))
        else:
            log.debug("ignored control message %s", t)

    # -- outbound UI --------------------------------------------------------

    async def _send(self, type_: str, data: dict[str, Any] | None = None) -> bool:
        return await self.server.send_control(P.Control(type_, data or {}))

    async def set_state(self, name: str, caption: str | None = None) -> None:
        self._last_state = (name, caption)
        await self._send(P.STATE, {"name": name, "caption": caption})

    async def caption(self, text: str, final: bool = False) -> None:
        await self._send(P.CAPTION, {"text": text, "final": final})

    async def rich(self, text: str) -> None:
        await self._send(P.RICH, {"text": text})

    async def hide_ui(self) -> None:
        await self._send(P.HIDE_UI)

    async def orb_queue(self, items: list[dict[str, Any]]) -> None:
        self._last_queue = list(items)
        await self._send(P.ORB_QUEUE, {"items": items})

    async def notify(self, title: str, body: str) -> None:
        await self._send(P.NOTIFY, {"title": title, "body": body})

    async def earcon(self, name: str) -> None:
        await self._send(P.EARCON, {"name": name})

    async def permission_prompt(self, which: str) -> None:
        await self._send(P.PERMISSION_PROMPT, {"which": which})

    async def set_mic_enabled(self, enabled: bool) -> None:
        self._mic_enabled = enabled
        await self._send(P.MIC, {"enabled": enabled})

    async def set_capture(self, open_: bool) -> None:
        self._capture_open = open_
        await self._send(P.MIC_OPEN, {"open": open_})

    # -- outbound audio -----------------------------------------------------

    async def play_begin(self, utt: str, interruptible: bool = True) -> None:
        fut = self.expect_playback(utt)
        if not await self._send(P.PLAY_BEGIN, {"utterance_id": utt, "interruptible": interruptible}):
            if not fut.done():
                fut.set_result("finished")  # no Shell: do not stall the conversation

    async def play_chunk(self, utt: str, pcm16: bytes, sample_rate: int) -> None:
        pcm = P.resample_pcm16(pcm16, sample_rate, P.TTS_SAMPLE_RATE)
        await self.server.send_binary(P.encode_tts_frame(utt, pcm))

    async def play_end(self, utt: str) -> None:
        if not await self._send(P.PLAY_END, {"utterance_id": utt}):
            self.resolve_playback(utt, "finished")

    async def stop_audio(self) -> None:
        await self._send(P.STOP_AUDIO, {})
        await super().stop_audio()

    # -- request/response ---------------------------------------------------

    async def request(self, op: str, args: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        if not self.connected:
            raise FrontendError("JarvisShell is not connected")
        msg_type, sub = P.split_op(op)
        rid = uuid.uuid4().hex[:12]
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        data = {"request_id": rid, "op": sub, "args": args or {}}
        if not await self._send(msg_type, data):
            self._pending.pop(rid, None)
            raise FrontendError("JarvisShell is not connected")
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(rid, None)
            raise FrontendError(f"{op} timed out") from e
        finally:
            with contextlib.suppress(Exception):
                self._pending.pop(rid, None)
