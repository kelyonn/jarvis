"""Speaker: LLM text stream -> sentences -> TTS -> frontend playback."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor

from jarvis_core.brain.display import ScreenSplitter
from jarvis_core.brain.sentence_splitter import SentenceSplitter
from jarvis_core.frontend import Frontend
from jarvis_core.interfaces import TTS
from jarvis_core.voice.text_norm import caption_text, speakable

log = logging.getLogger(__name__)


class Speaker:
    def __init__(self, tts: TTS, frontend: Frontend) -> None:
        self.tts = tts
        self.frontend = frontend
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-tts")
        self.current_utt: str | None = None
        self.spoken_text: list[str] = []
        self.last_screen = ""  # the formatted (markdown) part of the last reply, if it had one
        self.cap_source: Callable[[], int | None] | None = None  # how many sentences to voice for this reply (None = all)

    async def _synth(self, text: str):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self.tts.synth, text)

    async def speak_stream(
        self,
        chunks: AsyncIterator[str],
        *,
        interruptible: bool = True,
        on_first_audio: Callable[[], None] | None = None,
        wait: bool = True,
    ) -> str:
        """Speak streamed text. Returns the full text that was spoken."""
        utt = uuid.uuid4().hex
        self.current_utt = utt
        splitter = SentenceSplitter()
        screen = ScreenSplitter()
        sent_len = 0
        self.last_screen = ""
        spoken: list[str] = []
        voiced: list[str] = []
        shown: list[str] = []  # the reply as written (line breaks, lists, bold kept) for the chat box
        started = False
        try:
            await self.frontend.play_begin(utt, interruptible)

            async def emit(sentence: str) -> None:
                nonlocal started
                spoken.append(sentence)
                text = speakable(sentence)
                if not text:
                    return
                # The whole reply as written so far, so the chat box can format it (the sentence splitter drops line breaks).
                written = "".join(shown).strip()
                await self.frontend.caption(caption_text(written or " ".join(spoken)), False)
                cap = self.cap_source() if self.cap_source else None
                if cap is not None and len(voiced) >= cap:
                    return  # enough said aloud; the rest stays on the card
                voiced.append(sentence)
                chunk = await self._synth(text)
                if not chunk.pcm16:
                    return
                if not started:
                    started = True
                    if on_first_audio:
                        on_first_audio()
                await self.frontend.play_chunk(utt, chunk.pcm16, chunk.sample_rate)

            async def push_screen(final: bool) -> None:
                nonlocal sent_len
                text = screen.screen
                if text and (final or len(text) - sent_len >= 80) and len(text) != sent_len:
                    sent_len = len(text)
                    self.last_screen = text
                    await self.frontend.rich(text)

            async for piece in chunks:
                said = screen.feed(piece)
                shown.append(said)
                for s in splitter.feed(said):
                    await emit(s)
                await push_screen(False)
            said = screen.flush()
            shown.append(said)
            for s in splitter.feed(said):
                await emit(s)
            tail = splitter.flush()
            if tail:
                await emit(tail)
            await push_screen(True)
            if self.last_screen and not spoken:
                await emit("There you go, it's on your screen.")
            await self.frontend.play_end(utt)
            if wait:
                await self.frontend.wait_playback_done(utt)
        except asyncio.CancelledError:
            await asyncio.shield(self.frontend.stop_audio())
            raise
        finally:
            if self.current_utt == utt:
                self.current_utt = None
            self.cap_source = None  # one reply only
            full = " ".join(voiced).strip()
            self.spoken_text.append(full)
        return " ".join(voiced).strip()

    async def speak(self, text: str, **kw) -> str:
        async def one() -> AsyncIterator[str]:
            yield text

        return await self.speak_stream(one(), **kw)

    async def stop(self) -> None:
        await self.frontend.stop_audio()
