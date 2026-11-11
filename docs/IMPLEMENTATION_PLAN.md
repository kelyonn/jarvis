# Final implementation plan and feature list

Pulls together every decision so far. Background lists: [ASSISTANT_ROADMAP.md](ASSISTANT_ROADMAP.md) (everything found) and [TAKE_LIST.md](TAKE_LIST.md) (usable sources). This file is the trimmed, ordered, final version.

## Decisions

1. **Goal:** a personal assistant that is fast, good at talking, good at remembering, able to do tasks and control the laptop in any app safely, wide in range, and that doesn't mess things up.
2. **Not wanted:** automation (schedules, workflows, channels, sync), Claude as the brain (NIM stays), knowledge graph, full-duplex speech models, sandbox VM, hand gestures, spoken colour theming, a separate skills format, ClearerVoice, pyannote.
3. **Licence of our code:** open source, credit required: Apache-2.0 with a `NOTICE` file.
4. **Sources:** only MIT, Apache-2.0 and BSD. Reference clone of adewaskar's repo is at `../adewaskar` (reading only).
5. **How we take things:**
   - **Default: write our own.** Read to understand, then design for our codebase.
   - **Libraries** are used as packages, never copied in.
   - **At most two big code takes**, adapted to our code, each recorded in `THIRD_PARTY.md` with the original copyright and licence notice kept (see below).
6. **Measure before optimising.** Speed work is driven by a baseline, not guesses.
7. **Voice must be trustworthy:** it must not hear or interrupt itself, and it must hear you well.

## The two big code takes

