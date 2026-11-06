"""Make text speakable: numbers, currency, tickers, times, URLs, markdown, emoji."""

from __future__ import annotations

import re

_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _below_100(n: int) -> str:
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return _TENS[t] + (f"-{_ONES[o]}" if o else "")


def _below_1000(n: int) -> str:
    h, r = divmod(n, 100)
    parts = []
    if h:
        parts.append(f"{_ONES[h]} hundred")
    if r:
        parts.append(_below_100(r))
    return " ".join(parts)


def num_to_words(n: int, *, indian: bool = False) -> str:
    """Spell an integer. ``indian=True`` uses lakh/crore grouping."""
    if n < 0:
        return "minus " + num_to_words(-n, indian=indian)
    if n < 100:
        return _below_100(n)
    if n < 1000:
        return _below_1000(n)
    parts: list[str] = []
    if indian:
        units = [(10_000_000, "crore"), (100_000, "lakh"), (1000, "thousand")]
        for size, name in units:
            q, n = divmod(n, size)
            if q:
                parts.append(f"{num_to_words(q, indian=True)} {name}")
        if n:
            parts.append(_below_1000(n))
    else:
        units = [(10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (1000, "thousand")]
        for size, name in units:
            q, n = divmod(n, size)
            if q:
                parts.append(f"{num_to_words(q)} {name}")
        if n:
            parts.append(_below_1000(n))
    return " ".join(parts)


_ORD_SPECIAL = {
    "one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
    "nine": "ninth", "twelve": "twelfth",
}


def ordinal_words(n: int) -> str:
    """3 -> 'third', 21 -> 'twenty-first', 30 -> 'thirtieth'."""
    words = num_to_words(n)
    head, sep, last = words.rpartition("-" if "-" in words.split(" ")[-1] else " ")
    if sep == " " or not sep:
        head_part = head + (" " if head else "")
    else:
        head_part = head + "-"
    if last in _ORD_SPECIAL:
        last = _ORD_SPECIAL[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return head_part + last


def _digits_spoken(s: str) -> str:
    return " ".join(_ONES[int(c)] if c != "0" else "oh" for c in s)


def decimal_to_words(text: str) -> str:
    """'3.14' -> 'three point one four'; '1,25,000.50' handled by caller."""
    whole, _, frac = text.partition(".")
    whole_n = int(whole.replace(",", "") or 0)
    out = num_to_words(whole_n)
    if frac:
        out += " point " + " ".join(_ONES[int(c)] for c in frac if c.isdigit())
    return out


# -- tickers ----------------------------------------------------------------

TICKER_NAMES = {
    "^NSEI": "the Nifty",
    "^BSESN": "the Sensex",
    "^GSPC": "the S and P five hundred",
    "^IXIC": "the Nasdaq",
    "^DJI": "the Dow",
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ether",
    "NIFTY": "the Nifty",
    "SENSEX": "the Sensex",
    "S&P 500": "the S and P five hundred",
    "S&P500": "the S and P five hundred",
    "NASDAQ": "the Nasdaq",
}


def spoken_time(hour24: int, minute: int) -> str:
    """British spoken time: 14:30 -> 'half past two'."""
    h12 = hour24 % 12 or 12
    nxt = (hour24 + 1) % 12 or 12
    if minute == 0:
        return f"{_ONES[h12]} o'clock" if hour24 not in (0,) else "midnight"
    if minute == 15:
        return f"quarter past {_ONES[h12]}"
    if minute == 30:
        return f"half past {_ONES[h12]}"
    if minute == 45:
        return f"quarter to {_ONES[nxt]}"
    mm = f"oh {_ONES[minute]}" if minute < 10 else _below_100(minute)
    return f"{_ONES[h12]} {mm}"


_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0000FE0F\U0000200D]+"
)
_URL = re.compile(r"https?://\S+|www\.\S+")


