from __future__ import annotations

import base64
import inspect
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from jarvis_core.comms import gmail as gm
from jarvis_core.comms import imessage as im
from jarvis_core.comms import whatsapp_db as wa
from jarvis_core.comms.contacts import ContactBook, normalise_phone
from jarvis_core.comms.hub import Channel, CommsHub
from jarvis_core.comms.vip import VipList
from jarvis_core.config import Config
from jarvis_core.interfaces import Item
from jarvis_core.policy.action_filter import ActionFilter

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def core(dt):  # Core Data seconds
    return dt.timestamp() - wa.CORE_DATA_EPOCH


# ------------------------------------------------------------------------------- WhatsApp


@pytest.fixture
def wa_path(tmp_path):
    p = tmp_path / "ChatStorage.sqlite"
    db = sqlite3.connect(p)
    db.executescript("""
    CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZUNREADCOUNT INTEGER,
        ZLASTMESSAGEDATE REAL, ZARCHIVED INTEGER, ZSESSIONTYPE INTEGER);
    CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZTEXT TEXT, ZMESSAGEDATE REAL,
        ZMESSAGETYPE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER);
    CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
    CREATE TABLE ZWAPROFILEPUSHNAME (ZJID TEXT, ZPUSHNAME TEXT);
    """)
    t = lambda m: core(NOW - timedelta(minutes=m))  # noqa: E731
    db.executemany("INSERT INTO ZWACHATSESSION VALUES (?,?,?,?,?,?,?)", [
        (1, "919876500001@s.whatsapp.net", "Priya", 2, t(5), 0, 0),
        (2, "120363000@g.us", "Family", 1, t(20), 0, 1),
        (3, "919876500003@s.whatsapp.net", "Old Chat", 0, t(500), 0, 0),
        (4, "status@broadcast", "Status", 3, t(1), 0, 0),
        (5, "919876500005@s.whatsapp.net", "Archived Guy", 4, t(2), 1, 0),
    ])
    db.executemany("INSERT INTO ZWAGROUPMEMBER VALUES (?,?)", [(1, "919876500009@s.whatsapp.net")])
    db.execute("INSERT INTO ZWAPROFILEPUSHNAME VALUES ('919876500009@s.whatsapp.net','Mom')")
    db.executemany("INSERT INTO ZWAMESSAGE VALUES (?,?,?,?,?,?,?,?)", [
        (1, 1, 0, "Are you free for dinner Friday?", t(30), 0, "919876500001@s.whatsapp.net", None),
        (2, 1, 1, "Maybe! Let me check", t(28), 0, None, None),
        (3, 1, 0, "Let me know soon", t(10), 0, "919876500001@s.whatsapp.net", None),
        (4, 1, 0, None, t(5), 1, "919876500001@s.whatsapp.net", None),
        (5, 1, 0, "old message", t(3000), 0, "919876500001@s.whatsapp.net", None),
        (6, 2, 0, "Who is bringing cake?", t(20), 0, "120363000@g.us", 1),
        (7, 1, 1, "Sounds good, see you there", t(200), 0, None, None),
    ])
    db.commit()
    db.close()
    return p


def test_whatsapp_helpers():
    assert wa.jid_to_phone("919876543210@s.whatsapp.net") == "+919876543210"
    assert wa.jid_to_phone("120363@g.us") == ""
    assert wa.phone_to_jid("+91 98765-43210") == "919876543210@s.whatsapp.net"


def test_whatsapp_check_and_schema_guard(wa_path, tmp_path):
    assert wa.WhatsAppDB(wa_path).check() == (True, "")
    ok, why = wa.WhatsAppDB(tmp_path / "missing.sqlite").check()
    assert not ok and "wasn't found" in why
    bad = tmp_path / "bad.sqlite"
    c = sqlite3.connect(bad)
    c.execute("CREATE TABLE ZWACHATSESSION (Z_PK INTEGER, ZCONTACTJID TEXT)")
    c.execute("CREATE TABLE ZWAMESSAGE (Z_PK INTEGER)")
    c.commit()
    c.close()
    ok, why = wa.WhatsAppDB(bad).check()
    assert not ok and "schema changed" in why


