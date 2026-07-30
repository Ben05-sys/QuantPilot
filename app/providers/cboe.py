"""Cboe delayed option chains — free, no key, and greeks included.

Verified 2026-07-27: MU returned 12,096 contracts with delta/gamma/theta/
vega/rho, IV, open interest and volume, in 2.8s. Real-time options means
an OPRA licence starting around $99/month, so delayed-with-greeks is the
ceiling at zero cost — and it is a high ceiling. Finviz has no options
screening at all.

Contract identifiers are OCC-format: MU    260130C00500000
                                    ^root ^yymmdd ^C/P ^strike*1000
"""

from __future__ import annotations

import json
import re

from .. import net
from .base import ProviderError

URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"

# Root is 1-6 chars, then 6 digits of date, then C or P, then 8 of strike.
_OCC = re.compile(r"^(?P<root>[A-Z0-9]{1,6})(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


def _parse_occ(sym: str) -> tuple[str | None, str | None, float | None]:
    """-> (expiry ISO, 'call'|'put', strike). Returns Nones if unparseable
    rather than raising; a malformed contract should not kill the chain."""
    m = _OCC.match((sym or "").strip())
    if not m:
        return None, None, None
    ymd = m.group("ymd")
    expiry = f"20{ymd[0:2]}-{ymd[2:4]}-{ymd[4:6]}"
    kind = "call" if m.group("cp") == "C" else "put"
    return expiry, kind, int(m.group("strike")) / 1000.0


def chain(symbol: str, ttl: float = 600) -> dict:
    """Full option chain with greeks. Delayed — label it as such."""
    url = URL.format(sym=symbol.strip().upper())
    try:
        raw = net.fetch(url, ttl=ttl, retries=2, timeout=60)
    except net.FetchError as exc:
        if exc.status == 404:
            raise ProviderError(f"cboe: no chain for {symbol}") from exc
        raise ProviderError(f"cboe {symbol}: {exc}") from exc

    try:
        d = json.loads(raw)["data"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProviderError(f"cboe {symbol}: unexpected shape ({exc})") from exc

    contracts = []
    for c in d.get("options") or []:
        expiry, kind, strike = _parse_occ(c.get("option", ""))
        if expiry is None:
            continue
        contracts.append({
            "contract": c.get("option"),
            "expiry": expiry,
            "type": kind,
            "strike": strike,
            "bid": c.get("bid"), "ask": c.get("ask"),
            "last": c.get("last_trade_price"),
            "volume": c.get("volume"),
            "open_interest": c.get("open_interest"),
            "iv": c.get("iv"),
            "delta": c.get("delta"), "gamma": c.get("gamma"),
            "theta": c.get("theta"), "vega": c.get("vega"), "rho": c.get("rho"),
            "theo": c.get("theo"),
        })

    contracts.sort(key=lambda x: (x["expiry"], x["type"], x["strike"] or 0))
    return {
        "symbol": d.get("symbol", symbol),
        "price": d.get("current_price"),
        "iv30": d.get("iv30"),
        "expiries": sorted({c["expiry"] for c in contracts}),
        "contracts": contracts,
        "delayed": True,
        "source": "cboe",
    }
