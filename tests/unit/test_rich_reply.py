"""What is spoken versus what is shown, by question type."""

import asyncio

import pytest

from jarvis_core.brain.display import ScreenSplitter, classify_question, format_hint
from jarvis_core.brain.mathfmt import latex_to_unicode, render_math
from jarvis_core.frontend import FakeFrontend
from jarvis_core.skills.calc import _compute
from jarvis_core.voice.speaker import Speaker
from tests.fakes import FakeTTS


@pytest.mark.parametrize("q,kind", [
    ("write a python script to rename my files", "code"),
    ("give me a regex for email addresses", "code"),
    ("how do i reverse a list in python", "code"),
    ("what is 15% of 240", "math"),
    ("solve x^2 - 5x + 6 = 0", "math"),
    ("what's the derivative of x squared times sin x", "math"),
    ("compare react and vue", "table"),
    ("react vs vue", "table"),
    ("what's the difference between tcp and udp", "table"),
    ("how do i set up an ssh key", "steps"),
    ("walk me through installing homebrew", "steps"),
    ("what is the capital of france", "chat"),
    ("open safari", "chat"),
])
def test_classify(q, kind):
    assert classify_question(q) == kind


def test_hint_only_for_special_kinds():
    assert format_hint("hello there") == ""
    assert "<screen>" in format_hint("write a python function")


def feed_all(pieces):
    s = ScreenSplitter()
    spoken = "".join(s.feed(p) for p in pieces) + s.flush()
    return spoken, s.screen


def test_tag_split_across_chunks():
    spoken, screen = feed_all(["Here's a quick one. <scr", "een>```py", "thon\nprint(1)\n``", "`\n- note\n</scre", "en> Enjoy."])
    assert spoken.replace("  ", " ") == "Here's a quick one. Enjoy."
    assert screen == "```python\nprint(1)\n```\n- note"


def test_bare_fence_is_treated_as_screen():
    spoken, screen = feed_all(["Sure: ``", "`bash\nls -la\n```\nThat lists everything."])
    assert "ls" not in spoken and "That lists everything." in spoken
    assert screen == "```bash\nls -la\n```"


def test_unclosed_blocks_are_closed():
    _, screen = feed_all(["ok <screen>1. one\n2. two"])
    assert screen == "1. one\n2. two"
    _, screen = feed_all(["ok ```js\nlet a = 1"])
    assert screen.endswith("```") and "let a = 1" in screen


def test_no_screen_means_plain_speech():
    spoken, screen = feed_all(["It's twenty-two degrees ", "and sunny."])
    assert spoken == "It's twenty-two degrees and sunny." and screen == ""


def test_latex_to_unicode():
    assert latex_to_unicode(r"\frac{x^2+1}{2}") == "(x²+1)/2"
    assert latex_to_unicode(r"\sqrt{b^2-4ac}") == "√(b²-4ac)"
    assert latex_to_unicode(r"\int_0^1 x^2\,dx") == "∫₀¹ x² dx"
    assert latex_to_unicode(r"\alpha + \beta \leq \pi") == "α + β ≤ π"


def test_render_math_leaves_code_alone():
    out = render_math("Inline $x^2$ and\n$$E = mc^2$$\n```python\nprice = '$5'\n```")
    assert "x²" in out and "```math\nE = mc²\n```" in out and "price = '$5'" in out


def test_calculate():
    assert _compute("15% of 240", "evaluate", "x", "").startswith("Result: 36")
    assert "[2, 3]" in _compute("x^2-5x+6=0", "solve", "x", "")
    assert "(x - 3)*(x - 2)" in _compute("x^2-5x+6", "factor", "x", "")
    with pytest.raises(Exception):
        _compute("__import__('os').system('ls')", "evaluate", "x", "")


def test_speaker_speaks_the_short_part_and_shows_the_rest():
    fe, tts = FakeFrontend(), FakeTTS()
    sp = Speaker(tts, fe)

    async def stream():
        for piece in ["This renames them all. ", "<screen>```python\nimport os\n```\n</screen>"]:
            yield piece

    spoken = asyncio.run(sp.speak_stream(stream()))
    assert spoken == "This renames them all."
    assert all("import" not in t for t in tts.spoken)
    assert fe.rich_blocks and fe.rich_blocks[-1] == "```python\nimport os\n```"


def test_screen_only_reply_still_says_something():
    fe, tts = FakeFrontend(), FakeTTS()
    sp = Speaker(tts, fe)

    async def stream():
        yield "<screen>1. one\n2. two</screen>"

    spoken = asyncio.run(sp.speak_stream(stream()))
    assert "screen" in spoken
    assert fe.rich_blocks[-1] == "1. one\n2. two"