def test_whatsapp_unread(wa_path):
    db = wa.WhatsAppDB(wa_path)
    items = db.unread()
    by_id = {i.id: i for i in items}
    assert set(by_id) == {"3", "4", "6"}  # 2 unread in Priya's chat, 1 in Family; status/archived/zero-unread skipped
    assert by_id["3"].sender == "Priya" and by_id["3"].meta["phone"] == "+919876500001" and not by_id["3"].is_group
    assert by_id["4"].body == "sent a photo"
    g = by_id["6"]
    assert g.is_group and g.sender == "Mom in Family" and g.thread_id == "120363000@g.us"
    assert [i.id for i in items][0] == "4"  # newest first
    recent = db.unread(since=NOW - timedelta(minutes=15))
    assert {i.id for i in recent} == {"3", "4"}


def test_whatsapp_thread_and_samples(wa_path):
    db = wa.WhatsAppDB(wa_path)
    thread = db.thread("919876500001@s.whatsapp.net", 4)
    assert [m.text for m in thread] == ["Are you free for dinner Friday?", "Maybe! Let me check", "Let me know soon", "[a photo]"]
    assert [m.from_me for m in db.thread("919876500001@s.whatsapp.net", 10)].count(True) == 2
    assert db.sent_samples("919876500001@s.whatsapp.net", 5) == ["Maybe! Let me check", "Sounds good, see you there"]


def test_whatsapp_chats_excludes_noise(wa_path):
    names = [c.name for c in wa.WhatsAppDB(wa_path).chats()]
    assert "Status" not in names and "Archived Guy" not in names and names[0] == "Priya"


# -------------------------------------------------------------------------------- iMessage


def typedstream(text: str, long: bool = False) -> bytes:
    raw = text.encode()
    head = (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92"
            b"\x84\x84\x84\x08NSString\x01\x94\x84\x01+")
    ln = b"\x81" + len(raw).to_bytes(2, "little") if long or len(raw) >= 0x80 else bytes([len(raw)])
    return head + ln + raw + b"\x86\x84\x02iI\x01" + bytes([len(text) % 256]) + b"\x92\x84\x84\x84\x0cNSDictionary\x00"


def test_decode_attributed_body():
    assert im.decode_attributed_body(typedstream("Hello there")) == "Hello there"
    long_text = "x" * 300 + " end"
    assert im.decode_attributed_body(typedstream(long_text)) == long_text
    assert im.decode_attributed_body(typedstream("emoji 🙂 ok")) == "emoji 🙂 ok"
    assert im.decode_attributed_body(None) == "" and im.decode_attributed_body(b"junk") == ""


@pytest.fixture
def im_path(tmp_path):
    p = tmp_path / "chat.db"
    db = sqlite3.connect(p)
    db.executescript("""
    CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
    CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB, handle_id INTEGER, date INTEGER,
        is_from_me INTEGER, is_read INTEGER, service TEXT, associated_message_type INTEGER, cache_roomnames TEXT);
    """)
    ns = lambda m: int((NOW - timedelta(minutes=m)).timestamp() - im.APPLE_EPOCH) * 10**9  # noqa: E731
    db.executemany("INSERT INTO handle VALUES (?,?)", [(1, "+919876500001"), (2, "rahul@example.com")])
    db.executemany("INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?,?)", [
        (1, "Dinner at 8?", None, 1, ns(10), 0, 0, "iMessage", 0, None),
        (2, None, typedstream("Attributed only"), 2, ns(8), 0, 0, "iMessage", 0, None),
        (3, "Already read", None, 1, ns(30), 0, 1, "iMessage", 0, None),
        (4, "Loved \"Dinner at 8?\"", None, 1, ns(9), 0, 0, "iMessage", 2000, None),
        (5, "my own message", None, 1, ns(7), 1, 0, "iMessage", 0, None),
        (6, "Group hello", None, 2, ns(5), 0, 0, "iMessage", 0, "chat123"),
        (7, "Older unread", None, 1, ns(600), 0, 0, "SMS", 0, None),
    ])
    db.commit()
    db.close()
    return p


