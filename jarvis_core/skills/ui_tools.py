"""Control any app through its accessibility tree: look, then act on what you saw, then check it worked.

``ui_look`` reads the front app (or one you name) as a short list of the things you can interact with, each with a
reference such as ``s3:e12``. ``ui_do`` acts on one reference. Three habits keep it from messing things up:

* **References expire.** Every look starts a new snapshot. A reference from an older one is refused ("look again")
  instead of being applied to whatever now sits at that place.
* **Per-app rules.** ``safety.app_rules`` sets each app to ``auto`` (act), ``ask`` (a spoken yes first) or ``never``
  (look only). Terminals and password managers are ``never`` unless you say otherwise; messaging and mail apps ask.
* **Nothing is assumed.** After every action it looks again and reports what actually changed, including "nothing
  changed", so Jarvis can't claim success it did not see.

Controls labelled send, post, buy, delete and the like are always refused (the same list computer use obeys).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from jarvis_core.comms.axutil import label as node_label
from jarvis_core.comms.axutil import walk
from jarvis_core.errors import SkillError
from jarvis_core.policy.action_filter import ActionFilter
from jarvis_core.skills.base import Decision, E, S, Tool, ToolContext, ToolRegistry, obj

STATE_KEY = "ui_snapshot"
MAX_LINES = 90
MAX_NODES = 900
SETTLE_S = 0.45

# Built-in rules. Anything in ``safety.app_rules`` overrides these.
DEFAULT_RULES: dict[str, str] = {
    "terminal": "never", "iterm2": "never", "iterm": "never", "warp": "never",
    "1password": "never", "keychain access": "never", "passwords": "never", "bitwarden": "never",
    "messages": "ask", "mail": "ask", "outlook": "ask", "slack": "ask", "whatsapp": "ask", "telegram": "ask",
    "signal": "ask", "discord": "ask", "teams": "ask", "microsoft teams": "ask", "linkedin": "ask",
}
RULES = ("auto", "ask", "never")

_ROLE_NAMES = {
    "AXButton": "button", "AXTextField": "field", "AXTextArea": "text area", "AXSearchField": "search field",
    "AXComboBox": "combo box", "AXCheckBox": "checkbox", "AXRadioButton": "radio", "AXPopUpButton": "popup",
    "AXMenuItem": "menu item", "AXMenuButton": "menu button", "AXLink": "link", "AXSlider": "slider",
    "AXStaticText": "text", "AXRow": "row", "AXCell": "cell", "AXTabGroup": "tabs", "AXRadioGroup": "radio group",
    "AXStepper": "stepper", "AXDisclosureTriangle": "disclosure", "AXOutlineRow": "row", "AXImage": "image",
}
_INTERACTIVE_ROLES = {
    "AXButton", "AXTextField", "AXTextArea", "AXSearchField", "AXComboBox", "AXCheckBox", "AXRadioButton",
    "AXPopUpButton", "AXMenuItem", "AXMenuButton", "AXLink", "AXSlider", "AXStepper", "AXDisclosureTriangle",
}
_TEXT_INPUT = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}
_REF = re.compile(r"^s(\d+):(e\d+)$")


# ------------------------------------------------------------------ pure helpers (tested without a Mac)


def app_rule(cfg: Any, app: str) -> str:
    """auto, ask or never for this app. The user's ``safety.app_rules`` win over the built-in list."""
    key = (app or "").strip().lower().removesuffix(".app")
    user = {str(k).strip().lower(): str(v).strip().lower() for k, v in (getattr(cfg.safety, "app_rules", None) or {}).items()}
    rule = user.get(key) or DEFAULT_RULES.get(key) or "auto"
    return rule if rule in RULES else "ask"


def user_rule(cfg: Any, app: str) -> str | None:
    """The rule the user set for this app in ``safety.app_rules``, or None. Built-in defaults do not count."""
    key = (app or "").strip().lower().removesuffix(".app")
    user = {str(k).strip().lower(): str(v).strip().lower() for k, v in (getattr(cfg.safety, "app_rules", None) or {}).items()}
    rule = user.get(key)
    return None if rule is None else (rule if rule in RULES else "ask")


def _kind(node: dict[str, Any]) -> str:
    role = str(node.get("role", ""))
    return _ROLE_NAMES.get(role) or (role.removeprefix("AX").lower() or "element")


