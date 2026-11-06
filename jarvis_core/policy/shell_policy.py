"""Classifies terminal commands as allow / confirm / block."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from jarvis_core.skills.base import Decision

_META = re.compile(r"[;|&<>`$()\n\\]")
# Flags that make an otherwise read-only command write a file (git log --output=~/.zshrc)
_WRITES = re.compile(r"(?:^|\s)--output(?:-directory)?\b|(?:^|\s)--exec\b|(?:^|\s)-exec\b|(?:^|\s)--ext-diff\b")

_BLOCK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsudo\b"), "administrator commands are off limits"),
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*(/|~|\$HOME|/\*|~/\*)(\s|$)"), "that would wipe too much"),
    (re.compile(r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(/|~)"), "that would wipe too much"),
    (re.compile(r"\bmkfs\b|\bdiskutil\s+(erase|partition|apfs\s+delete)"), "disk formatting is off limits"),
    (re.compile(r"\bdd\b.*\bof=/dev/"), "writing to raw devices is off limits"),
    (re.compile(r":\s*\(\s*\)\s*\{"), "fork bombs are off limits"),
    (re.compile(r"(curl|wget)[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b"), "piping downloads into a shell is off limits"),
    (re.compile(r"\bchmod\s+-R\s+7[0-7][0-7]\s+/"), "that would break permissions"),
    (re.compile(r"\b(shutdown|reboot|halt)\b"), "use the power controls instead"),
    (re.compile(r"security\s+(find|dump|export)-"), "keychain access is off limits"),
    (re.compile(r"\.ssh/(id_|.*\.pem)|\.jarvis/(config|audit|run)|Keychains|\.aws/credentials"), "that touches secrets"),
    (re.compile(r"\bosascript\b.*\b(send|keystroke\s+return)"), "I never send things, sir"),
]

# Read-only commands that never need confirmation. Matched on the exact command (after normalisation)
# or on a prefix when listed with a trailing "*".
_DEFAULT_SAFE = {
    "ls", "pwd", "df -h", "uptime", "date", "whoami", "hostname", "sw_vers", "uname -a",
    "ls *", "git status", "git log --oneline *", "git branch", "git diff --stat",
}


def classify_command(cmd: str, extra_allow: list[str] | None = None) -> Decision:
    text = cmd.strip()
    if not text:
        return Decision.block("empty command")
    for pat, why in _BLOCK_PATTERNS:
        if pat.search(text):
            return Decision.block(why)
    allow = {a.strip() for a in (extra_allow or [])} | _DEFAULT_SAFE
    if not _META.search(text):
        norm = " ".join(text.split())
        if norm in allow:
            return Decision.allow()
        for a in allow:
            if a.endswith(" *") and norm.startswith(a[:-1]) and ".." not in norm and not _WRITES.search(norm):
                # prefix allowance, but never for paths into protected places
                if not re.search(r"\.ssh|\.jarvis|Keychains|\.aws", norm):
                    return Decision.allow()
    short = text if len(text) <= 80 else text[:77] + "..."
    return Decision.confirm(f"Shall I run: {short}?")


def split_argv(cmd: str) -> list[str] | None:
    """argv for a metachar-free command, else None (caller must use a shell)."""
    if _META.search(cmd):
        return None
    try:
        return shlex.split(cmd)
    except ValueError:
        return None


_PROTECTED_DIRS = [
    ".ssh", ".gnupg", ".aws", ".jarvis", "Library/Keychains", ".config/gcloud", ".kube", ".docker", ".config/gh", ".config/op",
    "Library/Cookies", "Library/Messages", "Library/Safari", "Library/Group Containers/group.net.whatsapp.WhatsApp.shared",
]
_PROTECTED_NAMES = {
    ".env", "id_rsa", "id_ed25519", "credentials.json", "token.json", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
    "Login Data", "Cookies", "Cookies.binarycookies", "Web Data", "logins.json", "key4.db",  # browser password and cookie stores
}


def is_protected_path(path: Path, home: Path | None = None) -> bool:
    home = (home or Path.home()).resolve()
    try:
        p = path.expanduser().resolve()
    except OSError:
        return True
    for d in _PROTECTED_DIRS:
        base = (home / d).resolve()
        if p == base or base in p.parents:
            return True
    return p.name in _PROTECTED_NAMES or p.name.startswith(".env")
