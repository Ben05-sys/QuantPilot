"""Build the screenable universe.

    Nasdaq screener  ->  7,033 symbols with sector/industry/market cap  (1 call)
    Yahoo batch quote ->  83 fields each, 200 symbols per call          (~36 calls)
                      ->  merge -> SQLite snapshot -> pandas

The whole thing takes well under a minute and costs nothing. Screening
then runs against the local DataFrame, so it is fast and unmetered — no
rate limit, no paywall, no network in the hot path. That is the single
architectural decision that separates this from a Finviz scraper.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import config, store
from .providers import nasdaq, yahoo
from .providers.base import ProviderError

Progress = Callable[[str, float], None]


def _noop(msg: str, frac: float) -> None:
    pass


def _q(quote) -> dict:
    """Yahoo quote -> universe columns. `extra` is the raw 83-field payload."""
    e = quote.extra or {}
    n = yahoo._num
    ext_label, ext_price, ext_change_pct = quote.extended()
    return {
        "price": quote.price,
        "change": quote.change,
        "change_pct": quote.change_pct,
        "volume": quote.volume,
        "market_cap": quote.market_cap,
        "day_high": quote.day_high,
        "day_low": quote.day_low,
        "prev_close": quote.prev_close,
        "open": n(e.get("regularMarketOpen")),
        "avg_volume_3m": n(e.get("averageDailyVolume3Month")),
        "avg_volume_10d": n(e.get("averageDailyVolume10Day")),
        "shares_outstanding": n(e.get("sharesOutstanding")),
        "pe": n(e.get("trailingPE")),
        "forward_pe": n(e.get("forwardPE")),
        "pb": n(e.get("priceToBook")),
        "eps_ttm": n(e.get("epsTrailingTwelveMonths")),
        "eps_forward": n(e.get("epsForward")),
        "book_value": n(e.get("bookValue")),
        "dividend_yield": n(e.get("dividendYield")),
        "dividend_rate": n(e.get("dividendRate")),
        "week52_high": n(e.get("fiftyTwoWeekHigh")),
        "week52_low": n(e.get("fiftyTwoWeekLow")),
        "week52_change_pct": n(e.get("fiftyTwoWeekChangePercent")),
        "sma50": n(e.get("fiftyDayAverage")),
        "sma200": n(e.get("twoHundredDayAverage")),
        "analyst_rating": n(e.get("averageAnalystRating")),
        "earnings_ts": n(e.get("earningsTimestamp")),
        "exchange": e.get("fullExchangeName"),
        "quote_type": e.get("quoteType"),
        "currency": e.get("currency"),
        "market_state": quote.market_state,
        "lag_seconds": quote.lag_seconds,
        "pre_price": quote.pre_price,
        "pre_change_pct": quote.pre_change_pct,
        "pre_time": quote.pre_time,
        "post_price": quote.post_price,
        "post_change_pct": quote.post_change_pct,
        "post_time": quote.post_time,
        # Resolved here rather than in the UI: which extended session counts
        # is the venue's call, and it has to be the same answer for every
        # row in a snapshot or a screen on `extchg` compares two sessions.
        "ext_label": ext_label,
        "ext_price": ext_price,
        "ext_change_pct": ext_change_pct,
    }


def refresh(progress: Progress = _noop, enrich: bool = True,
            limit: int | None = None, note: str = "") -> dict:
    """Rebuild the universe snapshot. Returns metadata about the run."""
    t0 = time.time()

    progress("fetching universe from Nasdaq", 0.02)
    rows = nasdaq.universe(ttl=0)
    by_symbol = {r["symbol"]: r for r in rows}
    if limit:
        keep = sorted(by_symbol, key=lambda s: -(by_symbol[s]["market_cap"] or 0))[:limit]
        by_symbol = {s: by_symbol[s] for s in keep}
    progress(f"{len(by_symbol)} symbols listed", 0.10)

    enriched = 0
    if enrich:
        symbols = sorted(by_symbol)
        batches = [symbols[i:i + yahoo.BATCH]
                   for i in range(0, len(symbols), yahoo.BATCH)]
        for i, batch in enumerate(batches):
            try:
                for quote in yahoo.provider.quotes(batch):
                    row = by_symbol.get(quote.symbol)
                    if row is None:
                        continue
                    # Only overwrite with values that actually arrived; a
                    # partial Yahoo response must not blank out Nasdaq data.
                    for k, v in _q(quote).items():
                        if v is not None:
                            row[k] = v
                    enriched += 1
            except ProviderError as exc:
                # One bad batch out of 36 should not lose the whole refresh.
                progress(f"batch {i + 1}/{len(batches)} failed: {exc}", 0)
            progress(f"quotes {i + 1}/{len(batches)}",
                     0.10 + 0.85 * (i + 1) / len(batches))

    elapsed = time.time() - t0
    # The note records *why* a generation exists — open / midday / close /
    # manual — which is what makes the point-in-time history readable
    # later instead of being an undifferentiated pile of timestamps.
    tag = f"{note} " if note else ""
    ts = store.write_snapshot(list(by_symbol.values()), elapsed=elapsed,
                              note=f"{tag}enriched={enriched}")
    progress("snapshot written", 1.0)
    return {"snapshot_ts": ts, "rows": len(by_symbol), "enriched": enriched,
            "elapsed": elapsed}


# ---- loading for the screener ----

_cache: dict = {"ts": None, "df": None, "live_at": None}


# Cumulative share of a normal session's volume, at half-hour marks from
# the 09:30 open. US equity volume is U-shaped: heavy at the open, thin
# over lunch, heavy into the closing auction. Comparing volume-so-far
# against a *full-day* average is what made every momentum screen return
# nothing before midday — at 10am a busy stock has traded maybe a fifth of
# its day, so an honest 2.5x looked like 0.5x.
#
# This curve is an approximation of typical large-cap behaviour, not a
# measurement of any particular name. It is far closer than assuming
# volume arrives evenly, and once enough snapshots have accumulated it
# could be replaced by a curve measured from our own history.
_VOLUME_CURVE = [
    (0.0, 0.00), (0.5, 0.13), (1.0, 0.21), (1.5, 0.28), (2.0, 0.34),
    (2.5, 0.39), (3.0, 0.44), (3.5, 0.49), (4.0, 0.54), (4.5, 0.59),
    (5.0, 0.65), (5.5, 0.72), (6.0, 0.81), (6.5, 1.00),
]

EASTERN = ZoneInfo("America/New_York")

# Floor on the divisor. Two minutes into the session the curve expects
# well under 1% of the day's volume, and dividing by that turns any normal
# opening print into a 250x reading. The first few minutes are genuinely
# noisy; this caps the exaggeration at 20x rather than pretending to
# precision the sample cannot support.
MIN_SESSION_FRACTION = 0.05


def session_fraction(now: datetime | None = None) -> float:
    """How much of a normal session's volume should have traded by now.

    1.0 outside regular hours, so a closed market compares full day
    against full day and the number means what it always did.
    """
    now = (now or datetime.now(EASTERN)).astimezone(EASTERN)
    hours = (now.hour - 9) + (now.minute - 30) / 60.0
    if hours <= 0 or hours >= 6.5:
        return 1.0
    for (h0, f0), (h1, f1) in zip(_VOLUME_CURVE, _VOLUME_CURVE[1:]):
        if hours <= h1:
            span = h1 - h0
            fraction = f0 + (f1 - f0) * ((hours - h0) / span if span else 0)
            return max(fraction, MIN_SESSION_FRACTION)
    return 1.0


def _live_fraction(df: pd.DataFrame) -> float:
    """Only adjust when the venue says the regular session is running.

    Deriving this from the wall clock instead would inflate every reading
    on a market holiday — 11am on a Tuesday looks like mid-session, but
    nothing has traded.
    """
    if "market_state" not in df.columns:
        return 1.0
    states = df["market_state"].dropna()
    if states.empty:
        return 1.0
    if str(states.mode().iloc[0]).upper() != "REGULAR":
        return 1.0
    return session_fraction()


def _earnings_when(timestamps: pd.Series) -> pd.Series:
    """Epoch -> BMO / AMC / DMH, read in exchange time.

    Yahoo gives a precise hour (verified: BA 08:30 ET, MSFT 16:00 ET), so
    this is read rather than guessed. Anything without a timestamp stays
    null instead of being bucketed into a default that would read as fact.
    """
    seconds = pd.to_numeric(timestamps, errors="coerce")
    out = pd.Series([None] * len(seconds), index=seconds.index, dtype=object)
    valid = seconds.notna()
    if not valid.any():
        return out
    local = (pd.to_datetime(seconds[valid], unit="s", utc=True)
             .dt.tz_convert(EASTERN))
    hours = local.dt.hour + local.dt.minute / 60.0
    out.loc[valid] = np.where(
        hours < 9.5, "BMO",
        np.where(hours >= 16.0, "AMC", "DMH"))
    return out


def derive(df: pd.DataFrame) -> pd.DataFrame:
    """Add the computed columns. Everything here is arithmetic on fields we
    already have — nothing is invented or imputed. A field we cannot
    compute stays NaN rather than being guessed at."""
    def safe(a, b):
        b = b.replace(0, np.nan)
        return a / b

    # Relative volume, against what *should* have traded by this point in
    # the session rather than against the whole day. `rel_volume_raw` keeps
    # the unadjusted ratio for anyone who wants it; `rel_volume` is the one
    # that answers the question people actually mean by "is this unusually
    # active right now".
    df["rel_volume_raw"] = safe(df["volume"], df["avg_volume_3m"])
    fraction = _live_fraction(df)
    df["rel_volume"] = (df["rel_volume_raw"] / fraction if fraction > 0
                        else df["rel_volume_raw"])
    df["session_fraction"] = fraction
    # The same question asked against a two-week baseline instead of a
    # three-month one. A name that has been busy for a fortnight has
    # already lifted its own 10-day average, so a reading near 1 here
    # sitting beside a `rel_volume` of 3 says "elevated, but this is its
    # new normal" — while a high one says today is unusual even against
    # that recent activity. Checked on a live 6,338-name snapshot: the two
    # correlate 0.73, so this is a genuinely different question and not a
    # second spelling of the first. Session-adjusted the same way, for the
    # same reason: an unadjusted ratio at 09:45 is small by construction.
    if "avg_volume_10d" in df.columns:
        raw_10d = safe(df["volume"], df["avg_volume_10d"])
        df["rel_volume_10d"] = raw_10d / fraction if fraction > 0 else raw_10d
    else:
        df["rel_volume_10d"] = np.nan
    # Dollar volume — what a name actually trades, in money rather than
    # shares. 40M shares of a $2 stock and 40M shares of a $200 stock are
    # the same number and nothing like the same market, which is why every
    # liquidity rule a desk uses is written in dollars. `avg_dollar_volume`
    # is the baseline to screen against: today's figure is honestly small
    # at 09:45 and says nothing about whether a name is normally liquid.
    df["dollar_volume"] = df["price"] * df["volume"]
    df["avg_dollar_volume"] = df["price"] * df["avg_volume_3m"]
    # Where the last price sits in the day's range, 0 at the low and 100 at
    # the high. A stock closing at 95 is a different tape from one closing
    # at 15 on the same percentage change. NaN, not 50, when the range has
    # no width — an untraded name has no position in a range it never made,
    # and a snapshot old enough to predate the high/low columns has no
    # business inventing one either.
    if "day_high" in df.columns and "day_low" in df.columns:
        df["day_range_pct"] = safe(df["price"] - df["day_low"],
                                   df["day_high"] - df["day_low"]) * 100
    else:
        df["day_range_pct"] = np.nan
    # The move that happened before the session opened — distinct from
    # anything that has traded since, and the first thing a gap-and-go
    # screen needs. Null rather than 0 when either side is missing, same
    # as every other ratio here.
    if "open" in df.columns and "prev_close" in df.columns:
        df["gap_pct"] = safe(df["open"] - df["prev_close"], df["prev_close"]) * 100
    else:
        df["gap_pct"] = np.nan
    # The real daily range, in dollars: `day_high - day_low` alone misses
    # the overnight gap, so a stock that gapped down hard and then sat
    # still reads as barely having moved. skipna=False so a missing input
    # yields null rather than a range computed from whatever happened to
    # be present.
    if {"day_high", "day_low", "prev_close"} <= set(df.columns):
        spans = pd.concat([
            df["day_high"] - df["day_low"],
            (df["day_high"] - df["prev_close"]).abs(),
            (df["day_low"] - df["prev_close"]).abs(),
        ], axis=1)
        df["true_range"] = spans.max(axis=1, skipna=False)
    else:
        df["true_range"] = np.nan
    df["pct_from_52w_high"] = safe(df["price"] - df["week52_high"], df["week52_high"]) * 100
    df["pct_from_52w_low"] = safe(df["price"] - df["week52_low"], df["week52_low"]) * 100
    df["pct_from_sma50"] = safe(df["price"] - df["sma50"], df["sma50"]) * 100
    df["pct_from_sma200"] = safe(df["price"] - df["sma200"], df["sma200"]) * 100
    # Relative strength against a stock's own sector. "Up 3%" on a day the
    # whole sector rose 3% is not strength; this separates the move from
    # the tide. Cap-weighted, because an unweighted sector average is
    # dominated by microcaps nobody trades.
    if "sector" in df.columns:
        weights = df["market_cap"].fillna(0)
        parts = pd.DataFrame({
            "sector": df["sector"],
            "w": weights,
            "cw": df["change_pct"].fillna(0) * weights,
        })
        agg = parts.groupby("sector", dropna=True).agg(w=("w", "sum"),
                                                       cw=("cw", "sum"))
        agg["chg"] = agg["cw"] / agg["w"].replace(0, np.nan)
        df["sector_change_pct"] = df["sector"].map(agg["chg"])
        df["rs_sector"] = df["change_pct"] - df["sector_change_pct"]
    else:
        df["sector_change_pct"] = np.nan
        df["rs_sector"] = np.nan

    df["earnings_days"] = (df["earnings_ts"] - time.time()) / 86400.0
    # When in the day a company reports. Before the open and after the
    # close are different risks entirely: one gaps the stock before you can
    # react, the other moves it while the market is shut.
    df["earnings_when"] = _earnings_when(df["earnings_ts"])
    df["peg"] = np.nan  # needs growth estimates; left honest rather than faked
    # ADRs, from the security type Nasdaq puts in the name. Derived rather
    # than stored so it works on snapshots taken before the field existed —
    # `name` has always been there.
    #
    # Anchored to the phrase, not to a bare ADR/ADS: the loose version
    # matches ADS-TEC Energy, an Irish ordinary-share listing that is not
    # an ADR.
    # A frame with no `name` at all yields "no ADRs" rather than raising:
    # this is the one derived column that depends on a text field, and a
    # missing one is not worth taking the whole load path down.
    df["is_adr"] = (df["name"].fillna("").str.contains(
        "american depositary", case=False, regex=False)
        if "name" in df.columns else False)
    # Convenience aliases so both spellings work in expressions.
    df["mktcap"] = df["market_cap"]
    df["chg"] = df["change_pct"]
    return df


def load(ts: float | None = None, force: bool = False) -> pd.DataFrame:
    """The universe as a DataFrame, memoized per snapshot."""
    ts = ts or store.latest_snapshot_ts()
    if ts is None:
        return pd.DataFrame()
    if not force and _cache["ts"] == ts and _cache["df"] is not None:
        return _cache["df"]

    rows = store.read_snapshot(ts)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.drop(columns=["snapshot_ts"], errors="ignore")
    numeric = [n for n, t in store.UNIVERSE_COLUMNS if t in ("REAL", "INTEGER")]
    for col in numeric:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = derive(df)
    df.attrs["snapshot_ts"] = ts
    _cache.update(ts=ts, df=df)
    return df


def apply_live(quotes: dict) -> int:
    """Patch fresh prices into the in-memory snapshot.

    Deliberately *not* written back to SQLite. A snapshot generation is a
    point-in-time observation of the whole market at one instant, and
    dribbling rolling price updates into it would quietly turn it into a
    smear across the session — destroying the one property that makes the
    append-only design worth its disk space. The database keeps honest
    generations; this keeps the screener current between them.

    Returns how many rows were touched.
    """
    df = _cache.get("df")
    if df is None or df.empty or not quotes or "symbol" not in df.columns:
        return 0

    index = df.index[df["symbol"].isin(quotes.keys())]
    if len(index) == 0:
        return 0
    symbols = df.loc[index, "symbol"]

    numeric = ("price", "change", "change_pct", "volume", "market_cap",
               "day_high", "day_low", "pre_price", "pre_change_pct",
               "post_price", "post_change_pct", "ext_price", "ext_change_pct")
    for field in numeric:
        if field not in df.columns:
            continue
        fresh = pd.to_numeric(
            symbols.map(lambda s: (quotes.get(s) or {}).get(field)),
            errors="coerce")
        df.loc[index, field] = fresh.where(fresh.notna(), df.loc[index, field])
    for field in ("ext_label", "market_state"):
        if field not in df.columns:
            continue
        fresh = symbols.map(lambda s: (quotes.get(s) or {}).get(field))
        df.loc[index, field] = fresh.where(fresh.notna(), df.loc[index, field])

    # The derived columns are arithmetic on what just changed, so they are
    # recomputed for the whole frame rather than left inconsistent.
    _cache["df"] = derive(df)
    _cache["live_at"] = time.time()
    return int(len(index))


def live_age() -> float | None:
    """Seconds since the in-memory frame last had prices patched in."""
    at = _cache.get("live_at")
    return (time.time() - at) if at else None


def is_stale() -> bool:
    ts = store.latest_snapshot_ts()
    if ts is None:
        return True
    max_age = config.load_config()["universe_max_age_hours"] * 3600
    return (time.time() - ts) > max_age