def test_imessage_unread_and_thread(im_path):
    db = im.IMessageDB(im_path)
    assert db.check() == (True, "")
    items = db.unread()
    assert [i.id for i in items] == ["6", "2", "1", "7"]
    assert items[1].body == "Attributed only" and items[1].sender == "rahul@example.com"
    assert items[0].is_group and items[0].thread_id == "chat123"
    assert [i.id for i in db.unread(since=NOW - timedelta(minutes=60))] == ["6", "2", "1"]
    thread = db.thread("+919876500001", 10)
    assert [m.text for m in thread] == ["Older unread", "Already read", "Dinner at 8?", "my own message"]  # tapbacks left out
    assert db.sent_samples("+919876500001") == ["my own message"]
    assert set(db.handles()) == {"+919876500001", "rahul@example.com"}


def test_imessage_missing_and_changed(tmp_path):
    ok, why = im.IMessageDB(tmp_path / "nope.db").check()
    assert not ok and "wasn't found" in why
    p = tmp_path / "c.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE message (ROWID INTEGER)")
    c.execute("CREATE TABLE handle (ROWID INTEGER, id TEXT)")
    c.commit()
    c.close()
    ok, why = im.IMessageDB(p).check()
    assert not ok and "schema changed" in why


# ----------------------------------------------------------------------------------- Gmail


def b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


MSG = {
    "id": "m1", "threadId": "t1", "labelIds": ["UNREAD", "INBOX"], "snippet": "Can you send the contract today?",
    "internalDate": "1793620800000",
    "payload": {"mimeType": "multipart/alternative", "headers": [
        {"name": "From", "value": "Priya Shah <Priya@Example.com>"}, {"name": "Subject", "value": "Contract"},
        {"name": "Date", "value": "Mon, 02 Nov 2026 11:30:00 +0000"}, {"name": "Message-ID", "value": "<abc@mail>"},
        {"name": "References", "value": "<prev@mail>"}],
        "parts": [{"mimeType": "text/plain", "body": {"data": b64("Hi, can you send the contract today? Thanks")}}]},
}


class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d


class FakeSession:
    def __init__(self, scopes="https://www.googleapis.com/auth/gmail.modify", connected=True):
        self._scopes, self._connected, self.calls = scopes, connected, []

    def connected(self):
        return self._connected

    def scopes(self):
        return self._scopes

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if method == "GET" and url.endswith("/messages"):
            return FakeResp({"messages": [{"id": "m1"}]})
        if method == "GET" and "/messages/m1" in url:
            return FakeResp(MSG)
        if method == "GET" and "/threads/t1" in url:
            return FakeResp({"messages": [MSG]})
        if method == "POST" and url.endswith("/drafts"):
            return FakeResp({"id": "d1", "message": {"id": "dm1", "threadId": kw["json"]["message"].get("threadId")}})
        return FakeResp({}, 404)


def test_gmail_parse_message():
    it = gm.parse_message(MSG)
    assert (it.source, it.sender, it.sender_id, it.title, it.thread_id) == ("gmail", "Priya Shah", "priya@example.com", "Contract", "t1")
    assert it.timestamp == datetime(2026, 11, 2, 11, 30, tzinfo=UTC) and it.unread
    assert it.meta["message_id"] == "<abc@mail>"
    promo = Item("gmail", "x", sender="Deals", sender_id="noreply@shop.com", meta={})
    assert gm.is_promotional(promo) and not gm.is_promotional(it)


