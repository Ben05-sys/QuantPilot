"""The forward corporate calendars: what goes ex, and what lists.

Both come from Nasdaq (`providers/nasdaq.py`) and both are kept here
rather than in the universe rebuild, for one measured reason: the dividend
calendar is a call *per exchange day* against a host limited to two
requests a second, so a six-week window is roughly twenty seconds. The
rebuild is twenty to forty seconds end to end and the whole design rests
on it staying that way, so bolting half a minute onto it to fetch data
that changes once a day would be paying a session's worth of latency for
nothing.

Instead this refreshes on its own clock, on a background thread, and the
screener joins whatever is currently known. Before the first refresh
finishes every dividend column is null — which is the honest answer and
renders as an em dash, not as "no dividend". That distinction is the
whole reason the columns are nullable rather than defaulted to zero.

A third clock, then, alongside the two in `clock.py`: the universe
snapshot moves in minutes, the visible-row tick in seconds, and the
corporate calendar in days.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

import numpy as np
import pandas as pd

from .providers import nasdaq
from .providers.base import ProviderError

# How far forward the dividend window reaches. Six weeks covers a full
# quarterly cycle plus the announcement lead, so a quarterly payer is
# always somewhere in it. Longer costs a call a day for rows almost
# nothing is screening on.
DIVIDEND_DAYS = 42

# Refresh cadence. Declarations land during the business day and never
# move intraday, so asking more often than this buys nothing.
DIVIDEND_TTL = 6 * 3600
IPO_TTL = 3 * 3600

# Months of IPO calendar, starting one month back — a deal that priced
# last week is the only row on the board with a market price to compare
# against what it asked for.
IPO_MONTHS = 3

# The columns this module contributes to the screener frame. Named here so
# `apply` can guarantee every one of them exists whether or not a refresh
# has ever succeeded: a column that is sometimes absent turns every screen
# mentioning it into "unknown field" instead of "nothing matched".
DIVIDEND_COLUMNS = ("ex_div_ts", "div_record_ts", "div_pay_ts",
                    "div_declared_ts", "div_amount", "div_annual_amount")


class _Calendar:
    """One background-refreshed payload with its own staleness clock."""

    def __init__(self, name: str, ttl: float, fetch):
        self.name = name
        self.ttl = ttl
        self._fetch = fetch
        self._lock = threading.Lock()
        self._data = None
        self._at = 0.0
        self._refreshing = False
        self.error = ""

    @property
    def age(self) -> float | None:
        return (time.time() - self._at) if self._at else None

    @property
    def version(self) -> float:
        """When this payload landed. The screener's frame cache compares
        it to decide whether the columns it holds are the current ones —
        a refresh arriving on a background thread has to reach a frame
        that was built before it, or the calendar would not appear until
        the next universe rebuild hours later."""
        return self._at

    def get(self):
        with self._lock:
            return self._data

    def stale(self) -> bool:
        return self._at == 0.0 or (time.time() - self._at) > self.ttl

    def ensure(self) -> None:
        """Kick a refresh if one is due. Never blocks the caller.

        The reader gets whatever is currently known — including nothing,
        on the first call of a session. Blocking here would put twenty
        seconds of Nasdaq in front of the first screen of the morning.
        """
        with self._lock:
            if self._refreshing or not self.stale():
                return
            self._refreshing = True
        threading.Thread(target=self._run, daemon=True,
                         name=f"pm-cal-{self.name}").start()

    def _run(self) -> None:
        try:
            data = self._fetch()
            with self._lock:
                self._data, self._at, self.error = data, time.time(), ""
        except Exception as exc:  # noqa: BLE001 — a calendar outage must
            self.error = f"{type(exc).__name__}: {exc}"   # not take the
        finally:                                          # terminal down
            with self._lock:
                self._refreshing = False

    def refresh_now(self):
        """Synchronous refresh, for the CLI and the tests."""
        self._run()
        return self.get()

    def status(self) -> dict:
        return {"age": self.age, "stale": self.stale(),
                "refreshing": self._refreshing, "error": self.error}


def _fetch_dividends():
    return nasdaq.dividends(days=DIVIDEND_DAYS, ttl=DIVIDEND_TTL)


def _fetch_ipos():
    return nasdaq.ipos(months=IPO_MONTHS, ttl=IPO_TTL)


dividends = _Calendar("dividends", DIVIDEND_TTL, _fetch_dividends)
ipos = _Calendar("ipos", IPO_TTL, _fetch_ipos)


def ensure_all() -> None:
    dividends.ensure()
    ipos.ensure()


def status() -> dict:
    div = dividends.get() or {}
    ipo = ipos.get() or {}
    return {
        "dividends": dict(dividends.status(), symbols=len(div)),
        "ipos": dict(ipos.status(),
                     **{b: len(rows) for b, rows in ipo.items()}),
    }


# ---- joining onto the screener frame ----

def apply(df: pd.DataFrame) -> pd.DataFrame:
    """Add the dividend-calendar columns to a universe frame.

    Mutates in place and returns the frame, matching `apply_live`. Every
    column in DIVIDEND_COLUMNS ends up present even when the calendar has
    never loaded, so the expression engine always recognises the field and
    a screen on it returns no rows rather than an error.
    """
    if df.empty or "symbol" not in df.columns:
        return df

    calendar = dividends.get() or {}
    if not calendar:
        for col in DIVIDEND_COLUMNS:
            if col not in df.columns:
                df[col] = np.nan
        return df

    symbols = df["symbol"]
    for col, key in (("ex_div_ts", "ex_ts"),
                     ("div_record_ts", "record_ts"),
                     ("div_pay_ts", "pay_ts"),
                     ("div_declared_ts", "declared_ts"),
                     ("div_amount", "amount"),
                     ("div_annual_amount", "annual_amount")):
        df[col] = pd.to_numeric(
            symbols.map(lambda s, key=key: (calendar.get(s) or {}).get(key)),
            errors="coerce")
    return df


def upcoming_dividends(df: pd.DataFrame, days: int = 14,
                       limit: int = 300) -> list[dict]:
    """Rows going ex within `days`, nearest first.

    Read off the screener frame rather than the raw calendar so every row
    carries what the terminal already knows about the company — price,
    yield, market cap, whether the payment is covered by earnings. The
    calendar alone would give four dates and a company name.
    """
    if df.empty or "ex_div_days" not in df.columns:
        return []
    hits = df[(df["ex_div_days"].notna()) & (df["ex_div_days"] >= -1)
              & (df["ex_div_days"] <= days)]
    hits = hits.sort_values("ex_div_days", kind="mergesort").head(limit)

    cols = ["symbol", "name", "price", "change_pct", "market_cap",
            "dividend_yield", "payout_ratio", "sector",
            "ex_div_ts", "div_record_ts", "div_pay_ts", "div_declared_ts",
            "div_amount", "div_annual_amount", "ex_div_days",
            "div_drop_pct", "div_yield_upcoming"]
    out = []
    for row in hits.to_dict("records"):
        item = {c: row.get(c) for c in cols if c in row}
        out.append({k: (None if isinstance(v, float) and np.isnan(v) else v)
                    for k, v in item.items()})
    return out


def ipo_board() -> dict:
    """The IPO calendar as the view wants it: four buckets and an as-of."""
    data = ipos.get() or {}
    return {
        "upcoming": data.get("upcoming") or [],
        "priced": list(reversed(data.get("priced") or [])),
        "filed": list(reversed(data.get("filed") or [])),
        "withdrawn": list(reversed(data.get("withdrawn") or [])),
        "as_of": time.time() - (ipos.age or 0),
        "stale": ipos.stale(),
        "error": ipos.error,
    }


def eastern_date() -> str:
    return datetime.now(nasdaq.EASTERN).date().isoformat()


def refresh_all(progress=None) -> dict:
    """Synchronous refresh of both, for `pilot.py --calendars`."""
    if progress:
        progress("fetching the dividend calendar", 0.05)
    dividends.refresh_now()
    if progress:
        progress("fetching the IPO calendar", 0.75)
    ipos.refresh_now()
    if progress:
        progress("calendars updated", 1.0)
    return status()


__all__ = ["DIVIDEND_COLUMNS", "apply", "dividends", "ensure_all",
           "eastern_date", "ipo_board", "ipos", "refresh_all", "status",
           "upcoming_dividends", "ProviderError"]
