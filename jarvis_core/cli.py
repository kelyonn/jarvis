"""``jarvisd`` command line interface."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys

from jarvis_core import __version__, paths
from jarvis_core.config import load_config


def _cmd_text(args: argparse.Namespace) -> int:
    from jarvis_core.logging_setup import setup_logging

    setup_logging("text", console=False)
    cfg = load_config()
    from jarvis_core.text_mode import run_text_mode

    return asyncio.run(run_text_mode(cfg, once=args.once))


def _cmd_config(args: argparse.Namespace) -> int:
    if args.action == "validate":
        try:
            load_config()
        except Exception as e:
            print(f"invalid config: {e}", file=sys.stderr)
            return 1
        print(f"ok: {paths.config_file()}")
    elif args.action == "show":
        print(json.dumps(load_config().model_dump(), indent=2))
    elif args.action == "path":
        print(paths.config_file())
    elif args.action == "edit":
        import os
        import subprocess

        from jarvis_core.config import save_config

        p = paths.config_file()
        if not p.exists():
            save_config(load_config())
        editor = os.environ.get("EDITOR")
        subprocess.run([editor, str(p)] if editor else ["open", "-t", str(p)])
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    from jarvis_core.setup_wizard import run_setup

    return asyncio.run(run_setup(non_interactive=args.non_interactive))


def _cmd_set_key(args: argparse.Namespace) -> int:
    from jarvis_core import secrets

    value = args.value or getpass.getpass("NVIDIA NIM API key (nvapi-...): ").strip()
    if not value:
        print("no key given", file=sys.stderr)
        return 1
    secrets.set_secret("NIM_API_KEY", value)
    print("stored in Keychain (service 'jarvis', account 'NIM_API_KEY')")
    return 0


def _cmd_spotify_setup(args: argparse.Namespace) -> int:
    from jarvis_core import secrets

    print("1. Open https://developer.spotify.com/dashboard and create an app (any name).")
    print("   Redirect URI: http://127.0.0.1:8888/callback   API: Web API")
    print("2. Copy its Client ID and Client secret here.")
    cid = input("Client ID: ").strip()
    sec = getpass.getpass("Client secret: ").strip()
    if not cid or not sec:
        print("nothing stored", file=sys.stderr)
        return 1
    secrets.set_secret("SPOTIFY_CLIENT_ID", cid)
    secrets.set_secret("SPOTIFY_CLIENT_SECRET", sec)
    print("stored in Keychain. Try: jarvisd text, then 'play Mr Brightside by The Killers on Spotify'")
    return 0


def _cmd_spotify_login(args: argparse.Namespace) -> int:
    """Sign in to Spotify once so Jarvis can see your playlists (the consent page opens in your browser)."""
    import http.server
    import secrets as pysecrets
    import subprocess
    import threading
    import urllib.parse

    from jarvis_core import secrets
    from jarvis_core.skills import spotify

    cid = secrets.get_secret("SPOTIFY_CLIENT_ID")
    if not cid or not secrets.get_secret("SPOTIFY_CLIENT_SECRET"):
        print("run `jarvisd spotify-setup` first", file=sys.stderr)
        return 1
    state = pysecrets.token_urlsafe(12)
    got: dict[str, str] = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state and "code" in q:
                got["code"] = q["code"][0]
                body = b"<h2>Jarvis is connected to Spotify. You can close this tab.</h2>"
            else:
                got["error"] = q.get("error", ["bad request"])[0]
                body = b"<h2>Spotify sign-in was not completed.</h2>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 8888), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = spotify.auth_url(cid, state)
    print("Opening Spotify's consent page. Click Agree to let Jarvis read your playlists.")
    print(url)
    if not args.no_open:
        subprocess.run(["open", url])
    import time

    end = time.time() + 240
    while time.time() < end and not got:
        time.sleep(0.3)
    srv.shutdown()
    if "code" not in got:
        print(f"not connected: {got.get('error', 'timed out')}", file=sys.stderr)
        return 1
    asyncio.run(spotify.exchange_code(got["code"]))
    print("connected. Try: jarvisd text, then 'play my <playlist name> playlist'")
    return 0


def _cmd_voice_id(args: argparse.Namespace) -> int:
    """Voice recognition: only your voice is acted on once you have enrolled."""
    import json

    from jarvis_core.voice import speaker_id

    if args.action == "status":
        f = speaker_id.voiceprint_file()
        print("enrolled: only your voice is acted on" if f.exists() else "not enrolled (run: jarvisd voice-id enroll)")
        return 0
    if args.action == "forget":
        speaker_id.voiceprint_file().unlink(missing_ok=True)
    (paths.run_dir() / "voice_id.json").write_text(json.dumps({"action": args.action}))
    if args.action == "enroll":
        print("Jarvis will now ask you to say six phrases. Make sure it's running and you're where you usually talk.")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from jarvis_core.doctor import run_doctor

    return asyncio.run(run_doctor(fix=args.fix))


def _cmd_logs(args: argparse.Namespace) -> int:
    import subprocess

    if args.audit:
        target = paths.audit_log()
    elif args.transcripts:
        target = paths.transcripts_dir()
        subprocess.run(["ls", "-lt", str(target)])
        return 0
    else:
        target = paths.logs_dir() / "daemon.log"
    if not target.exists():
        print(f"(nothing at {target})")
        return 0
    cmd = ["tail", "-n", "100"] + (["-f"] if args.follow else []) + [str(target)]
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from jarvis_core.bootstrap import run_daemon

    return run_daemon(standalone_audio=args.standalone, ipc_file=args.ipc_file)


def _cmd_say(args: argparse.Namespace) -> int:
    from jarvis_core.voice.say import say_text

    return asyncio.run(say_text(" ".join(args.text)))


def _cmd_brief(args: argparse.Namespace) -> int:
    from jarvis_core.briefing.cli import run_brief

    mode = "full" if args.full else "delta" if args.delta else "auto"
    return asyncio.run(run_brief(mode=mode, speak=not args.print))


def _cmd_alerts(args: argparse.Namespace) -> int:
    from jarvis_core.alerts.cli import run_alerts_check

    return asyncio.run(run_alerts_check())


def _cmd_google(args: argparse.Namespace) -> int:
    from jarvis_core.adapters import services as oj

    session = oj.GoogleSession("gmail")
    if args.reauth:
        revoked = oj.reset_google("gmail")
        print("Old Google access revoked." if revoked else "Old tokens removed (Google couldn't confirm the revoke; "
              "you can also remove Jarvis at https://myaccount.google.com/permissions).")
        if not oj.has_google_client("gmail"):
            print("No OAuth client saved; run `jarvisd setup` to enter one.")
            return 1
        oj.run_google_oauth("gmail")
        session = oj.GoogleSession("gmail")
    if not session.connected():
        print("Not signed in. Run `jarvisd google --reauth` (or `jarvisd setup`).")
        return 1
    if not session.valid():
        print("The saved sign-in was revoked or has expired. Run `jarvisd google --reauth`.")
        return 1
    scopes = [x.rsplit("/", 1)[-1] for x in session.scopes().split()] or ["(unknown)"]
    extra = [x.rsplit("/", 1)[-1] for x in oj.google_extra_scopes("gmail")]
    print("Signed in. Granted: " + ", ".join(scopes))
    print("Broader than Jarvis needs: " + ", ".join(extra) + "  ->  run `jarvisd google --reauth`" if extra
          else "Permissions: mail (read and drafts, never send), Calendar, and read-only Drive.")
    return 0


def _cmd_vip(args: argparse.Namespace) -> int:
    from jarvis_core.comms.vip import vip_cli

    return vip_cli(args.action, args.name)


def _cmd_forget(args: argparse.Namespace) -> int:
    from jarvis_core.memory.cli import forget_cli

    return forget_cli(all_=args.all, person=args.person)


def _cmd_memory(args: argparse.Namespace) -> int:
    from jarvis_core.memory.cli import list_cli

    return list_cli()


def _cmd_shortcuts(args: argparse.Namespace) -> int:
    from jarvis_core.adapters import shortcuts as shortcuts_kit
    from jarvis_core.skills.base import ToolContext

    ctx = ToolContext(cfg=load_config())

    async def go() -> int:
        have = await shortcuts_kit.installed(ctx)
        names = args.names or shortcuts_kit.DEFAULT_SET
        if args.action == "list":
            for n in names:
                print(f"{'✓' if n in have else '✗'} {n}")
            return 0
        for n in names:
            if n in have and not args.force:
                print(f"✓ {n} (already installed)")
                continue
            print(await shortcuts_kit.install(ctx, n))
        return 0

    return asyncio.run(go())


def _cmd_routines(args: argparse.Namespace) -> int:
    from jarvis_core.skills.routines import routines_cli

    return routines_cli(args.action, args.name)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jarvisd", description="Jarvis personal voice assistant")
    p.add_argument("--version", action="version", version=f"jarvisd {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("run", help="start the daemon (normally started by JarvisShell)")
    s.add_argument("--standalone", action="store_true", help="use the Python mic/speaker (no Shell)")
    s.add_argument("--ipc-file", default=None)
    s.set_defaults(fn=_cmd_run)

    s = sub.add_parser("text", help="chat with Jarvis in the terminal")
    s.add_argument("--once", metavar="TEXT", default=None, help="send one message and exit")
    s.set_defaults(fn=_cmd_text)

    s = sub.add_parser("say", help="speak text in the Jarvis voice")
    s.add_argument("text", nargs="+")
    s.set_defaults(fn=_cmd_say)

    s = sub.add_parser("brief", help="generate a briefing now")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--full", action="store_true")
    g.add_argument("--delta", action="store_true")
    s.add_argument("--print", action="store_true", help="print instead of speaking")
    s.set_defaults(fn=_cmd_brief)

    s = sub.add_parser("alerts", help="dry-run the alert rules and show Focus state")
    s.set_defaults(fn=_cmd_alerts)

    s = sub.add_parser("doctor", help="check permissions, models, and connectivity")
    s.add_argument("--fix", action="store_true")
    s.set_defaults(fn=_cmd_doctor)

    s = sub.add_parser("setup", help="first-run setup wizard")
    s.add_argument("--non-interactive", action="store_true")
    s.set_defaults(fn=_cmd_setup)

    s = sub.add_parser("set-key", help="store the NVIDIA NIM API key in the Keychain")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=_cmd_set_key)

    s = sub.add_parser("spotify-setup", help="store Spotify API credentials so Jarvis can play songs directly")
    s.set_defaults(fn=_cmd_spotify_setup)

    s = sub.add_parser("voice-id", help="voice recognition: enroll, forget or show whether only your voice is acted on")
    s.add_argument("action", choices=["enroll", "forget", "status"])
    s.set_defaults(fn=_cmd_voice_id)

    s = sub.add_parser("spotify-login", help="sign in to Spotify so Jarvis can play your own playlists")
    s.add_argument("--no-open", action="store_true", help="print the URL instead of opening the browser")
    s.set_defaults(fn=_cmd_spotify_login)

    s = sub.add_parser("logs", help="show logs")
    s.add_argument("--audit", action="store_true")
    s.add_argument("--transcripts", action="store_true")
    s.add_argument("-f", "--follow", action="store_true")
    s.set_defaults(fn=_cmd_logs)

    s = sub.add_parser("config", help="show/validate/edit the config")
    s.add_argument("action", choices=["show", "validate", "edit", "path"], default="show", nargs="?")
    s.set_defaults(fn=_cmd_config)

    s = sub.add_parser("google", help="show or redo the Gmail sign-in (--reauth asks for Gmail access only)")
    s.add_argument("--reauth", action="store_true", help="revoke the old grant and sign in again with minimal permissions")
    s.set_defaults(fn=_cmd_google)

    s = sub.add_parser("vip", help="manage VIP contacts")
    s.add_argument("action", choices=["add", "remove", "list"])
    s.add_argument("name", nargs="?")
    s.set_defaults(fn=_cmd_vip)

    s = sub.add_parser("forget", help="delete memory")
    s.add_argument("--all", action="store_true")
    s.add_argument("--person", default=None)
    s.set_defaults(fn=_cmd_forget)

    s = sub.add_parser("memory", help="list what Jarvis remembers")
    s.set_defaults(fn=_cmd_memory)

    s = sub.add_parser("shortcuts", help="list or install the Shortcuts Jarvis uses (Focus, Night Shift, True Tone, Stage Manager)")
    s.add_argument("action", choices=["list", "install"])
    s.add_argument("names", nargs="*", help="shortcut names; default is the standard set")
    s.add_argument("--force", action="store_true", help="re-open even if already installed")
    s.set_defaults(fn=_cmd_shortcuts)

    s = sub.add_parser("routines", help="list/delete routines")
    s.add_argument("action", choices=["list", "delete"])
    s.add_argument("name", nargs="?")
    s.set_defaults(fn=_cmd_routines)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        code = args.fn(args)
    except KeyboardInterrupt:
        code = 130
    sys.exit(code or 0)


if __name__ == "__main__":
    main()
