"""Apps Jarvis must never quit, force quit or close, whatever it is asked (Dia is always on the list)."""

from __future__ import annotations

import re

from jarvis_core.errors import SkillError

ALWAYS_PROTECTED = frozenset({"dia"})


def protected_names(cfg=None) -> frozenset[str]:
    extra = getattr(getattr(cfg, "safety", None), "protected_apps", None) or []
    return ALWAYS_PROTECTED | {str(n).strip().lower() for n in extra if str(n).strip()}


def is_protected(app: str, cfg=None) -> bool:
    return (app or "").strip().lower().removesuffix(".app") in protected_names(cfg)


def guard(app: str, cfg=None) -> None:
    """Raise if ``app`` is protected. Call before anything that quits or closes an app."""
    if is_protected(app, cfg):
        raise SkillError(f"{app.strip()} is off limits, sir. I never close it.")


_KILLERS = re.compile(r"\b(?:pkill|killall|kill|osascript|quit|terminate)\b", re.I)


def command_targets_protected(command: str, cfg=None) -> bool:
    """A shell command that looks like it would kill or quit a protected app (``pkill Dia``, ``osascript -e 'quit app "Dia"'``)."""
    if not _KILLERS.search(command or ""):
        return False
    low = (command or "").lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", low) for n in protected_names(cfg))