def _is_interactive(node: dict[str, Any]) -> bool:
    return node.get("role") in _INTERACTIVE_ROLES or "Press" in (node.get("actions") or [])


def describe(node: dict[str, Any]) -> str:
    text = " ".join(str(node.get(k, "")) for k in ("title", "desc") if node.get(k)).strip()
    kind = _kind(node)
    out = f'{kind} "{text[:70]}"' if text else kind
    value = node.get("value")
    if node.get("role") == "AXSecureTextField":
        return f"{kind} (password field)"
    if value not in (None, "") and node.get("role") != "AXStaticText":
        v = {"1": "on", "0": "off"}.get(str(value), str(value))
        out += f' = "{v[:60]}"'
    elif node.get("role") == "AXStaticText" and not text:
        out = f'text "{str(value)[:80]}"'
    return out


def compact(snapshot_id: int, tree: dict[str, Any] | None, limit: int = MAX_LINES) -> tuple[list[str], dict[str, dict[str, Any]], int]:
    """Short lines for the model plus ``ref -> node``. Things you can interact with come first, then readable text."""
    interactive: list[tuple[str, dict[str, Any]]] = []
    texts: list[tuple[str, dict[str, Any]]] = []
    total = 0
    for node in walk(tree):
        if not node.get("id"):
            continue
        total += 1
        f = node.get("frame")
        if f and (f[2] <= 0 or f[3] <= 0):
            continue
        ref = f"s{snapshot_id}:{node['id']}"
        if _is_interactive(node) and (node_label(node) or node.get("role") in _TEXT_INPUT):
            interactive.append((ref, node))
        elif node.get("role") == "AXStaticText" and (node.get("value") or node.get("title")):
            texts.append((ref, node))
    keep = interactive[:limit] + texts[: max(0, limit - len(interactive))]
    refs = {ref: n for ref, n in keep}
    lines = [f"{ref} {describe(n)}" for ref, n in keep]
    return lines, refs, total


def signature(node: dict[str, Any]) -> tuple[str, str, str]:
    return (str(node.get("role", "")), node_label(node)[:80], str(node.get("value", ""))[:80])


def summarize_change(before: dict[str, Any] | None, after: dict[str, Any] | None, target: dict[str, Any], action: str,
                     text: str = "") -> str:
    """Say what really changed on screen, from two trees. Never claims more than was seen."""
    old = {signature(n) for n in walk(before)}
    new = {signature(n) for n in walk(after)}
    added, gone = new - old, old - new
    if action == "set_value":
        want_role, want_label = target.get("role"), node_label({k: v for k, v in target.items() if k != "value"})
        for n in walk(after):
            if n.get("role") == want_role and node_label({k: v for k, v in n.items() if k != "value"}) == want_label:
                if str(n.get("value", "")) == text:
                    return "The field now holds that text."
        return "I couldn't confirm the field took the text."
    if not added and not gone:
        return "Nothing on screen visibly changed."
    shown = [lbl for _, lbl, _ in sorted(added) if lbl][:3]
    base = f"The screen changed ({len(added)} new, {len(gone)} gone)"
    return base + (f", now showing {', '.join(repr(s) for s in shown)}." if shown else ".")


def parse_ref(ref: str) -> tuple[int, str]:
    m = _REF.match((ref or "").strip())
    if not m:
        raise SkillError("That isn't a reference I gave you, sir. Look first, then use one from the list.")
    return int(m.group(1)), m.group(2)


# ------------------------------------------------------------------ tools


async def _snapshot(ctx: ToolContext, app: str | None) -> dict[str, Any]:
    args: dict[str, Any] = {"max_depth": 16, "max_nodes": MAX_NODES}
    if app:
        args["app"] = app
    data = await ctx.shell_request("ax.tree", args, timeout=15)
    n = ctx.state.get("ui_counter", 0) + 1
    ctx.state["ui_counter"] = n
    lines, refs, total = compact(n, data.get("tree"))
    snap = {"id": n, "app": data.get("app", ""), "tree": data.get("tree"), "refs": refs, "lines": lines, "total": total}
    ctx.state[STATE_KEY] = snap
    return snap


