"""Display, power, Wi-Fi/Bluetooth, system info, clipboard, time."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from datetime import datetime
from typing import Any

import psutil
from rapidfuzz import fuzz, process

from jarvis_core.errors import SkillError
from jarvis_core.skills.base import (
    B,
    Decision,
    E,
    I,
    N,
    Risk,
    S,
    Tool,
    ToolContext,
    ToolRegistry,
    obj,
)
from jarvis_core.voice.text_norm import spoken_time

# ---- display ----------------------------------------------------------------


async def _get_brightness(ctx: ToolContext) -> float | None:
    res = await ctx.run(["brightness", "-l"])
    if res.ok:
        m = re.search(r"brightness\s+([0-9.]+)", res.out)
        if m:
            return float(m.group(1))
    return None


async def _set_brightness(ctx: ToolContext, a: dict[str, Any]) -> str:
    pct = max(0, min(100, int(a["level"])))
    res = await ctx.run(["brightness", f"{pct / 100:.2f}"])
    if res.ok:
        return f"Brightness at {pct} percent."
    # Fallback: brightness keys (16 steps across the range).
    cur = 0.5
    steps = round((pct / 100 - cur) * 16)
    key = 144 if steps > 0 else 145
    script = f"tell application \"System Events\" to repeat {abs(steps)} times\nkey code {key}\nend repeat" if steps else ""
    if script:
        await ctx.osa(script)
    return "Brightness adjusted (install the 'brightness' tool for exact levels)."


async def _change_brightness(ctx: ToolContext, a: dict[str, Any]) -> str:
    cur = await _get_brightness(ctx)
    step = int(a.get("amount") or 15)
    if cur is None:
        key = 144 if a["direction"] == "up" else 145
        await ctx.osa(f"tell application \"System Events\" to repeat {max(1, step // 6)} times\nkey code {key}\nend repeat")
        return "Brightness adjusted."
    new = max(0, min(100, round(cur * 100) + (step if a["direction"] == "up" else -step)))
    return await _set_brightness(ctx, {"level": new})


async def _dark_mode(ctx: ToolContext, a: dict[str, Any]) -> str:
    mode = a["mode"]
    value = {"on": "true", "off": "false", "toggle": "not dark mode"}[mode]
    await ctx.osa(f'tell application "System Events" to tell appearance preferences to set dark mode to {value}')
    return {"on": "Dark mode on.", "off": "Light mode on.", "toggle": "Appearance toggled."}[mode]


async def _lock_screen(ctx: ToolContext, a: dict[str, Any]) -> str:
    try:
        await ctx.osa('tell application "System Events" to keystroke "q" using {control down, command down}')
    except SkillError:
        res = await ctx.run(["pmset", "displaysleepnow"])
        if not res.ok:
            raise
    return "Locked."


async def _display_sleep(ctx: ToolContext, a: dict[str, Any]) -> str:
    await ctx.run(["pmset", "displaysleepnow"])
    return "Display off."


# ---- power -------------------------------------------------------------------


async def battery_info(ctx: ToolContext) -> dict[str, Any] | None:
    b = psutil.sensors_battery()
    if b is None:
        return None
    return {"percent": round(b.percent), "plugged": bool(b.power_plugged), "secs_left": b.secsleft}


async def _battery(ctx: ToolContext, a: dict[str, Any]) -> str:
    b = await battery_info(ctx)
    if not b:
        return "This Mac has no battery."
    state = "charging" if b["plugged"] else "on battery"
    extra = ""
    if not b["plugged"] and isinstance(b["secs_left"], int) and b["secs_left"] > 0:
        h, m = divmod(b["secs_left"] // 60, 60)
        extra = f", about {h} hours {m} minutes left" if h else f", about {m} minutes left"
    return f"Battery at {b['percent']} percent, {state}{extra}."


async def _sleep_mac(ctx: ToolContext, a: dict[str, Any]) -> str:
    await ctx.run(["pmset", "sleepnow"])
    return "Sleeping."


async def _restart(ctx: ToolContext, a: dict[str, Any]) -> str:
    await ctx.osa('tell application "System Events" to restart')
    return "Restarting."


async def _shutdown(ctx: ToolContext, a: dict[str, Any]) -> str:
    await ctx.osa('tell application "System Events" to shut down')
    return "Shutting down."


async def _empty_trash(ctx: ToolContext, a: dict[str, Any]) -> str:
    await ctx.osa('tell application "Finder" to empty trash')
    return "Trash emptied."


async def _keep_awake(ctx: ToolContext, a: dict[str, Any]) -> str:
    procs: dict[str, Any] = ctx.state.setdefault("caffeinate", {})
    old = procs.pop("proc", None)
    if old and old.returncode is None:
        old.terminate()
    if a.get("minutes") == 0 or a.get("cancel"):
        return "Stay-awake cancelled."
    minutes = max(1, int(a.get("minutes") or 60))
    proc = await asyncio.create_subprocess_exec("caffeinate", "-dims", "-t", str(minutes * 60))
    procs["proc"] = proc
    return f"Keeping the Mac awake for {minutes} minutes."


# ---- wifi / bluetooth ------------------------------------------------------------


async def _wifi_device(ctx: ToolContext) -> str:
    res = await ctx.run(["networksetup", "-listallhardwareports"])
    m = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\s+Device: (\w+)", res.out)
    return m.group(1) if m else "en0"


async def _wifi(ctx: ToolContext, a: dict[str, Any]) -> str:
    dev = await _wifi_device(ctx)
    action = a["action"]
    if action == "status":
        res = await ctx.run(["networksetup", "-getairportpower", dev])
        on = "On" in res.out
        net = await ctx.run(["networksetup", "-getairportnetwork", dev])
        name = net.out.split(":", 1)[1].strip() if ":" in net.out and "not associated" not in net.out.lower() else ""
        return f"Wi-Fi is {'on' if on else 'off'}" + (f", connected to {name}." if name else ".")
    res = await ctx.run(["networksetup", "-setairportpower", dev, "on" if action == "on" else "off"])
    if not res.ok:
        raise SkillError("I couldn't change Wi-Fi, sir.")
    return f"Wi-Fi {action}."


def _need_blueutil(res_err: str) -> SkillError:
    if "not found" in res_err:
        return SkillError("I need blueutil for Bluetooth, sir. Run: brew install blueutil.")
    if "access to bluetooth" in res_err.lower() or "abort signal" in res_err.lower():
        return SkillError("I need Bluetooth permission, sir. Allow it in System Settings, Privacy and Security, Bluetooth.")
    return SkillError("Bluetooth didn't respond, sir.")


async def _bluetooth(ctx: ToolContext, a: dict[str, Any]) -> str:
    action = a["action"]
    if action == "status":
        res = await ctx.run(["blueutil", "--power"])
        if not res.ok:
            raise _need_blueutil(res.err)
        return f"Bluetooth is {'on' if res.out.strip() == '1' else 'off'}."
    res = await ctx.run(["blueutil", "--power", "1" if action == "on" else "0"])
    if not res.ok:
        raise _need_blueutil(res.err)
    return f"Bluetooth {action}."


async def _bt_connect(ctx: ToolContext, a: dict[str, Any]) -> str:
    res = await ctx.run(["blueutil", "--paired", "--format", "json"])
    if not res.ok:
        raise _need_blueutil(res.err)
    devices = json.loads(res.out or "[]")
    want = a.get("device") or ctx.cfg.devices.airpods or "airpods"
    names = {d["name"]: d for d in devices if d.get("name")}
    best = process.extractOne(want, list(names), scorer=fuzz.WRatio) if names else None
    if not best or best[1] < 60:
        raise SkillError(f"I don't see a paired device called {want}, sir.")
    dev = names[best[0]]
    if dev.get("connected") and not a.get("disconnect"):
        return f"{best[0]} is already connected, sir."  # reconnecting would drop the audio for no reason
    verb = "--disconnect" if a.get("disconnect") else "--connect"
    r = await ctx.run(["blueutil", verb, dev["address"]], timeout=20)
    if not r.ok:
        raise SkillError(f"I couldn't {'disconnect' if a.get('disconnect') else 'connect'} {best[0]}, sir.")
    return f"{'Disconnected' if a.get('disconnect') else 'Connected'} {best[0]}."


# ---- sysinfo ------------------------------------------------------------------------


async def _system_status(ctx: ToolContext, a: dict[str, Any]) -> str:
    cpu = psutil.cpu_percent(interval=0.3)
    vm = psutil.virtual_memory()
    disk = shutil.disk_usage("/")
    boot = datetime.fromtimestamp(psutil.boot_time())
    up = datetime.now() - boot
    parts = [
        f"CPU at {cpu:.0f} percent",
        f"memory {vm.percent:.0f} percent used",
        f"{disk.free / 1e9:.0f} gigabytes free on disk",
        f"up {up.days} days {up.seconds // 3600} hours",
    ]
    b = await battery_info(ctx)
    if b:
        parts.append(f"battery {b['percent']} percent{' charging' if b['plugged'] else ''}")
    return ", ".join(parts) + "."


async def _top_processes(ctx: ToolContext, a: dict[str, Any]) -> str:
    n = int(a.get("count") or 5)
    procs = []
    for p in psutil.process_iter(["name", "cpu_percent", "memory_percent"]):
        procs.append(p.info)
    await asyncio.sleep(0.4)
    procs = []
    for p in psutil.process_iter(["name", "cpu_percent", "memory_percent"]):
        procs.append(p.info)
    key = "memory_percent" if a.get("by") == "memory" else "cpu_percent"
    procs.sort(key=lambda x: x.get(key) or 0, reverse=True)
    return "\n".join(f"{p['name']}: cpu {p['cpu_percent']:.0f}%, mem {p['memory_percent']:.1f}%" for p in procs[:n])


async def _network_info(ctx: ToolContext, a: dict[str, Any]) -> str:
    dev = await _wifi_device(ctx)
    ip = await ctx.run(["ipconfig", "getifaddr", dev])
    wifi = await _wifi(ctx, {"action": "status"})
    return f"{wifi} Local address {ip.out or 'unavailable'}."


async def _time_now(ctx: ToolContext, a: dict[str, Any]) -> str:
    now = ctx.clock()
    return f"It is {spoken_time(now.hour, now.minute)} on {now.strftime('%A, %d %B %Y')}."


# ---- clipboard -------------------------------------------------------------------------


async def _clip_read(ctx: ToolContext, a: dict[str, Any]) -> str:
    res = await ctx.run(["pbpaste"])
    text = res.out
    if not text.strip():
        return "The clipboard has no text in it. It may hold an image or a file."
    more = f"\n(only the first 4000 of {len(text)} characters)" if len(text) > 4000 else ""
    return text[:4000] + more


async def _clip_write(ctx: ToolContext, a: dict[str, Any]) -> str:
    res = await ctx.run(["pbcopy"], input=a["text"])
    if not res.ok:
        raise SkillError("I couldn't write to the clipboard, sir.")
    return "Copied to the clipboard."


def _wifi_decision(ctx: ToolContext, a: dict[str, Any]) -> Decision:
    if a.get("action") == "off":
        return Decision.confirm("Turn Wi-Fi off? I'll lose my connection.")
    return Decision.allow()


# ---- registry ------------------------------------------------------------------------------


def register(reg: ToolRegistry) -> None:
    lvl = {"level": I("0-100")}
    reg.register(Tool("set_brightness", "Set screen brightness 0-100.", obj(lvl, ["level"]), _set_brightness, category="display"))
    reg.register(Tool(
        "change_brightness", "Raise or lower screen brightness.",
        obj({"direction": E("up or down", ["up", "down"]), "amount": I("Percent, default 15")}, ["direction"]),
        _change_brightness, category="display"))
    reg.register(Tool("dark_mode", "Switch dark/light appearance.", obj({"mode": E("on, off or toggle", ["on", "off", "toggle"])}, ["mode"]), _dark_mode, category="display"))
    reg.register(Tool("lock_screen", "Lock the screen.", obj(), _lock_screen, category="display"))
    reg.register(Tool("display_off", "Turn the display off.", obj(), _display_sleep, category="display"))

    reg.register(Tool("battery_status", "Battery level and charging state.", obj(), _battery, category="power"))
    reg.register(Tool("sleep_mac", "Put the Mac to sleep.", obj(), _sleep_mac, risk=Risk.CONFIRM, confirm_prompt=lambda a: "Put the Mac to sleep?", category="power"))
    reg.register(Tool("restart_mac", "Restart the Mac.", obj(), _restart, risk=Risk.CONFIRM, confirm_prompt=lambda a: "Restart the Mac? Open work may be lost.", category="power"))
    reg.register(Tool("shutdown_mac", "Shut the Mac down.", obj(), _shutdown, risk=Risk.CONFIRM, confirm_prompt=lambda a: "Shut the Mac down?", category="power"))
    reg.register(Tool("empty_trash", "Empty the Trash permanently.", obj(), _empty_trash, risk=Risk.CONFIRM, confirm_prompt=lambda a: "Empty the Trash permanently?", category="power"))
    reg.register(Tool("keep_awake", "Prevent the Mac from sleeping for N minutes (0 cancels).", obj({"minutes": I("Minutes; 0 cancels")}), _keep_awake, category="power"))

    reg.register(Tool(
        "wifi", "Check, enable or disable Wi-Fi.", obj({"action": E("status, on or off", ["status", "on", "off"])}, ["action"]),
        _wifi, category="connectivity",
        classify=_wifi_decision))
    reg.register(Tool("bluetooth", "Check, enable or disable Bluetooth.", obj({"action": E("status, on or off", ["status", "on", "off"])}, ["action"]), _bluetooth, category="connectivity"))
    reg.register(Tool(
        "bluetooth_device", "Connect or disconnect a paired Bluetooth device such as AirPods.",
        obj({"device": S("Device name, default the configured AirPods"), "disconnect": B("true to disconnect")}), _bt_connect, category="connectivity"))

    reg.register(Tool("system_status", "CPU, memory, disk, uptime and battery summary.", obj(), _system_status, category="sysinfo"))
    reg.register(Tool(
        "top_processes", "Processes using the most CPU or memory.",
        obj({"by": E("cpu or memory", ["cpu", "memory"]), "count": I("How many")}), _top_processes, category="sysinfo"))
    reg.register(Tool("network_info", "Wi-Fi network and local IP.", obj(), _network_info, category="sysinfo"))
    reg.register(Tool("current_time", "The current time and date.", obj(), _time_now, category="sysinfo"))

    reg.register(Tool("clipboard_read", "Read the clipboard text.", obj(), _clip_read, category="clipboard", untrusted_output=True))
    reg.register(Tool("clipboard_write", "Put text on the clipboard.", obj({"text": S("Text to copy")}, ["text"]), _clip_write, category="clipboard"))
    _ = (N, time)
