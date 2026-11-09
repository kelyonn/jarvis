"""Several signed-in Google accounts: the adapter (list/add/remove/email), build_comms wiring them into MultiGmail,
and the `jarvisd google --add/--remove/--list` CLI commands."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis_core import paths
from jarvis_core.adapters import google as gg
from jarvis_core.briefing.store import Store
from jarvis_core.comms.gmail import GmailClient, MultiGmail
from jarvis_core.comms.setup import build_comms
from jarvis_core.config import Config
from jarvis_core.skills.base import ToolContext

pytestmark = pytest.mark.anyio


def write_account(connector: str, *, refresh_token="rt", client_id="cid", client_secret="sec") -> None:
    tokens = {"client_id": client_id, "client_secret": client_secret}
    if refresh_token:
        tokens["refresh_token"] = refresh_token
    gg.save_tokens(paths.sub("google") / f"{connector}.json", tokens)


# --------------------------------------------------------------------------------------------- list_accounts


def test_list_accounts_empty_when_none_signed_in():
    assert gg.list_accounts() == []


def test_list_accounts_puts_main_first_and_skips_token_less_files():
    write_account("gmail-work")
    write_account("gmail")
    write_account("gmail-college")
    (paths.sub("google") / "gmail-empty.json").write_text("{}", encoding="utf-8")  # no refresh/access token: not signed in
    assert gg.list_accounts() == ["gmail", "gmail-college", "gmail-work"]


def test_list_accounts_ignores_unrelated_files():
    write_account("gmail-work")
    (paths.sub("google") / "gmail_backup.json").write_text('{"refresh_token": "x"}', encoding="utf-8")
    assert gg.list_accounts() == ["gmail-work"]


# --------------------------------------------------------------------------------------------- account_label


def test_account_label():
    assert gg.account_label("gmail") == "main"
    assert gg.account_label("gmail-work") == "work"
    assert gg.account_label("gmail-college-2") == "college-2"


# --------------------------------------------------------------------------------------------- account_email


def test_account_email_reads_profile(monkeypatch):
    class FakeResp:
        status_code = 200

        def json(self):
            return {"emailAddress": "work@example.com"}

    class FakeSession:
        def __init__(self, connector):
            self.connector = connector

        def request(self, method, url):
            assert method == "GET" and "profile" in url
            return FakeResp()

    monkeypatch.setattr(gg, "GoogleSession", FakeSession)
    assert gg.account_email("gmail-work") == "work@example.com"


def test_account_email_blank_when_offline_or_unauthorized(monkeypatch):
    class Denied:
        status_code = 401

        def json(self):
            return {}

    class FakeSession:
        def __init__(self, connector):
            pass

        def request(self, method, url):
            return Denied()

    monkeypatch.setattr(gg, "GoogleSession", FakeSession)
    assert gg.account_email("gmail-work") == ""

    class Raises:
        def __init__(self, connector):
            pass

        def request(self, method, url):
            raise RuntimeError("offline")

    monkeypatch.setattr(gg, "GoogleSession", Raises)
    assert gg.account_email("gmail-work") == ""


# --------------------------------------------------------------------------------------------- add_account


def test_add_account_rejects_bad_labels():
    write_account("gmail")
    with pytest.raises(gg.GoogleAuthError, match="short name"):
        gg.add_account("")
    with pytest.raises(gg.GoogleAuthError, match="short name"):
        gg.add_account("   ")
    with pytest.raises(gg.GoogleAuthError, match="short name"):
        gg.add_account("Gmail")  # the primary account's own name


def test_add_account_requires_primary_signed_in_first(monkeypatch):
    monkeypatch.delenv("JARVIS_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("JARVIS_GOOGLE_CLIENT_SECRET", raising=False)
    with pytest.raises(gg.GoogleAuthError, match="Sign in the main account first"):
        gg.add_account("work")


def test_add_account_reuses_primary_client_and_runs_oauth(monkeypatch):
    write_account("gmail", client_id="shared-cid", client_secret="shared-sec")
    seen = {}

    def fake_oauth(connector, scopes=None, *, open_browser=True, timeout_s=180.0):
        seen["connector"] = connector
        seen["open_browser"] = open_browser
        # run_google_oauth would normally save the new account's tokens itself
        write_account(connector, client_id="shared-cid", client_secret="shared-sec")

    monkeypatch.setattr(gg, "run_google_oauth", fake_oauth)
    connector = gg.add_account("Work Account!", open_browser=False)
    assert connector == "gmail-work-account"
    assert seen == {"connector": "gmail-work-account", "open_browser": False}
    saved = gg.load_tokens(paths.sub("google") / "gmail-work-account.json")
    assert saved["client_id"] == "shared-cid" and saved["client_secret"] == "shared-sec"


def test_add_account_label_is_slugified_and_truncated(monkeypatch):
    write_account("gmail")
    monkeypatch.setattr(gg, "run_google_oauth", lambda *a, **kw: None)
    connector = gg.add_account("  My  Work!! Account  " + "x" * 40)
    assert connector.startswith("gmail-my-work-account-x")
    assert len(connector.removeprefix("gmail-")) <= 24


# --------------------------------------------------------------------------------------------- remove_account


def test_remove_account_deletes_the_token_file(monkeypatch):
    write_account("gmail-work")
    monkeypatch.setattr(gg, "reset_google", lambda connector: True)
    assert gg.remove_account("work") is True
    assert not (paths.sub("google") / "gmail-work.json").exists()


def test_remove_account_survives_missing_file_and_reports_unconfirmed_revoke(monkeypatch):
    monkeypatch.setattr(gg, "reset_google", lambda connector: False)
    assert gg.remove_account("never-added") is False  # no file to delete: unlink's OSError is swallowed


def test_remove_account_rejects_the_primary_account():
    with pytest.raises(gg.GoogleAuthError, match="short name"):
        gg.remove_account("gmail")


# --------------------------------------------------------------------------------------------- build_comms wiring


def make_tool_ctx() -> ToolContext:
    return ToolContext(cfg=Config())


def test_build_comms_single_account_uses_plain_gmail_client(monkeypatch, tmp_path):
    write_account("gmail")
    monkeypatch.setattr(gg, "GoogleSession", lambda connector: SimpleNamespace(connector=connector))
    store = Store(tmp_path / "state.db")
    hub = build_comms(Config(), make_tool_ctx(), store)
    assert type(hub.readers["gmail"]) is GmailClient
    store.close()


def test_build_comms_several_accounts_wires_a_multi_gmail(monkeypatch, tmp_path):
    write_account("gmail")
    write_account("gmail-work")
    monkeypatch.setattr(gg, "GoogleSession", lambda connector: SimpleNamespace(connector=connector))
    store = Store(tmp_path / "state.db")
    hub = build_comms(Config(), make_tool_ctx(), store)
    assert isinstance(hub.readers["gmail"], MultiGmail)
    assert set(hub.readers["gmail"].clients) == {"main", "work"}
    assert hub.readers["gmail"].primary == "main"  # the primary account is offered first for brand-new emails
    store.close()


def test_build_comms_no_accounts_falls_back_to_a_bare_gmail_connector(monkeypatch, tmp_path):
    # Nobody has signed in yet: list_accounts() is empty, so build_comms still tries the default "gmail" connector
    # (and GmailClient.check() will later report it unavailable - that is exercised elsewhere).
    monkeypatch.setattr(gg, "GoogleSession", lambda connector: SimpleNamespace(connector=connector))
    store = Store(tmp_path / "state.db")
    hub = build_comms(Config(), make_tool_ctx(), store)
    assert type(hub.readers["gmail"]) is GmailClient
    store.close()


# --------------------------------------------------------------------------------------------- CLI: jarvisd google --add/--remove/--list


def ns(**kw):
    return SimpleNamespace(**{"add": None, "remove": None, "list": False, "reauth": False, **kw})


def test_cli_google_add_success(monkeypatch, capsys):
    from jarvis_core.cli import _cmd_google

    monkeypatch.setattr(gg, "add_account", lambda label, **kw: "gmail-work")
    monkeypatch.setattr(gg, "account_label", lambda c: "work")
    monkeypatch.setattr(gg, "account_email", lambda c: "work@example.com")
    assert _cmd_google(ns(add="work")) == 0
    out = capsys.readouterr().out
    assert "work@example.com" in out and "Restart Jarvis" in out


def test_cli_google_add_reports_the_auth_error(monkeypatch, capsys):
    from jarvis_core.cli import _cmd_google

    def boom(label, **kw):
        raise gg.GoogleAuthError("Give the account a short name such as work or college.")

    monkeypatch.setattr(gg, "add_account", boom)
    assert _cmd_google(ns(add="")) == 1
    assert "short name" in capsys.readouterr().out


def test_cli_google_remove_success_and_unconfirmed(monkeypatch, capsys):
    from jarvis_core.cli import _cmd_google

    monkeypatch.setattr(gg, "remove_account", lambda label: True)
    assert _cmd_google(ns(remove="work")) == 0
    assert capsys.readouterr().out.strip() == "Removed."

    monkeypatch.setattr(gg, "remove_account", lambda label: False)
    assert _cmd_google(ns(remove="work")) == 0
    assert "couldn't confirm" in capsys.readouterr().out


def test_cli_google_remove_reports_the_auth_error(monkeypatch, capsys):
    from jarvis_core.cli import _cmd_google

    def boom(label):
        raise gg.GoogleAuthError("nope")

    monkeypatch.setattr(gg, "remove_account", boom)
    assert _cmd_google(ns(remove="gmail")) == 1
    assert "nope" in capsys.readouterr().out


def test_cli_google_list_shows_every_account(monkeypatch, capsys):
    from jarvis_core.cli import _cmd_google

    monkeypatch.setattr(gg, "list_accounts", lambda: ["gmail", "gmail-work"])
    monkeypatch.setattr(gg, "account_label", lambda c: "main" if c == "gmail" else "work")
    monkeypatch.setattr(gg, "account_email", lambda c: "me@example.com" if c == "gmail" else "")
    assert _cmd_google(ns(list=True)) == 0
    out = capsys.readouterr().out
    assert "main" in out and "me@example.com" in out and "work" in out and "offline or expired" in out


def test_cli_google_list_empty(monkeypatch, capsys):
    from jarvis_core.cli import _cmd_google

    monkeypatch.setattr(gg, "list_accounts", lambda: [])
    assert _cmd_google(ns(list=True)) == 1
    assert "No Google accounts" in capsys.readouterr().out
