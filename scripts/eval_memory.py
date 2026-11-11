"""Score memory search against the private fact pack: for each question, does a fact that holds the answer come back near the top?

    uv run python scripts/eval_memory.py            # score the current search
    uv run python scripts/eval_memory.py --misses   # also list the questions it gets wrong
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from jarvis_core.interfaces import Fact
from jarvis_core.memory.store import SQLiteMemory

PACK = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "memory_pack.txt"

# (question, substrings; a returned fact is right if its text contains ALL of them, case-insensitive)
CASES: list[tuple[str, list[str]]] = [
    ("When does my MBA start?", ["start in 2030"]),
    ("Is Devsper still running?", ["shut down in oct 2026"]),
    ("What was Shuttr called before?", ["retrocam"]),
    ("How long was my hackathon?", ["actually ran at 24"]),
    ("What's my CGPA?", ["cgpa after sem 6"]),
    ("What was my sem 5 SGPA?", ["sem 3 and sem 5"]),
    ("What was my role at CIE?", ["project intern at cie"]),
    ("What stipend do I want?", ["stipend above"]),
    ("Which projects did I drop from my resume?", ["cerberus", "dropped"]),
    ("What's my GitHub username?", ["github is kelyonn"]),
    ("Which local model do I use daily?", ["qwen3.6"]),
    ("Which platform is my Hermes agent on?", ["hermes agent runs on discord"]),
    ("Where's my portfolio?", ["kelyonn.github.io/system32"]),
    ("What was the Samarpana footfall per day?", ["3,000"]),
    ("Which two offers am I weighing?", ["latent view"]),
    ("What does ICP stand for in my Devsper notes?", ["ideal customer profile"]),
    ("What time do I wake up?", ["wake 8"]),
    ("Which language did I pick for DSA?", ["python for dsa"]),
    ("What is ServerCrash358?", ["servercrash358"]),
    ("What's the difference between Coli and Colibrì?", ["same thing"]),
    ("What does FDE stand for?", ["forward deployed engineer"]),
    ("When was my Couchbase interview?", ["couchbase", "17 aug"]),
    ("What is my sem 7 schedule like?", ["sem 7", "no classes"]),
    ("Is Hermes the same as my Discord server?", ["name of my private discord server"]),
    # reworded: little or no word overlap with the stored fact
    ("where do I study?", ["pes university"]),
    ("how often do I work out?", ["train 6 days"]),
    ("what is my employer called?", ["project intern at cie"]),
    ("did my startup die?", ["devsper shut down"]),
    ("how much money do I want to make after business school?", ["post-mba pay"]),
    ("what do I want to eat while cutting?", ["cutting meal plan"]),
    ("which cameras app am I building?", ["shuttr is a retro"]),
    ("what's my phone number?", ["phone number"]),
    ("when is my birthday?", ["born on 15 dec"]),
    ("what's my date of birth?", ["born on 15 dec"]),
    ("how many tests does Nexus have?", ["478 tests"]),
    ("what does my container runtime do?", ["airlock is a container runtime"]),
    ("what am I doing for my final year project?", ["capstone is a drug interaction"]),
    ("which job boards do I use?", ["linkedin, wellfound"]),
    ("what injuries have I had?", ["ankle"]),
    ("how do I like answers to be?", ["short, tight answers"]),
    ("what languages do I program in?", ["i code in go"]),
    ("which city do I live in?", ["i live in bengaluru"]),
    ("what kind of roles do I want?", ["tech and non-tech"]),
    ("what is my salary target?", ["10 lpa"]),
    ("do I want to get married?", ["wife and kids"]),
    ("what is my dream career goal?", ["c-suite"]),
    ("how do I store my local LLMs?", ["external ssd"]),
    ("who are the competitors of my startup?", ["lindy"]),
    ("what is the TCA loop?", ["trigger, context, action"]),
    ("what's my email address?", ["my email is"]),
]

# written after the search was tuned, never used to tune it: the honest check
HELDOUT: list[tuple[str, list[str]]] = [
    ("what colour palette does my website use?", ["warm paper 1-bit palette"]),
    ("who am I up against in the AI automation space?", ["lindy"]),
    ("what sports do I want to take up?", ["badminton"]),
    ("how big is my external drive?", ["420gb"]),
    ("which chatbot platform hosts my agent?", ["hermes agent runs on discord"]),
    ("what's the plan to earn extra money?", ["amazon kdp"]),
    ("when do I eat lunch?", ["lunch till 3"]),
    ("which college club did I lead?", ["club head of appex"]),
    ("what accessories or books do I use for the drug project?", ["stockley"]),
    ("how many people were on my hackathon team?", ["120+"]),
    ("am I good at writing code by myself?", ["can't write all the code"]),
    ("what do I do about footwear injuries?", ["ankle"]),
    ("what's my favourite football formation?", ["4-3-3"]),
    ("what is the role at the robotics company?", ["forward deployed engineer role at aktoria"]),
    ("do I plan to publish research?", ["survey paper"]),
    ("how do I keep my notes app private?", ["no cloud"]),
]


def load(store: SQLiteMemory) -> int:
    n = 0
    for line in PACK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.split(" | ")[0].endswith("?") and " | " in line:
            continue
        store.add(Fact(kind="fact", text=line))
        n += 1
    return n


def score(search, k: int = 3, cases=None) -> tuple[float, float, list[str], float]:
    hits, rr, misses, t = 0, 0.0, [], 0.0
    cases = cases or CASES
    for q, need in cases:
        t0 = time.perf_counter()
        got = search(q, k)
        t += time.perf_counter() - t0
        rank = next((i for i, f in enumerate(got, 1) if all(s in f.text.lower() for s in need)), 0)
        if rank:
            hits += 1
            rr += 1 / rank
        else:
            misses.append(q)
    n = len(cases)
    return hits / n, rr / n, misses, t / n * 1000


def main() -> None:
    if not PACK.exists():
        sys.exit(f"missing {PACK}")
    with tempfile.TemporaryDirectory() as d:
        store = SQLiteMemory(Path(d) / "m.db")
        n = load(store)
        recall, mrr, misses, ms = score(store.search)
    print(f"{n} facts, {len(CASES)} questions | recall@3 {recall:.0%} | MRR {mrr:.2f} | {ms:.1f} ms/query")
    if "--misses" in sys.argv:
        for q in misses:
            print("  miss:", q)


if __name__ == "__main__":
    main()
