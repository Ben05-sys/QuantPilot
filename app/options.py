"""Option chains, shaped for a terminal.

`providers/cboe.py` returns a flat list of every contract — 3,530 of them
for AAPL across 24 expiries. This turns that into what a chain view
actually needs: one expiry at a time, calls and puts paired by strike,
centred on the money.

Everything here is **delayed**. Cboe publishes delayed quotes for free and
real-time options means an OPRA licence starting around $99/month, so
delayed-with-greeks is the ceiling at zero cost. It is a high ceiling —
Finviz has no options screening at all — but the UI says DELAYED and
means it. The `delayed` flag rides on every payload out of this module so
no caller can lose it by accident.
"""

from __future__ import annotations

import time

from .providers import cboe
from .providers.base import ProviderError

# Volume this small is noise, not a signal, whatever the ratio says: one
# contract against zero open interest is an infinite ratio and means
# nothing.
MIN_UNUSUAL_VOLUME = 50


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def chain(symbol: str, expiry: str | None = None, strikes: int = 16,
          ttl: float = 300) -> dict:
    """One expiry, calls and puts paired by strike, centred on the money.

    `strikes` counts rows either side of spot, not in total — a chain
    showing only strikes above the money is useless for reading skew.
    """
    raw = cboe.chain(symbol, ttl=ttl)
    contracts = raw.get("contracts") or []
    expiries = raw.get("expiries") or []
    if not contracts:
        raise ProviderError(f"no option chain for {symbol}")

    chosen = expiry if expiry in expiries else (expiries[0] if expiries else None)
    spot = _f(raw.get("price"))

    rows: dict[float, dict] = {}
    for c in contracts:
        if c.get("expiry") != chosen:
            continue
        strike = _f(c.get("strike"))
        if strike is None:
            continue
        row = rows.setdefault(strike, {"strike": strike, "call": None,
                                       "put": None})
        row[c["type"]] = _leg(c)

    ordered = [rows[k] for k in sorted(rows)]
    # Centre the window on spot rather than taking the first N strikes,
    # which on a name like AAPL would return deep out-of-the-money puts
    # nobody is looking at.
    if spot is not None and len(ordered) > strikes * 2:
        nearest = min(range(len(ordered)),
                      key=lambda i: abs(ordered[i]["strike"] - spot))
        lo = max(0, nearest - strikes)
        ordered = ordered[lo:lo + strikes * 2 + 1]

    return {
        "symbol": raw.get("symbol", symbol.upper()),
        "price": spot,
        "iv30": _f(raw.get("iv30")),
        "expiry": chosen,
        "expiries": expiries,
        "rows": ordered,
        "stats": stats(contracts, chosen),
        "delayed": True,
        "source": "cboe",
        "as_of": time.time(),
    }


def _leg(c: dict) -> dict:
    bid, ask = _f(c.get("bid")), _f(c.get("ask"))
    volume, oi = _f(c.get("volume")), _f(c.get("open_interest"))
    return {
        "contract": c.get("contract"),
        "bid": bid, "ask": ask, "last": _f(c.get("last")),
        # A wide spread is the single most useful thing to see next to a
        # greek, and it is never quoted directly.
        "spread": (ask - bid) if (bid is not None and ask is not None) else None,
        "mid": ((bid + ask) / 2) if (bid is not None and ask is not None) else None,
        "volume": volume, "open_interest": oi,
        "vol_oi": (volume / oi) if (volume and oi) else None,
        "iv": _f(c.get("iv")),
        "delta": _f(c.get("delta")), "gamma": _f(c.get("gamma")),
        "theta": _f(c.get("theta")), "vega": _f(c.get("vega")),
        "rho": _f(c.get("rho")), "theo": _f(c.get("theo")),
    }


def stats(contracts: list[dict], expiry: str | None = None) -> dict:
    """Put/call ratios for the whole chain and for one expiry.

    Volume ratio reads today's positioning; open-interest ratio reads what
    is already on the books. They frequently disagree, and the difference
    is the interesting part, so both are reported rather than one being
    picked.
    """
    def totals(rows):
        out = {"call_volume": 0.0, "put_volume": 0.0,
               "call_oi": 0.0, "put_oi": 0.0}
        for c in rows:
            kind = c.get("type")
            if kind not in ("call", "put"):
                continue
            out[f"{kind}_volume"] += _f(c.get("volume")) or 0.0
            out[f"{kind}_oi"] += _f(c.get("open_interest")) or 0.0
        out["pc_volume"] = (out["put_volume"] / out["call_volume"]
                            if out["call_volume"] else None)
        out["pc_oi"] = (out["put_oi"] / out["call_oi"]
                        if out["call_oi"] else None)
        return out

    return {
        "all": totals(contracts),
        "expiry": totals([c for c in contracts if c.get("expiry") == expiry]),
        "contracts": len(contracts),
    }


def unusual(symbol: str, min_volume: float = MIN_UNUSUAL_VOLUME,
            min_ratio: float = 1.0, limit: int = 60,
            ttl: float = 300) -> dict:
    """Contracts trading more today than their entire open interest.

    Volume above open interest means the position is being *opened* now
    rather than traded among existing holders — the closest thing a free
    feed gives you to unusual activity. Finviz has no equivalent at all.

    Ranked by volume rather than by ratio: a 40x ratio on 60 contracts is
    arithmetic, while 60,000 contracts against 4,000 open is somebody
    doing something.
    """
    raw = cboe.chain(symbol, ttl=ttl)
    spot = _f(raw.get("price"))
    hits = []
    for c in raw.get("contracts") or []:
        volume = _f(c.get("volume")) or 0.0
        oi = _f(c.get("open_interest")) or 0.0
        if volume < min_volume or oi <= 0 or volume / oi < min_ratio:
            continue
        strike = _f(c.get("strike"))
        leg = _leg(c)
        leg.update({
            "expiry": c.get("expiry"), "type": c.get("type"),
            "strike": strike,
            # Notional at the mid, which is what makes a row worth
            # noticing: 60,000 contracts at $0.02 is not a big trade.
            "notional": ((leg["mid"] or 0) * volume * 100) or None,
            "moneyness": ((strike - spot) / spot * 100)
                         if (strike is not None and spot) else None,
        })
        hits.append(leg)

    hits.sort(key=lambda h: -(h["volume"] or 0))
    return {
        "symbol": raw.get("symbol", symbol.upper()),
        "price": spot,
        "rows": hits[:limit],
        "total": len(hits),
        "delayed": True,
        "source": "cboe",
        "as_of": time.time(),
    }
