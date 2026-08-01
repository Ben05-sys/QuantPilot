"""Yahoo Finance — quotes, batch quotes, and OHLCV bars.

These are undocumented internal endpoints. Yahoo retired its public API in
2017 and never replaced it, and its developer terms prohibit automated
access and monetization. This is fine for a single-user local terminal and
is NOT fine as the basis of anything published or sold. That constraint is
why everything here lives behind providers.base.QuoteProvider.

Measured behaviour (verified 2026-07-27 during US market hours):
  /v8/finance/chart   200, needs a User-Agent, no key, 3-25s behind the tape
  /v7/finance/quote   401 without cookie+crumb, 200 with, 500 symbols/call
  full consolidated volume, so this is the real tape and not IEX-only

The crumb dance:
  1. GET https://fc.yahoo.com/          -> 404, but sets the A3 cookie
  2. GET /v1/test/getcrumb              -> the crumb, using that cookie
  3. GET /v7/finance/quote?...&crumb=   -> 200
"""

from __future__ import annotations

import json
import threading
import time
from urllib.parse import quote_plus

from .. import net
from .base import Bars, ProviderError, Quote, register

BASE = "https://query1.finance.yahoo.com"

# Yahoo truncates well before this; 500 requested reliably returns ~466.
# Chunking at 200 keeps every response whole.
BATCH = 200

_crumb: str | None = None
_crumb_at: float = 0.0
_crumb_lock = threading.Lock()
CRUMB_TTL = 3600.0


def _get_crumb(force: bool = False) -> str:
    global _crumb, _crumb_at
    with _crumb_lock:
        if not force and _crumb and (time.time() - _crumb_at) < CRUMB_TTL:
            return _crumb
        if force:
            net.reset_session()
        # Priming call. It 404s by design; we only want the Set-Cookie.
        try:
            net.fetch("https://fc.yahoo.com/", ttl=0, retries=1, timeout=10)
        except net.FetchError:
            pass
        except Exception:
            pass
        try:
            raw = net.fetch(f"{BASE}/v1/test/getcrumb", ttl=0, retries=2,
                            timeout=15)
        except net.FetchError as exc:
            raise ProviderError(f"yahoo: could not mint crumb ({exc})") from exc
        crumb = raw.decode("utf-8", "replace").strip()
        if not crumb or len(crumb) > 32 or "<" in crumb:
            raise ProviderError(f"yahoo: implausible crumb {crumb[:40]!r}")
        _crumb, _crumb_at = crumb, time.time()
        return crumb


def _num(v):
    """Yahoo sometimes ships {'raw': 1.23, 'fmt': '1.23'} and sometimes a
    bare number. Accept both, and treat anything else as missing."""
    if isinstance(v, dict):
        v = v.get("raw")
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


NEWS_URL = (BASE + "/v1/finance/search"
            "?q={sym}&newsCount={count}&quotesCount=0&enableFuzzyQuery=false")


