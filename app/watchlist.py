"""The watchlist.

A thin layer over the `positions` table in `store.py`, which has existed
since the first commit and until now was called from nowhere. Shares and
cost basis are optional: most rows on a watchlist are things you are
looking at, not things you own, and demanding a position to watch a
symbol would be the wrong trade-off.

Live prices are not fetched here. The server merges quotes in, because it
already has the stream and the short-lived quote cache and this module
should not care where a price came from.
"""

from __future__ import annotations

from . import store


def all() -> list[dict]:
    """Watched symbols, oldest first so the list doesn't reshuffle."""
    rows = store.positions()
    rows.sort(key=lambda r: (r.get("opened_at") or 0))
    return rows


def symbols() -> list[str]:
    return [r["symbol"] for r in all()]


def add(symbol: str, shares: float = 0.0, cost_basis: float | None = None,
        note: str = "") -> dict:
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise ValueError("need a symbol")
    store.upsert_position(symbol, float(shares or 0), cost_basis, note)
    return {"symbol": symbol}


def remove(symbol: str) -> None:
    store.delete_position((symbol or "").strip().upper())


def toggle(symbol: str) -> bool:
    """Add if absent, remove if present. Returns whether it is now watched
    — this is what the `W` key binds to, and it needs to be one call."""
    symbol = (symbol or "").strip().upper()
    if symbol in set(symbols()):
        remove(symbol)
        return False
    add(symbol)
    return True


def enrich(rows: list[dict], quotes: dict) -> list[dict]:
    """Merge live quotes in and compute P&L where a cost basis exists.

    `market_value` only needs shares and a live price — it used to sit
    behind the same `basis` guard as P&L and went null on every position
    logged without a cost basis, even though nothing about that number
    was actually unknown. P&L itself still needs a basis to mean anything,
    and a row with no shares gets none rather than a $0.00 that would read
    as a real number and isn't.
    """
    out = []
    for row in rows:
        q = quotes.get(row["symbol"]) or {}
        item = dict(row)
        item.update({
            "price": q.get("price"),
            "change": q.get("change"),
            "change_pct": q.get("change_pct"),
            "volume": q.get("volume"),
            "ext_label": q.get("ext_label"),
            "ext_change_pct": q.get("ext_change_pct"),
        })
        shares = row.get("shares") or 0
        basis = row.get("cost_basis")
        price = q.get("price")
        item["market_value"] = (shares * price
                                if shares and price is not None else None)
        # `basis` as a truthiness check reads a real $0 cost basis — gifted
        # or granted shares, not an uncommon position to be watching — the
        # same way it reads a never-entered one: no P&L at all. `is not
        # None` is the only test that tells the two apart.
        if shares and basis is not None and price is not None:
            item["pnl"] = (price - basis) * shares
            item["pnl_pct"] = ((price - basis) / basis * 100) if basis else None
        else:
            item["pnl"] = item["pnl_pct"] = None
        out.append(item)
    return out
