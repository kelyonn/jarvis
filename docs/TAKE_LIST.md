# What we can take

Trimmed from [ASSISTANT_ROADMAP.md](ASSISTANT_ROADMAP.md). Only things with an MIT, Apache-2.0 or BSD licence, which can be used without asking or telling the authors. The one obligation is keeping their copyright and licence notice with anything copied (record it in `THIRD_PARTY.md`). Checked on GitHub 2026-11-11.

## How we take things

Decision: write our own by default. For each item we read the source only to understand the approach, then design and write our own implementation for our codebase. Libraries (RNNoise, mem0, smart-turn, echo-cancellation crates, WhisperKit and similar) are used as packages, never copied in. The owner allows **one or two big code takes** when needed, each recorded in `THIRD_PARTY.md` with its notice kept. The chosen ones are in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Take (code or models)

| # | What | From | Licence | Gives us |
|---|---|---|---|---|
| 1 | Policy hook, SSRF guard, path confinement, capability switches | [adewaskar/jarvis](https://github.com/adewaskar/jarvis) `bridge/policy.mjs`, `net.mjs` | MIT | Safe tool calls |
| 2 | Per-app autonomy rules, approval cards, privacy filter (redact before sending to NIM) | [osaurus-ai/osaurus](https://github.com/osaurus-ai/osaurus) | MIT | Safe control |
| 3 | Accessibility-tree control with numbered refs and re-check after every action | [vercel-labs/agent-desktop](https://github.com/vercel-labs/agent-desktop) | Apache-2.0 | Control any app reliably |
| 4 | Vision fallback for apps with no accessibility tree | [bytedance/UI-TARS-desktop](https://github.com/bytedance/UI-TARS-desktop), [simular-ai/Agent-S](https://github.com/simular-ai/Agent-S) | Apache-2.0 | Control any app |
| 5 | Real logged-in browser control | [browser-use](https://github.com/browser-use/browser-use) (MIT), [playwright-mcp](https://github.com/microsoft/playwright-mcp) (Apache-2.0) | MIT / Apache-2.0 | Web tasks |
| 6 | MCP client, so any MCP server's tools join the agent | adewaskar bridge | MIT | Wide range |
| 7 | Installable skills (agentskills.io format) | [OpenJarvis](https://github.com/open-jarvis/OpenJarvis) | Apache-2.0 | Wide range |
| 8 | Camera `look` tool, hand cursor smoothing | adewaskar `vision.mjs`, `oneEuro.ts` | MIT | Eyes, gestures |
| 9 | Spoken UI control, HUD panels, HTML sanitiser | adewaskar `ui.mjs`, `panels.mjs`, `sanitise.ts` | MIT | Better answers |
| 10 | Memory add/update/delete logic | [mem0](https://github.com/mem0ai/mem0) | Apache-2.0 | Accurate memory |
| 11 | Layered memory (small core plus retrieval) | [Letta](https://github.com/letta-ai/letta), Osaurus | Apache-2.0, MIT | Fast, small prompts |
| 12 | Echo cancellation | [thewh1teagle/aec](https://github.com/thewh1teagle/aec), [pyaec](https://github.com/ewan-xu/pyaec) | MIT, Apache-2.0 | No self-interruption |
| 13 | Noise suppression | [RNNoise](https://github.com/xiph/rnnoise) (BSD-3), [ClearerVoice-Studio](https://github.com/modelscope/ClearerVoice-Studio) (Apache-2.0) | BSD-3, Apache-2.0 | Clean voice intake |
| 14 | End-of-turn detection | [pipecat-ai/smart-turn](https://github.com/pipecat-ai/smart-turn) | BSD-2 | No cut-offs, less waiting |
| 15 | Faster speech recognition on Apple Silicon | [parakeet-mlx](https://github.com/senstella/parakeet-mlx), [FluidAudio](https://github.com/FluidInference/FluidAudio) (Apache-2.0), [WhisperKit](https://github.com/argmaxinc/argmax-oss-swift) (MIT) | Apache-2.0, MIT | Speed, lower idle cost |
| 16 | Local model on the Mac for the fast path | [mlx-lm](https://github.com/ml-explore/mlx-lm), [vllm-mlx](https://github.com/waybarrios/vllm-mlx) | MIT, Apache-2.0 | Speed, offline |
| 17 | Streaming voice patterns (speak first sentence while tools run) | [Pipecat](https://github.com/pipecat-ai/pipecat) (BSD-2), [LiveKit Agents](https://github.com/livekit/agents) (Apache-2.0) | BSD-2, Apache-2.0 | Speed |
| 18 | Hold-a-key dictation with filler removal | [akshayaggarwal99/jarvis-ai-assistant](https://github.com/akshayaggarwal99/jarvis-ai-assistant), [OpenWhisper](https://github.com/Knuckles92/OpenWhisper) | MIT | Daily-use dictation |
| 19 | "Judge intent and risk before drafting" step | [jev-chat](https://github.com/jev-chat/jev-chat-jarvis) | MIT | Smarter reply drafts |

## Build ourselves (no one to take from, small)

Test Apple voice processing with minimal ducking; combine echo, voiceprint and energy before interrupting; route-aware barge-in; match transcripts to what we just said; duck music while listening; low-confidence "did you mean" handling; memory viewer; undo and trash; echo and noise test rigs.

## Dropped

- **Cannot copy:** isair/jarvis and ethanplusai/jarvis (custom), Priler/jarvis (non-commercial), cherry-studio (AGPL), the GPL-3.0 projects.
- **Licence unclear, skip until read:** DeepFilterNet, TEN VAD, OmniParser model weights, individual MCP reference servers.
- **Not wanted:** Claude as the brain, automations, channels, sync, knowledge graph, full-duplex speech models, sandbox VM (later, if ever).
