from jarvis_core import cards
from jarvis_core.frontend import FakeFrontend
from jarvis_core.ipc import protocol as P


async def test_ipc_card_action_say_becomes_typed_input():
    from jarvis_core.ipc.frontend import IPCFrontend

    typed = []

    class Ev:
        def on_text_input(self, t):
            typed.append(t)

    fe = IPCFrontend.__new__(IPCFrontend)
    fe.events = Ev()
    await fe._on_control(P.Control(P.CARD_ACTION, {"kind": "say", "value": "undo that"}))
    await fe._on_control(P.Control(P.CARD_ACTION, {"kind": "url", "value": "https://example.com"}))  # the Shell handles these
    assert typed == ["undo that"]


async def test_fake_frontend_records_cards_and_ignores_empty():
    fe = FakeFrontend()
    await fe.card({})
    await fe.card(cards.clean(cards.done_card("Moved 3 files", undo=True)))
    assert len(fe.cards) == 1 and fe.cards[0]["actions"][0]["value"] == "undo that"
