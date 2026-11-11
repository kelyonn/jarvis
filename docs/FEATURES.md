# More features

Things added after the README was rewritten. Each one is on by default unless it says otherwise.

## Talking and listening

**Space to interrupt.** While Jarvis talks it does not listen to your voice. Hold Space: it stops at once, listens while
you hold, and acts on what you said when you let go. The Shell only takes the Space key while Jarvis is speaking, so Space
types normally the rest of the time. Set `activation.space_to_interrupt = false` to go back to voice interruption.

**Half-sentences are not requests.** If you stop mid-sentence ("open the ...") Jarvis waits a moment for the rest. If it
still ends on a word that cannot end a sentence ("I'm going to"), Jarvis lets it go instead of answering it.

**Dictation.** Hold Control+Option+Space, speak, let go: the words are typed into the front app, minus the "um"s, with
"comma" and "question mark" turned into punctuation. It never presses Return and never types into password fields.

**Did it hear you?** If the recogniser is unsure what it heard, Jarvis asks "I think you said ... is that right?" before
acting.

**Echo cancellation (optional).** `activation.aec`, off by default, lets you talk over Jarvis on speakers. Check the log
line "aec: removing N dB of echo" on your own Mac before relying on it.

## Answers you can see

**Rich cards.** A reply can carry a card: email, calendar, weather, markets, unread messages, drafts, file results, web
sources, battery, now playing, "Needs your OK" and errors. The spoken line stays short and the card holds the detail, with
buttons that open a link, copy text or say a follow-up for you. Buttons go through the same safety checks as speech.

**Reading email.** `read_email` gets the whole message, not the short inbox preview, and `open_email` opens it in Gmail.

## Control

**Any app, through accessibility.** `ui_look` reads the front app's controls as a short list, and `ui_do` presses a
button, sets a text field or steps a slider by reference. References expire after every look or action, each action is
checked afterwards (it tells you when nothing changed), and `safety.app_rules` sets each app to `auto`, `ask` or `never`.
Terminals and password managers are look-only by default; messaging and mail apps ask first.

**More tools through MCP.** List MCP servers in `~/.jarvis/mcp.json` and their tools join Jarvis's own, gated like any
other (read-only tools run, everything else asks first):

```json
{"servers": {"playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]}}}
```

## Models

**Backup and offline.** If the main online model stops answering, Jarvis moves to a backup model; with no internet it uses
a model on your Mac through Ollama (`llm.local_model`, `qwen3.5:4b` by default). It says out loud whenever it switches and
when it is back (`llm.announce_switches`). `jarvisd doctor` checks that the local model is ready.

## Memory

**Spoken to you.** Facts are stored in your own words ("I'm 20") and said back as "you're 20". "Tell me everything you
know about me" gives a short summary of who you are, not a dump of every fact or old chat. A newer fact replaces the older
one it corrects, and Jarvis says what it replaced.

## Safety

- One gate decides every tool call, and whole capabilities (`shell`, `browser`, `screen`, `files`, `messaging`) can be
  switched off in `config.toml` with `safety.disabled_capabilities`. Their tools are hidden and refused if called.
- Web pages are fetched only from public addresses. Links to your own machine or network, including ones reached by
  redirect, are refused.
- Passwords, API keys, private keys and card numbers are masked before anything is sent to the language model
  (`llm.redact_secrets`, on by default).

## Commands

| Command | What it does |
|---|---|
| `jarvisd stats [--days N]` | How fast it is: speech recognition, thinking, time to first sound, idle CPU and memory |

## Next

- **Local model fast path, if the numbers justify it.** Run `jarvisd stats` for a week first.