# Words the voice gets wrong, respelled the way they should sound. Extend with [voice.pronunciations] in config.toml.
LEXICON: dict[str, str] = {
    "Bengaluru": "Bengaloo-roo", "Bangalore": "Bang-guh-lore", "Sensex": "Sen-sex", "WhatsApp": "Whats App",
    "iMessage": "eye Message", "macOS": "mac O S", "iOS": "eye O S", "iPhone": "eye Phone", "iPad": "eye Pad",
    "NVIDIA": "en-VID-ee-uh", "OpenAI": "Open A I", "API": "A P I", "SSH": "S S H", "GPU": "G P U", "CPU": "C P U",
    "UI": "U I", "URL": "U R L", "PDF": "P D F", "USB": "U S B", "Wi-Fi": "Why-Fi", "WiFi": "Why-Fi",
    "Gmail": "G-mail", "LinkedIn": "Linked-In", "YouTube": "You-Tube", "GitHub": "Git-Hub", "ChatGPT": "Chat G P T",
    "Nemotron": "Nemo-tron", "Kokoro": "Kokoro", "AirPods": "Air Pods", "OTP": "O T P", "FAQ": "F A Q",
    "vs": "versus", "approx": "approximately", "e.g.": "for example", "i.e.": "that is", "etc.": "et cetera",
    "Ave.": "Avenue", "Dr.": "Doctor", "Mr.": "Mister", "Mrs.": "Missus", "Ms.": "Miz",
}
_UNITS = {
    "km": "kilometres", "kms": "kilometres", "kg": "kilograms", "g": "grams", "mg": "milligrams", "gb": "gigabytes",
    "mb": "megabytes", "tb": "terabytes", "kb": "kilobytes", "ms": "milliseconds", "cm": "centimetres",
    "mm": "millimetres", "mph": "miles per hour", "kmph": "kilometres per hour", "hr": "hours", "hrs": "hours",
    "min": "minutes", "mins": "minutes", "sec": "seconds", "secs": "seconds", "ghz": "gigahertz", "mhz": "megahertz",
    "x": "times", "ml": "millilitres", "l": "litres", "lb": "pounds", "lbs": "pounds",
}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"]


def _daypart(h: int) -> str:
    return "in the morning" if h < 12 else "in the afternoon" if h < 17 else "in the evening" if h < 21 else "at night"


def _date_words(d: int, m: int, y: int) -> str:
    if not (1 <= d <= 31 and 1 <= m <= 12):
        return f"{d} {m} {y}"
    yr = f"{_below_100(y // 100)} {_below_100(y % 100) if y % 100 else 'hundred'}" if 1000 <= y <= 2099 else str(y)
    return f"{_MONTHS[m - 1]} {ordinal_words(d)}, {yr}"


