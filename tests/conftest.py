from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def jarvis_home(tmp_path, monkeypatch):
    """Isolate every test in its own ~/.jarvis."""
    home = tmp_path / "jarvis_home"
    monkeypatch.setenv("JARVIS_HOME", str(home))
    monkeypatch.delenv("NIM_API_KEY", raising=False)
    # The Google adapter migrates tokens from ~/.openjarvis; point that at an empty folder so tests never pick up
    # (or copy) the real sign-in on this machine.
    from jarvis_core.adapters import google

    monkeypatch.setattr(google, "_LEGACY_DIR", tmp_path / "openjarvis_legacy")
    return home


@pytest.fixture
def apps_dir(tmp_path) -> Path:
    d = tmp_path / "Applications"
    d.mkdir()
    for n in ["Google Chrome", "Spotify", "Visual Studio Code", "WhatsApp", "Notes", "Safari", "System Settings"]:
        (d / f"{n}.app").mkdir()
    return d
