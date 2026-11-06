"""Filesystem locations. ``JARVIS_HOME`` overrides ``~/.jarvis`` (used by tests)."""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    p = Path(os.environ.get("JARVIS_HOME") or Path.home() / ".jarvis").expanduser()
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:  # logs, transcripts and tokens live here: nobody else on this Mac should be able to look inside
        if p.stat().st_mode & 0o077:
            p.chmod(0o700)
    except OSError:
        pass
    return p


def sub(name: str) -> Path:
    p = home() / name
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


def conversation_file() -> Path:
    return home() / "conversation.json"


def config_file() -> Path:
    return home() / "config.toml"


def logs_dir() -> Path:
    return sub("logs")


def run_dir() -> Path:
    return sub("run")


def ipc_file() -> Path:
    return run_dir() / "ipc.json"


def audit_log() -> Path:
    return home() / "audit.log"


def state_db() -> Path:
    return home() / "state.db"


def memory_db() -> Path:
    return home() / "memory.db"


def cache_db() -> Path:
    return home() / "cache.db"


def transcripts_dir() -> Path:
    return sub("transcripts")


def models_dir() -> Path:
    return sub("models")
