from __future__ import annotations

import base64
import json

import pytest

from jarvis_core.brain.llm import ScriptedLLM
from jarvis_core.brain.toolselect import match_categories
from jarvis_core.config import Config
from jarvis_core.errors import SkillError
from jarvis_core.policy.audit import AuditLog
from jarvis_core.skills import vision_tools as VT
from jarvis_core.skills.base import CmdResult, ToolContext, ToolRegistry
from jarvis_core.vision.editor import CodeEditor
from jarvis_core.vision.screen import ScreenVision

BUGGY = "def maxDepth(s):\n    depth = 0\n    for c in s:\n        if c == '(':\n            depth -= 1\n"
EDITOR_TREE = {"id": "w", "role": "AXWindow", "children": [
    {"id": "s", "role": "AXTextField", "desc": "Search", "frame": [0, 0, 100, 20]},
    {"id": "ed", "role": "AXTextArea", "desc": "Editor content;Press Alt+F1 for Accessibility Options.", "frame": [400, 200, 600, 500]},
]}


class Shell:
    connected = True

    def __init__(self, app="Google Chrome", tree=None):
        self.app, self.tree = app, tree or EDITOR_TREE
        self.requests: list[tuple[str, dict]] = []
        self.on_copy = None

    async def request(self, op, args, timeout=10.0):
        self.requests.append((op, dict(args)))
        if op == "input.key" and args.get("key") == "c" and self.on_copy:
            self.on_copy()
        if op == "ax.frontmost":
            return {"app": self.app, "window": "LeetCode"}
        if op == "ax.tree":
            return {"tree": self.tree}
        if op == "screenshot":
            return {"image_b64": "AAAA", "mime": "image/jpeg", "width": 800, "height": 500}
        return {"ok": True}

    def keys(self):
        return [("+".join([*a["modifiers"], a["key"]])) for o, a in self.requests if o == "input.key"]


class Clipboard:
    """A runner that fakes pbcopy/pbpaste (Cmd+C copies `editor`) and osascript (the in-page script)."""

    def __init__(self, shell, editor=BUGGY, js=None):
        self.shell, self.editor, self.js = shell, editor, js
        self.clip = "user's clipboard"
        self.scripts: list[str] = []
        shell.on_copy = lambda: setattr(self, "clip", self.editor)

    async def __call__(self, argv, **kw):
        if argv[0] == "pbpaste":
            return CmdResult(0, self.clip)
        if argv[0] == "pbcopy":
            self.clip = kw.get("input", "")
            return CmdResult(0, "")
        if argv[0] == "osascript":
            self.scripts.append(argv[-1])
            return self.js(argv[-1]) if self.js else CmdResult(1, "", "Executing JavaScript through AppleScript is turned off")
        return CmdResult(0, "")


def ctx_for(shell, runner, llm=None):
    ctx = ToolContext(cfg=Config(), frontend=shell, runner=runner, audit=AuditLog())
    vision = ScreenVision(ctx, llm or ScriptedLLM(["Case 2: expected 3, got -3"]))
    ctx.services["vision"] = vision
    return ctx, CodeEditor(ctx, vision, settle_s=0)


async def test_reads_monaco_exactly_through_the_page():
    shell = Shell()
    runner = Clipboard(shell, js=lambda js: CmdResult(0, json.dumps({"ok": True, "kind": "monaco", "lang": "python", "code": BUGGY})))
    _, ed = ctx_for(shell, runner)
    got = await ed.read()
    assert got.code == BUGGY and got.source == "monaco"
    report = got.report()
    assert "exact" in report and "depth -= 1" in report and "expected 3, got -3" in report
    assert shell.keys() == []  # nothing typed


async def test_writes_monaco_as_one_edit_without_keystrokes():
    shell = Shell()

    def js(script):
        if "atob('" in script:
            b64 = script.split("atob('")[1].split("'")[0]
            assert base64.b64decode(b64).decode() == BUGGY.replace("depth -= 1", "depth += 1")
            return CmdResult(0, json.dumps({"ok": True, "kind": "monaco"}))
        return CmdResult(0, json.dumps({"ok": True, "kind": "monaco", "code": BUGGY}))

    runner = Clipboard(shell, js=js)
    _, ed = ctx_for(shell, runner)
    msg = await ed.replace(find="depth -= 1", replace="depth += 1")
    assert "Cmd+Z" in msg and shell.keys() == []


