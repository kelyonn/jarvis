"""Typed configuration loaded from ``~/.jarvis/config.toml``.

Secrets never live here; see :mod:`jarvis_core.secrets`.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis_core import paths

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

log = logging.getLogger(__name__)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _hhmm(value: str) -> str:
    h, m = value.split(":")
    if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
        raise ValueError(f"bad time {value!r}")
    return f"{int(h):02d}:{int(m):02d}"


class UserCfg(_Model):
    name: str = "sir"
    timezone: str = "Asia/Kolkata"
    # Your day runs from day_start_hour to day_end_hour (wrapping past midnight). Waking up after day_end_hour starts a
    # new day: the first lid-open of it gets the morning brief, later ones a short welcome back.
    day_start_hour: int = Field(9, ge=0, le=23)
    day_end_hour: int = Field(3, ge=0, le=23)
    quiet_hours: list[str] = Field(default_factory=lambda: ["03:00", "09:00"])

    @field_validator("quiet_hours")
    @classmethod
    def _qh(cls, v: list[str]) -> list[str]:
        if len(v) != 2:
            raise ValueError("quiet_hours must be [start, end]")
        return [_hhmm(x) for x in v]


class ActivationCfg(_Model):
    # "wake_word" (default, Siri-style): say "Hey Jarvis" any time, even while it is talking (the OS mic indicator stays on).
    # "hotkey": the microphone is closed until you press the hotkey / click the orb (nothing listens in between).
    listen_mode: Literal["hotkey", "wake_word"] = "wake_word"
    # Frontends without echo cancellation (the Shell): keep the mic open while Jarvis speaks and stop on "Hey Jarvis".
    wake_interrupt: bool = True
    wake_word: str = "hey_jarvis"  # the pre-trained acoustic model (it only knows this phrase)
    duck_media: bool = True  # lower Spotify / Music while Jarvis listens, so your voice is clear (like Siri)
    duck_to: float = 0.2  # fraction of the current volume while ducked
    noisy_floor_rms: int = 350  # background level above which the room counts as loud (music, TV)
    welcome_back: bool = True  # say "Welcome back." when the lid opens or the screen unlocks
    welcome_back_min_away_s: float = 4.0  # ignore screen-lock flickers
    welcome_back_cooldown_min: float = 5.0  # at most one greeting per this many minutes
    # Skip wake-word analysis while the room is quiet (saves most of the idle CPU). 0 turns the gate off.
    wake_gate_rms: int = 60
    wake_threshold: float = 0.6
    hotkey: str = "option+space"
    # Hold Ctrl+Option+Space, speak, let go: the words are typed into the front app (no assistant, no reply).
    dictation: bool = True
    follow_up_seconds: float = 8.0
    # Once you are properly talking (2+ turns), Jarvis keeps listening this long between replies, like a person would.
    conversation_follow_up_seconds: float = 14.0
    # While Jarvis talks it does NOT listen to your voice (no talking over it, no "Hey Jarvis" to interrupt). Hold Space instead:
    # the moment you press it Jarvis stops, it listens while you hold, and acts on what you said when you let go. The Shell
    # only grabs the Space key while Jarvis is talking, so typing is untouched. The voice interruption code below stays; set
    # this to false to use it again.
    space_to_interrupt: bool = True
    barge_in: bool = True  # talking over Jarvis stops it: on headphones always, on speakers only with a voiceprint (see below)
    # Off by default: on speakers there is no echo cancellation, and a voiceprint alone does not stop Jarvis hearing itself
    # (it interrupted its own replies). Use "Hey Jarvis", the Stop button or Option+Space to interrupt on speakers.
    barge_in_speakers: bool = False
    # Software echo cancellation for frontends without it (the Shell): subtracts what Jarvis is playing from the mic so you
    # can talk over it on speakers. Off until you have checked it on your own Mac: say something long, watch the log line
    # "aec: removing N dB of echo", and enable it if N stays above aec_min_erle_db.
    aec: bool = False
    aec_min_erle_db: float = 12.0  # barge-in on speakers is only trusted once this much echo is being removed
    end_silence_ms: int = 600
    # You stopped right after "open the" or "set a timer for": wait this much longer for the rest instead of acting on half a sentence.
    extend_unfinished: bool = True
    extend_wait_s: float = 1.6
    follow_up_end_silence_ms: int = 700
    no_speech_timeout_s: float = 5.0
    max_utterance_s: float = 30.0


class VoiceCfg(_Model):
    stt: str = "mlx-whisper"
    stt_model: str = "mlx-community/whisper-large-v3-turbo"
    tts: str = "kokoro"
    tts_voice: str = "bm_george,bm_fable"  # comma-separated voices are blended (see KokoroTTS)
    tts_speed: float = 1.0
    pronunciations: dict[str, str] = {}  # extra respellings: Rahul = "Rah-hool" under [voice.pronunciations]
    speaker_verify: bool = True  # once you have enrolled (jarvisd voice-id enroll / "learn my voice"), act only on your voice
    # Real speech over Bluetooth earbuds scores anywhere from ~0.55 to ~0.95 against your own voiceprint, so these are lenient.
    speaker_threshold: float = 0.58  # similarity needed to accept an utterance
    speaker_wake_threshold: float = 0.46  # for "Hey Jarvis" itself: about a second of audio, so a little more forgiving
    speaker_barge_threshold: float = 0.50  # when interrupting: the audio then also holds Jarvis's own voice
    speaker_grace_s: float = 30.0  # right after it has recognised you, it keeps talking to you without re-proving it...
    speaker_grace_threshold: float = 0.5  # ...at this lower bar
    # When the recogniser is this unsure of what it heard (Whisper's average log-probability, usually -0.2 to -0.6 when it
    # is confident), Jarvis asks "did you say ...?" before acting. 0 turns this off.
    low_confidence_logprob: float = -1.25
    speaker_adapt: bool = True  # every clear match nudges the voiceprint toward how you sound through this mic
    humanize: bool = True  # clause-level pacing, pauses and speed variation
    edge_voice: str = "en-GB-RyanNeural"  # used when tts = "edge" (online; alt: en-GB-ThomasNeural)
    edge_rate: str = "+0%"
    edge_pitch: str = "+0Hz"
    say_voice: str = "Daniel"


class LLMCfg(_Model):
    provider: str = "nim"
    host: str = "https://integrate.api.nvidia.com"
    # Chosen by probing the live NIM endpoint (chat, tool calling and image input all work, ~1-2 s to first token).
    chat_model: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    fast_model: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    vision_model: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    # Tried in order when a model above is gone (404/410) or unreachable. NIM's catalogue lists dead models.
    fallback_models: list[str] = Field(default_factory=lambda: [
        "nvidia/nemotron-3.5-super-vl-120b-a12b", "nvidia/nemotron-3.5-lightning-30b-a3b"])
    # Extra fields merged into every chat request. Default switches off the model's hidden 'thinking' phase, which
    # otherwise eats the token budget and adds latency to a voice reply. Set to {} for models that reject it.
    extra_body: dict = Field(default_factory=lambda: {"chat_template_kwargs": {"enable_thinking": False}})
    temperature: float = 0.6
    max_tokens: int = 700
    max_tool_iterations: int = 14
    filler_after_s: float = 1.5  # a tool slower than this gets a short spoken line ("One moment."); 0 turns it off
    redact_secrets: bool = True  # mask passwords, API keys, private keys and card numbers before anything is sent to the model
    tool_selection: bool = True  # offer only relevant tool categories per request (faster, more accurate)
    reasoning: bool = True  # think privately (chain / tree / graph of thoughts) on requests that benefit; simple ones skip it
    max_think_words: int = 260  # cap on the private working notes per reply, so a voice answer stays quick
    think_timeout_s: float = 8.0  # give up on the private first pass after this long and answer directly
    request_timeout_s: float = 20.0
    # Offline (or every online model down): answer from a model running on this Mac through Ollama (https://ollama.com).
    local_fallback: bool = True
    local_model: str = "qwen3.5:4b"
    local_host: str = "http://localhost:11434"
    local_timeout_s: float = 60.0
    announce_switches: bool = True  # say so when Jarvis moves to a backup model, goes local because you're offline, or comes back


class BriefingCfg(_Model):
    evening_hour: int = 17  # briefings from this hour on also cover tomorrow's first meeting and the overnight weather
    first_open_full_brief: bool = True
    slots: list[str] = Field(default_factory=lambda: ["13:00", "19:00"])
    deliver_missed_slots_on_wake: bool = True
    target_minutes: float = 0.6  # a normal full brief is about this long (spoken)
    max_minutes: float = 1.0  # never longer than this, unless something is genuinely urgent
    urgent_max_minutes: float = 1.4  # allowance when a VIP message, imminent meeting or big market move deserves the time
    day_rollover_hour: int = 3  # a new briefing day starts after this hour (match user.day_end_hour)
    include_linkedin: bool = False
    sections: list[str] = Field(
        default_factory=lambda: [
            "greeting", "weather", "calendar", "email", "messages",
            "markets", "news_tech", "reminders",
        ]
    )

    @field_validator("slots")
    @classmethod
    def _slots(cls, v: list[str]) -> list[str]:
        return [_hhmm(x) for x in v]


class MarketsCfg(_Model):
    indices: list[str] = Field(default_factory=lambda: ["^NSEI", "^BSESN", "^GSPC", "^IXIC"])
    crypto: list[str] = Field(default_factory=lambda: ["BTC-USD", "ETH-USD"])
    watchlist: list[str] = Field(default_factory=list)


class NewsCfg(_Model):
    world: list[str] = Field(
        default_factory=lambda: [
            "https://feeds.bbci.co.uk/news/world/rss.xml",
            "https://www.theguardian.com/world/rss",
            "https://www.aljazeera.com/xml/rss/all.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        ]
    )
    india: list[str] = Field(
        default_factory=lambda: [
            "https://www.thehindu.com/news/national/feeder/default.rss",
            "https://indianexpress.com/section/india/feed/",
        ]
    )
    tech: list[str] = Field(
        default_factory=lambda: [
            "https://hnrss.org/frontpage?points=200",
            "https://www.theverge.com/rss/index.xml",
            "https://techcrunch.com/category/artificial-intelligence/feed/",
        ]
    )
    items_per_section: int = 2


class AlertsCfg(_Model):
    poll_seconds: int = 120
    meeting_lead_minutes: int = 10
    market_index_move_pct: float = 1.5
    crypto_move_pct: float = 5.0
    rain_lookahead_hours: int = 2
    rain_probability_pct: int = 60
    battery_low_pct: int = 15
    breaking_news_min_sources: int = 3
    cooldown_minutes: dict[str, int] = Field(
        default_factory=lambda: {"market": 60, "rain": 180, "news": 30, "battery": 30}
    )


class MessagingCfg(_Model):
    apps: list[str] = Field(default_factory=lambda: ["whatsapp", "imessage", "gmail", "linkedin"])
    proactive_reply_offers: bool = True
    offer_for: str = "vip_and_important"
    offer_cooldown_minutes: int = 10
    offer_snooze_hours: int = 2
    default_country_code: str = "91"  # for numbers saved without a +country prefix
    style_examples: int = 6  # your own recent messages to the contact, shown to the drafter
    whatsapp_db: str = ""  # override the ChatStorage.sqlite path
    imessage_db: str = ""  # override the chat.db path


class VipCfg(_Model):
    contacts: list[str] = Field(default_factory=list)


class RoutineCfg(_Model):
    apps: list[str] = Field(default_factory=list)
    layout: dict[str, str] = Field(default_factory=dict)
    focus: str | None = None


class SafetyCfg(_Model):
    confirm_risky: bool = True
    never_click_labels: list[str] = Field(
        default_factory=lambda: [
            "send", "post", "submit", "buy", "pay", "purchase", "delete", "remove",
            "confirm order",
        ]
    )
    terminal_allowlist_no_confirm: list[str] = Field(
        default_factory=lambda: ["ls", "pwd", "df -h", "uptime"]
    )
    transcript_retention_days: int = 7
    protected_apps: list[str] = Field(default_factory=list)  # never quit or closed; Dia is always protected on top of these
    # Capabilities switched off entirely: their tools are hidden from the model and refused if called anyway.
    # Names: shell, browser, screen, files, messaging (or any single tool category).
    disabled_capabilities: list[str] = Field(default_factory=list)
    # How Jarvis may act in each app: "auto" (just do it), "ask" (a spoken yes first) or "never" (look only).
    # Terminals and password managers are "never", messaging and mail apps are "ask", unless you say otherwise here.
    app_rules: dict[str, str] = Field(default_factory=dict)


class MemoryCfg(_Model):
    enabled: bool = True
    auto_extract: bool = True  # quietly remember lasting facts the user states (user's own words only)
    max_facts: int = 2000
    episodes: bool = True  # keep a one-line note of each exchange so you can ask about earlier conversations
    persist_conversation: bool = True  # an ongoing conversation survives a restart
    conversation_turns: int = 16  # how many of your turns the model sees verbatim
    conversation_minutes: float = 60.0  # silence this long ends the conversation


class DevicesCfg(_Model):
    airpods: str = ""
    location_city: str = ""  # optional manual override of auto-detected city


class Config(_Model):
    user: UserCfg = Field(default_factory=UserCfg)
    activation: ActivationCfg = Field(default_factory=ActivationCfg)
    voice: VoiceCfg = Field(default_factory=VoiceCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    briefing: BriefingCfg = Field(default_factory=BriefingCfg)
    markets: MarketsCfg = Field(default_factory=MarketsCfg)
    news: NewsCfg = Field(default_factory=NewsCfg)
    alerts: AlertsCfg = Field(default_factory=AlertsCfg)
    messaging: MessagingCfg = Field(default_factory=MessagingCfg)
    vip: VipCfg = Field(default_factory=VipCfg)
    routines: dict[str, RoutineCfg] = Field(default_factory=dict)
    safety: SafetyCfg = Field(default_factory=SafetyCfg)
    memory: MemoryCfg = Field(default_factory=MemoryCfg)
    devices: DevicesCfg = Field(default_factory=DevicesCfg)


def load_config(path: Path | None = None) -> Config:
    """Load and validate the config; a missing file yields defaults."""
    path = path or paths.config_file()
    data: dict[str, Any] = {}
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
    return Config.model_validate(data)


def save_config(cfg: Config, path: Path | None = None) -> Path:
    import tomli_w

    path = path or paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="python", exclude_none=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)
    return path


def update_config(mutator, path: Path | None = None) -> Config:
    """Load, apply ``mutator(cfg)``, save, and return the config."""
    cfg = load_config(path)
    mutator(cfg)
    save_config(cfg, path)
    return cfg


def parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def reload_into(target: Config, new: Config) -> None:
    """Copy ``new`` into ``target`` in place so every holder sees the update."""
    for name in Config.model_fields:
        setattr(target, name, getattr(new, name))