async def test_gmail_unread_thread_and_draft():
    s = FakeSession()
    client = gm.GmailClient(s)
    assert client.check() == (True, "") and client.can_draft()
    (item,) = await client.unread(since=NOW - timedelta(hours=2))
    assert item.id == "m1" and "after:" in s.calls[0][2]["params"]["q"] and "is:unread" in s.calls[0][2]["params"]["q"]
    thread = await client.thread("t1")
    assert "send the contract today" in thread[0]["text"]
    res = await client.create_draft(to="priya@example.com", subject="Re: Contract", body="On it.", thread_id="t1",
                                    in_reply_to="<abc@mail>", references="<prev@mail>")
    assert res["draft_id"] == "d1" and res["url"] == "https://mail.google.com/mail/u/0/#drafts?compose=dm1"
    method, url, kw = s.calls[-1]
    assert method == "POST" and url.endswith("/drafts") and kw["json"]["message"]["threadId"] == "t1"
    raw = base64.urlsafe_b64decode(kw["json"]["message"]["raw"]).decode()
    assert "In-Reply-To: <abc@mail>" in raw and "References: <prev@mail> <abc@mail>" in raw and "To: priya@example.com" in raw


async def test_gmail_refuses_drafts_without_scope_and_never_sends():
    from jarvis_core.errors import SkillError

    client = gm.GmailClient(FakeSession(scopes="https://www.googleapis.com/auth/gmail.readonly"))
    assert not client.can_draft()
    with pytest.raises(SkillError):
        await client.create_draft(to="a@b.c", subject="s", body="b")
    assert gm.GmailClient(FakeSession(connected=False)).check()[0] is False
    src = inspect.getsource(gm)
    assert "/send" not in src and "messages.send" not in src and "drafts.send" not in src and "gmail.send" not in src.replace(
        "never ``gmail.send``", "")


def test_reply_subject():
    assert gm.reply_subject("Contract") == "Re: Contract" and gm.reply_subject("re: Contract") == "re: Contract"


# ---------------------------------------------------------------------------------- contacts


def test_normalise_phone():
    assert normalise_phone("98765 43210") == "+919876543210"
    assert normalise_phone("+1 (415) 555-0100") == "+14155550100"
    assert normalise_phone("0091 98765 43210") == "+919876543210"
    assert normalise_phone("12") == "" and normalise_phone("09876543210") == "+919876543210"


def book():
    b = ContactBook("91", aliases=lambda: {"my brother": "Arjun Mehta"})
    b.add("Rahul Sharma", phones=["9876500011"], source="contacts")
    b.add("Rahul Verma", phones=["9876500012"], source="contacts")
    b.add("Priya Shah", phones=["+919876500001"], emails=["priya@example.com"], source="contacts")
    b.add("Arjun Mehta", phones=["9876500020"], source="contacts")
    b.add("Mom", phones=["9876500030"], source="contacts")
    return b


def test_contacts_resolution():
    b = book()
    assert b.resolve("priya").best.name == "Priya Shah" and not b.resolve("priya").ambiguous
    res = b.resolve("Rahul")
    assert res.ambiguous and res.spoken_choices() == "Rahul Sharma or Rahul Verma"
    assert b.resolve("Rahul Verma").best.name == "Rahul Verma" and not b.resolve("rahul verma").ambiguous
    assert b.resolve("my brother").best.name == "Arjun Mehta"
    assert b.resolve("Priyaa Shaah").best.name == "Priya Shah"  # speech-to-text typos
    assert b.resolve("zebra").candidates == []
    # one Rahul is somebody you actually chat with: no need to ask
    b.add("Rahul Verma", wa_jid="919876500012@s.whatsapp.net", source="whatsapp", last_contact=NOW)
    assert not b.resolve("rahul").ambiguous and b.resolve("rahul").best.name == "Rahul Verma"