def speakable(text: str) -> str:
    """Rewrite ``text`` so a TTS engine reads it naturally."""
    t = text
    t = _URL.sub("", t)
    t = _EMOJI.sub("", t)
    # markdown
    t = re.sub(r"```[\s\S]*?```", " ", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\*\*?([^*]+)\*\*?", r"\1", t)
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*#+\s*", "", t, flags=re.M)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)

    # " - Battery is low" in the middle of a reply is a list marker, not a word: pause instead of saying "dash"
    t = re.sub(r"(?<=[:.!?])\s+[-–—•]\s+(?=[A-Z0-9])", " ", t)
    t = re.sub(r"\s[-–—]\s(?=[A-Z])", ". ", t)

    # respell words the voice mispronounces (whole words only, longest first)
    for k in sorted(LEXICON, key=len, reverse=True):
        t = re.sub(r"(?<![A-Za-z0-9])" + re.escape(k) + r"(?![A-Za-z0-9])", LEXICON[k], t)

    # dates: 2026-11-04 and 04/11/2026 (day first, as in India)
    t = re.sub(r"\b(20\d\d|19\d\d)-(\d{2})-(\d{2})\b", lambda m: _date_words(int(m.group(3)), int(m.group(2)), int(m.group(1))), t)
    t = re.sub(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d\d)\b", lambda m: _date_words(int(m.group(1)), int(m.group(2)), int(m.group(3))), t)

    # tickers (longest keys first)
    for k in sorted(TICKER_NAMES, key=len, reverse=True):
        t = re.sub(re.escape(k) + r"(?![A-Za-z])", TICKER_NAMES[k], t)

    # times: 14:30, 9:05 am
    def _time(m: re.Match) -> str:
        h, mi, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower().replace(".", "")
        if ampm == "pm" and h < 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        if h > 23 or mi > 59:
            return m.group(0)
        s = spoken_time(h, mi)
        return f"{s} {_daypart(h)}" if ampm else s

    t = re.sub(r"\b(\d{1,2}):(\d{2})\s*([AaPp]\.?[Mm]\.?)?(?!\d)", _time, t)

    def _hour_only(m: re.Match) -> str:
        h, ampm = int(m.group(1)), m.group(2).lower().replace(".", "")
        if not 1 <= h <= 12:
            return m.group(0)
        h24 = h % 12 + (12 if ampm == "pm" else 0)
        return f"{num_to_words(h)} o'clock {_daypart(h24)}"

    t = re.sub(r"\b(\d{1,2})\s?([AaPp]\.?[Mm]\.?)(?![A-Za-z])", _hour_only, t)

    # currency: ₹1,25,000 / Rs. 500 / $12.50 / £3
    def _money(m: re.Match) -> str:
        sym, amount, scale = m.group(1), m.group(2), (m.group(3) or "").lower()
        names = {"₹": "rupees", "rs": "rupees", "rs.": "rupees", "inr": "rupees",
                 "$": "dollars", "usd": "dollars", "£": "pounds", "€": "euros"}
        unit = names.get(sym.lower(), "")
        whole, _, frac = amount.replace(",", "").partition(".")
        n = int(whole or 0)
        indian = sym.lower() in {"₹", "rs", "rs.", "inr"}
        words = num_to_words(n, indian=indian)
        scale_words = {"k": "thousand", "m": "million", "b": "billion", "cr": "crore",
                       "crore": "crore", "lakh": "lakh", "l": "lakh", "bn": "billion",
                       "mn": "million", "million": "million", "billion": "billion",
                       "thousand": "thousand"}
        if scale in scale_words:
            if frac:
                words = decimal_to_words(f"{n}.{frac}")
            words += " " + scale_words[scale]
        elif frac and any(c != "0" for c in frac):
            cents = int((frac + "0")[:2])
            sub = {"rupees": "paise", "dollars": "cents", "pounds": "pence", "euros": "cents"}.get(unit, "")
            words += f" {unit} and {num_to_words(cents)} {sub}"
            return words
        return f"{words} {unit}".strip()

    t = re.sub(
        r"(₹|\$|£|€|\bRs\.?|\bINR)\s?(\d[\d,]*(?:\.\d+)?)(?:\s?(crore|lakh|million|billion|thousand|cr|bn|mn|[kKmMbBlL])\b)?",
        _money,
        t,
        flags=re.I,
    )

    # percentages
    def _pct(m: re.Match) -> str:
        num = m.group(1)
        sign = m.group(0)[0] if m.group(0)[0] in "+-−" else ""
        word = decimal_to_words(num.lstrip("+-−"))
        prefix = {"+": "up ", "-": "down ", "−": "down "}.get(sign, "")
        return f"{prefix}{word} percent"

    t = re.sub(r"([+\-−]?\d+(?:\.\d+)?)\s?%", _pct, t)

    # temperatures
    t = re.sub(r"(\d+)\s?°\s?C\b", lambda m: f"{num_to_words(int(m.group(1)))} degrees", t)
    t = re.sub(r"(\d+)\s?°\s?F\b", lambda m: f"{num_to_words(int(m.group(1)))} degrees Fahrenheit", t)
    t = t.replace("°", " degrees")

    # units glued to numbers: 5km, 20GB, 1.5x
    t = re.sub(r"(\d)\s?([A-Za-z]{1,5})\b(?![A-Za-z'])",
               lambda m: f"{m.group(1)} {_UNITS[m.group(2).lower()]}" if m.group(2).lower() in _UNITS else m.group(0), t)

    # ordinals: 3rd -> third, 21st -> twenty-first
    t = re.sub(r"\b(\d{1,3})(st|nd|rd|th)\b", lambda m: ordinal_words(int(m.group(1))), t)

    # remaining numbers with thousands separators, decimals, plain ints
    def _num(m: re.Match) -> str:
        raw = m.group(0)
        if re.fullmatch(r"(19|20)\d\d", raw):  # years read as pairs
            return f"{_below_100(int(raw[:2]))} {_below_100(int(raw[2:])) if raw[2:] != '00' else 'hundred'}"
        if "." in raw:
            return decimal_to_words(raw)
        n = int(raw.replace(",", ""))
        return num_to_words(n, indian=bool(re.search(r",\d\d,", raw)))

    t = re.sub(r"\d[\d,]*(?:\.\d+)?", _num, t)

    t = t.replace("&", " and ").replace("/", " ").replace("_", " ").replace("~", "about ")
    t = re.sub(r"[\"“”]", "", t)
    t = re.sub(r"\b(the|a|an|of|and|to)\s+\1\b", r"\1", t, flags=re.I)  # "the the S and P" after ticker rewrites
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    return t.strip()


def caption_text(text: str) -> str:
    """What the chat box shows: list markers become real bullets on their own lines."""
    t = re.sub(r"(?<=[:.!?])\s+[-–—•*]\s+(?=[A-Z0-9])", "\n• ", text)
    t = re.sub(r"^\s*[-*]\s+", "• ", t, flags=re.M)
    return t.strip()