def news(symbol: str, count: int = 12, strict: bool = False,
         ttl: float = 120) -> dict:
    """Recent stories for one company.

    Verified 2026-07-28: ten items for NVDA, every one under half an hour
    old, each carrying `relatedTickers`. That field is the useful part —
    Yahoo's search returns anything *mentioning* the ticker, so a piece
    about three other companies that name-checks NVDA comes back too.
    `strict` keeps only stories where the symbol is actually tagged.

    A short TTL because news is not a quote: a two-minute-old headline is
    still news, and re-fetching per keystroke is not.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise ProviderError("news: no symbol")
    url = NEWS_URL.format(sym=quote_plus(symbol), count=max(1, min(count * 3, 50)))
    try:
        payload = json.loads(net.fetch(url, ttl=ttl, retries=2, timeout=20))
    except net.FetchError as exc:
        raise ProviderError(f"yahoo news {symbol}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"yahoo news {symbol}: bad json ({exc})") from exc

    items = []
    for row in (payload.get("news") or []):
        related = [str(t).upper() for t in (row.get("relatedTickers") or [])]
        tagged = symbol in related
        if strict and not tagged:
            continue
        published = _num(row.get("providerPublishTime"))
        items.append({
            "uuid": row.get("uuid"),
            "title": (row.get("title") or "").strip(),
            "publisher": (row.get("publisher") or "").strip(),
            "link": row.get("link"),
            "published": published,
            "age_seconds": (time.time() - published) if published else None,
            "type": row.get("type"),
            # Which other companies the story is about. Shown in the UI so
            # a piece tagged with eight tickers is visibly a roundup.
            "related": related,
            "tagged": tagged,
            "thumbnail": _thumb(row.get("thumbnail")),
        })
    items.sort(key=lambda i: -(i["published"] or 0))
    items = items[:count]
    # Mark these as readable in-terminal. Only stories we have actually
    # served can be fetched later, so the reader route can never be used
    # to make the server request an arbitrary address.
    from .. import article
    for item in items:
        article.register(item["uuid"], item["link"])
    return {"symbol": symbol, "items": items,
            "source": "yahoo", "as_of": time.time()}


SEARCH_URL = (BASE + "/v1/finance/search"
              "?q={q}&quotesCount={count}&newsCount=0&enableFuzzyQuery=true")

# Where a listing trades, roughly in the order someone typing a US ticker
# means it. Yahoo returns the Toronto, Frankfurt and Amsterdam listings of
# NVIDIA above some US names otherwise.
_EXCHANGE_RANK = {"NMS": 0, "NGM": 0, "NCM": 0, "NYQ": 0, "ASE": 1,
                  "PCX": 1, "BTS": 1, "NYS": 0}


def search(query: str, count: int = 10, ttl: float = 900) -> dict:
    """Symbol and company lookup.

    Ranked before returning: a search for "nvid" comes back from Yahoo
    with NVIDIA's Toronto CDR and Frankfurt listing interleaved among US
    names, which is not what anyone typing into a US terminal wants.
    """
    query = (query or "").strip()
    if not query:
        return {"query": "", "results": []}
    url = SEARCH_URL.format(q=quote_plus(query), count=max(count * 3, 15))
    try:
        payload = json.loads(net.fetch(url, ttl=ttl, retries=2, timeout=15))
    except net.FetchError as exc:
        raise ProviderError(f"yahoo search: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"yahoo search: bad json ({exc})") from exc

    results = []
    for row in (payload.get("quotes") or []):
        symbol = (row.get("symbol") or "").strip()
        if not symbol:
            continue
        results.append({
            "symbol": symbol,
            "name": row.get("shortname") or row.get("longname") or "",
            "type": row.get("quoteType") or "",
            "exchange": row.get("exchDisp") or row.get("exchange") or "",
            "sector": row.get("sectorDisp") or row.get("sector") or "",
            "industry": row.get("industryDisp") or row.get("industry") or "",
            "score": row.get("score") or 0,
        })

    upper = query.upper()

    def rank(item):
        symbol = item["symbol"].upper()
        return (
            symbol != upper,                     # an exact ticker wins outright
            not symbol.startswith(upper),        # then a prefix match
            # US listings carry no suffix; ".TO", ".F", ".AS" are the
            # Toronto, Frankfurt and Amsterdam lines of the same company,
            # and someone typing into a US terminal does not mean those.
            "." in symbol,
            item["type"] != "EQUITY",            # the company before its ETFs
            -float(item["score"] or 0),
        )

    results.sort(key=rank)
    return {"query": query, "results": results[:count], "source": "yahoo"}


def _thumb(thumbnail) -> str | None:
    """Smallest available resolution — these sit in a dense list, not a
    hero image, and the big ones are 700px."""
    try:
        resolutions = (thumbnail or {}).get("resolutions") or []
        if not resolutions:
            return None
        best = min(resolutions, key=lambda r: r.get("width") or 9999)
        return best.get("url")
    except (AttributeError, TypeError, ValueError):
        return None


def _sessions(meta: dict) -> dict:
    """Session boundaries -> {"pre": [[start, end], ...], "regular": [...],
    "post": [...]}.

    Yahoo ships `tradingPeriods` as a dict of lists-of-lists — one inner
    list per day in the range — and `currentTradingPeriod` as a single
    day's worth. Daily-interval charts only get the latter. Flatten both
    into the same shape so the caller never has to care which arrived.
    """
    out: dict[str, list] = {}
    periods = meta.get("tradingPeriods")
    if isinstance(periods, dict):
        for name, days in periods.items():
            spans = []
            for day in (days or []):
                # A day is normally a list of spans, but Yahoo occasionally
                # collapses a single span to a bare dict.
                for span in (day if isinstance(day, list) else [day]):
                    if isinstance(span, dict) and span.get("start") is not None:
                        spans.append([span["start"], span.get("end")])
            if spans:
                out[name] = spans
    if out:
        return out
    current = meta.get("currentTradingPeriod")
    if isinstance(current, dict):
        for name, span in current.items():
            if isinstance(span, dict) and span.get("start") is not None:
                out[name] = [[span["start"], span.get("end")]]
    return out


class YahooProvider:
    name = "yahoo"

    def quotes(self, symbols: list[str]) -> list[Quote]:
        out: list[Quote] = []
        for i in range(0, len(symbols), BATCH):
            out.extend(self._quote_chunk(symbols[i:i + BATCH]))
        return out

    def _quote_chunk(self, symbols: list[str], _retried: bool = False) -> list[Quote]:
        if not symbols:
            return []
        crumb = _get_crumb(force=_retried)
        url = (f"{BASE}/v7/finance/quote"
               f"?symbols={quote_plus(','.join(symbols))}"
               f"&crumb={quote_plus(crumb)}")
        try:
            raw = net.fetch(url, ttl=0, retries=2, timeout=25)
        except net.FetchError as exc:
            # A stale crumb shows up as 401/403. Re-mint once, then give up.
            if exc.status in (401, 403) and not _retried:
                return self._quote_chunk(symbols, _retried=True)
            raise ProviderError(f"yahoo quote failed: {exc}") from exc

        try:
            payload = json.loads(raw)["quoteResponse"]["result"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ProviderError(f"yahoo quote: unexpected shape ({exc})") from exc

        now = time.time()
        quotes = []
        for r in payload:
            ts = _num(r.get("regularMarketTime"))
            lag = max(0.0, now - ts) if ts else None
            quotes.append(Quote(
                symbol=r.get("symbol", ""),
                price=_num(r.get("regularMarketPrice")),
                change=_num(r.get("regularMarketChange")),
                change_pct=_num(r.get("regularMarketChangePercent")),
                volume=_num(r.get("regularMarketVolume")),
                market_cap=_num(r.get("marketCap")),
                day_high=_num(r.get("regularMarketDayHigh")),
                day_low=_num(r.get("regularMarketDayLow")),
                prev_close=_num(r.get("regularMarketPreviousClose")),
                lag_seconds=lag,
                market_state=r.get("marketState"),
                # These have been arriving in the payload all along, gated
                # by `hasPrePostMarketData`. They are absent rather than
                # stale when the session hasn't happened yet.
                pre_price=_num(r.get("preMarketPrice")),
                pre_change_pct=_num(r.get("preMarketChangePercent")),
                pre_time=_num(r.get("preMarketTime")),
                post_price=_num(r.get("postMarketPrice")),
                post_change_pct=_num(r.get("postMarketChangePercent")),
                post_time=_num(r.get("postMarketTime")),
                source=self.name,
                extra=r,
            ))
        return quotes

    def bars(self, symbol: str, rng: str = "1y", interval: str = "1d",
             prepost: bool = False) -> Bars:
        """OHLCV bars. `prepost` includes the pre- and after-hours sessions.

        Note the endpoint changes meaning when you ask for them: with
        prepost off, `range=1d` returns the last *completed* regular
        session; with it on, `range=1d` returns *today*, which before 9:30
        is nothing but pre-market. Callers wanting yesterday's tape plus
        this morning's extended trading should ask for 2d.
        """
        # Intraday bars go stale in seconds; daily bars are good for an hour.
        ttl = 0 if interval.endswith("m") else 3600
        url = (f"{BASE}/v8/finance/chart/{quote_plus(symbol)}"
               f"?range={quote_plus(rng)}&interval={quote_plus(interval)}")
        if prepost:
            url += "&includePrePost=true"
        try:
            raw = net.fetch(url, ttl=ttl, retries=2, timeout=25)
        except net.FetchError as exc:
            raise ProviderError(f"yahoo chart {symbol}: {exc}") from exc

        try:
            res = json.loads(raw)["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            ts = res.get("timestamp") or []
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"yahoo chart {symbol}: unexpected shape ({exc})") from exc

        return Bars(
            symbol=symbol,
            timestamps=list(ts),
            open=[_num(v) for v in (q.get("open") or [])],
            high=[_num(v) for v in (q.get("high") or [])],
            low=[_num(v) for v in (q.get("low") or [])],
            close=[_num(v) for v in (q.get("close") or [])],
            volume=[_num(v) for v in (q.get("volume") or [])],
            interval=interval,
            source=self.name,
            sessions=_sessions(res.get("meta") or {}),
        )

    def snapshot_lag(self, symbol: str = "SPY") -> tuple[float | None, str | None]:
        """Measure how far behind the tape we actually are, for the UI badge.
        Returns (seconds, market_state)."""
        url = (f"{BASE}/v8/finance/chart/{quote_plus(symbol)}"
               f"?range=1d&interval=1d")
        try:
            meta = json.loads(net.fetch(url, ttl=0, retries=1,
                                        timeout=15))["chart"]["result"][0]["meta"]
        except Exception:
            return None, None
        ts = _num(meta.get("regularMarketTime"))
        lag = max(0.0, time.time() - ts) if ts else None
        return lag, meta.get("marketState")


provider = register(YahooProvider())
