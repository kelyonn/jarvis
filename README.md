# Jarvis

A voice assistant for macOS that runs all day on your own Mac. Say "Hey Jarvis", ask for something, and it does it:
opens and arranges apps, plays music, sets timers, reads your messages, drafts replies, briefs you on your day, and
tells you when something needs your attention.

Speech recognition, the wake word and the voice all run locally. Only the language model is remote, through
NVIDIA NIM's OpenAI-compatible API. Jarvis never sends a message on your behalf; it drafts, and you press send.

## How it works

Jarvis is two processes that talk over a local WebSocket.

```
 JarvisShell (Swift)                          jarvis_core (Python)
 menu-bar app                                 daemon
 ------------------                           ---------------------------------------------
 microphone and speaker      <-- audio -->    wake word -> VAD -> Whisper -> router / agent
 orb and chat card           <-- events -->   tools (124) -> Mac, Shortcuts, Google, web
 hotkeys, menu, login item                    briefings, alerts, memory, timers
 sleep / wake / lock                          Kokoro TTS -> streamed back to the Shell
 accessibility, screenshots  <-- requests --  audit log, Keychain secrets, ~/.jarvis
```

- **JarvisShell** owns everything that needs a signed macOS app: the microphone, playback, the floating orb and chat
  card, global hotkeys, the menu-bar item, accessibility and screen capture, and starting and supervising the daemon.
- **jarvis_core** does the rest. Each request first goes through a fast pattern router (`intents.yaml`), which answers
  common commands with no model call at all. Anything else goes to a tool-calling agent that picks how hard to think
  about the request, calls tools, asks for spoken confirmation on risky ones, and streams its reply to the speaker
  sentence by sentence.

| Layer | What it uses |
|---|---|
| Wake word | openWakeWord, "Hey Jarvis" |
| Voice activity | Silero VAD |
| Speech to text | mlx-whisper, `whisper-large-v3-turbo`, on Apple Silicon |
| Language model | NVIDIA NIM (Nemotron by default), with fallback models |
| Text to speech | Kokoro, British male voice |
| Speaker check | Local voiceprint, so it only acts on your voice once enrolled |

## What it can do

**Talk to it however suits you.** Say "Hey Jarvis", press Option+Space, or press Option+Shift+Space and type. You
can interrupt it mid-sentence by voice. After a reply it keeps listening for a few seconds, so follow-ups need no wake
word.

**Control the Mac.**
- Open, focus, quit and force-quit apps; open sites, files and settings panes.
- Move, snap, resize, minimise and full-screen windows; arrange several apps at once.
- Volume, brightness, dark mode, lock, sleep, display off, keep awake.
- Wi-Fi, Bluetooth, and connecting paired devices such as AirPods.
- Focus (Do Not Disturb), Night Shift, True Tone and Stage Manager, through Shortcuts it builds for you.
- Files: find, read, write, move, copy, reveal in Finder, move to Trash.
- Browser tabs: list, switch, close, search.
- Run terminal commands, with an allow-list and confirmation for anything risky.

**Keep track of time.**
- Timers and alarms that survive sleep and restarts. Ask how long is left, change one, cancel one.
- Timers of 90 seconds or more also become a Reminders alarm, so they ring on your phone too.
- Reminders, notes and Calendar events, plus Google Calendar if you sign in.

**Media.** Play, pause and skip in Spotify or Music. With a Spotify sign-in it plays your own playlists and liked
songs by name.

**Briefings.** A spoken morning brief on your first unlock, and shorter updates at times you choose: calendar,
weather with UV and air quality, markets on your watch list, news, unread messages. Ask "brief me" any time for just
what changed. With no network it falls back to a template brief.

**Alerts.** Upcoming meetings, messages from VIP contacts, market moves past your thresholds, rain, breaking news and
low battery. Alerts are held while you are on a call, sharing your screen or in Focus, and read back when you ask
"what did I miss".

**Messages.** Reads WhatsApp, iMessage, Gmail and LinkedIn. Drafts replies in your own writing style, learned from
your sent messages, and places them in the compose box. It never presses send.

**Memory.** Remembers facts you tell it ("my brother is Arjun") and things it learns about people you talk to. You
can list everything it knows and make it forget a person or everything.

**The screen.** Describes what is on screen or explains an error you are looking at. A narrated computer-use mode can
carry out multi-step tasks, and stops the moment you move the mouse, press the hotkey or say "stop".

**Answers.** Web search and page reading, exact arithmetic, weather and markets. Harder questions (comparisons, plans,
debugging, maths) get a private first pass of reasoning before the answer.

## What it cannot do

- **Send anything.** By design, there is no send action for any messaging app.
- **Run without the network.** Speech is local, but understanding requests needs the NIM API. Without it Jarvis can
  still speak queued timers and give the template brief.
- **Run on Intel Macs or other platforms.** It needs macOS 14 or later on Apple Silicon.
- **Set Clock app timers.** macOS blocks other apps and imported shortcuts from setting a Clock timer's length, so
  Jarvis keeps its own timers instead.
- **Toggle Low Power Mode, or your custom Focus modes.** Shortcuts cannot do the first on a Mac, and custom Focus
  modes have per-Mac identifiers Jarvis cannot know. Make the second yourself in the Shortcuts app as
  `Set Focus <Name>` and Jarvis will use it.
- **Wake on any phrase you like.** The wake word model only knows "Hey Jarvis".
- **Idle cheaply, yet.** It uses about 10 percent of one core and 3.3 GB of memory while idle, mostly the speech
  models.

## Requirements

