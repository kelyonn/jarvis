from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from jarvis_core.adapters import apple_data, google


@pytest.fixture
def jhome(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jh"))
    monkeypatch.setattr(google, "_LEGACY_DIR", tmp_path / "legacy")
    return tmp_path


def test_google_tokens_are_private_and_legacy_file_is_migrated(jhome):
    legacy = jhome / "legacy"
    legacy.mkdir()
    (legacy / "gmail.json").write_text(json.dumps({"access_token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s"}))
    s = google.GoogleSession("gmail")
    assert s.connected()
    assert oct(s._path.stat().st_mode & 0o777) == "0o600"
    assert google.has_google_client("gmail")


def test_oauth_uses_pkce_state_and_gmail_only_scopes(jhome, monkeypatch):
    google.save_google_client("cid", "secret", "gmail")
    seen: dict = {}

    def fake_open(url: str) -> bool:
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        seen.update(q)

        def hit() -> None:
            httpx.get(f"{google.REDIRECT_URI}?code=abc&state={q['state']}", timeout=5)

        threading.Timer(0.2, hit).start()
        return True

    posted: dict = {}

    def fake_post(url, **kw):
        posted.update(kw["data"])
        return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "scope": q_scope()},
                              request=httpx.Request("POST", url))

    def q_scope() -> str:
        return seen["scope"]

    monkeypatch.setattr(google.webbrowser, "open", fake_open)
    monkeypatch.setattr(google.httpx, "post", fake_post)
    google.run_google_oauth("gmail", timeout_s=20)
    assert seen["scope"].split() == google.GOOGLE_SCOPES
    assert not any("gmail.send" in x or x.endswith("drive") for x in google.GOOGLE_SCOPES)
    assert not any(w in seen["scope"] for w in ("contacts", "tasks", "gmail.send", "auth/drive ", "mail.google.com"))
    assert seen["scope"].endswith("drive.readonly")
    assert seen["code_challenge_method"] == "S256" and seen["code_challenge"]
    assert posted["code"] == "abc" and posted["code_verifier"]
    assert google.GoogleSession("gmail").connected()


def test_oauth_rejects_a_callback_with_the_wrong_state(jhome, monkeypatch):
    google.save_google_client("cid", "secret", "gmail")

    def fake_open(url: str) -> bool:
        threading.Timer(0.2, lambda: httpx.get(f"{google.REDIRECT_URI}?code=evil&state=nope", timeout=5)).start()
        return True

    monkeypatch.setattr(google.webbrowser, "open", fake_open)
    with pytest.raises(google.GoogleAuthError):
        google.run_google_oauth("gmail", timeout_s=10)


def _apple(dt: datetime) -> float:
    return apple_data._to_apple(dt)


def test_calendar_reader_returns_events_and_occurrences(tmp_path):
    db = tmp_path / "Calendar.sqlitedb"
    c = sqlite3.connect(db)
    c.executescript("""
    CREATE TABLE Calendar (ROWID INTEGER PRIMARY KEY, title TEXT);
    CREATE TABLE Location (ROWID INTEGER PRIMARY KEY, title TEXT, address TEXT);
    CREATE TABLE CalendarItem (ROWID INTEGER PRIMARY KEY, summary TEXT, start_date REAL, end_date REAL, all_day INTEGER,
        start_tz TEXT, location_id INTEGER, has_attendees INTEGER, UUID TEXT, hidden INTEGER, calendar_id INTEGER);
    CREATE TABLE OccurrenceCache (event_id INTEGER, calendar_id INTEGER, occurrence_start_date REAL, occurrence_end_date REAL);
    INSERT INTO Calendar VALUES (1, 'Work');
    INSERT INTO Location VALUES (1, 'HQ', '1 Main St');
    """)
    now = datetime.now(timezone.utc)
    one = now + timedelta(hours=2)
    weekly_first = now - timedelta(days=7)
    weekly_next = now + timedelta(hours=5)
    c.execute("INSERT INTO CalendarItem VALUES (1,'Standup',?,?,0,'UTC',1,0,'u1',0,1)", (_apple(one), _apple(one + timedelta(hours=1))))
    c.execute("INSERT INTO CalendarItem VALUES (2,'Weekly sync',?,?,0,'UTC',NULL,1,'u2',0,1)",
              (_apple(weekly_first), _apple(weekly_first + timedelta(hours=1))))
    c.execute("INSERT INTO OccurrenceCache VALUES (2,1,?,?)", (_apple(weekly_next), _apple(weekly_next + timedelta(hours=1))))
    c.commit()
    c.close()
    items = apple_data.calendar_events(now, now + timedelta(days=1), db)
    assert [i.title for i in items] == ["Standup", "Weekly sync"]
    assert items[0].meta["location"] == "HQ, 1 Main St" and items[1].meta["has_attendees"] is True


def test_contacts_reader_collects_phones_and_emails(tmp_path, monkeypatch):
    book = tmp_path / "AddressBook"
    book.mkdir()
    c = sqlite3.connect(book / "AddressBook-v22.abcddb")
    c.executescript("""
    CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZMIDDLENAME TEXT, ZLASTNAME TEXT, ZORGANIZATION TEXT,
        ZNICKNAME TEXT, ZUNIQUEID TEXT);
    CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT, ZLABEL TEXT);
    CREATE TABLE ZABCDEMAILADDRESS (ZOWNER INTEGER, ZADDRESS TEXT, ZLABEL TEXT);
    INSERT INTO ZABCDRECORD VALUES (1, 'Arjun', NULL, 'Rao', NULL, NULL, 'x1');
    INSERT INTO ZABCDPHONENUMBER VALUES (1, '+91 98765 43210', '_$!<Mobile>!$_');
    INSERT INTO ZABCDEMAILADDRESS VALUES (1, 'arjun@example.com', NULL);
    """)
    c.commit()
    c.close()
    monkeypatch.setattr(apple_data, "ADDRESSBOOK_DIR", book)
    recs = apple_data.apple_contacts()
    assert recs[0]["name"] == "Arjun Rao"
    assert "+91 98765 43210" in recs[0]["content"] and "arjun@example.com" in recs[0]["content"]
