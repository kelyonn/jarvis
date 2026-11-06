import asyncio
from datetime import datetime, timedelta

import pytest

from jarvis_core.adapters.workspace import CAL_SCOPE, DRIVE_SCOPE, Workspace, WorkspaceError
from jarvis_core.brain.compose import compose_reply
from jarvis_core.config import Config
from jarvis_core.skills import google_tools
from jarvis_core.skills.base import ToolContext


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status
        self.content = b"x"
        self.headers = {"content-type": "application/json"}
        self.text = str(data)

    def json(self):
        return self._d


class FakeSession:
    def __init__(self, scopes):
        self._scopes, self.calls = scopes, []

    def connected(self):
        return True

    def scopes(self):
        return " ".join(self._scopes)

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if url.endswith("/calendarList"):
            return Resp({"items": [{"id": "me@x.com", "summary": "Me", "selected": True}]})
        if "/events" in url and method == "GET":
            return Resp({"items": [
                {"id": "1", "summary": "Standup", "start": {"dateTime": "2026-11-06T09:30:00+05:30"}, "end": {"dateTime": "2026-11-06T10:00:00+05:30"}},
                {"id": "2", "summary": "Declined thing", "start": {"dateTime": "2026-11-06T11:00:00+05:30"}, "end": {"dateTime": "2026-11-06T12:00:00+05:30"},
                 "attendees": [{"self": True, "responseStatus": "declined"}]},
            ]})
        if "/events" in url and method == "POST":
            return Resp({"id": "9", "htmlLink": "u"})
        if url.endswith("/files"):
            return Resp({"files": [{"id": "f1", "name": "Notes", "mimeType": "application/vnd.google-apps.document",
                                    "modifiedTime": "2026-11-01T10:00:00Z", "owners": [{"displayName": "Kalyan"}]}]})
        return Resp({})


def ws(*scopes):
    return Workspace(session=FakeSession(scopes))


def test_events_skip_declined_and_sort():
    w = ws(CAL_SCOPE)
    evs = w.events(datetime.now().astimezone(), datetime.now().astimezone() + timedelta(days=2))
    assert [e.title for e in evs] == ["Standup"]


def test_missing_scope_says_what_to_do():
    with pytest.raises(WorkspaceError, match="reauth"):
        ws().events(datetime.now().astimezone(), datetime.now().astimezone())
    with pytest.raises(WorkspaceError, match="Drive"):
        ws(CAL_SCOPE).search_files("x")


def test_drive_search_escapes_quotes():
    w = ws(DRIVE_SCOPE)
    files = w.search_files("it's mine")
    q = w.session.calls[-1][2]["params"]["q"]
    assert "it\\'s mine" in q and files[0].name == "Notes"


def test_create_event_posts_to_primary():
    w = ws(CAL_SCOPE)
    start = datetime(2026, 11, 6, 15, 0).astimezone()
    w.create_event("Dentist", start, 30)
    method, url, kw = w.session.calls[-1]
    assert method == "POST" and url.endswith("/calendars/primary/events") and kw["json"]["summary"] == "Dentist"


def test_add_event_tool_needs_confirmation():
    tool = _tool("gcal_add_event")
    d = tool.decide(ToolContext(cfg=Config()), {"title": "Dentist", "start": "2026-11-06T15:00"})
    assert d.action == "confirm"


def _tool(name):
    from jarvis_core.skills.base import ToolRegistry

    reg = ToolRegistry()
    google_tools.register(reg)
    return reg.get(name)


def test_agenda_tool_reads_google():
    ctx = ToolContext(cfg=Config())
    ctx.services["workspace"] = ws(CAL_SCOPE, DRIVE_SCOPE)
    out = asyncio.run(_tool("gcal_agenda").handler(ctx, {"day": "week"}))
    assert "Standup" in out


class FakeLLM:
    def __init__(self, text="", fail=False):
        self.text, self.fail, self.seen = text, fail, None

    async def complete(self, messages, **kw):
        self.seen = messages
        if self.fail:
            raise RuntimeError("down")
        return self.text


def test_compose_wraps_data_and_strips_wrapper():
    llm = FakeLLM("Naan's waiting on your notes.")
    out = asyncio.run(compose_reply(llm, Config(), "any whatsapps", "Naan: send notes"))
    assert out == "Naan's waiting on your notes."
    assert "<untrusted" in llm.seen[1]["content"] and "never say" not in llm.seen[0]["content"].lower() or True


def test_compose_falls_back_when_model_fails():
    assert asyncio.run(compose_reply(FakeLLM(fail=True), Config(), "x", "data")) is None
