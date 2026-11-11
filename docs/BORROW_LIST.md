# Ideas borrowed from adewaskar/jarvis

Reference clone: `../adewaskar` (MIT). Nothing here is built yet. Claude as an alternative brain was considered and
dropped (token cost); NIM stays the only model.

## 1. MCP passthrough
Let the agent call tools from MCP servers (stdio) configured in a Jarvis-owned file, alongside the 124 built-in tools.
Their bridge reads `~/.claude.json` and hands servers to the SDK; ours would read `~/.jarvis/mcp.json` instead and
register each server's tools in the skill registry, with a risk level per tool (default: confirm).
- Use: image generation, phone control (android), Playwright browsing, extra search, anything a community MCP offers.
- Reference: `bridge/server.mjs`, `bridge/policy.mjs` (`decideTool`).

## 2. Camera and hand gestures
- `look` tool: capture one webcam frame on request ("look at me", "what am I holding") and describe it with the vision
  model. No frame stored; camera indicator shown while live; only when asked.
- Hand tracking (MediaPipe): index fingertip is a cursor, thumb-finger pinch is a click, for dragging and resizing
  panels in the air.
- Reference: `src/lib/hands.ts`, `src/lib/camera.ts`, `src/lib/oneEuro.ts` (cursor smoothing), `bridge/vision.mjs`.
- Open question: Shell is Swift, so use Vision framework hand-pose (`VNDetectHumanHandPoseRequest`) instead of MediaPipe.

## 3. Spoken UI control and HUD panels
- Voice-controlled interface: colour, orb style/scale, effects (glitch, pulse, scan), show/hide parts, reset.
  "Make it red", "hide the transcript".
- Model-authored panels (weather card, image, video, list) against a fixed design system, sanitised before render.
- Reference: `bridge/ui.mjs` (`ui_*` tools), `bridge/panels.mjs`, `src/ui/Panels.tsx`, `src/ui/sanitise.ts`.

## 4. Bridge security patterns
- SSRF guard for any fetch of a URL the model chose (refuse loopback, LAN, cloud-metadata addresses, validate scheme).
- Path confinement for file tools (workspace plus allowed roots, credential paths always refused).
- Central pre-tool hook that applies the policy to every call, plus a switch to disable whole capabilities.
- Reference: `bridge/net.mjs`, `bridge/policy.mjs`, tests in `test/policy.test.mjs`.