async def test_falls_back_to_clipboard_when_page_scripting_is_off():
    shell = Shell()
    runner = Clipboard(shell)
    _, ed = ctx_for(shell, runner)
    got = await ed.read(with_output=False)
    assert got.code == BUGGY and got.source == "clipboard"
    clicks = [a for o, a in shell.requests if o == "input.click"]
    assert clicks and "Editor content" in clicks[0]["label"]  # clicked the editor, not the search box
    assert runner.clip == "user's clipboard"  # restored

    fixed = BUGGY.replace("-=", "+=")
    await ed.replace(fixed)
    assert shell.keys()[-2:] == ["cmd+a", "cmd+v"]
    assert all(k not in ("return", "enter", "cmd+return") for k in shell.keys())
    assert runner.clip == "user's clipboard"


async def test_find_must_be_unique_and_present():
    shell = Shell()
    runner = Clipboard(shell, js=lambda js: CmdResult(0, json.dumps({"ok": True, "kind": "monaco", "code": BUGGY})))
    _, ed = ctx_for(shell, runner)
    with pytest.raises(SkillError, match="isn't in the editor"):
        await ed.replace(find="depth *= 2", replace="x")
    with pytest.raises(SkillError, match="appears 2 times"):
        await ed.replace(find="depth", replace="d")


async def test_vision_fallback_when_no_editor_can_be_focused():
    shell = Shell(tree={"id": "w", "role": "AXWindow", "children": []})
    runner = Clipboard(shell)
    llm = ScriptedLLM(["CODE:\ndef f():\n    return 1\nOUTPUT:\nnone"])
    _, ed = ctx_for(shell, runner, llm)
    got = await ed.read()
    assert got.source == "vision" and got.code == "def f():\n    return 1"
    assert "may have small errors" in got.report()
    with pytest.raises(SkillError, match="whole corrected code"):
        await ed.replace(find="return 1", replace="return 2")


async def test_private_apps_are_refused():
    shell = Shell(app="1Password")
    _, ed = ctx_for(shell, Clipboard(shell))
    with pytest.raises(SkillError):
        await ed.read()


async def test_tools_registered_without_confirmation():
    reg = ToolRegistry()
    VT.register(reg)
    edit = reg.get("edit_screen_code")
    assert edit.risk.value == "safe" and reg.get("read_screen_code") is not None
    shell = Shell()
    runner = Clipboard(shell, js=lambda js: CmdResult(0, json.dumps({"ok": True, "kind": "monaco", "code": BUGGY})))
    ctx, _ = ctx_for(shell, runner)
    out = await reg.get("read_screen_code").handler(ctx, {})
    assert "depth -= 1" in out


def test_code_requests_offer_the_editor_tools():
    for text in ("change it for me", "fix my code", "what's wrong on line 7", "type it in on leetcode"):
        assert "vision" in match_categories(text), text


async def test_finds_leetcode_editor_labelled_code_editor_beyond_700_nodes():
    filler = [{"id": f"f{i}", "role": "AXStaticText", "title": f"t{i}", "frame": [0, 0, 5, 5]} for i in range(1000)]
    tree = {"id": "w", "role": "AXWindow", "children": [
        *filler,
        {"id": "other", "role": "AXTextArea", "desc": "Notes", "frame": [0, 0, 50, 50]},
        {"id": "ed", "role": "AXTextArea", "title": "Code editor", "desc": "Code editor", "value": BUGGY, "frame": [813, 241, 665, 20]},
    ]}
    shell = Shell(app="Dia", tree=tree)
    runner = Clipboard(shell)
    _, ed = ctx_for(shell, runner)
    got = await ed.read(with_output=False)
    assert got.code == BUGGY and got.source == "clipboard"
    clicks = [a for o, a in shell.requests if o == "input.click"]
    assert clicks and clicks[0]["label"].startswith("Code editor")
    assert [a for o, a in shell.requests if o == "ax.tree"][0]["max_nodes"] >= 1500


async def test_accessibility_text_is_the_last_resort_before_vision():
    tree = {"id": "w", "role": "AXWindow", "children": [
        {"id": "ed", "role": "AXTextArea", "desc": "Code editor", "value": BUGGY, "frame": [10, 10, 600, 400]}]}
    shell = Shell(app="Dia", tree=tree)
    runner = Clipboard(shell, editor="")  # copy yields nothing
    _, ed = ctx_for(shell, runner)
    got = await ed.read(with_output=False)
    assert got.source == "accessibility" and got.code == BUGGY
    with pytest.raises(SkillError, match="whole corrected code"):
        await ed.replace(find="depth -= 1", replace="x")