async def _look(ctx: ToolContext, a: dict[str, Any]) -> str:
    snap = await _snapshot(ctx, (a.get("app") or "").strip() or None)
    rule = app_rule(ctx.cfg, snap["app"])
    if not snap["lines"]:
        return f"{snap['app'] or 'That app'} shows nothing I can interact with through accessibility, sir."
    head = f"{snap['app']} (snapshot {snap['id']}, {len(snap['lines'])} of {snap['total']} elements"
    head += ", look only: that app is off limits for actions)" if rule == "never" else ")"
    return head + "\n" + "\n".join(snap["lines"])


def _target(ctx: ToolContext, ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
    snap = ctx.state.get(STATE_KEY)
    sid, _ = parse_ref(ref)
    if snap is None or sid != snap["id"]:
        raise SkillError("The screen has moved on since that look, sir. I need to look again before I touch anything.")
    node = snap["refs"].get(ref.strip())
    if node is None:
        raise SkillError("That reference isn't in the current look, sir.")
    return snap, node


def _classify(ctx: ToolContext, a: dict[str, Any]) -> Decision:
    try:
        snap, node = _target(ctx, a.get("ref", ""))
    except SkillError:
        return Decision.allow()  # the handler reports the real problem; nothing can run without a valid reference
    app = snap["app"]
    name = node_label(node) or _kind(node)
    rule = app_rule(ctx.cfg, app)
    action = a.get("action", "press")
    if rule == "never":
        return Decision.block(f"{app} is off limits for me, so I only look at it")
    if action in {"press", "click"}:
        verdict = ActionFilter(ctx.cfg.safety).check_click(name)
        if not verdict:
            return Decision.block(verdict.reason)
    if rule == "ask":
        return Decision.confirm(_preview(action, name, a.get("text", ""), app))
    return Decision.allow()


def _preview(action: str, name: str, text: str, app: str) -> str:
    if action == "set_value":
        return f"Shall I put '{text[:40]}' into {name} in {app}?"
    if action in {"increment", "decrement"}:
        return f"Shall I {action} {name} in {app}?"
    return f"Shall I {'click' if action == 'click' else 'press'} {name} in {app}?"


async def _do(ctx: ToolContext, a: dict[str, Any]) -> str:
    snap, node = _target(ctx, a["ref"])
    action = a.get("action", "press")
    text = a.get("text", "")
    eid = node["id"]
    name = node_label(node) or _kind(node)
    if action == "set_value":
        if node.get("role") not in _TEXT_INPUT:
            raise SkillError(f"{name} isn't a text field, sir.")
        await ctx.shell_request("ax.set_value", {"element_id": eid, "text": text})
    elif action == "press":
        await ctx.shell_request("ax.press", {"element_id": eid, "label": name})
    elif action in {"increment", "decrement"}:
        await ctx.shell_request("ax.adjust", {"element_id": eid, "direction": action})
    elif action == "click":
        f = node.get("frame")
        if not f:
            raise SkillError(f"I can't tell where {name} is on screen, sir.")
        await ctx.shell_request("input.click", {"x": f[0] + f[2] / 2, "y": f[1] + f[3] / 2, "label": name})
    else:
        raise SkillError("I can press, click, set a field's text, or step a slider, sir.")
    await asyncio.sleep(SETTLE_S)
    try:
        after = await _snapshot(ctx, snap["app"])
        report = summarize_change(snap["tree"], after["tree"], node, action, text)
    except SkillError:
        report = "I couldn't look again to check the result."
    verb = {"press": "Pressed", "click": "Clicked", "set_value": "Set", "increment": "Raised", "decrement": "Lowered"}[action]
    return f"{verb} {name} in {snap['app']}. {report} (The old references have expired; look again to continue.)"


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "ui_look",
        "Read the controls of an app (default: the front one) as a short list with references. Use before ui_do. "
        "Works in any app; apps that expose little through accessibility will show few items.",
        obj({"app": S("Optional app name, default the front app")}),
        _look, category="ui", untrusted_output=True, timeout_s=25))
    reg.register(Tool(
        "ui_do",
        "Act on one reference from the latest ui_look: press a button, click, set a text field, or step a slider. "
        "It checks afterwards and reports what really changed. References expire after every look or action.",
        obj({
            "ref": S("Reference from the latest ui_look, like s3:e12"),
            "action": E("What to do", ["press", "click", "set_value", "increment", "decrement"]),
            "text": S("Text for set_value"),
        }, ["ref"]),
        _do, classify=_classify, category="ui", untrusted_output=True, timeout_s=40))
