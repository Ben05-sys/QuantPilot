"""The market news feed, and what makes a story hot.

Two sources, because neither is sufficient alone:

  - Yahoo's top-stories RSS is broad and fast (45 items, ~0.3s) but
    carries **no tickers at all** — checked, not assumed. Good for market
    context, useless for "what is moving".
  - The search endpoint returns `relatedTickers`, so news can be attached
    to companies — but only one symbol per request.

So the feed queries news for the day's biggest movers and merges it with
the broad wire. Which symbols to ask about is the interesting part, and
it is free: the snapshot already knows exactly what is moving.

**Hotness** is scored rather than assumed from recency alone. A story is
hot when it is recent *and* about something that is actually moving. A
40-minute-old piece on a stock down 16% outranks a five-minute-old
opinion column, and a roundup tagged with eight tickers is discounted
because it is not really news about any of them.
"""

from __future__ import annotations

import concurrent.futures
import time
import xml.etree.ElementTree as ET

from . import article, net
from .providers import yahoo
from .providers.base import ProviderError

RSS_URL = "https://finance.yahoo.com/news/rssindex"

# How many movers to pull company news for. Each is one request; they run
# concurrently and the whole set is cached, so this is a latency budget
# rather than a rate-limit concern.
MOVER_COUNT = 10
MAX_WORKERS = 5

# Recency half-life. Two hours: long enough that the feed isn't empty on a
# quiet afternoon, short enough that yesterday cannot lead it.
HALF_LIFE = 2 * 3600

# A story tagged with this many symbols is a roundup, not company news.
ROUNDUP_AT = 4


def _rss(ttl: float = 120) -> list[dict]:
    """Yahoo's top-stories wire. No tickers — that is what the mover
    queries are for."""
    try:
        raw = net.fetch(RSS_URL, ttl=ttl, retries=2, timeout=20)
    except net.FetchError:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    out = []
    for item in root.findall(".//item"):
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        if not link:
            continue
        out.append({
            "uuid": guid,
            "title": (item.findtext("title") or "").strip(),
            "publisher": (item.findtext("source") or "Yahoo Finance").strip(),
            "link": link,
            "published": _parse_date(item.findtext("pubDate")),
            "related": [],
            "tagged": False,
            "thumbnail": None,
        })
    return out


def _parse_date(text: str | None) -> float | None:
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            from datetime import datetime, timezone
            parsed = datetime.strptime(text.strip(), fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            continue
    return None


def movers(df, count: int = MOVER_COUNT) -> list[dict]:
    """The names worth asking about: biggest movers with enough liquidity
    that the move means something."""
    if df is None or df.empty:
        return []
    liquid = df[(df["market_cap"].fillna(0) > 2e9)
                & (df["volume"].fillna(0) > 300_000)
                & df["change_pct"].notna()]
    if liquid.empty:
        return []
    ranked = liquid.reindex(
        liquid["change_pct"].abs().sort_values(ascending=False).index)
    return [{"symbol": r.symbol, "change_pct": float(r.change_pct),
             "name": r.name_ if hasattr(r, "name_") else None}
            for r in ranked.head(count).itertuples(index=False)]


def hotness(item: dict, moves: dict) -> float:
    """0-100. Recency times the size of the move it is about.

    Deliberately not recency alone: the wire is full of five-minute-old
    columns about nothing, and they should not outrank a story about the
    stock that just fell 16%.
    """
    age = time.time() - (item.get("published") or 0)
    if age < 0:
        age = 0
    recency = 0.5 ** (age / HALF_LIFE)

    related = item.get("related") or []
    move = max((abs(moves.get(sym, 0.0)) for sym in related), default=0.0)
    # 5% is treated as a full-strength move; beyond that the curve flattens
    # so one halted microcap cannot dominate the whole feed.
    magnitude = min(move / 5.0, 1.0)

    # A story about nothing in particular still has value as market
    # context, so the floor is not zero.
    weight = 0.35 + 0.65 * magnitude
    if len(related) >= ROUNDUP_AT:
        weight *= 0.55            # roundups mention everything and say little
    return round(100 * recency * weight, 1)


def feed(df=None, limit: int = 40, ttl: float = 120) -> dict:
    """The market feed: broad wire plus news on what is moving."""
    top = movers(df) if df is not None else []
    moves = {m["symbol"]: m["change_pct"] for m in top}

    items: dict[str, dict] = {}
    for item in _rss(ttl=ttl):
        items[item["uuid"]] = item

    if top:
        def fetch_one(symbol):
            try:
                return yahoo.news(symbol, count=6, ttl=ttl)["items"]
            except (ProviderError, Exception):  # noqa: BLE001
                return []

        with concurrent.futures.ThreadPoolExecutor(MAX_WORKERS) as pool:
            for got in pool.map(fetch_one, [m["symbol"] for m in top]):
                for item in got:
                    # Merge rather than overwrite: the same story arrives
                    # from several movers and each visit adds tickers.
                    existing = items.get(item["uuid"])
                    if existing:
                        merged = set(existing.get("related") or [])
                        merged.update(item.get("related") or [])
                        existing["related"] = sorted(merged)
                    else:
                        items[item["uuid"]] = item

    out = list(items.values())
    for item in out:
        item["hot"] = hotness(item, moves)
        item["age_seconds"] = (time.time() - item["published"]
                               if item.get("published") else None)
        # The symbol the row is *about*, picked as the tagged ticker with
        # the biggest move — that is the one worth putting on the bar.
        related = item.get("related") or []
        item["lead"] = (max(related, key=lambda s: abs(moves.get(s, 0.0)))
                        if related else None)
        item["lead_change"] = moves.get(item["lead"]) if item.get("lead") else None
        article.register(item["uuid"], item["link"])

    out.sort(key=lambda i: -i["hot"])
    return {"items": out[:limit], "movers": top,
            "total": len(out), "as_of": time.time()}
