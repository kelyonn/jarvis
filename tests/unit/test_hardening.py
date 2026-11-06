"""Fixes from the codebase review: single daemon, private data folder, shell policy, typed turns, dev hook expiry."""

import json
import os
import stat
import time

from jarvis_core import bootstrap, paths
from jarvis_core.policy.shell_policy import classify_command
from jarvis_core.state import State, StateMachine


def test_only_one_daemon_can_hold_the_lock():
    first = bootstrap._single_instance()
    assert first is not None
    try:
        assert bootstrap._single_instance() is None
    finally:
        first.close()
    again = bootstrap._single_instance()
    assert again is not None
    again.close()


async def test_stopping_never_deletes_another_daemons_ipc_file():
    from jarvis_core.ipc.server import IPCServer

    srv = IPCServer(on_control=lambda c: None, on_mic=lambda b: None)
    await srv.start()
    paths.ipc_file().write_text(json.dumps({"pid": os.getpid() + 1, "port": 1}))  # a newer daemon took over
    await srv.stop()
    assert paths.ipc_file().exists()
    srv2 = IPCServer(on_control=lambda c: None, on_mic=lambda b: None)
    await srv2.start()
    await srv2.stop()
    assert not paths.ipc_file().exists()


def test_data_folder_is_private():
    h = paths.home()
    os.chmod(h, 0o755)
    h = paths.home()
    assert stat.S_IMODE(h.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.logs_dir().stat().st_mode) == 0o700


def test_read_only_prefixes_cannot_write_files():
    assert classify_command("git log --oneline -5").action == "allow"
    assert classify_command("git log --oneline --output=/Users/x/.zshrc").action == "confirm"
    assert classify_command("ls -la").action == "allow"


def test_typed_requests_can_interrupt_listening_and_follow_up():
    for start in (State.LISTENING, State.FOLLOW_UP):
        sm = StateMachine(strict=True)
        sm.state = start
        sm.go(State.THINKING)  # raises if not allowed


def test_dev_hook_switch_expires(monkeypatch):
    from jarvis_core import devhook

    assert not devhook._dev_mode_on()
    flag = paths.home() / "dev"
    flag.touch()
    assert devhook._dev_mode_on()
    old = time.time() - 13 * 3600
    os.utime(flag, (old, old))
    assert not devhook._dev_mode_on()


async def test_terminal_refuses_protected_working_folder(tmp_path, apps_dir):
    from jarvis_core.skills.registry import build_registry
    from tests.unit.test_skills import make_ctx

    ctx = make_ctx(tmp_path, apps_dir)
    (ctx.home_dir / ".ssh").mkdir()
    tool = build_registry().get("run_command")
    assert tool.decide(ctx, {"command": "ls", "directory": ".ssh"}).action == "block"
    assert tool.decide(ctx, {"command": "ls", "directory": "Documents"}).action == "allow"
