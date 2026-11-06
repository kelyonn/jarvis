"""Localhost WebSocket server that JarvisShell connects to."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from jarvis_core import paths
from jarvis_core.ipc import protocol as P

log = logging.getLogger(__name__)

OnControl = Callable[[P.Control], Awaitable[None] | None]
OnBinary = Callable[[bytes], None]


class IPCServer:
    def __init__(
        self,
        *,
        on_control: OnControl,
        on_mic: OnBinary,
        on_connect: Callable[[dict], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str | None = None,
        ipc_file: str | Path | None = None,
        hello_timeout_s: float = 5.0,
        silence_timeout_s: float = P.PING_INTERVAL_S * P.MISSED_PINGS_LIMIT + 2,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(24)
        self.ipc_file = Path(ipc_file) if ipc_file else paths.ipc_file()
        self._on_control = on_control
        self._on_mic = on_mic
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._hello_timeout = hello_timeout_s
        self._silence_timeout = silence_timeout_s
        self._server = None
        self._conn: ServerConnection | None = None
        self._last_rx = 0.0
        self._watchdog: asyncio.Task | None = None
        self._pinger: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def start(self) -> None:
        self._server = await serve(self._handler, self.host, self.port, max_size=2**22,
                                   ping_interval=None)
        self.port = self._server.sockets[0].getsockname()[1]
        self.ipc_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ipc_file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"host": self.host, "port": self.port, "token": self.token, "pid": os.getpid()}, f)
        os.replace(tmp, self.ipc_file)
        os.chmod(self.ipc_file, 0o600)
        self._pinger = asyncio.create_task(self._ping_loop())
        log.info("IPC listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        for t in (self._pinger, self._watchdog):
            if t:
                t.cancel()
        if self._conn:
            with contextlib.suppress(Exception):
                await self._conn.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # Only remove the file if it is still ours: a second daemon must never delete the running one's address.
        with contextlib.suppress(FileNotFoundError, ValueError, OSError):
            if json.loads(self.ipc_file.read_text()).get("pid") == os.getpid():
                self.ipc_file.unlink()

    # -- sending ------------------------------------------------------------

    async def send_control(self, c: P.Control) -> bool:
        conn = self._conn
        if conn is None:
            return False
        try:
            await conn.send(c.encode())
            return True
        except ConnectionClosed:
            return False

    async def send_binary(self, data: bytes) -> bool:
        conn = self._conn
        if conn is None:
            return False
        try:
            await conn.send(data)
            return True
        except ConnectionClosed:
            return False

    # -- connection handling -------------------------------------------------

    async def _handler(self, ws: ServerConnection) -> None:
        # Authenticate: the first frame must be hello{token}.
        try:
            raw = await asyncio.wait_for(ws.recv(), self._hello_timeout)
            if not isinstance(raw, str):
                raise ValueError("first frame must be text")
            hello = P.Control.decode(raw)
            if hello.type != P.HELLO or not secrets.compare_digest(
                str(hello.data.get("token", "")), self.token
            ):
                raise ValueError("bad hello")
        except Exception as e:
            log.warning("rejected IPC client: %s", e)
            await ws.close(code=4401, reason="unauthorized")
            return

        old = self._conn
        self._conn = ws  # a restarted Shell replaces its stale connection
        if old is not None:
            with contextlib.suppress(Exception):
                await old.close(code=4000, reason="replaced")
        loop = asyncio.get_running_loop()
        self._last_rx = loop.time()
        await ws.send(P.Control(P.WELCOME, {"core_version": "0.1.0"}).encode())
        if self._on_connect:
            self._on_connect({k: v for k, v in hello.data.items() if k != "token"})
        self._watchdog = asyncio.create_task(self._watch(ws))
        try:
            async for msg in ws:
                self._last_rx = loop.time()
                if isinstance(msg, bytes):
                    try:
                        kind, _, payload = P.decode_binary(msg)
                    except ValueError:
                        continue
                    if kind == P.MIC_FRAME:
                        self._on_mic(payload)
                    continue
                try:
                    ctl = P.Control.decode(msg)
                except ValueError:
                    log.warning("bad control frame dropped")
                    continue
                if ctl.type == P.PING:
                    await ws.send(P.Control(P.PONG, {}, ctl.id).encode())
                    continue
                if ctl.type == P.PONG:
                    continue
                res = self._on_control(ctl)
                if asyncio.iscoroutine(res):
                    await res
        except ConnectionClosed:
            pass
        finally:
            if self._watchdog:
                self._watchdog.cancel()
            if self._conn is ws:
                self._conn = None
                if self._on_disconnect:
                    self._on_disconnect()

    async def _watch(self, ws: ServerConnection) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(1.0)
            if loop.time() - self._last_rx > self._silence_timeout:
                log.warning("Shell silent for %.0fs; dropping connection", self._silence_timeout)
                with contextlib.suppress(Exception):
                    await ws.close(code=4408, reason="timeout")
                return

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(P.PING_INTERVAL_S)
            await self.send_control(P.Control(P.PING, {}))
