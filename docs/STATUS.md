# Status

What is built, what has been checked and how, and what is still open. Updated as features land.

Status words used below:

- **Verified**: covered by automated tests and used on a real Mac.
- **Tested**: covered by automated tests; live use has been limited.
- **Built**: implemented, but not yet checked end to end.
- **Gap**: known not to work, or not built.

## Areas

| Area | Scope | Status |
|---|---|---|
| Core | Config, Keychain secrets, interfaces, adapters, `jarvisd text` | Verified |
| Voice loop | Wake word, VAD, Whisper STT, streamed TTS, follow-up window, barge-in | Verified |
| Shell | IPC, audio, orb and chat card, hotkeys, menu bar, login item, supervisor, sleep/wake | Verified |
| Brain | Intent router, tool agent, risk levels, spoken confirmation, audit log, per-turn reasoning modes | Verified |
| Mac control | Apps, windows, media, display, power, network, files, notes, reminders, terminal | Verified |
| Shortcuts | Focus, Night Shift, True Tone, Stage Manager; builds missing shortcuts on request | Verified |
| Timers | Wall-clock timers and alarms that survive sleep and restarts, with backup Reminders | Verified |
| Briefings | Collectors, composer, scheduled slots, deltas, offline template fallback | Tested |
| Alerts | Meetings, VIP messages, markets, rain, news, battery; held during calls and Focus | Tested |
| Messaging | WhatsApp, iMessage, Gmail, LinkedIn readers; drafts that are never sent | Tested |
| Memory | Facts, people, writing-style cards, forget | Tested |
| Vision | Describe the screen, explain what is on it | Built |
| Computer use | Narrated multi-step control with a kill switch | Built |
| Setup | Doctor, setup wizard, install script | Verified |

## Checked so far

- Python suite (576 tests including integration), ruff clean, Swift build and 19 Swift tests.
- Tool calls on a real Mac: time, battery, system status, volume, timers, reminders, notes, web search, open and quit
  apps, URLs, Spotify control, weather, markets, calendar, news, briefings, dark mode, Wi-Fi, windows, screenshots.
- Multi-step turns: closing and opening several apps at once, finding a file and reporting it, doing arithmetic and
  then setting a timer of that length, comparing two cities' weather, remembering and recalling a fact.
- Confirmation policy: destructive requests ask first, and "no" leaves things alone.
- Prompt injection: a file containing instructions to run commands was summarised and nothing ran.
- Timers and reminders that end while Jarvis is busy are queued, shown as a notification, spoken at the first gap and
  kept across restarts.
- Call detection ignores macOS's own audio services (CoreSpeech, historicalaudiod), so announcements are not held back
  by them.
- Config hot reload, log rotation, transcript retention, and an audit log that stores only a hash for message and
  clipboard content.

## Known gaps

- **Idle cost.** About 10 percent of one CPU core and 3.3 GB of memory while idle. The wake-word model and the local
  speech models dominate. Targets are under 3 percent and under 3 GB.
- **Clock app timers.** macOS does not let other apps or imported shortcuts set a Clock timer's length. Jarvis keeps
  its own timers instead.
- **Low Power Mode.** Not settable from Shortcuts on a Mac.
- **Custom Focus modes.** Jarvis can build shortcuts for Apple's built-in Focus modes only.
- **Computer use** has not been run end to end on a real task.
- **IPC robustness** under repeated Shell disconnects is seen working in practice but not tested in isolation.

## Retest rule

After any change: run `pytest` and `swift test`, then a few commands in text mode, then restart Jarvis and do one
voice turn (wake, command, interrupt). Anything touching audio, the Shell or permissions also needs the voice checks
in [E2E_CHECKLIST.md](E2E_CHECKLIST.md).
