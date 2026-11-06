"""Market quotes from Yahoo Finance's public chart endpoint."""

from __future__ import annotations

import asyncio

from jarvis_core.briefing.env import Env
from jarvis_core.briefing.models import Quote

NAMES = {
    "^NSEI": "Nifty 50", "^BSESN": "Sensex", "^GSPC": "the S&P 500", "^IXIC": "the Nasdaq", "^DJI": "the Dow",
    "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "^NSEBANK": "Bank Nifty", "^FTSE": "the FTSE",
}
CRYPTO = {"BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "XRP-USD"}


def parse_chart(symbol: str, data: dict) -> Quote:
    res = (data.get("chart", {}).get("result") or [None])[0]
    if not res:
        raise ValueError(f"no data for {symbol}")
    meta = res["meta"]
    price = float(meta["regularMarketPrice"])
    closes = [c for c in (res.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c is not None]
    if len(closes) >= 2 and abs(closes[-1] - price) <= max(0.01, price * 0.002):
        prev = float(closes[-2])
    elif closes:
        prev = float(closes[-1])
    else:
        prev = float(meta.get("previousClose") or meta.get("chartPreviousClose") or price)
    return Quote(
        symbol=symbol,
        name=NAMES.get(symbol) or meta.get("shortName") or meta.get("longName") or symbol,
        price=price,
        prev_close=prev,
        currency=meta.get("currency", ""),
        market_state=meta.get("marketState", ""),
    )


async def fetch_quote(env: Env, symbol: str) -> Quote:
    data, _ = await env.get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"range": "7d", "interval": "1d"},
        cache_key=f"quote:{symbol}",
        ttl_s=60,
    )
    return parse_chart(symbol, data)


async def collect(env: Env, symbols: list[str] | None = None) -> list[Quote]:
    cfg = env.cfg.markets
    syms = symbols or [*cfg.indices, *cfg.crypto, *cfg.watchlist]
    results = await asyncio.gather(*(fetch_quote(env, s) for s in syms), return_exceptions=True)
    quotes = [r for r in results if isinstance(r, Quote)]
    if not quotes:
        errs = [str(r) for r in results if isinstance(r, Exception)]
        raise RuntimeError(errs[0] if errs else "no quotes")
    return quotes


def describe_quote(q: Quote) -> str:
    direction = "up" if q.change_pct >= 0 else "down"
    price = f"{q.price:,.0f}" if q.price >= 100 else f"{q.price:,.2f}"
    return f"{q.name or q.symbol} {direction} {abs(q.change_pct):.1f} percent at {price}"


def describe(quotes: list[Quote]) -> str:
    return "; ".join(describe_quote(q) for q in quotes) + "."