def test_contacts_identifiers_and_merge():
    b = book()
    assert b.by_identifier("priya@example.com").name == "Priya Shah"
    assert b.by_identifier("+919876500001").name == "Priya Shah" and b.name_for("9876500001") == "Priya Shah"
    merged = b.add("Priya S", phones=["9876500001"], wa_jid="919876500001@s.whatsapp.net", source="whatsapp")
    assert merged.name == "Priya Shah" and merged.wa_jid.startswith("9198765")
    b.load_imessage_handles(["priya@example.com", "+919876500020", "unknown@x.com"])
    assert "+919876500020" in b.by_identifier("+919876500020").handles
    assert b.by_identifier("unknown@x.com") is None


def test_load_apple_records():
    b = ContactBook("91")
    n = b.load_apple([{"name": "Dev Patel", "content": "Dev Patel\nPhone: +91 98765 11111\nEmail: dev@acme.io", "metadata": {}, "participants": []},
                      {"name": "", "content": "x"}])
    c = b.resolve("dev").best
    assert n == 1 and c.phones == ["+919876511111"] and c.emails == ["dev@acme.io"]


# -------------------------------------------------------------------------------------- VIP


def test_vip_list_persistence_and_matching(tmp_path):
    cfg = Config()
    cfg.vip.contacts = ["Mom"]
    v = VipList(cfg, tmp_path / "vip.json")
    assert v.add("Priya Shah") and not v.add("priya shah") and not v.add("Mom")
    assert v.names() == ["Mom", "Priya Shah"]
    assert v.is_vip("Priya Shah") and v.is_vip("Priya") is False and v.is_vip("Mom in Family")
    assert v.is_vip("Unknown", ["priya shah"]) is True
    assert v.add("+91 98765 00001") and v.is_vip("Someone", ["+919876500001"])
    assert v.remove("Priya Shah") and not v.is_vip("Priya Shah") and not v.remove("Mom")  # config names aren't removable
    assert json.loads((tmp_path / "vip.json").read_text()) == ["+91 98765 00001"]


# -------------------------------------------------------------------------------------- hub


class StubReader:
    def __init__(self, items, ok=True, boom=False):
        self.items, self.ok, self.boom, self.calls = items, ok, boom, 0

    def check(self):
        return (self.ok, "" if self.ok else "disabled")

    def unread(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("db locked")
        return list(self.items)


def make_hub(**readers):
    cfg = Config()
    contacts = book()
    vip = VipList(cfg, None)
    vip.path = Path_for_tests()
    return CommsHub(cfg, contacts=contacts, vip=vip, **readers), contacts, vip


def Path_for_tests():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp()) / "vip.json"


async def test_hub_merges_filters_caches_and_isolates_errors():
    t = lambda m: NOW - timedelta(minutes=m)  # noqa: E731
    wa_r = StubReader([Item("whatsapp", "w1", sender="Priya Shah", sender_id="919876500001@s.whatsapp.net", body="hi", timestamp=t(5))])
    im_r = StubReader([Item("imessage", "i1", sender="+919876500020", sender_id="+919876500020", body="yo", timestamp=t(50))])
    gm_r = StubReader([], boom=True)
    hub, contacts, _ = make_hub(whatsapp=wa_r, imessage=im_r, gmail=gm_r)
    items = await hub.unread()
    assert [i.id for i in items] == ["w1", "i1"]
    assert items[1].sender == "Arjun Mehta"  # handle replaced with the contact's name
    assert "db locked" in hub.status()["gmail"][1] and hub.status()["linkedin"] == (False, "not configured")
    assert [i.id for i in await hub.unread(since=t(30))] == ["w1"]
    await hub.unread()
    assert wa_r.calls == 1  # served from the short cache
    assert [i.id for i in await hub.unread(sources=["imessage"])] == ["i1"]


async def test_hub_reports_disabled_reader():
    hub, *_ = make_hub(whatsapp=StubReader([], ok=False))
    assert await hub.unread() == [] and hub.status()["whatsapp"] == (False, "disabled")


