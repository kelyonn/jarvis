"""News from RSS feeds: fetch, dedupe, rank, and cluster across sources."""

from __future__ import annotations

import asyncio
import calendar
import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser

from jarvis_core.briefing.env import Env
from jarvis_core.briefing.models import Headline

_STOP = {"the", "a", "an", "of", "in", "on", "to", "for", "and", "at", "as", "by", "with", "is", "are", "after",
         "over", "from", "says", "say", "new", "will", "has", "have", "its", "his", "her"}


def tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in _STOP and len(w) > 2}


def similar(a: set[str], b: set[str], threshold: float = 0.5) -> bool:
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= threshold and len(a & b) >= 3


def clean_title(t: str) -> str:
    t = html.unescape(t)
    t = re.sub(r"\s+[-|–—]\s+[A-Z][\w .&'’]{2,30}$", "", t).strip()
    return re.sub(r"\s+", " ", t)


def parse_feed(text: str, url: str, now: datetime) -> list[Headline]:
    feed = feedparser.parse(text)
    source = (feed.feed.get("title") or urlparse(url).netloc).split(" - ")[0].strip()
    out: list[Headline] = []
    for e in feed.entries[:40]:
        title = clean_title(e.get("title", ""))
        if not title:
            continue
        pub = None
        ts = e.get("published_parsed") or e.get("updated_parsed")
        if ts:
            pub = datetime.fromtimestamp(calendar.timegm(ts), tz=timezone.utc)
        out.append(Headline(title, source, pub, e.get("link", ""), html.unescape(re.sub(r"<[^>]+>", " ", e.get("summary", "")))[:300].strip()))
    return out


async def fetch_feed(env: Env, url: str) -> list[Headline]:
    text, _ = await env.get_text(url, cache_key=f"rss:{url}", ttl_s=300)
    return await asyncio.get_running_loop().run_in_executor(None, parse_feed, text, url, env.now())


def cluster(headlines: list[Headline]) -> list[list[Headline]]:
    """Group headlines about the same story. Larger clusters first, then newest."""
    clusters: list[tuple[set[str], list[Headline]]] = []
    for h in headlines:
        tk = tokens(h.title)
        for ctoks, members in clusters:
            if similar(tk, ctoks):
                members.append(h)
                ctoks |= tk
                break
        else:
            clusters.append((set(tk), [h]))
    groups = [m for _, m in clusters]

    def key(g: list[Headline]) -> tuple[int, float]:
        newest = max((h.published.timestamp() for h in g if h.published), default=0)
        return (len({h.source for h in g}), newest)

    groups.sort(key=key, reverse=True)
    return groups


async def collect_urls(env: Env, urls: list[str], limit: int, max_age_h: float = 24) -> list[Headline]:
    results = await asyncio.gather(*(fetch_feed(env, u) for u in urls), return_exceptions=True)
    items = [h for r in results if isinstance(r, list) for h in r]
    if not items and all(isinstance(r, Exception) for r in results):
        raise RuntimeError("all feeds failed")
    cutoff = env.now() - timedelta(hours=max_age_h)
    fresh = [h for h in items if h.published is None or h.published >= cutoff]
    top: list[Headline] = []
    for group in cluster(fresh)[:limit]:
        best = max(group, key=lambda h: (h.published.timestamp() if h.published else 0))
        best.summary = best.summary
        top.append(best)
    return top


async def collect(env: Env, topic: str) -> list[Headline]:
    cfg = env.cfg.news
    urls = {"world": cfg.world, "india": cfg.india, "tech": cfg.tech}.get(topic)
    if urls is None:
        raise ValueError(f"unknown news topic {topic}")
    return await collect_urls(env, urls, cfg.items_per_section)


def describe(items: list[Headline]) -> str:
    return " ".join(f"{h.title}." if not h.title.endswith(".") else h.title for h in items)
