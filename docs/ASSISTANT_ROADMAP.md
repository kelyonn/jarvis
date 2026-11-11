# Personal assistant roadmap

Supersedes [BORROW_LIST.md](BORROW_LIST.md). Star counts and licences were checked on GitHub on 2026-11-11.

## The requirement

A personal assistant that is:

1. **Fast and quick.**
2. **Good at talking.**
3. **Good at remembering.**
4. **Able to do tasks and control the laptop, in any app, safely and correctly.**
5. **Wide in range.**

It does not need automation (scheduled jobs, workflows, channels, proactive alerts). It must not mess things up.

Decisions already made:

- No Claude as the brain (token cost). NIM stays unless a local model is added.
- Reference repos are cloned only to read. Copy code only from permissive licences (MIT, Apache-2.0, BSD).
- "Not automated" means we stop extending briefings and alerts. They stay as they are.

---

## 1. Fast

| # | Item | Why | Take from | Licence | Effort |
|---|---|---|---|---|---|
| 1.1 | Local LLM option (MLX) for the fast path: intent routing, short answers, offline | Removes the network round trip for most turns. Already on our roadmap | [mlx-lm](https://github.com/ml-explore/mlx-lm) 7.6k, [vllm-mlx](https://github.com/waybarrios/vllm-mlx) 1.7k, Osaurus model handling | MIT, Apache-2.0, MIT | Medium |
| 1.2 | Faster, lighter speech-to-text on Apple Silicon (Parakeet or WhisperKit on the Neural Engine) | Cuts our 10% idle CPU and 3.3 GB memory, and lowers latency | [FluidAudio](https://github.com/FluidInference/FluidAudio) 3.3k, [argmax-oss-swift / WhisperKit](https://github.com/argmaxinc/argmax-oss-swift) 6.5k, [mlx-audio](https://github.com/Neywa-Labs/mlx-audio) 8.2k | Apache-2.0, MIT, MIT | Medium |
| 1.3 | Better voice activity detection and turn-end detection (stop waiting too long after you finish) | Biggest felt-latency win | [TEN VAD](https://github.com/TEN-framework/ten-vad) 2.3k (licence "other": read first), [Pipecat](https://github.com/pipecat-ai/pipecat) 17k smart-turn ideas | BSD-2 (Pipecat) | Small |
| 1.4 | Stream everything: start speaking on the first sentence, run tools while talking, short acknowledgement ("on it") before slow tools | Feels quick even when the task is slow | Pipecat and [LiveKit Agents](https://github.com/livekit/agents) 15k patterns | BSD-2, Apache-2.0 | Small |
| 1.5 | Lower idle cost | Under 3% CPU and 3 GB, our own roadmap target | Items 1.2 and 1.3 | | |

## 2. Good at talking

| # | Item | Why | Take from | Licence | Effort |
|---|---|---|---|---|---|
| 2.1 | Conversation-aware follow-ups ("what do you think?" uses what was just said) | Feels like a person, not a command line | Idea only from [isair/jarvis](https://github.com/isair/jarvis) | custom, no code | Medium |
| 2.2 | Hold-a-key dictation into any app, with filler-word removal | Daily-use feature. We already have the hotkey and Whisper | [akshayaggarwal99/jarvis-ai-assistant](https://github.com/akshayaggarwal99/jarvis-ai-assistant) 673, [OpenWhisper](https://github.com/Knuckles92/OpenWhisper) | MIT, MIT | Small |
| 2.3 | Better interruption handling (a spoken "stop" is not mistaken for our own echo) | Known problem in other projects too | Pipecat, LiveKit patterns | permissive | Small |
| 2.4 | Voice quality options | Optional | [mlx-audio](https://github.com/Neywa-Labs/mlx-audio) TTS models, [Kokoro](https://github.com/hexgrad/kokoro) | MIT, Apache-2.0 | Small |
| 2.5 | A "judge first" pass for replies: work out intent and risk before drafting | Smarter message drafts | Idea from [jev-chat](https://github.com/jev-chat/jev-chat-jarvis) 8.3k | MIT | Small |

## 3. Good at remembering

| # | Item | Why | Take from | Licence | Effort |
|---|---|---|---|---|---|
| 3.1 | Smarter memory updates: when you say something new, add, update or delete the right fact instead of piling up duplicates | Keeps memory accurate | [mem0](https://github.com/mem0ai/mem0) 68.8k (extraction and update logic) | Apache-2.0 | Medium |
| 3.2 | Layered memory that stays small: core facts always in context, details retrieved on demand | Fast and cheap prompts | [Letta](https://github.com/letta-ai/letta) 25k memory-block pattern, [Osaurus](https://github.com/osaurus-ai/osaurus) memory docs | Apache-2.0, MIT | Medium |
| 3.3 | Memory viewer and editor in the Shell (see, correct and delete what it knows) | Trust. We only have a CLI today | Idea from isair; build our own | | Medium |
| 3.4 | Search past conversations ("what did I ask about last week?") | Real recall | mem0 or [Cognee](https://github.com/topoteretes/cognee) 33k patterns | Apache-2.0 | Medium |
| 3.5 | Knowledge graph (people, projects, links) | Only if 3.1-3.4 are not enough. Needs a graph database | [Graphiti](https://github.com/getzep/graphiti) 32.7k | Apache-2.0 | Large, later |

## 4. Does tasks and controls the laptop, in any app

| # | Item | Why | Take from | Licence | Effort |
|---|---|---|---|---|---|
| 4.1 | Accessibility-tree control as the main way to use any app: read the app's UI as structured data, act on numbered references, and re-read after each action | More reliable and faster than screenshots. Handles stale references and ambiguous targets | [agent-desktop](https://github.com/vercel-labs/agent-desktop) 21.6k | Apache-2.0 | Medium-large |
| 4.2 | Vision fallback for apps with a poor accessibility tree | Covers games, canvas apps, remote desktops | [UI-TARS-desktop](https://github.com/bytedance/UI-TARS-desktop) 39.6k, [Agent-S](https://github.com/simular-ai/Agent-S) 12.9k, [OmniParser](https://github.com/microsoft/OmniParser) 25.6k (CC-BY-4.0: check model terms) | Apache-2.0, Apache-2.0, CC-BY-4.0 | Medium |
| 4.3 | Real browser control with your logged-in sessions | Web tasks in sites you are already signed into | [browser-use](https://github.com/browser-use/browser-use) 120k, [playwright-mcp](https://github.com/microsoft/playwright-mcp) 39k, [nanobrowser](https://github.com/nanobrowser/nanobrowser) 14k | MIT, Apache-2.0, Apache-2.0 | Medium |
| 4.4 | Native Apple app tools (Calendar, Reminders, Notes, Mail drafts, Messages read, Maps, Music, Shortcuts) per agent | Mostly done in our tools. Check gaps | [Osaurus](https://github.com/osaurus-ai/osaurus) 8.2k | MIT | Small |
| 4.5 | Task verification: after each step, check the result before moving on, and say plainly if it failed | "Without messing me up" | agent-desktop's snapshot, act, observe loop | Apache-2.0 | Small |
| 4.6 | Code-editor reader and writer (LeetCode, Monaco, CodeMirror, desktop editors) | Already built, uncommitted (`vision/editor.py`) | ours | | Done, commit it |

## 5. Safe

| # | Item | Why | Take from | Licence | Effort |
|---|---|---|---|---|---|
| 5.1 | Per-app autonomy rules: for each app, "ask every time", "ask for risky only" or "never" | The clearest model for control that doesn't mess you up | [Osaurus](https://github.com/osaurus-ai/osaurus) computer-use docs | MIT | Medium |
| 5.2 | Privacy filter before anything goes to NIM: replace names, emails, keys and numbers with placeholders, restore them after | Our model is remote, so this is a real gap | Osaurus privacy filter design | MIT | Medium |
| 5.3 | Approval card before any change to settings or files, showing exactly what will happen | No surprise actions | Osaurus | MIT | Small |
| 5.4 | Central pre-tool hook that applies policy to every call, with switches to turn whole capabilities off (camera, browser, shell) | One place to reason about safety | [adewaskar/jarvis](https://github.com/adewaskar/jarvis) `bridge/policy.mjs` | MIT | Small |
| 5.5 | SSRF guard and path confinement: refuse loopback, LAN and cloud-metadata addresses, and credential folders (`~/.ssh`, `.env`) | Needed once MCP and browsers are added | adewaskar `bridge/net.mjs` | MIT | Small |
| 5.6 | Undo and trash, not delete: prefer reversible actions, keep a log of what changed | Recoverable mistakes | our policy layer plus ideas above | | Small |
| 5.7 | Sandbox for running code (VM or container) | Run risky code away from your files | [cua](https://github.com/trycua/cua) 32k, Osaurus sandbox (macOS 26+ only) | MIT, MIT | Large, later |

## 6. Wide range

| # | Item | Why | Take from | Licence | Effort |
|---|---|---|---|---|---|
| 6.1 | MCP passthrough: any MCP server's tools join the agent, each with a risk level, default "confirm" | One config line adds image generation, search, phone control and more | adewaskar bridge, [MCP servers](https://github.com/modelcontextprotocol/servers) 92k (licence varies per server) | MIT | Medium |
| 6.2 | Skills as installable packages in the agentskills.io format | Share and add abilities without Python | [OpenJarvis](https://github.com/open-jarvis/OpenJarvis) 14.3k, Osaurus skills docs | Apache-2.0, MIT | Medium |
| 6.3 | Camera `look` tool: "what's in my hand", "read this label" | Adds a sense we lack | adewaskar `bridge/vision.mjs` | MIT | Small-medium |
| 6.4 | Hand gestures (pinch to click, move the chat card in the air) | Nice, not essential. Use Apple's Vision hand-pose, not MediaPipe | adewaskar `src/lib/hands.ts`, `oneEuro.ts` | MIT | Medium |
| 6.5 | Spoken UI control and rich answer cards (images, tables, video) | Better answers than plain text | adewaskar `bridge/ui.mjs`, `panels.mjs`, `sanitise.ts` | MIT | Medium |
| 6.6 | Settings window instead of editing `config.toml` | Easier day to day | Idea from isair and Osaurus | | Medium |

---

## 7. Not tripping over its own voice

**What we have today** (from our code and git history):
- An echo filter that drops transcripts that look like its own speech or repeated garbage.
- A voiceprint check, so it acts only on your voice once enrolled.
- Talking over it on speakers is **off by default** (`barge_in_speakers`), because a voiceprint alone did not stop it hearing itself.
- The wake word can interrupt while it is thinking, and a cancelled reply is recorded as interrupted.
- The Shell deliberately avoids Apple's voice-processing audio unit, because that unit ducks and degrades all other audio system-wide.

**The gap:** there is no real echo cancellation, so on speakers it can't be interrupted by voice and can still mishear itself.

| # | Item | Why | Take from | Licence | Effort |
|---|---|---|---|---|---|
| 7.1 | Software echo cancellation: we know exactly what we play, so subtract it from the mic signal before wake word, VAD and STT | Fixes the cause, not the symptoms. Lets barge-in work on speakers | [thewh1teagle/aec](https://github.com/thewh1teagle/aec) (speexdsp, Rust), [pyaec](https://github.com/ewan-xu/pyaec), or WebRTC audio processing (BSD-3) | MIT, Apache-2.0, BSD-3 | Medium-large |
| 7.2 | Test Apple's voice-processing unit again with its ducking turned down to minimum, and compare with 7.1 | Cheapest route if the ducking problem is solvable. Check the API on this macOS before committing | Apple AVAudioEngine docs | n/a | Small test |
| 7.3 | Combine signals before interrupting: echo-cancelled audio + your voiceprint + energy above the echo floor | Each alone failed before. Together they should be safe | ours | | Small |
| 7.4 | Know the output route: free barge-in on headphones, strict on speakers, re-check when the route changes (AirPods connect, display speakers) | Right behaviour per setup | CoreAudio route notifications | n/a | Small |
| 7.5 | Compare transcripts against the exact text it just spoke (fuzzy match), not just generic patterns | Stronger than the generic echo filter | ours | | Small |
| 7.6 | Duck or pause music (Spotify, Music) while listening and speaking, and restore after | Music is the other thing it trips over | ours (we control media already) | | Small |
| 7.7 | A recorded-audio test rig: play known speech through the speakers, record the mic, and check it never triggers itself | Without this, echo fixes can't be proved | ours | | Medium |

## 8. Good voice intake

**What we have today:** Silero VAD, openWakeWord, mlx-whisper with a prompt of your names and apps, a filter for Whisper's known false outputs ("thank you for watching"), a voiceprint, and an Option+Space push-to-talk fallback.

**The gap:** no noise suppression, no end-of-turn model, no confidence handling, and no streaming transcripts.

| # | Item | Why | Take from | Licence | Effort |
|---|---|---|---|---|---|
| 8.1 | Noise suppression before VAD and STT (fans, keyboard, café) | Cleaner audio gives fewer mishears and false wakes | [RNNoise](https://github.com/xiph/rnnoise) 6k (tiny, fast), [ClearerVoice-Studio](https://github.com/modelscope/ClearerVoice-Studio) 4.6k (heavier), [DeepFilterNet](https://github.com/Rikorose/DeepFilterNet) 5k (licence shown as unclear: read before use) | BSD-3, Apache-2.0, unclear | Small-medium |
| 8.2 | An end-of-turn model, so it doesn't cut you off mid-sentence and doesn't wait too long | Fixes both "interrupts me" and "feels slow" | [Pipecat smart-turn](https://github.com/pipecat-ai/smart-turn) 1.7k | BSD-2 | Small-medium |
| 8.3 | More accurate speech recognition: Parakeet or WhisperKit, and vocabulary taken from your real contacts, apps and files instead of a fixed list | Fewer wrong names and apps | [parakeet-mlx](https://github.com/senstella/parakeet-mlx), [WhisperKit](https://github.com/argmaxinc/argmax-oss-swift), [FluidAudio](https://github.com/FluidInference/FluidAudio) | Apache-2.0, MIT, Apache-2.0 | Medium |
| 8.4 | Low-confidence handling: when it isn't sure what you said, ask a short question instead of acting. Always read back names and numbers before risky actions | "Without messing me up", applied to hearing | ours | | Small |
| 8.5 | Streaming partial transcripts, so routing starts before you finish | Faster replies | WhisperKit, Parakeet streaming | permissive | Medium |
| 8.6 | Robust microphone handling: device changes (AirPods switching sample rate), level normalisation, and a clear spoken message when the mic is lost or muted | Common real-world failure | CoreAudio | n/a | Small |
| 8.7 | Only your voice when others are talking: better speaker verification | Shared rooms | [pyannote](https://github.com/pyannote/pyannote-audio) 10.8k (heavy, optional) | MIT | Medium, optional |
| 8.8 | A noisy-audio test set with word-error-rate measurement | So every change in this area is measured, not guessed | ours | | Medium |

---

## Explicitly not taking

- Claude or any paid-token brain (your decision).
- Automations, schedules, workflows, channels (Slack, Discord, Telegram), agent identity, device sync (Osaurus extras).
- Full-duplex speech models ([Moshi](https://github.com/kyutai-labs/moshi) 11.5k): too heavy for a laptop that also does tasks.
- Knowledge graph, unless memory proves too weak.

## Licences: what we cannot copy

| Project | Licence | Use |
|---|---|---|
| [Priler/jarvis](https://github.com/Priler/jarvis) | CC BY-NC-SA | Ideas only |
| [isair/jarvis](https://github.com/isair/jarvis), [ethanplusai/jarvis](https://github.com/ethanplusai/jarvis) | custom | Ideas only |
| [cherry-studio](https://github.com/CherryHQ/cherry-studio) | AGPL-3.0 | Do not copy |
| [AnubhavChaturvedi jarvis](https://github.com/AnubhavChaturvedi-GitHub/jarvis-ai-assistant), [agenticSeek](https://github.com/Fosowl/agenticSeek) | GPL-3.0 | Do not copy |
| [OmniParser](https://github.com/microsoft/OmniParser) | CC-BY-4.0, models vary | Read the model licences first |
| [microsoft/JARVIS](https://github.com/microsoft/JARVIS) | MIT | Research system, not useful |

MIT, Apache-2.0 and BSD code can be reused with the licence notice kept. Keep a `THIRD_PARTY.md` of anything copied.

## Suggested order

1. **Safety base:** 5.4, 5.5, 5.2, 5.1. Do this before giving the assistant more power.
2. **Voice reliability (hearing and not hearing itself):** 7.2 test first, then 7.1, 7.3, 7.4, 8.1, 8.2, with the test rigs 7.7 and 8.8 built alongside. This comes early because every other feature depends on the voice loop being trustworthy.
3. **Control that works on any app:** 4.1, 4.5, then 4.3, 4.2.
4. **Remembering:** 3.1, 3.3, 3.2.
5. **Feels fast:** 1.3 (shared with 8.2), 1.4, 1.2, 8.5, then 1.1.
6. **Talking polish:** 2.2, 2.1, 2.3, 8.4.
7. **Range:** 6.1, 6.2, then 6.5, 6.3.
8. **Later or optional:** 6.4, 5.7, 3.5, 6.6, 8.7.
