"""Nasdaq's public screener JSON — the whole US universe in one call.

Verified 2026-07-27: 7,033 rows, 2.2 MB, 2.35 seconds, no API key. This
single endpoint is what makes a Finviz-class screener feasible at zero
cost; without it you would be paying someone for a ticker list with
sectors attached.

Every value arrives as a display string ("$138.57", "-0.837%",
"39136594480.00", or "" / "NA" for missing), so parsing is deliberately
forgiving — a few hundred of the 7,000 rows are warrants, units, and
preferreds with half the fields blank.

Two forward calendars live here too, verified 2026-08-11:

  /api/calendar/dividends?date=YYYY-MM-DD   one exchange day per call
  /api/ipo/calendar?date=YYYY-MM            one month per call

The dividend calendar is the only free source that gives all four dates
that matter — declared, ex, record and payment — for the whole market
rather than one symbol at a time. It is a call *per day*, which is what
shapes everything downstream: `api.nasdaq.com` is rate-limited to 2/s
here, so a six-week window costs the better part of half a minute and
cannot sit in the universe rebuild.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import net
from .base import ProviderError

URL = ("https://api.nasdaq.com/api/screener/stocks"
       "?tableonly=true&limit=10000&offset=0&download=true")

DIVIDENDS_URL = "https://api.nasdaq.com/api/calendar/dividends?date={date}"
IPO_URL = "https://api.nasdaq.com/api/ipo/calendar?date={month}"

# Corporate calendars are exchange dates, so they are anchored to the
# exchange's own day rather than to the reader's. From India a 2026-08-14
# ex-date parsed as local midnight lands nine and a half hours early, and
# "goes ex tomorrow" quietly becomes "went ex today".
EASTERN = ZoneInfo("America/New_York")


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


def _date(v) -> float | None:
    """Nasdaq's M/D/YYYY -> epoch seconds at exchange midnight.

    Returns None for the blanks and the em-dash placeholders these
    calendars use for a date that has not been set yet — a record date is
    genuinely unknown until the dividend is declared, and inventing one
    would be worse than showing nothing.
    """
    s = str(v or "").strip()
    if not s or s in ("N/A", "NA", "--", "-", "TBD"):
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return d.replace(tzinfo=EASTERN).timestamp()
    return None


def _range(v) -> tuple[float | None, float | None]:
    """A price that may be a range: "19.00-22.00" -> (19.0, 22.0).

    An IPO that has not priced yet has a filed range, and one that has
    priced has a single number. Both arrive in the same field, so the
    caller gets a low and a high and they are equal once it prices.
    """
    s = str(v or "").strip().replace("$", "")
    if not s:
        return None, None
    if "-" in s[1:]:
        lo, _, hi = s.partition("-")
        return _f(lo), _f(hi)
    one = _f(s)
    return one, one


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


# ---- dividend calendar ----

def dividends_on(day: date, ttl: float = 12 * 3600) -> list[dict]:
    """Every dividend going ex on one exchange day.

    A missing day is an empty list, not an error: weekends and holidays
    legitimately have none, and so does a quiet Tuesday. Only a transport
    failure raises, because that is the case the caller must not mistake
    for "nothing goes ex that day".
    """
    url = DIVIDENDS_URL.format(date=day.isoformat())
    try:
        payload = json.loads(net.fetch(url, ttl=ttl, retries=2, timeout=30))
    except net.FetchError as exc:
        raise ProviderError(f"nasdaq dividends {day}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"nasdaq dividends {day}: bad json ({exc})") from exc

    rows = (((payload.get("data") or {}).get("calendar") or {}).get("rows")
            or [])
    return _parse_dividend_rows(rows)


def _parse_dividend_rows(rows: list[dict]) -> list[dict]:
    """Nasdaq's calendar rows -> ours. Split out so the shape can be
    tested against a pasted payload rather than against the market."""
    out = []
    for r in rows or []:
        symbol = (r.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        out.append({
            "symbol": symbol.replace("/", "-"),
            "name": (r.get("companyName") or "").strip(),
            # The four dates, in the order they happen. Ex and record are
            # usually the same day now that settlement is T+1; they were
            # two days apart under T+2 and the calendar still ships both.
            "declared_ts": _date(r.get("announcement_Date")),
            "ex_ts": _date(r.get("dividend_Ex_Date")),
            "record_ts": _date(r.get("record_Date")),
            "pay_ts": _date(r.get("payment_Date")),
            # This payment, and what a year of them comes to. The annual
            # figure is Nasdaq's own indication, not four times the rate —
            # it already accounts for monthly and semi-annual payers.
            "amount": _f(r.get("dividend_Rate")),
            # A zero indicated annual is Nasdaq saying it has no recurring
            # rate for this name, which is what a special or irregular
            # dividend looks like. Kept as null rather than 0.0: a company
            # paying five cents this week rendered as "yield 0.00%" reads
            # as paying nothing at all, when the truth is the payment is
            # real and simply not annualisable.
            "annual_amount": _f(r.get("indicated_Annual_Dividend")) or None,
        })
    return out


def dividends(start: date | None = None, days: int = 42,
              ttl: float = 12 * 3600,
              progress=None) -> dict[str, dict]:
    """The dividend calendar over a window, keyed by symbol.

    One call per day, at 2 requests a second — a six-week window is about
    twenty seconds of wall clock and is why this is refreshed on its own
    schedule rather than inside a universe rebuild.

    Where a symbol pays twice inside the window the *nearest* payment
    wins: the question this feeds is "what is coming up", and the second
    one is still there next time the window rolls.
    """
    start = start or datetime.now(EASTERN).date()
    out: dict[str, dict] = {}
    for offset in range(max(1, days)):
        day = start + timedelta(days=offset)
        # Saturdays and Sundays never carry an ex-date, so skipping them
        # takes a fifth off the window's cost for nothing given up.
        if day.weekday() >= 5:
            continue
        try:
            rows = dividends_on(day, ttl=ttl)
        except ProviderError:
            continue        # one bad day must not lose the other forty-one
        for row in rows:
            out.setdefault(row["symbol"], row)
        if progress:
            progress(offset + 1, days)
    return out


# ---- IPO calendar ----

_IPO_BUCKETS = ("upcoming", "priced", "filed", "withdrawn")


def ipos_in(month: str, ttl: float = 3 * 3600) -> dict[str, list[dict]]:
    """One month of the IPO calendar, split by where each deal has got to.

    Nasdaq nests `upcoming` one level deeper than the other three — it is
    `upcoming.upcomingTable.rows` where the rest are `<bucket>.rows` — and
    reports an empty bucket as an error object rather than an empty list.
    Both are normal and neither is a failure.
    """
    url = IPO_URL.format(month=month)
    try:
        payload = json.loads(net.fetch(url, ttl=ttl, retries=2, timeout=30))
    except net.FetchError as exc:
        raise ProviderError(f"nasdaq ipos {month}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"nasdaq ipos {month}: bad json ({exc})") from exc

    data = payload.get("data") or {}
    out: dict[str, list[dict]] = {}
    for bucket in _IPO_BUCKETS:
        node = data.get(bucket) or {}
        if bucket == "upcoming":
            node = node.get("upcomingTable") or {}
        out[bucket] = _parse_ipo_rows(node.get("rows") or [], bucket)
    return out


def _parse_ipo_rows(rows: list[dict], bucket: str) -> list[dict]:
    """One bucket's rows -> ours, testable without the network."""
    out = []
    for r in rows or []:
        symbol = (r.get("proposedTickerSymbol") or "").strip().upper()
        low, high = _range(r.get("proposedSharePrice"))
        out.append({
            "symbol": symbol.replace("/", "-") or None,
            "name": (r.get("companyName") or "").strip(),
            "exchange": (r.get("proposedExchange") or "").strip() or None,
            "price_low": low,
            "price_high": high,
            "shares": _f(r.get("sharesOffered")),
            "value": _f(r.get("dollarValueOfSharesOffered")),
            # Whichever date this bucket has. A deal that has priced
            # carries the day it did; one that has not carries the day it
            # is expected to, which moves.
            "date_ts": _date(r.get("pricedDate")
                             or r.get("expectedPriceDate")
                             or r.get("filedDate")
                             or r.get("withdrawDate")),
            "status": (r.get("dealStatus") or "").strip() or None,
            "bucket": bucket,
            "deal_id": r.get("dealID"),
        })
    return out


def ipos(months: int = 3, ttl: float = 3 * 3600) -> dict[str, list[dict]]:
    """The IPO calendar across a few months, merged bucket by bucket.

    Reaches one month back as well as forward: a deal that priced last
    week is the most useful row on the board — it is the only one with a
    market price to compare against what it asked for.
    """
    today = datetime.now(EASTERN).date()
    out: dict[str, list[dict]] = {b: [] for b in _IPO_BUCKETS}
    seen: set = set()
    cursor = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    for _ in range(max(1, months)):
        try:
            got = ipos_in(cursor.strftime("%Y-%m"), ttl=ttl)
        except ProviderError:
            got = {}
        for bucket, rows in got.items():
            for row in rows:
                key = (bucket, row.get("deal_id") or row.get("name"))
                if key in seen:
                    continue
                seen.add(key)
                out[bucket].append(row)
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    for rows in out.values():
        rows.sort(key=lambda r: r["date_ts"] or 0)
    return out
