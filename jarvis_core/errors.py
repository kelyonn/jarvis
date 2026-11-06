"""Exception types shared across Jarvis."""

from __future__ import annotations


class JarvisError(Exception):
    """Base class."""


class LLMUnavailable(JarvisError):
    """The model endpoint is unreachable or timed out."""


class LLMRateLimited(LLMUnavailable):
    """HTTP 429 from the model endpoint."""


class LLMAuthError(LLMUnavailable):
    """Missing or rejected API key."""


class SkillError(JarvisError):
    """A skill could not complete; the message is safe to speak."""


class PermissionMissing(SkillError):
    """A macOS permission (Accessibility, Screen Recording, ...) is not granted."""

    def __init__(self, which: str, message: str | None = None) -> None:
        self.which = which
        super().__init__(message or f"I need the {which} permission for that, sir.")


class ShortcutMissing(SkillError):
    """A Shortcuts shortcut Jarvis needs isn't installed. ``buildable`` says whether Jarvis can make it."""

    def __init__(self, name: str, buildable: bool) -> None:
        self.name = name
        self.buildable = buildable
        if buildable:
            msg = f"The '{name}' shortcut isn't set up yet, sir."
        else:
            msg = f"There's no '{name}' shortcut, sir, and I can't make that one. Create it in the Shortcuts app."
        super().__init__(msg)


class Blocked(JarvisError):
    """An action was refused by policy (never-send, never-click, blocked tool)."""
