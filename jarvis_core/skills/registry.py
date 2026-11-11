"""Builds the full tool registry. Later phases register their own tools here."""

from __future__ import annotations

import importlib
import logging

from jarvis_core.skills.base import ToolRegistry

log = logging.getLogger(__name__)

# Module names under jarvis_core.skills / elsewhere that expose ``register(reg)``.
CORE_MODULES = [
    "jarvis_core.skills.apps",
    "jarvis_core.skills.windows",
    "jarvis_core.skills.browser",
    "jarvis_core.skills.files",
    "jarvis_core.skills.media",
    "jarvis_core.skills.spotify",
    "jarvis_core.skills.system",
    "jarvis_core.skills.productivity",
    "jarvis_core.skills.extras",
    "jarvis_core.skills.terminal",
    "jarvis_core.skills.routines",
    "jarvis_core.skills.more",
    "jarvis_core.skills.calc",
    "jarvis_core.skills.ui_tools",
]

# Optional modules from later phases; absent ones are skipped.
OPTIONAL_MODULES = [
    "jarvis_core.skills.briefing_tools",
    "jarvis_core.skills.alerts_tools",
    "jarvis_core.skills.comms_tools",
    "jarvis_core.skills.google_tools",
    "jarvis_core.skills.vision_tools",
    "jarvis_core.skills.camera_tools",
    "jarvis_core.skills.memory_tools",
    "jarvis_core.skills.voice_id_tools",
]


def build_registry(extra: list[str] | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    for mod in CORE_MODULES:
        importlib.import_module(mod).register(reg)
    for mod in [*OPTIONAL_MODULES, *(extra or [])]:
        try:
            m = importlib.import_module(mod)
        except ModuleNotFoundError as e:
            if e.name == mod:
                continue
            raise
        m.register(reg)
    return reg
