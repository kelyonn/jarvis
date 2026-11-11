from datetime import datetime, timezone

from jarvis_core.interfaces import Item
from jarvis_core.skills.comms_tools import _open_email, _read_email


class FakeGmail:
    def __init__(self):
        self.item = Item("gmail", "m1", sender="Boomi Software", title="JD and hiring schedule", body="Dear students, Find the JD",
                         timestamp=datetime(2026, 11, 11, 11, tzinfo=timezone.utc), thread_id="t1")

    async def unread(self, since=None, max_items=15):
        return [self.item]

    async def search(self, q, n=8):
        return []

    async def thread(self, thread_id, limit=6):
        return [{"item": self.item, "text": "Dear students,\nEligible: CGPA 6.5 and above.\nRegister on the placement app."}]


class Hub:
    readers = {"gmail": FakeGmail()}


class Res:
    ok = True


class Ctx:
    services = {"comms": Hub()}
    state: dict = {}
    opened: list = []

    async def run(self, argv, **kw):
        self.opened.append(argv)
        return Res()


async def test_read_email_returns_full_body():
    out = await _read_email(Ctx(), {"query": "Boomi"})
    assert "CGPA 6.5" in out and "Boomi Software" in out


async def test_open_email_opens_gmail_thread():
    ctx = Ctx()
    out = await _open_email(ctx, {"query": "Boomi Software"})
    assert ctx.opened[-1][-1].endswith("#all/t1") and "Boomi" in out
