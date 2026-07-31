"""Live US finance television, as a wall of embedded streams.

The financial networks run their broadcast on YouTube around the clock, and
the embed is the sanctioned way to watch it: `oembed` says yes for every
channel below, so nothing here rips a stream or works around a player.

What this module does *not* do is read the audio. YouTube renders live
captions inside its own player but exposes no retrievable track — the watch
page carries no `captionTracks` for a live video, the innertube player
returns an empty caption list, and `timedtext` answers 200 with zero bytes.
Their own documentation puts a downloadable transcript 12-24 hours after the
broadcast ends, and `captions.download` needs OAuth as the channel owner.
So the terminal does not pretend to know what is being said. It shows what
is being *traded* alongside the picture, which it can establish honestly
from the snapshot it already holds.

Liveness is read from the channel's own `/live` page rather than the Data
API, because the API would mean a key, and a key would mean the front page
stops being able to say there isn't one.
"""

from __future__ import annotations

import json
import re

from . import net

# Curated rather than searched: a query for "finance live" returns an
# endless tail of reupload channels running someone else's feed. These are
# the networks' own accounts, US markets, all verified embeddable.
CHANNELS = [
    {"id": "bloomberg", "handle": "@markets", "name": "Bloomberg Television",
     "note": "24/7 · markets, macro, Asia through US close"},
    {"id": "yahoo", "handle": "@YahooFinance", "name": "Yahoo Finance",
     "note": "24/7 · US session coverage and earnings"},
    {"id": "schwab", "handle": "@SchwabNetwork", "name": "Schwab Network",
     "note": "US market hours · trader-facing"},
    {"id": "benzinga", "handle": "@Benzinga", "name": "Benzinga",
     "note": "pre-market prep and the open"},
    {"id": "foxbusiness", "handle": "@FoxBusiness", "name": "Fox Business",
     "note": "US session · markets and policy"},
    {"id": "cnbc", "handle": "@CNBCtelevision", "name": "CNBC Television",
     "note": "segments and specials; not always live"},
    {"id": "reuters", "handle": "@Reuters", "name": "Reuters",
     "note": "breaking news; live when there is news to carry"},
]

# Long enough not to hammer YouTube, short enough that a channel coming on
# air shows up within a few minutes. A stream that has been running for
# hours does not need second-by-second polling.
TTL = 240.0

_VIDEO_ID = re.compile(r'"videoId":"([\w-]{11})"')
_TITLE = re.compile(r'<meta name="title" content="([^"]{0,200})"')
_WATCHING = re.compile(r'"viewCount":"(\d+)"')

# The /live path 302s to a watch page while the channel is on air and to the
# channel home when it is not, so the presence of a live marker is the test.
_LIVE_MARKERS = ('"isLiveNow":true', '"isLive":true', '"liveBroadcastDetails"')