def test_importance_scoring():
    hub, contacts, vip = make_hub()
    vip.add("Priya Shah")
    mk = lambda **kw: Item(**{"source": "whatsapp", "id": "1", "sender": "Stranger", "sender_id": "x", "body": "hello", **kw})  # noqa: E731
    assert hub.importance(mk(sender="Priya Shah", sender_id="919876500001@s.whatsapp.net")) == (6, True)
    assert hub.importance(mk(sender="Priya Shah in Family", is_group=True))[1] is False  # VIP in a group isn't a personal ping
    plain, _ = hub.importance(mk())
    urgent, _ = hub.importance(mk(body="URGENT call me?"))
    assert plain == 1 and urgent >= 4
    assert hub.importance(mk(is_group=True, body="lol"))[0] < 0
    promo = Item("gmail", "g", sender="Shop", sender_id="noreply@shop.com", title="50% off", body="unsubscribe here", meta={"list_unsubscribe": True})
    assert hub.importance(promo)[0] < 0
    ask = Item("gmail", "g2", sender="Priya Shah", sender_id="priya@example.com", title="Contract", body="Can you reply today?", meta={})
    assert hub.importance(ask)[0] >= 3


async def test_hub_channels():
    hub, contacts, _ = make_hub(whatsapp=StubReader([]), imessage=StubReader([]), gmail=StubReader([]))
    priya = contacts.resolve("priya").best
    apps = [c.app for c in hub.channels_for(priya)]
    assert apps == ["whatsapp", "imessage", "gmail"]
    wa_ch = hub.channels_for(priya)[0]
    assert wa_ch.ident == "919876500001@s.whatsapp.net" and wa_ch.phone == "+919876500001"
    assert (await hub.pick_channel(priya, "gmail")).email == "priya@example.com"
    mom = contacts.resolve("mom").best
    assert await hub.pick_channel(mom, "gmail") is None
    assert (await hub.pick_channel(mom, "linkedin")).app == "linkedin"
    item = Item("gmail", "m1", sender="Priya Shah", sender_id="priya@example.com", title="Contract", thread_id="t1",
                meta={"message_id": "<a@b>", "references": ""})
    ch = hub.channel_for_item(item)
    assert (ch.app, ch.thread_id, ch.in_reply_to, ch.subject) == ("gmail", "t1", "<a@b>", "Contract")
    wa_item = Item("whatsapp", "w", sender="Mom in Family", thread_id="1@g.us", is_group=True, meta={"chat": "Family"})
    assert hub.channel_for_item(wa_item) == Channel("whatsapp", "1@g.us", "Family", thread_id="1@g.us", is_group=True)


# ------------------------------------------------------------------------------ action filter


def test_action_filter_keys():
    f = ActionFilter()
    assert f.check_key("v", ["cmd"]).allowed and f.check_key("f", ["cmd"]).allowed and f.check_key("tab").allowed
    assert not f.check_key("return", app="WhatsApp").allowed
    assert not f.check_key("Return", focused_role="AXTextArea", app="Notes").allowed
    assert not f.check_key("return", ["cmd"], app="Mail").allowed
    assert not f.check_key("enter").allowed  # unknown context: refuse
    assert f.check_key("return", app="Finder", focused_role="AXButton").allowed
    assert not f.check_key("q", ["cmd"]).allowed and not f.check_key("w", ["cmd", "ctrl"]).allowed


def test_action_filter_clicks_and_typing():
    f = ActionFilter()
    for label in ("Send", "send message", "Post", "Submit application", "Pay now", "Delete chat", "Confirm order"):
        assert not f.check_click(label).allowed, label
    for label in ("Type a message", "Search", "Sender info", "Compose", ""):
        assert f.check_click(label).allowed, label
    assert not f.check_type("hunter2", secure=True).allowed and f.check_type("hello").allowed




def test_timestamp_sentinels_do_not_crash_readers():
    from jarvis_core.comms import imessage, whatsapp_db

    assert whatsapp_db.to_dt(8.6e12) is None and whatsapp_db.to_dt(1e30) is None and whatsapp_db.to_dt(None) is None
    assert whatsapp_db.to_dt(0) is not None
    assert imessage.to_dt(1e30) is None
    assert imessage.to_dt(7.5e17) is not None
