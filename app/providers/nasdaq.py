"""Nasdaq's public screener JSON — the whole US universe in one call.

Verified 2026-07-27: 7,033 rows, 2.2 MB, 2.35 seconds, no API key. This
single endpoint is what makes a Finviz-class screener feasible at zero
cost; without it you would be paying someone for a ticker list with
sectors attached.

Every value arrives as a display string ("$138.57", "-0.837%",
"39136594480.00", or "" / "NA" for missing), so parsing is deliberately
forgiving — a few hundred of the 7,000 rows are warrants, units, and
preferreds with half the fields blank.
"""

from __future__ import annotations

import json

from .. import net
from .base import ProviderError

URL = ("https://api.nasdaq.com/api/screener/stocks"
       "?tableonly=true&limit=10000&offset=0&download=true")


def _f(v) -> float | None:
    """Parse Nasdaq's display strings into numbers. '$1,234.56' -> 1234.56,
    '-0.837%' -> -0.837, '' / 'NA' / '--' -> None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("NA", "N/A", "--", "-"):
        return None
    s = s.replace("$", "").replace(",", "").replace("%", "").strip()
    if s.startswith("(") and s.endswith(")"):  # (1.23) == -1.23
        s = "-" + s[1:-1]
    try:
        f = float(s)
    except ValueError:
        return None
    return f if f == f else None


def _i(v) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


def universe(ttl: float = 6 * 3600) -> list[dict]:
    """The full listed universe with sector, industry and market cap."""
    try:
        raw = net.fetch(URL, ttl=ttl, retries=3, timeout=60)
    except net.FetchError as exc:
        raise ProviderError(f"nasdaq screener: {exc}") from exc

    try:
        rows = json.loads(raw)["data"]["rows"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProviderError(f"nasdaq screener: unexpected shape ({exc})") from exc

    if not rows:
        raise ProviderError("nasdaq screener returned zero rows")

    out = []
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        # Nasdaq uses '/' for share classes where Yahoo uses '-' (BRK/B vs
        # BRK-B). Normalize now so the join with quotes actually matches.
        out.append({
            "symbol": sym.replace("/", "-"),
            "name": (r.get("name") or "").strip(),
            "price": _f(r.get("lastsale")),
            "change": _f(r.get("netchange")),
            "change_pct": _f(r.get("pctchange")),
            "volume": _f(r.get("volume")),
            "market_cap": _f(r.get("marketCap")),
            "country": (r.get("country") or "").strip() or None,
            "ipo_year": _i(r.get("ipoyear")),
            "industry": (r.get("industry") or "").strip() or None,
            "sector": (r.get("sector") or "").strip() or None,
        })
    return out