| # | What | From | Licence | Why this one |
|---|---|---|---|---|
| **A** | The macOS app-control driver: accessibility tree reading (`Driver/Mac/Accessibility.swift`, about 1.8k lines), element reference cache that detects stale targets (`ElementCache.swift`, about 260 lines), per-app autonomy rules (`Policy/AutonomyPolicy.swift`, about 320 lines) and the action-effect classifier (`Policy/EffectClassifier.swift`, about 185 lines). About 2.5k of the module's 13.7k lines. | [Osaurus](https://github.com/osaurus-ai/osaurus) `Packages/OsaurusCore/ComputerUse/` | MIT | It is Swift, like our Shell, so it adapts directly. It is the hardest part of "control any app" to get right. We take the slice, not the whole agent loop. |
| **B** (conditional) | A small acoustic echo canceller, only if the Apple voice-processing spike in phase 2 fails. | [thewh1teagle/aec](https://github.com/thewh1teagle/aec) (speexdsp, Rust) or [pyaec](https://github.com/ewan-xu/pyaec) | MIT, Apache-2.0 | Echo cancellation is signal processing that is slow and risky to write from scratch. |

Considered and rejected as big takes:
- **agent-desktop (Apache-2.0):** its macOS crate is about 35k lines of Rust, too big to port and the wrong language. Read it for ideas only (the snapshot, act, re-check loop).
- **Osaurus privacy filter:** about 16k lines including a vendored model kit, and the model's weight licence is unchecked. We write a simpler filter ourselves.
- **mem0, MCP, RNNoise, smart-turn, Pipecat:** used as libraries or patterns, not copied.

Before taking A, confirm the exact files compile and build in our Shell, check that nothing depends on Osaurus-only internals, and check the licence header of each file we copy.

## Final feature list

Codes are used in the phases below. `Exists` means it is already in the code.

### Safety
- **S1** One gate for every tool call (including MCP), built on the existing `Decision` and `Risk`. *Extend.*
- **S2** Per-app autonomy rules: ask every time, ask for risky only, or never. *Big take A, adapted.*
- **S3** Guards on model-chosen input: no private or loopback URLs, file tools confined, credential paths always refused. *Write.*
- **S4** Privacy filter before anything goes to NIM: swap names, emails, numbers and secrets for placeholders and restore them in the reply. Fails closed. *Write.*
- **S5** A spoken preview before risky actions that says exactly what will happen. *Write.*
- **S6** Trash instead of delete, and a change log so actions can be undone. *Write.*
- **S7** Switches in config to turn off whole capabilities (camera, browser, shell, MCP). *Write.*
- **Exists:** action filter, shell allow-list, protected paths, audit log with hashes.

### Voice: not hearing itself
- **V1** No self-interruption: Apple voice processing with minimal ducking if the spike works, otherwise echo cancellation. *Spike, then big take B.*
- **V2** Interrupt only when echo-cancelled audio, your voiceprint and energy above the echo floor agree. *Write.*
- **V3** Route-aware: free barge-in on headphones, strict on speakers, re-check when the route changes. *Write.*
- **V4** Match transcripts against the exact sentence just spoken. *Write.*
- **V5** Duck or pause music while listening and speaking, then restore. *Write.*

### Voice: hearing you well
- **H1** Noise suppression before VAD and STT. *RNNoise, as a library.*
- **H2** End-of-turn detection, so it neither cuts you off nor waits too long. *smart-turn, as a library.*
- **H3** Low-confidence handling: ask a short question instead of acting, and read back names and numbers before risky actions. *Write.*
- **H4** Robust microphone handling: device changes, level normalisation, a spoken message when the mic is lost. *Write.*
- **H5** Test rig: self-trigger test and a noisy-audio set with word-error-rate. *Write.*
- **Exists:** wake word, Silero VAD, mlx-whisper with vocabulary prompt, Whisper false-output filter, voiceprint, push-to-talk.

### Control any app
- **C1** Accessibility snapshot of an app with numbered references, and actions on those references. *Big take A.*
- **C2** Re-read after every action and report failures plainly. Never claim success. *Write.*
- **C3** `ui_snapshot` and `ui_act` tools in the daemon, with stale-reference and ambiguous-target errors the model can recover from. *Write.*
- **C4** Make accessibility control the first choice in computer use, with screenshots as the fallback. *Write.*
- **Exists:** app, window, file, shell, media tools; computer use; screen description; code-editor reader and writer (commit pending).

### Memory
- **M1** New facts are compared with similar stored ones, then added, updated or deleted, so there are no duplicates. *Write (idea from mem0).*
- **M2** Better retrieval than keywords (small local embedding model), plus a small always-in-prompt core. *Write.*
- **M3** Memory viewer in the Shell: see, correct and delete what it knows. *Write.*
- **M4** Search past conversations. *Write.*
- **Exists:** SQLite fact store, extractor, writing-style cards, forget commands.

### Talking
- **T1** Hold-a-key dictation into any app, with filler-word removal. *Write.*
- **T2** Conversation-aware follow-ups, using a short rolling window with clear privacy limits. *Write.*
- **T3** A "judge intent and risk first" step in the message drafter. *Write.*
- **T4** A spoken "on it" before slow tools. *Write.*

### Range
- **R1** MCP client: servers from `~/.jarvis/mcp.json`, tools registered like any other, default risk "confirm", gated by S1. Playwright MCP is the first one to try. *Official MCP Python SDK as a library.*
- **R2** Optional: camera `look` tool (on request only, indicator on, no frames stored). *Write.*
- **R3** Optional: rich answer cards (images, tables). *Write.*

### Speed
- **P1** Baseline: time per stage (wake, VAD, STT, router, model, TTS start), idle CPU and memory. *Write.*
- **P2** Targeted fixes only where P1 shows the time goes (faster STT, a local model for routing, partial transcripts). *Decide after P1.*

## Phases

Each item is done with unit tests, `ruff`, `swift test` when the Shell changes, and a pass of the relevant section of [E2E_CHECKLIST.md](E2E_CHECKLIST.md) on a real Mac. One small commit per item.

| Phase | Work | Size |
|---|---|---|
| **0. Groundwork** | Commit the editor work once tests pass. Add `LICENSE`, `NOTICE`, `THIRD_PARTY.md`. Build P1 baseline and the H5 test rig. | Small |
| **1. Safety** | S1, S3, S7, then S4, S5, S6. S2 arrives with phase 3 because it ships with big take A. | Medium |
| **2. Voice** | V1 spike first. Then V2, V3, V4, V5, H1, H2, H3, H4. Big take B only if the spike fails. | Medium-large |
| **3. Control any app** | Big take A (C1 and S2), then C2, C3, C4. | Large |
| **4. Memory** | M1, M2, M3, M4. | Medium |
| **5. Speed** | P2, only for what P1 shows is slow. | Small-medium |
| **6. Range** | R1 first. R2 and R3 only if wanted. | Medium |
| **7. Talking polish** | T1, T2, T3, T4. | Small |

Order: 0, 1, 2, 3, 4, 5, 6, 7. Safety comes before more power. Voice comes before features because they all depend on a trustworthy voice loop. Phases 2 and 3 touch different files (Shell audio versus accessibility and tools), so they can run in two sessions in parallel if wanted.

## Progress (2026-11-11)

Nothing is committed. 816 Python tests and 19 Swift tests pass, `ruff` is clean, and the Shell builds. **None of it has been
run on a real Mac with a real microphone, speakers or app**: it is tested with fakes and simulated rooms.

No third-party code was copied, so `THIRD_PARTY.md` is still empty. The app-control rules and the echo canceller are
our own implementations.

| Item | State |
|---|---|
| Licence files (`LICENSE`, `NOTICE`, `THIRD_PARTY.md`) | Done |
| S1 one gate, S3 fetch guard, S4 secret masking, S5 previews, S7 capability switches | Done, tested |
| S2 per-app rules (`safety.app_rules`) | Done, tested, for the new UI tools and for dictation |
| S6 undo | Built, tested: file changes are logged and `undo_last_change` reverses the last one |
| P1 baseline: `jarvisd stats` | Built and tested; **the numbers only exist once you have used Jarvis** |
| V1 echo cancellation | Built, 28 to 39 dB on simulated rooms, **off by default** until checked on your Mac |
| V2 combined barge-in gate | Built: on speakers, interruption is trusted only once the canceller reports enough echo removed |
| V3 route-aware, V4 echo text filter, V5 music ducking | Already existed |
| H1 noise suppression | Not built: Whisper copes with noise, and without measurements a denoiser could make things worse |
| H2 stopped mid-sentence | Built, tested: waits 1.6 s for the rest of "open the ..." |
| H3 unsure what it heard | Built, tested: "I think you said ... is that right?" |
| H4 microphone handling | Already existed (dead-mic reopen) |
| H5 audio test rig | Simulated rooms in the tests only; a real playback-and-record rig is not built |
| C1 to C3 control any app (`ui_look`, `ui_do`) | Built, tested against a fake Mac; **never run on a real app** |
| C4 computer use prefers the accessibility tree | Already true (it reads the tree and prefers element ids). Gap closed: it now obeys `safety.app_rules` too |
| M1 facts update instead of piling up, M2 better retrieval, M4 conversation search | Built, tested |
| M3 memory viewer in the Shell | Built (`MemoryWindow.swift`); compiles, **not tried on a real Mac** |
| Embedding-based retrieval | Tried and rejected: Apple's on-device sentence embedding could not tell a birthday fact from "I live in Bengaluru", and LLM write-time expansion netted zero on held-out questions. Search instead got stemming, a general synonym lexicon and word pairings learned from the facts: recall@3 62% to 86% on 50 questions, 56% to 69% on 16 held-out ones, still offline (`scripts/eval_memory.py`) |
| R1 MCP client | Built, tested against a real MCP server over stdio; no real-world server tried |
| R2 camera | Built (`Camera.swift`, `camera_tools.py`); **not tried on a real Mac** |
| R3 rich cards | Built: `jarvis_core/cards.py` (schema, sanitiser, builders), `CardView.swift`, `CardData.swift`; tools that emit cards: email, messages, drafts, weather, markets, news, calendar, files, web search, memory, battery, system, now playing, calculate, timers, reminders, moves and trash, commands, confirmations, errors. Tested and rendered in snapshots; **not tried live yet** |
| T1 dictation (Ctrl+Option+Space) | Built and tested in Python; Shell compiles; **not tried on a real Mac** |
| T2 conversation-aware follow-ups | Already existed (conversation persists, follow-up windows) |
| T3 judge-first drafting | Built, tested |
| T4 spoken "on it" | Already existed |
| P2 speed fixes (local model, faster STT) | Not started: needs the P1 numbers first |
| Apple voice-processing spike | Not done: needs a real Mac |

## Risks

- **Echo cancellation** may not reach speaker-level interruption quality. The fallback is what we have today: barge-in on headphones, wake-word interrupt on speakers.
- **Accessibility trees** are poor in some apps (games, canvas apps, remote desktops). Those use the screenshot fallback.
- **Copying code** (A and B) needs the notices kept and the files checked for hidden dependencies.
- **Privacy filter** can miss things. S4 reduces exposure to NIM, it doesn't remove it.
