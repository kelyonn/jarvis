"""Keep secrets out of what is sent to the language model.

The model is remote, and its input includes whatever a tool returned: a file, a web page, a message, the clipboard. A
password, API key, private key or card number in there has no reason to leave the Mac, and the model never needs the
real value to do its job. So this replaces them with a short marker just before a request is sent.

Scope, on purpose: secrets and card numbers only. Names, emails and phone numbers are not touched, because Jarvis needs
them to read your messages and make calls, and masking them would break those jobs.
"""

from __future__ import annotations

import re
from typing import Any

SECRET = "[redacted secret]"
CARD = "[redacted card number]"

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\b(?:sk|pk|rk|nvapi)-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
]
# "password: hunter22", "api_key=abc123...": keep the label, hide the value.
_KEYVALUE = re.compile(
    r"(?i)\b(pass(?:word|wd)?|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token)(\s*[:=]\s*)([\"']?)([^\s\"',;]{6,})\3"
)
_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")


def _luhn(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if flip:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        flip = not flip
    return total % 10 == 0


def _card(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return CARD if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)


def redact(text: str) -> tuple[str, int]:
    """Return the text with secrets masked, and how many were masked."""
    count = 0
    for pat in _PATTERNS:
        text, n = pat.subn(SECRET, text)
        count += n
    text, n = _KEYVALUE.subn(lambda m: f"{m.group(1)}{m.group(2)}{SECRET}", text)
    count += n
    masked = _CARD.sub(_card, text)
    if masked != text:
        count += masked.count(CARD) - text.count(CARD)
    return masked, count


def redact_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Copy of a chat message list with secrets masked in text content and in tool-call arguments."""
    total = 0
    out: list[dict[str, Any]] = []
    for msg in messages:
        new = dict(msg)
        content = msg.get("content")
        if isinstance(content, str):
            new["content"], n = redact(content)
            total += n
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text, n = redact(part["text"])
                    parts.append({**part, "text": text})
                    total += n
                else:
                    parts.append(part)
            new["content"] = parts
        calls = msg.get("tool_calls")
        if isinstance(calls, list):
            new_calls = []
            for call in calls:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                    args, n = redact(fn["arguments"])
                    new_calls.append({**call, "function": {**fn, "arguments": args}})
                    total += n
                else:
                    new_calls.append(call)
            new["tool_calls"] = new_calls
        out.append(new)
    return out, total
