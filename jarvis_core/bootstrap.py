"""Wire everything together and run the daemon."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from jarvis_core import paths
from jarvis_core.config import Config, load_config, reload_into
from jarvis_core.logging_setup import setup_logging

log = logging.getLogger(__name__)


async def _watch_config(cfg: Config) -> None:
    try:
        from watchfiles import awatch
    except ImportError:
        return
    path = paths.config_file()
    async for _ in awatch(path.parent, recursive=False):
        if not path.exists():
            continue
        try:
            reload_into(cfg, load_config(path))
            log.info("config reloaded")
        except Exception as e:
            log.error("config reload rejected: %s", e)


async def _main(cfg: Config, standalone: bool, ipc_file: str | None) -> int:
    from jarvis_core.runtime import build_runtime

    rt = await build_runtime(cfg, standalone=standalone, ipc_file=ipc_file)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, rt.daemon.request_stop)
    from jarvis_core.permissions_probe import watch_probe

    watcher = asyncio.create_task(_watch_config(cfg))
    prober = asyncio.create_task(watch_probe(rt.frontend))
    from jarvis_core.devhook import watch_devhook

    devhook = asyncio.create_task(watch_devhook(rt.assistant))
    from jarvis_core.voice.enroll import watch_requests

    voice_id = asyncio.create_task(watch_requests(rt.daemon))
    try:
        await rt.run()
    finally:
        watcher.cancel()
        prober.cancel()
        devhook.cancel()
        voice_id.cancel()
    return 0


def _single_instance():
    """Hold an exclusive lock for the daemon's lifetime. Returns the open lock file, or None if another daemon runs."""
    import fcntl

    f = open(paths.run_dir() / "daemon.lock", "a+")  # noqa: SIM115 - kept open on purpose
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def _models_cached(cfg: Config) -> bool:
    """True when the speech-to-text model and every configured Kokoro voice are already on disk."""
    from pathlib import Path

    hub = Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME") or Path.home() / ".cache/huggingface") / "hub")
    stt = hub / ("models--" + cfg.voice.stt_model.replace("/", "--"))
    kokoro = list((hub / "models--hexgrad--Kokoro-82M" / "snapshots").glob("*"))
    if not any(stt.glob("snapshots/*/*")) or not kokoro:
        return False
    voices = [v.strip() for v in str(getattr(cfg.voice, "tts_voice", "") or "").split(",") if v.strip()]
    return all(any((k / "voices" / f"{v}.pt").exists() for k in kokoro) for v in voices)


def run_daemon(*, standalone_audio: bool = False, ipc_file: str | None = None) -> int:
    # The wake-word model runs every 80 ms; OpenMP worker threads that spin between calls burn a core for nothing.
    for k, v in {"KMP_BLOCKTIME": "0", "OMP_WAIT_POLICY": "PASSIVE", "OMP_NUM_THREADS": "2",
                 "TOKENIZERS_PARALLELISM": "false"}.items():
        os.environ.setdefault(k, v)
    setup_logging("daemon", console=standalone_audio)
    lock = _single_instance()
    if lock is None:
        msg = "another Jarvis daemon is already running (Jarvis.app starts one for you); not starting a second"
        log.error(msg)
        print(msg)
        return 3
    cfg = load_config()
    if _models_cached(cfg):  # skip the "any updates?" round trip to Hugging Face on every launch
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        log.info("speech models cached; loading them offline")
    from jarvis_core.voice import text_norm

    text_norm.LEXICON.update(cfg.voice.pronunciations)
    log.info("starting jarvis_core (standalone=%s)", standalone_audio)
    return asyncio.run(_main(cfg, standalone_audio, ipc_file))
