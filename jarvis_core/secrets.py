"""Secrets via the macOS Keychain (``security`` CLI), with an env-var fallback.

Service name is ``jarvis``; the account is the secret name (e.g. ``NIM_API_KEY``).
Secrets are never written to config files or logs.
"""

from __future__ import annotations

import os
import subprocess

SERVICE = "jarvis"


class SecretError(RuntimeError):
    pass


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=10)


def get_secret(name: str, *, env_fallback: bool = True) -> str | None:
    """Return the secret or ``None``. Environment variable wins when set."""
    if env_fallback and os.environ.get(name):
        return os.environ[name]
    try:
        r = _run(["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.rstrip("\n") or None


def set_secret(name: str, value: str) -> None:
    try:
        r = _run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", name, "-w", value])
    except FileNotFoundError as e:
        raise SecretError("macOS `security` tool not available") from e
    if r.returncode != 0:
        raise SecretError(f"could not store {name} in Keychain: {r.stderr.strip()}")


def delete_secret(name: str) -> bool:
    try:
        r = _run(["security", "delete-generic-password", "-s", SERVICE, "-a", name])
    except FileNotFoundError:
        return False
    return r.returncode == 0


def mask(value: str | None) -> str:
    if not value:
        return "<unset>"
    return value[:4] + "…" + value[-2:] if len(value) > 8 else "***"