- macOS 14 or later on Apple Silicon
- Xcode command line tools (to build the Shell), Homebrew, Python 3.12
- An NVIDIA NIM API key (free tier at build.nvidia.com)
- About 4 GB of disk for the speech models
- Optional: Spotify developer credentials, a Google OAuth client for Gmail and Calendar, `blueutil` for Bluetooth

## Install

```bash
./scripts/install.sh      # Python venv, speech models, Homebrew tools, builds Jarvis.app
jarvisd set-key           # store your NIM key in the Keychain
jarvisd setup             # name, permissions, routines, VIPs, briefing times, Google sign-in
jarvisd doctor            # should end with 0 broken
open ~/Applications/Jarvis.app
```

On first launch Jarvis registers itself as a login item, then asks for microphone, accessibility, screen recording
and Full Disk Access (the last is needed to read iMessage, WhatsApp and Focus state).

To make Jarvis act only on your voice, say "Hey Jarvis, learn my voice" and read the six phrases it gives you.

## Everyday commands

| Command | What it does |
|---|---|
| `jarvisd text` | Chat in the terminal, same brain, no voice |
| `jarvisd say "..."` | Speak text in Jarvis's voice |
| `jarvisd brief [--full\|--delta] [--print]` | Run a briefing now |
| `jarvisd doctor [--fix]` | Check permissions, models and connectivity; `--fix` downloads missing models |
| `jarvisd logs [--audit] [-f]` | Show the daemon log or the audit log |
| `jarvisd shortcuts list\|install` | See or build the Shortcuts Jarvis uses |
| `jarvisd vip add "Mom"` | Manage VIP contacts |
| `jarvisd memory` / `jarvisd forget --person Rahul` | See or delete what Jarvis remembers |
| `jarvisd routines list` | List saved routines (apps, layout and Focus in one command) |
| `jarvisd config edit` | Edit `~/.jarvis/config.toml` |
| `jarvisd voice-id enroll\|forget\|status` | Manage voice recognition |
| `jarvisd run --standalone` | Voice loop with no Shell, for debugging |

## Configuration and data

- **Settings** live in `~/.jarvis/config.toml` and reload without a restart. Every option is documented in
  [`config.example.toml`](config.example.toml).
- **Secrets** (NIM key, Spotify and Google tokens) live in the macOS Keychain, never in files.
- **Data** lives in `~/.jarvis/`, readable only by you: memory, timers, the announcement queue, transcripts with a
  retention limit, logs, and the audit log.
- **Shortcuts** that Jarvis builds are written to `~/.jarvis/shortcuts/` and opened in the Shortcuts app for you to
  add, since macOS does not let apps add them silently.

## Safety

- Nothing is ever sent. Drafts go into the compose box and wait for you.
- Destructive or risky actions (deleting, power, emptying the Trash, shell commands outside the allow-list, computer
  use) need a spoken yes.
- Text from web pages, emails, files and messages is treated as data, never as instructions.
- Every tool call is written to `~/.jarvis/audit.log`. Message and clipboard content is stored as a hash, not text.
- Only one daemon can run at a time, and the Shell restarts it with backoff if it crashes.

## Project layout

```
JarvisShell/                Swift menu-bar app
  Sources/JarvisShell/      audio, orb, hotkeys, IPC client, accessibility, screenshots, call detection
  Sources/JarvisShellCore/  pure logic shared with tests: protocol, layout, markdown, supervision
jarvis_core/
  daemon.py, runtime.py     the voice loop, state machine and service wiring
  router.py, intents.yaml   fast pattern routing for common commands
  brain/                    agent loop, reasoning modes, persona, history, display formatting
  skills/                   the 124 tools, grouped by area
  adapters/                 outside-world code: NIM, Google, Apple databases, Shortcuts, web
  voice/                    wake word, VAD, speech to text, text to speech, speaker ID
  briefing/, alerts/        scheduled briefs and alert rules
  comms/                    WhatsApp, iMessage, Gmail, LinkedIn readers and the drafter
  memory/, vision/, policy/ memory store, screen and computer use, safety and presence rules
  ipc/                      WebSocket protocol between the daemon and the Shell
tests/                      unit and integration tests
scripts/                    install and Shell build scripts
docs/                       status and the manual end-to-end checklist
```

## Development

```bash
uv venv --python 3.12 .venv && VIRTUAL_ENV=.venv uv pip install -e ".[dev,voice]"
.venv/bin/python -m pytest          # 576 tests
.venv/bin/python -m ruff check .
cd JarvisShell && swift test        # 19 tests
./scripts/build_shell.sh            # rebuild and sign Jarvis.app
```

[`docs/STATUS.md`](docs/STATUS.md) tracks what has been verified and what is still open.
[`docs/E2E_CHECKLIST.md`](docs/E2E_CHECKLIST.md) is the manual pass to run on a real Mac before trusting a change.

## Roadmap

- **Lighter idle.** A smaller Whisper model and a cheaper wake-word gate, to get under 3 percent CPU and 3 GB.
- **A local model option.** Run the language model on the Mac with MLX, so Jarvis works fully offline.
- **A custom wake word.** Train a verifier so the wake phrase can be changed.
- **Computer use, proven.** Run it end to end on real tasks and tighten the kill switch.
- **More switches.** Extend the Shortcuts builder as macOS exposes more actions to Shortcuts.
- **Smarter alerts.** Learn which alerts you act on and quiet the rest.
- **A phone companion.** Hear briefings and timers on the iPhone when you are away from the Mac.

## Credits

The Calendar and Contacts database readers follow the schema handling in the open-source
[OpenJarvis](https://github.com/open-jarvis/OpenJarvis) connectors (Apache-2.0). Jarvis does not import or install
OpenJarvis.
