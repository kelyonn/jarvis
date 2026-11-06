"""Rotating file + console logging with secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler

from jarvis_core import paths

_SECRET_RE = re.compile(r"(nvapi-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9_\-\.]{12,})")


def redact(text: str) -> str:
    """Mask API keys and bearer tokens in free text."""
    return _SECRET_RE.sub("[redacted]", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        red = _SECRET_RE.sub("[redacted]", msg)
        if red != msg:
            record.msg, record.args = red, ()
        return True


def setup_logging(name: str = "daemon", *, level: int = logging.INFO, console: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = RotatingFileHandler(paths.logs_dir() / f"{name}.log", maxBytes=2_000_000, backupCount=5)
    fh.setFormatter(fmt)
    fh.addFilter(RedactFilter())
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
        ch.addFilter(RedactFilter())
        root.addHandler(ch)
    for noisy in ("httpx", "httpcore", "websockets", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Harmless chatter that otherwise fills the log on every reply or every launch.
    for chatty in ("phonemizer", "huggingface_hub.utils._http"):
        logging.getLogger(chatty).setLevel(logging.ERROR)
