"""`jarvisd memory`: list, search, forget, edit, json."""

from __future__ import annotations

import json
import time

import pytest

from jarvis_core import cli
from jarvis_core.interfaces import Fact
from jarvis_core.memory.store import SQLiteMemory


@pytest.fixture
def mem():
    m = SQLiteMemory()  # JARVIS_HOME is isolated by conftest, and the CLI opens the same file
    now = time.time()
    m.add(Fact(id="aaaa1111", kind="person", text="The user's brother is Arjun", meta={"alias": "brother", "name": "Arjun"}, created_at=now - 100))
    m.add(Fact(id="aaaa2222", kind="fact", text="I live in Pune", created_at=now - 50))
    m.add(Fact(id="bbbb3333", kind="fact", text="I work on search", created_at=now - 10))
    m.add(Fact(id="cccc4444", kind="preference", text="I prefer tea", created_at=now))
    m.add(Fact(id="dddd5555", kind="episode", text="[Mon] You said: hi | I replied: hello", created_at=now))
    m.add(Fact(id="eeee6666", kind="style", text="short and warm", meta={"app": "whatsapp"}, created_at=now))
    yield m
    m.close()


def run(capsys, *argv):
    with pytest.raises(SystemExit) as e:
        cli.main(["memory", *argv])
    out = capsys.readouterr()
    return e.value.code, out.out, out.err


def test_lists_grouped_by_kind_newest_first(mem, capsys):
    code, out, _ = run(capsys)
    assert code == 0
    assert out.index("People (1)") < out.index("Preferences (1)") < out.index("Facts (2)")
    assert out.index("bbbb3333") < out.index("aaaa2222")  # newest first inside a group
    assert "aaaa1111" in out and "I prefer tea" in out
    assert "Conversations" not in out and "Writing styles" not in out


def test_episodes_and_styles_only_when_asked(mem, capsys):
    _, out, _ = run(capsys, "--episodes")
    assert "Conversations (1)" in out and "Writing styles" not in out
    _, out, _ = run(capsys, "--styles")
    assert "Writing styles (1)" in out and "short and warm" in out


def test_search(mem, capsys):
    _, out, _ = run(capsys, "--search", "pune")
    assert "I live in Pune" in out and "I prefer tea" not in out
    code, out, _ = run(capsys, "--search", "quantum")
    assert code == 0 and "(nothing found)" in out


def test_forget_by_full_id_and_prefix(mem, capsys):
    code, out, _ = run(capsys, "--forget", "aaaa2222")
    assert code == 0 and "forgot aaaa2222" in out and mem.get("aaaa2222") is None
    code, out, _ = run(capsys, "--forget", "cccc")
    assert code == 0 and mem.get("cccc4444") is None


def test_forget_errors(mem, capsys):
    code, _, err = run(capsys, "--forget", "aaaa")  # matches aaaa1111 and aaaa2222
    assert code == 1 and "matches 2" in err and mem.get("aaaa1111") is not None
    code, _, err = run(capsys, "--forget", "zzzzzz")
    assert code == 1 and "no memory" in err
    code, _, err = run(capsys, "--forget", "aa")
    assert code == 1 and "at least 4" in err


def test_edit_a_fact_keeps_its_kind(mem, capsys):
    code, out, _ = run(capsys, "--edit", "aaaa2222", "I live in Delhi")
    assert code == 0 and "updated aaaa2222" in out
    f = mem.get("aaaa2222")
    assert f.text == "I live in Delhi" and f.kind == "fact"


def test_edit_a_person_rederives_the_name(mem, capsys):
    code, _, _ = run(capsys, "--edit", "aaaa1111", "my brother is Rahul")
    f = mem.get("aaaa1111")
    assert code == 0 and f.kind == "person" and f.meta == {"alias": "brother", "name": "Rahul"} and "Rahul" in f.text


def test_edit_a_person_with_free_text_keeps_the_old_details(mem, capsys):
    run(capsys, "--edit", "aaaa1111", "Arjun, lives in Mumbai")
    f = mem.get("aaaa1111")
    assert f.kind == "person" and f.text == "Arjun, lives in Mumbai" and f.meta["name"] == "Arjun"


def test_edit_errors(mem, capsys):
    code, _, err = run(capsys, "--edit", "nope1234", "x")
    assert code == 1 and "no memory" in err
    code, _, err = run(capsys, "--edit", "aaaa2222", "   ")
    assert code == 1 and "empty" in err and mem.get("aaaa2222").text == "I live in Pune"


def test_json_output(mem, capsys):
    code, out, _ = run(capsys, "--json", "--search", "tea")
    rows = json.loads(out)
    assert code == 0 and [r["id"] for r in rows] == ["cccc4444"]
    assert set(rows[0]) == {"id", "kind", "date", "text"}
    assert json.loads(run(capsys, "--json")[1])  # whole memory too


def test_secrets_are_never_printed(mem, capsys):
    mem.add(Fact(id="ffff7777", kind="fact", text="my password: hunter2hunter and key AKIAIOSFODNN7EXAMPLE"))
    _, out, _ = run(capsys)
    assert "hunter2hunter" not in out and "AKIAIOSFODNN7EXAMPLE" not in out
    _, out, _ = run(capsys, "--json")
    assert "hunter2hunter" not in out


def test_empty_memory(capsys):
    code, out, _ = run(capsys)
    assert code == 0 and "(memory is empty)" in out