def _probe(channel: dict) -> dict:
    """One channel's current state. Never raises — a network blip must not
    take the whole wall down, so a failed probe reports itself as offline
    with the reason attached rather than vanishing from the list."""
    out = {**channel, "live": False, "video_id": None,
           "title": None, "viewers": None, "error": ""}
    url = f"https://www.youtube.com/{channel['handle']}/live"
    try:
        html = net.fetch(url, ttl=TTL, retries=2, timeout=15).decode(
            "utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — one dead channel, not a dead view
        out["error"] = type(exc).__name__
        return out

    if not any(m in html for m in _LIVE_MARKERS):
        return out
    vid = _VIDEO_ID.search(html)
    if not vid:
        return out

    out["live"] = True
    out["video_id"] = vid.group(1)
    title = _TITLE.search(html)
    if title:
        out["title"] = title.group(1)
    watching = _WATCHING.search(html)
    if watching:
        out["viewers"] = int(watching.group(1))
    return out


def channels() -> dict:
    """Every channel with its current liveness, live ones first.

    Ordering is deliberate: whoever is actually broadcasting should be at
    the top of the rail, and within that the busiest stream first, because
    on a quiet overnight the one with six million viewers is the one
    carrying the news.
    """
    rows = [_probe(c) for c in CHANNELS]
    rows.sort(key=lambda r: (not r["live"], -(r["viewers"] or 0)))
    live = [r for r in rows if r["live"]]
    return {
        "channels": rows,
        "live_count": len(live),
        # The client opens on whatever is both live and busiest rather than
        # a hard-coded favourite, so the view is never a dead player.
        "default": live[0]["id"] if live else None,
    }


def embed_url(video_id: str) -> str:
    """The official IFrame embed, on the no-cookie host.

    `youtube-nocookie.com` serves the same player without setting a
    tracking cookie unless the viewer presses play, which is the right
    default for something that otherwise talks to nobody. `autoplay` is
    deliberately absent for the same reason: a terminal that starts making
    noise the moment you switch views is a terminal you turn off.
    """
    return (f"https://www.youtube-nocookie.com/embed/{video_id}"
            "?rel=0&modestbranding=1&playsinline=1")


def mentioned(text: str, frame) -> list[dict]:
    """Tickers a piece of text is plausibly about.

    Matches `$NVDA` cashtags and company names from the snapshot — "Nvidia"
    in a chyron is a mention of NVDA whether or not the symbol is spoken.
    A bare uppercase word is deliberately NOT treated as a symbol: half the
    S&P is also an English word, and "he said the CEO was OPEN to a deal"
    is not a quote about Opendoor.

    Returns what is found with the current move attached, so the caller can
    show why it matters. Empty rather than raising on a frame without the
    columns, same as every other read of the snapshot here.
    """
    if frame is None or not len(frame) or "symbol" not in frame.columns:
        return []
    words = text or ""
    if not words.strip():
        return []

    hits: dict[str, str] = {}
    for m in re.finditer(r"\$([A-Za-z][A-Za-z.\-]{0,5})\b", words):
        hits[m.group(1).upper()] = "cashtag"

    # Walk the sentence, not the universe. Testing 7,000 name patterns
    # against one headline costs 200ms and gets re-paid for every headline;
    # looking a dozen words up in a prebuilt index is free. Same answers,
    # three orders of magnitude apart.
    index = _root_index(frame)
    tokens = re.findall(r"[a-z][a-z&.]*", words.lower())
    tokens = [t.strip(".") for t in tokens]
    for i, tok in enumerate(tokens):
        for phrase in ((f"{tok} {tokens[i+1]}" if i + 1 < len(tokens) else None),
                       tok):
            if phrase and phrase in index:
                hits.setdefault(index[phrase], "name")
                break

    if not hits:
        return []

    known = frame[frame["symbol"].astype(str).str.upper().isin(hits)]
    out = []
    for row in known.to_dict("records"):
        sym = str(row["symbol"]).upper()
        out.append({
            "symbol": sym,
            "name": row.get("name"),
            "how": hits.get(sym, "name"),
            "change_pct": _num(row.get("change_pct")),
            "price": _num(row.get("price")),
        })
    out.sort(key=lambda r: -abs(r["change_pct"] or 0))
    return out


# Single words that are both a company's distinctive name and ordinary
# English. Matching these on speech produces confident nonsense, which is
# worse than a quiet miss: a rail that tags "data" as a semiconductor stock
# stops being worth reading. A cashtag still resolves them, because "$OPEN"
# is unambiguous in a way "open" is not.
_AMBIGUOUS = {
    "data", "open", "block", "match", "shop", "wave", "core", "edge", "peak",
    "prime", "pure", "true", "fair", "smart", "next", "live", "real", "main",
    "best", "first", "global", "national", "general", "united", "star", "sun",
    "sky", "summit", "apex", "growth", "income", "value", "capital", "trust",
    "fund", "index", "future", "target", "power", "energy", "health", "digital",
    "advance", "premier", "select", "unity", "focus", "range", "signal",
    "stride", "vital", "origin", "compass", "beacon", "anchor", "pioneer",
    # Generic industry nouns. These are the other half of the problem and
    # the sneakier one: they sit *inside* longer company names, so "908
    # Devices" fires on "Advanced Micro Devices" and tags the wrong stock in
    # a sentence that was about AMD. Two-word roots are unaffected.
    "devices", "systems", "solutions", "networks", "labs", "laboratories",
    "pharma", "pharmaceuticals", "therapeutics", "biosciences", "sciences",
    "motors", "foods", "brands", "resources", "materials", "industries",
    "enterprises", "partners", "properties", "communications", "holdings",
    "semiconductor", "semiconductors", "medical", "financial", "bancorp",
    "bank", "insurance", "realty", "mining", "steel", "airlines", "automotive",
    "robotics", "dynamics", "instruments", "electronics", "software",
    "computing", "storage", "wireless", "telecom", "media", "entertainment",
    "gaming", "apparel", "retail", "restaurants", "hotels", "transport",
    "logistics", "shipping", "freight", "products", "services", "ventures",
}

_INDEX_CACHE: dict[tuple, dict[str, str]] = {}


def _root_index(frame) -> dict[str, str]:
    """{company root -> symbol}, built once per snapshot generation.

    Keyed on the frame's shape and endpoints rather than its identity, so a
    refresh rebuilds it and a re-read of the same snapshot does not. One
    entry per root: where two companies reduce to the same root the first
    wins, which on a market-cap-ordered frame is the one anyone on air
    means.
    """
    if frame is None or not len(frame) or "name" not in frame.columns:
        return {}
    syms = frame["symbol"].astype(str)
    key = (len(frame), syms.iloc[0], syms.iloc[-1])
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached

    index: dict[str, str] = {}
    for sym, name in zip(syms, frame["name"]):
        if not isinstance(name, str):
            continue
        base = _company_root(name)
        # Three characters would match "3M" against "warm".
        if len(base) < 4:
            continue
        # A single-word root that is also ordinary English, or a generic
        # industry noun, is not evidence. Two-word roots stand alone.
        if " " not in base and base in _AMBIGUOUS:
            continue
        index.setdefault(base, str(sym).upper())

    _INDEX_CACHE.clear()  # one generation at a time; this is not a cache to grow
    _INDEX_CACHE[key] = index
    return index


_SUFFIXES = re.compile(
    # `com` is here for Amazon.com and Salesforce.com, whose roots would
    # otherwise be two words and never match the one word anyone says.
    r"\b(inc|incorporated|corp|corporation|co|com|company|plc|ltd|limited|holdings?|"
    r"group|the|class|common|stock|shares?|american|depositary|receipts?|"
    r"technologies|technology|international|worldwide)\b", re.I)


def _company_root(name: str) -> str:
    """"NVIDIA Corporation" -> "nvidia". The distinctive head of the name,
    which is what anyone on air actually says."""
    cleaned = _SUFFIXES.sub(" ", str(name))
    cleaned = re.sub(r"[^A-Za-z& ]", " ", cleaned)
    parts = [p for p in cleaned.split() if len(p) > 1]
    return " ".join(parts[:2]).strip().lower() if parts else ""


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN stays null rather than becoming 0
