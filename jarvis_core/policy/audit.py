"""Append-only audit log of everything Jarvis does on the user's behalf."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from jarvis_core import paths
from jarvis_core.logging_setup import redact

_MAX_FIELD = 400
_SENSITIVE_KEYS = {"token", "password", "secret", "api_key", "authorization", "text_body"}


# Tools that handle other people's messages or your clipboard: the log records that they ran and a fingerprint of
# the content, never the content itself.
_PRIVATE_TOOLS = {
    "read_messages", "read_conversation", "search_messages", "check_linkedin", "draft_message", "revise_draft", "insert_draft",
    "clipboard_read", "clipboard_write",
}


def _fingerprint(value: Any) -> str:
    import hashlib

    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()[:12]} ({len(text)} chars)"


def _hide_content(fields: dict[str, Any]) -> dict[str, Any]:
    out = dict(fields)
    if isinstance(out.get("args"), dict):
        out["args"] = {k: (_fingerprint(v) if isinstance(v, str) and len(v) > 24 else v) for k, v in out["args"].items()}
    if "result" in out and out["result"] is not None:
        out["result"] = _fingerprint(out["result"])
    return out


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        value = redact(value)
        return value if len(value) <= _MAX_FIELD else value[:_MAX_FIELD] + f"...[{len(value)} chars]"
    if isinstance(value, dict):
        return {
            k: ("***" if str(k).lower() in _SENSITIVE_KEYS else _clip(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in value[:20]]
    return value


class AuditLog:
    """One JSON object per line: ``{ts, kind, ...}``. Thread-safe, 0600, size-capped rotation."""

    def __init__(self, path: Path | None = None, max_bytes: int = 5_000_000) -> None:
        self.path = Path(path) if path else paths.audit_log()
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def write(self, kind: str, **fields: Any) -> None:
        if fields.get("tool") in _PRIVATE_TOOLS:
            fields = _hide_content(fields)
        rec = {"ts": round(time.time(), 3), "kind": kind, **{k: _clip(v) for k, v in fields.items()}}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                os.replace(self.path, self.path.with_suffix(".log.1"))
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out
