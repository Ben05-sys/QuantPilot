"""Local HTTP + SSE server for the terminal (stdlib http.server).

Binds 127.0.0.1 — this is a personal terminal, not a service. Same shape
as the OfflinePilotX cockpit: a per-launch token injected into the served
page and echoed on every API call, plus a loopback Host check, so no other
website can drive the API through the browser.

The two clocks the UI runs on:
  /api/screen   filters the local snapshot   — no network, unmetered
  /api/quotes   refreshes the visible rows   — 1-3 upstream calls per tick

Everything slow (a universe rebuild) happens on a worker thread and
reports over /api/events, so the terminal is usable while it runs.
"""

from __future__ import annotations

import json
import queue
import re
import secrets
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import (article, clock, diffs, news, screen, store, stream,
               universe, watchlist)
from .config import load_config
from .providers import sec, yahoo
from .providers.base import ProviderError

WEB_DIR = Path(__file__).resolve().parent / "web"

# The strip across the top. Yahoo symbol -> display label.
INDICES = [
    ("^GSPC", "SPX"), ("^IXIC", "COMP"), ("^DJI", "DJI"), ("^RUT", "RUT"),
    ("^VIX", "VIX"), ("^TNX", "10Y"), ("DX-Y.NYB", "DXY"),
    ("GC=F", "GOLD"), ("CL=F", "WTI"), ("BTC-USD", "BTC"),
]

# The futures strip. Delayed ~10 minutes on the free feed, and they do not
# appear on the websocket at all — both measured, both surfaced in the UI.
FUTURES = [
    ("ES=F", "ES"), ("NQ=F", "NQ"), ("YM=F", "YM"), ("RTY=F", "RTY"),
    ("CL=F", "CRUDE"), ("GC=F", "GOLD"), ("SI=F", "SILVER"),
    ("NG=F", "NATGAS"), ("ZN=F", "10Y"), ("6E=F", "EUR"),
]

# Columns the grid ships. Anything else is a click away in the ticker view.
GRID_COLUMNS = [
    "symbol", "name", "price", "change", "change_pct", "volume",
    "rel_volume", "market_cap", "pe", "forward_pe", "pb", "dividend_yield",
    "eps_ttm", "week52_high", "week52_low", "pct_from_52w_high",
    "pct_from_sma50", "pct_from_sma200", "earnings_days", "sector",
    "industry", "country", "exchange", "quote_type", "market_state",
    "ext_label", "ext_price", "ext_change_pct",
    "pre_change_pct", "post_change_pct", "is_adr",
    "rs_sector", "sector_change_pct",
    # Not grid columns — the inputs the cursor detail strip needs, so it
    # can answer an arrow key from the row the client already holds instead
    # of firing a request. Deliberately the *inputs*: dollar volume and
    # day-range position are recomputed in the browser from the ticking
    # price, because a server-derived figure would freeze at the value it
    # had when the screen ran while the price beside it kept moving.
    "day_high", "day_low", "open", "prev_close",
    "avg_volume_3m", "rel_volume_raw",
]

# Screens narrower than this get re-priced from live quotes before the
# price predicates are applied. Measured warm: a 200-symbol batch is
# ~0.3s, so 2,000 is about three seconds. Wider than that and the wait
# stops being worth it, so the response says it used the snapshot instead
# of pretending otherwise.
LIVE_PRICE_LIMIT = 2000

# Re-running a screen seconds later should not re-hit Yahoo. Long enough
# to absorb a burst of sort clicks, short enough that nothing on screen is
# meaningfully behind the tape.
QUOTE_CACHE_TTL = 10.0

# The header strips. Every open window polls these on a timer; the data
# moves much more slowly than the polling does.
STRIP_TTL = 8.0
STRIP_TTL_CLOSED = 60.0


def build_stamp() -> float:
    """When the running code was last written.

    A browser holds whatever JavaScript it downloaded, and restarting the
    server does not reload an open page — which is exactly how an
    afternoon got spent wondering why a finished feature "wasn't built".
    Showing this in the header makes a stale page answerable at a glance.
    """
    newest = 0.0
    for path in [WEB_DIR / "index.html", *Path(__file__).parent.glob("*.py")]:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


BUILD_AT = build_stamp()


def session_label(state: str | None) -> str:
    """What to call the session a print belongs to.

    PREPRE and POSTPOST are Yahoo's names for the gaps either side of the
    pre- and post-market windows. Those gaps are no longer dead: US
    equities trade on overnight ATS venues from roughly 20:00 to 04:00 ET,
    which is daytime across Asia. Calling that CLOSED while prices move on
    screen is the sort of small lie that makes a terminal untrustworthy.
    """
    return {"PRE": "PRE", "POST": "AH",
            "PREPRE": "ON", "POSTPOST": "ON",
            "CLOSED": "ON"}.get((state or "").upper(), "EXT")


class AppState:
    """Everything shared between request threads."""

    def __init__(self, config: dict):
        self.config = config
        self.token = secrets.token_hex(16)
        self.refresh_lock = threading.Lock()
        self.refreshing = False
        self.refresh_progress = {"message": "", "frac": 0.0}
        self.last_error = ""
        self._subs: list[queue.Queue] = []
        self._subs_lock = threading.Lock()
        # snapshot_lag() costs an upstream call; the header asks constantly.
        self._lag_cache = {"at": 0.0, "lag": None, "state": None}
        # Ticks arrive far faster than a browser needs repainting, so they
        # are batched and flushed on a timer rather than forwarded one by
        # one down the event stream.
        self.stream = stream.TickStream(on_tick=self._on_tick)
        self._pending_ticks: dict[str, dict] = {}
        self._ticks_lock = threading.Lock()
        self._quote_cache: dict[str, tuple[float, dict]] = {}
        self._quote_lock = threading.Lock()
        self._strips: dict[str, tuple[float, dict]] = {}
        self.clock = clock.MarketClock(self)

    # ---- header strips ----

    def strip_cache(self, name: str):
        """The index and futures strips are polled on a timer by every open
        window. Each poll used to cost an upstream round trip; the data
        moves far slower than the polling does."""
        entry = self._strips.get(name)
        if not entry:
            return None
        at, payload = entry
        ttl = STRIP_TTL_CLOSED if self._lag_cache["state"] in (
            "CLOSED", "PREPRE", "POSTPOST") else STRIP_TTL
        return payload if time.time() - at < ttl else None

    def store_strip(self, name: str, payload: dict) -> None:
        self._strips[name] = (time.time(), payload)

    # ---- short-lived quote cache ----

    def cached_quotes(self, symbols: list[str]) -> tuple[dict, list[str]]:
        """(what we already have that is fresh, what still needs fetching)."""
        cutoff = time.time() - QUOTE_CACHE_TTL
        have, need = {}, []
        with self._quote_lock:
            for s in symbols:
                entry = self._quote_cache.get(s)
                if entry and entry[0] >= cutoff:
                    have[s] = entry[1]
                else:
                    need.append(s)
        return have, need

    def store_quotes(self, quotes: dict) -> None:
        now = time.time()
        with self._quote_lock:
            for symbol, q in quotes.items():
                self._quote_cache[symbol] = (now, q)
            # Unbounded growth would eventually matter; 20k entries is far
            # more than the universe and costs nothing to keep.
            if len(self._quote_cache) > 20000:
                cutoff = now - QUOTE_CACHE_TTL
                self._quote_cache = {k: v for k, v in self._quote_cache.items()
                                     if v[0] >= cutoff}

    # ---- tick fan-out ----

    def _on_tick(self, tick: dict) -> None:
        with self._ticks_lock:
            self._pending_ticks[tick["symbol"]] = tick

    def shape_tick(self, tick: dict, state: str | None = None) -> dict:
        """A websocket tick, as the browser is allowed to see it.

        Outside regular hours a tick is an *extended-hours* print, not a
        new last price. US equities trade overnight on ATS venues from
        roughly 20:00 to 04:00 ET, so ticks keep arriving all night — and
        writing one into `price` replaces the closing print with a
        different venue's quote and flashes the row as though the stock
        just traded. `price` stays the regular session's; the overnight
        move goes where every other extended move goes.

        Both routes to the browser come through here — the /api/quotes
        poll and the SSE broadcast. They used to shape ticks separately,
        and only the poll had this rule, so a closed market still flashed
        on the streaming path.
        """
        if state is None:
            _, state = self.lag()
        if (state or "").upper() == "REGULAR":
            return dict(tick)
        return {
            "ext_price": tick.get("price"),
            "ext_change_pct": tick.get("change_pct"),
            "ext_label": session_label(state),
            "market_state": state,
        }

    def drain_ticks(self) -> dict:
        """Take everything the socket has collected, shaped for the browser.

        Split out of the flush loop so the session rule can be tested
        without a websocket and a 0.7-second wait. Shaped once for the
        whole batch: `lag()` is cached, and the session cannot turn over
        inside one flush.
        """
        with self._ticks_lock:
            batch, self._pending_ticks = self._pending_ticks, {}
        if not batch:
            return {}
        _, state = self.lag()
        return {symbol: self.shape_tick(tick, state)
                for symbol, tick in batch.items()}

    def live_quotes(self, symbols: list[str]) -> dict:
        """Fresh quotes for these symbols.

        Three sources, cheapest first: ticks already arriving on the
        websocket, then the short-lived quote cache, then an actual round
        trip for whatever is left. Lives on the state rather than the
        handler because the clock needs the same path for its rolling
        re-quote, and two implementations would drift.
        """
        _, state = self.lag()
        regular = (state or "").upper() == "REGULAR"

        ticks = self.stream.snapshot(symbols)
        out = {symbol: self.shape_tick(tick, state)
               for symbol, tick in ticks.items()}
        # Every symbol still needs its regular-session price, so outside
        # regular hours nothing is satisfied by a tick alone.
        remaining = [s for s in symbols
                     if s not in out or not regular]
        cached, need = self.cached_quotes(remaining)
        for symbol, quote in cached.items():
            out.setdefault(symbol, {})
            merged = dict(quote)
            merged.update({k: v for k, v in out[symbol].items()
                           if v is not None})
            out[symbol] = merged

        fetched = {}
        for q in (yahoo.provider.quotes(need) if need else []):
            if not q.symbol:
                continue
            label, ext_price, ext_pct = q.extended()
            fetched[q.symbol] = {
                "price": q.price, "change": q.change,
                "change_pct": q.change_pct, "volume": q.volume,
                "market_cap": q.market_cap,
                "day_high": q.day_high, "day_low": q.day_low,
                "pre_price": q.pre_price, "pre_change_pct": q.pre_change_pct,
                "post_price": q.post_price, "post_change_pct": q.post_change_pct,
                "ext_label": label, "ext_price": ext_price,
                "ext_change_pct": ext_pct,
                "lag_seconds": q.lag_seconds, "market_state": q.market_state,
            }
        if fetched:
            self.store_quotes(fetched)
            for symbol, quote in fetched.items():
                # A tick already collected for this symbol outranks the
                # REST payload for the extended fields — it is seconds old
                # where Yahoo's stops at the post-market close.
                overlay = {k: v for k, v in (out.get(symbol) or {}).items()
                           if v is not None}
                merged = dict(quote)
                merged.update(overlay)
                out[symbol] = merged
        return out

    def requote(self, symbols: list[str]) -> dict:
        """What the clock's rolling pass calls. Skips the stream and the
        cache deliberately — the whole point is to reach symbols nobody is
        looking at, which are exactly the ones neither has."""
        try:
            fetched = {}
            for q in yahoo.provider.quotes(symbols):
                if not q.symbol:
                    continue
                label, ext_price, ext_pct = q.extended()
                fetched[q.symbol] = {
                    "price": q.price, "change": q.change,
                    "change_pct": q.change_pct, "volume": q.volume,
                    "market_cap": q.market_cap,
                    "day_high": q.day_high, "day_low": q.day_low,
                    "pre_price": q.pre_price,
                    "pre_change_pct": q.pre_change_pct,
                    "post_price": q.post_price,
                    "post_change_pct": q.post_change_pct,
                    "ext_label": label, "ext_price": ext_price,
                    "ext_change_pct": ext_pct,
                    "market_state": q.market_state,
                }
            return fetched
        except ProviderError:
            return {}

    def pin_watchlist(self) -> None:
        """Watched symbols stay subscribed whatever the viewport shows.

        Without this, scrolling the grid unsubscribes your watchlist and
        the WATCH panel quietly stops updating — which is the one panel
        where you would not notice, because you are looking somewhere
        else.
        """
        try:
            self.stream.pin(watchlist.symbols())
        except Exception:  # noqa: BLE001 — a broken watchlist must not
            pass          # take the stream down

    def start_stream(self) -> None:
        """Open the websocket and start the flush timer. Safe to call when
        the streaming stack is missing — it simply doesn't start, and the
        UI keeps polling."""
        if not self.stream.start():
            return

        def flush():
            while True:
                time.sleep(0.7)
                batch = self.drain_ticks()
                if batch:
                    self.broadcast("ticks", {"quotes": batch,
                                             "as_of": time.time()})

        threading.Thread(target=flush, daemon=True, name="pm-ticks").start()

    # ---- SSE fan-out ----

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._subs_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._subs_lock:
            if q in self._subs:
                self._subs.remove(q)

    def broadcast(self, event: str, data: dict) -> None:
        with self._subs_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                pass  # a wedged browser must not block the refresh thread

    # ---- market clock ----

    def lag(self) -> tuple[float | None, str | None]:
        """Measured delay behind the tape, cached for a few seconds.

        Read from a /v7 quote rather than chart metadata: the chart
        endpoint returns `marketState: null` outside regular hours, which
        would leave the header saying UNKNOWN all evening when the honest
        answer is CLOSED.
        """
        now = time.time()
        if now - self._lag_cache["at"] < 5:
            return self._lag_cache["lag"], self._lag_cache["state"]
        lag = state = None
        try:
            quotes = yahoo.provider.quotes(["SPY"])
            if quotes:
                lag, state = quotes[0].lag_seconds, quotes[0].market_state
        except Exception:  # noqa: BLE001 — the header must never take the app down
            pass
        self._lag_cache.update(at=now, lag=lag, state=state)
        return lag, state

    def status(self) -> dict:
        meta = store.snapshot_meta() or {}
        ts = meta.get("snapshot_ts")
        lag, state = self.lag()
        stream_status = self.stream.status()
        # Yahoo reports PREPRE/POSTPOST for the overnight gaps, but the
        # overnight ATS session runs through them and the stream proves it:
        # if ticks are arriving, something is trading, whatever the venue
        # calls the window.
        overnight = (stream_status.get("healthy")
                     and (state or "").upper() in ("PREPRE", "POSTPOST",
                                                   "CLOSED"))
        return {
            "snapshot_ts": ts,
            "snapshot_age": (time.time() - ts) if ts else None,
            "rows": meta.get("rows", 0),
            "stale": universe.is_stale(),
            "refreshing": self.refreshing,
            "progress": self.refresh_progress,
            "lag_seconds": lag,
            "market_state": state,
            "provider": yahoo.provider.name,
            "tick_seconds": (self.config.get("closed_tick_seconds", 60)
                             if state in ("CLOSED", "PREPRE", "POSTPOST")
                             else self.config.get("tick_seconds", 5)),
            "build_at": BUILD_AT,
            "session": ("OVERNIGHT" if overnight
                        else session_label(state) if state != "REGULAR"
                        else "REGULAR"),
            "overnight": bool(overnight),
            "stream": stream_status,
            "clock": self.clock.status(),
            # How long since the in-memory prices were last refreshed by
            # the rolling pass — distinct from snapshot age, which is when
            # the generation was written.
            "live_age": universe.live_age(),
            "error": self.last_error,
            "server_time": time.time(),
        }

    # ---- universe refresh ----

    def start_refresh(self, limit: int | None = None,
                      note: str = "manual") -> bool:
        """Kick a rebuild on a worker thread. False if one is already
        running — two concurrent refreshes would just rate-limit each
        other."""
        if not self.refresh_lock.acquire(blocking=False):
            return False

        def work():
            self.refreshing = True
            self.last_error = ""
            self.broadcast("refresh_start", {})

            def progress(message: str, frac: float):
                self.refresh_progress = {"message": message, "frac": frac}
                self.broadcast("refresh_progress",
                               {"message": message, "frac": frac})

            try:
                result = universe.refresh(progress=progress, limit=limit,
                                          note=note)
                store.prune_snapshots(keep=90)
                universe.load(force=True)   # warm the frame before the UI asks
                # Record what each saved screen matches at this instant.
                # Done here rather than in the clock so a manual refresh
                # also produces a comparison point.
                result["screens"] = diffs.record_all()
                # What just entered a saved screen is the whole point of
                # recording runs — push it so the browser can raise a
                # notification while nobody is watching the window.
                fired = diffs.alerts()
                result["alerts"] = fired
                if fired:
                    self.broadcast("alerts", {"alerts": fired,
                                              "at": time.time()})
                self.broadcast("refresh_done", result)
            except Exception as exc:  # noqa: BLE001 — surface, never crash
                self.last_error = str(exc)
                self.broadcast("refresh_error", {"error": str(exc)})
            finally:
                self.refreshing = False
                self.refresh_progress = {"message": "", "frac": 0.0}
                self.refresh_lock.release()

        threading.Thread(target=work, daemon=True, name="pm-refresh").start()
        return True


class Handler(BaseHTTPRequestHandler):
    state: AppState  # bound by make_server()
    protocol_version = "HTTP/1.1"
    server_version = "QuantPilot"

    def log_message(self, fmt, *args):
        pass

    # ---- plumbing ----

    def _json(self, obj, status: int = 200) -> None:
        # allow_nan=False because `NaN` is not JSON and JSON.parse rejects
        # it; _clean() turns every one into null on the way out.
        body = json.dumps(_clean(obj), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except (ValueError, OSError):
            return {}

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
        return name in ("127.0.0.1", "localhost", "[::1]")

    def _authorized(self, params: dict | None = None) -> bool:
        supplied = self.headers.get("X-PM-Token") or ""
        if secrets.compare_digest(supplied, self.state.token):
            return True
        # EventSource cannot set request headers, so the SSE stream accepts
        # the same per-launch token as a query parameter instead.
        query_token = (params or {}).get("token", [""])[0]
        return bool(query_token) and secrets.compare_digest(query_token,
                                                            self.state.token)

    @staticmethod
    def _one(params: dict, key: str, default: str = "") -> str:
        return (params.get(key) or [default])[0]

    def _frame(self):
        """The current universe snapshot as a DataFrame, or None."""
        df = universe.load()
        return None if df is None or df.empty else df

    # ---- GET ----

    def do_GET(self):
        if not self._host_ok():
            self._json({"error": "forbidden host"}, 403)
            return
        parsed = urlparse(self.path)
        path, params = parsed.path, parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._page()
            return
        if not path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return
        if not self._authorized(params):
            self._json({"error": "unauthorized"}, 403)
            return

        try:
            if path == "/api/status":
                self._json(self.state.status())
            elif path == "/api/filters":
                self._filters()
            elif path == "/api/screen":
                self._screen(params)
            elif path == "/api/quotes":
                self._quotes(params)
            elif path == "/api/indices":
                self._indices()
            elif path == "/api/heatmap":
                self._heatmap(params)
            elif path == "/api/world":
                self._world(params)
            elif path == "/api/futures":
                self._futures()
            elif path == "/api/watchlist":
                self._watchlist_get()
            elif path == "/api/news":
                self._news(params)
            elif path == "/api/article":
                self._article(params)
            elif path == "/api/market-news":
                self._market_news(params)
            elif path == "/api/search":
                self._search(params)
            elif path == "/api/related":
                self._related(params)
            elif path == "/api/diffs":
                self._json(diffs.all_screens())
            elif path == "/api/earnings":
                self._earnings(params)
            elif path == "/api/screens":
                self._json({"screens": store.list_screens()})
            elif path.startswith("/api/ticker/"):
                self._ticker(unquote(path.rsplit("/", 1)[-1]), params)
            elif path == "/api/events":
                self._events()
            else:
                self._json({"error": "not found"}, 404)
        except screen.ScreenError as exc:
            self._json({"error": str(exc)}, 400)
        except ProviderError as exc:
            self._json({"error": str(exc)}, 503)
        except (BrokenPipeError, ConnectionError):
            pass  # browser navigated away mid-response
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        if not self._host_ok():
            self._json({"error": "forbidden host"}, 403)
            return
        if not self._authorized():
            self._json({"error": "unauthorized"}, 403)
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/refresh":
                body = self._body()
                started = self.state.start_refresh(limit=body.get("limit"))
                self._json({"started": started,
                            "error": "" if started else "already refreshing"},
                           200 if started else 409)
            elif path == "/api/subscribe":
                self._subscribe()
            elif path == "/api/watchlist":
                self._watchlist_post()
            elif path == "/api/screens":
                self._save_screen()
            else:
                self._json({"error": "not found"}, 404)
        except screen.ScreenError as exc:
            self._json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---- routes ----

    def _page(self):
        try:
            body = (WEB_DIR / "index.html").read_bytes()
        except OSError:
            self._json({"error": "app/web/index.html is missing"}, 500)
            return
        body = body.replace(
            b"<head>",
            b'<head><script>window.PM_TOKEN="%s";</script>'
            % self.state.token.encode("ascii"), 1)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _filters(self):
        """Everything the UI needs to build its chrome, in one call."""
        self._json({
            "filters": [{"id": fid, "label": label,
                         "options": [o for o, _ in opts]}
                        for fid, label, opts in screen.FILTER_SPECS],
            "presets": [{"name": n, "expr": e} for n, e in screen.PRESETS],
            "screens": store.list_screens(),
            "columns": GRID_COLUMNS,
            "fields": sorted(set(screen.COLUMNS) | set(screen.ALIASES)),
        })

    def _expression(self, params: dict) -> str:
        """`expr` and the dropdown state and-ed together — the filter bar
        and the command line are the same mechanism."""
        expr = self._one(params, "expr").strip()
        raw = self._one(params, "filters").strip()
        if raw:
            try:
                selected = json.loads(raw)
            except json.JSONDecodeError:
                raise screen.ScreenError("filters must be a JSON object")
            compiled = screen.filters_to_expr(selected)
            if compiled and expr:
                return f"({compiled}) and ({expr})"
            return compiled or expr
        return expr

    def _screen(self, params: dict):
        df = self._frame()
        if df is None:
            self._json({"rows": [], "total": 0, "expr": "",
                        "empty": True, "snapshot_ts": None})
            return
        expr = self._expression(params)
        limit = min(int(self._one(params, "limit", "200") or 200), 2000)
        offset = max(int(self._one(params, "offset", "0") or 0), 0)
        sort = self._one(params, "sort", "market_cap") or "market_cap"
        descending = self._one(params, "dir", "desc") != "asc"
        allow_live = self._one(params, "live", "1") != "0"

        frame, priced, candidates = self._price(df, expr, allow_live)
        page, total = screen.run(frame, expr, sort=sort, descending=descending,
                                 limit=limit, offset=offset)
        self._json({
            "rows": screen.to_records(page, GRID_COLUMNS),
            "total": total,
            "universe": int(len(df)),
            "expr": expr,
            "chips": screen.describe(expr),
            "sort": sort,
            "dir": "desc" if descending else "asc",
            "offset": offset,
            "snapshot_ts": df.attrs.get("snapshot_ts"),
            # "live"     — price predicates were applied to fresh quotes
            # "snapshot" — too many candidates to re-price; this is history
            # "static"   — nothing in the screen depends on the tape
            "priced": priced,
            "candidates": candidates,
        })

    def _price(self, df, expr: str, allow_live: bool):
        """Re-price the candidate set before the tape-dependent predicates
        are applied.

        Screening `chg > 3` against a six-hour-old snapshot answers a
        question about six hours ago. So the static half of the expression
        narrows the field first, and if what survives is small enough, it
        gets real quotes before the live half runs.
        """
        static_expr, live_expr = screen.split_live(expr)
        if not live_expr:
            return df, "static", int(len(df))

        candidates = df[screen.mask(df, static_expr)] if static_expr else df
        count = int(len(candidates))
        if not count:
            return df, "static", 0
        if not allow_live:
            # The caller asked for the instant answer and is fetching the
            # priced one alongside. "pending" says the rows are real but
            # the selection is about to be redone.
            return df, "pending", count
        if count > LIVE_PRICE_LIMIT:
            return df, "snapshot", count

        symbols = [s for s in candidates["symbol"].tolist() if s]
        try:
            quotes = self._live_quotes(symbols)
        except ProviderError:
            return df, "snapshot", count
        if not quotes:
            return df, "snapshot", count
        return screen.apply_quotes(candidates, quotes), "live", count

    def _live_quotes(self, symbols: list[str]) -> dict:
        return self.state.live_quotes(symbols)

    def _quotes(self, params: dict):
        """Quotes for the rows currently on screen.

        The websocket is the primary path; this is the fallback when it is
        unavailable or has dropped, and it doubles as the subscription
        hint — asking for a set of symbols is what puts them on the stream.
        """
        raw = self._one(params, "symbols")
        symbols = [s.strip().upper() for s in raw.split(",") if s.strip()][:500]
        if not symbols:
            self._json({"quotes": {}, "as_of": time.time()})
            return
        self.state.stream.subscribe(symbols)
        self._json({"quotes": self._live_quotes(symbols),
                    "as_of": time.time(),
                    "stream": self.state.stream.status()})

    def _subscribe(self):
        """Point the stream at the viewport. Called on scroll, so it must
        stay cheap — TickStream.subscribe is a set comparison."""
        body = self._body()
        symbols = [str(s).upper() for s in (body.get("symbols") or [])][:500]
        self.state.stream.subscribe(symbols)
        status = self.state.stream.status()
        # Anything already ticking can go back immediately; the rest will
        # arrive on the stream. Shaped like every other route to the
        # browser — this one fed applyTicks() raw, so scrolling the grid
        # after hours flashed rows at an ATS price.
        _, market_state = self.state.lag()
        ticks = self.state.stream.snapshot(symbols)
        self._json({"ok": True, "stream": status,
                    "quotes": {s: self.state.shape_tick(t, market_state)
                               for s, t in ticks.items()}})

    def _indices(self):
        cached = self.state.strip_cache("indices")
        if cached is not None:
            self._json(cached)
            return
        try:
            quotes = {q.symbol: q for q in
                      yahoo.provider.quotes([s for s, _ in INDICES])}
        except ProviderError as exc:
            self._json({"indices": [], "error": str(exc)})
            return
        out = []
        for symbol, label in INDICES:
            q = quotes.get(symbol)
            out.append({"symbol": symbol, "label": label,
                        "price": q.price if q else None,
                        "change_pct": q.change_pct if q else None,
                        "change": q.change if q else None})
        payload = {"indices": out, "as_of": time.time()}
        self.state.store_strip("indices", payload)
        self._json(payload)

    def _heatmap(self, params: dict):
        df = self._frame()
        if df is None:
            self._json({"cells": [], "empty": True})
            return
        expr = self._expression(params) or "mcap > 0"
        limit = min(int(self._one(params, "limit", "400") or 400), 2000)
        page, total = screen.run(df, expr, sort="market_cap", descending=True,
                                 limit=limit)
        cells = screen.to_records(
            page, ["symbol", "name", "sector", "industry", "market_cap",
                   "change_pct", "price", "volume"])
        # A cell with no size can't be drawn.
        cells = [c for c in cells if c.get("market_cap")]
        self._json({"cells": cells, "total": total,
                    "summary": screen.market_summary(df)})

    def _world(self, params: dict):
        """Per-country aggregates for the map. `scope=adr` narrows to
        depositary receipts, which is the view where geography actually
        carries information — 75% of the whole universe is one country."""
        df = self._frame()
        if df is None:
            self._json({"countries": [], "empty": True})
            return
        scope = self._one(params, "scope", "all")
        expr = self._expression(params)
        if scope == "adr":
            expr = f"adr and ({expr})" if expr else "adr"
        subset = df[screen.mask(df, expr)] if expr else df
        out = screen.country_summary(subset)
        out["scope"] = scope
        out["expr"] = expr
        self._json(out)

    def _futures(self):
        """The futures strip. Delayed roughly ten minutes — CME's standard
        free delay — and labelled as such. Worth carrying anyway: futures
        trade nearly around the clock, so they are the only live US signal
        while the cash market is shut."""
        cached = self.state.strip_cache("futures")
        if cached is not None:
            self._json(cached)
            return
        try:
            quotes = {q.symbol: q for q in
                      yahoo.provider.quotes([s for s, _ in FUTURES])}
        except ProviderError as exc:
            self._json({"futures": [], "error": str(exc)})
            return
        out, lags = [], []
        for symbol, label in FUTURES:
            q = quotes.get(symbol)
            if q and q.lag_seconds is not None:
                lags.append(q.lag_seconds)
            out.append({"symbol": symbol, "label": label,
                        "price": q.price if q else None,
                        "change_pct": q.change_pct if q else None,
                        "change": q.change if q else None,
                        "lag_seconds": q.lag_seconds if q else None})
        payload = {"futures": out, "delayed": True,
                   # Measured, not assumed — the same rule as everywhere
                   # else. If CME's delay changes, the badge changes.
                   "lag_seconds": (sum(lags) / len(lags)) if lags else None,
                   "as_of": time.time()}
        self.state.store_strip("futures", payload)
        self._json(payload)

    def _watchlist_get(self):
        rows = watchlist.all()
        quotes = self._live_quotes([r["symbol"] for r in rows]) if rows else {}
        self._json({"watchlist": watchlist.enrich(rows, quotes),
                    "as_of": time.time()})

    def _watchlist_post(self):
        body = self._body()
        action = str(body.get("action", "toggle"))
        symbol = str(body.get("symbol", "")).strip().upper()
        if not symbol:
            self._json({"error": "need a symbol"}, 400)
            return
        try:
            if action == "remove":
                watchlist.remove(symbol)
                watched = False
            elif action == "add":
                watchlist.add(symbol, body.get("shares") or 0,
                              body.get("cost_basis"), str(body.get("note", "")))
                watched = True
            else:
                watched = watchlist.toggle(symbol)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return
        # Keep the stream pinned to whatever is watched, so a watchlist
        # row keeps ticking after you navigate away from it.
        self.state.pin_watchlist()
        rows = watchlist.all()
        quotes = self._live_quotes([r["symbol"] for r in rows]) if rows else {}
        self._json({"ok": True, "watched": watched,
                    "watchlist": watchlist.enrich(rows, quotes)})

    def _earnings(self, params: dict):
        """Who reports, grouped by exchange day.

        Grouped in New York time, not the viewer's: a company reports on
        an exchange day, and from India a 16:00 ET print falls after
        midnight the next morning. Bucketing by local date would scatter
        one earnings day across two.
        """
        df = self._frame()
        if df is None:
            self._json({"days": [], "empty": True})
            return
        days = max(1, min(int(self._one(params, "days", "7") or 7), 60))
        min_cap = float(self._one(params, "min_cap", "0") or 0)

        expr = f"0 < earnings < {days}"
        if min_cap:
            expr += f" and mcap > {min_cap}"
        hits, total = screen.run(df, expr, sort="market_cap", descending=True,
                                 limit=1200)
        rows = screen.to_records(hits, [
            "symbol", "name", "sector", "price", "change_pct", "market_cap",
            "pe", "volume", "rel_volume", "week52_high", "earnings_ts",
            "earnings_days", "earnings_when"])

        buckets: dict[str, list] = {}
        for row in rows:
            ts = row.get("earnings_ts")
            if not ts:
                continue
            key = datetime.fromtimestamp(ts, clock.EASTERN).strftime("%Y-%m-%d")
            buckets.setdefault(key, []).append(row)
        out = [{"date": key,
                "weekday": datetime.strptime(key, "%Y-%m-%d").strftime("%a"),
                "count": len(items),
                "bmo": sum(1 for i in items if i.get("earnings_when") == "BMO"),
                "amc": sum(1 for i in items if i.get("earnings_when") == "AMC"),
                "market_cap": sum(i.get("market_cap") or 0 for i in items),
                "rows": items}
               for key, items in sorted(buckets.items())]
        self._json({"days": out, "total": total, "window": days,
                    "as_of": time.time()})

    def _market_news(self, params: dict):
        """The market feed. Built from a broad wire plus news on whatever
        is moving, which takes a few seconds — so it is cached and every
        open window shares one build."""
        cached = self.state.strip_cache("market_news")
        if cached is not None and self._one(params, "force") != "1":
            self._json(cached)
            return
        limit = min(int(self._one(params, "limit", "40") or 40), 120)
        payload = news.feed(self._frame(), limit=limit)
        self.state.store_strip("market_news", payload)
        self._json(payload)

    def _search(self, params: dict):
        """Symbol and company lookup for the search bar."""
        query = self._one(params, "q").strip()
        if len(query) < 1:
            self._json({"query": query, "results": []})
            return
        try:
            self._json(yahoo.search(query, count=int(
                self._one(params, "limit", "10") or 10)))
        except ProviderError as exc:
            self._json({"query": query, "results": [], "error": str(exc)})

    def _related(self, params: dict):
        """Companies that name this one in their SEC filings.

        Not a supplier list — EDGAR cannot tell a supplier from a customer
        from a competitor, and pretending otherwise would be the kind of
        confident wrongness that makes a terminal useless. What it can say
        is *who disclosed a relationship*, and whether they are in the same
        line of business, which is the strongest free hint at which kind.
        """
        symbol = self._one(params, "symbol").strip().upper()
        if not symbol:
            self._json({"error": "need a symbol"}, 400)
            return

        df = self._frame()
        name = self._one(params, "name").strip()
        sector = None
        if df is not None and "symbol" in df.columns:
            row = df[df["symbol"] == symbol]
            if not row.empty:
                name = name or str(row.iloc[0].get("name") or "")
                sector = row.iloc[0].get("sector")
        # Nasdaq names carry the security type ("Apple Inc. Common Stock");
        # searching EDGAR for that phrase finds nothing.
        name = re.sub(r"\s+(Common Stock|Class [A-Z].*|Ordinary Shares.*|"
                      r"American Depositary.*|Common Shares.*)$", "", name,
                      flags=re.I).strip()
        if not name:
            self._json({"symbol": symbol, "related": [],
                        "error": "no company name to search for"})
            return

        try:
            hits = sec.related(name, limit=30)
        except ProviderError as exc:
            self._json({"symbol": symbol, "related": [], "error": str(exc)})
            return

        by_symbol = {}
        if df is not None:
            frame = df[df["symbol"].isin([h["symbol"] for h in hits])]
            by_symbol = {r["symbol"]: r for r in screen.to_records(
                frame, ["symbol", "name", "sector", "industry", "price",
                        "change_pct", "market_cap"])}

        out = []
        for hit in hits:
            if hit["symbol"] == symbol:
                continue          # every company names itself most of all
            row = by_symbol.get(hit["symbol"]) or {}
            same = (sector and row.get("sector") == sector)
            out.append({
                **hit,
                "sector": row.get("sector"),
                "industry": row.get("industry"),
                "price": row.get("price"),
                "change_pct": row.get("change_pct"),
                "market_cap": row.get("market_cap"),
                # A peer is in the same business; anyone else who bothered
                # to name them is more likely up or down the chain.
                "kind": ("peer" if same else
                         "chain" if row.get("sector") else "other"),
                "listed": bool(row),
                "filings_url":
                    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                    f"&CIK={hit['cik']:010d}&type=10-K",
            })
        self._json({"symbol": symbol, "searched": name,
                    "related": out, "as_of": time.time()})

    def _article(self, params: dict):
        """Body text for a story already served by /api/news.

        Takes an id, never a URL: handing this route an arbitrary address
        would make the terminal an open proxy on your own machine.
        """
        story_id = self._one(params, "id").strip()
        if not story_id:
            self._json({"error": "need an article id"}, 400)
            return
        try:
            self._json(article.fetch(story_id))
        except article.ArticleError as exc:
            self._json({"error": str(exc), "id": story_id}, 404)

    def _news(self, params: dict):
        symbol = self._one(params, "symbol").strip().upper()
        if not symbol:
            self._json({"error": "need a symbol"}, 400)
            return
        limit = min(int(self._one(params, "limit", "12") or 12), 40)
        # Off by default: Yahoo's search returns anything mentioning the
        # ticker, and a story about three other companies that name-checks
        # NVDA is noise on NVDA's page.
        strict = self._one(params, "strict", "0") == "1"
        try:
            self._json(yahoo.news(symbol, count=limit, strict=strict))
        except ProviderError as exc:
            self._json({"symbol": symbol, "items": [], "error": str(exc)})

    def _ticker(self, symbol: str, params: dict):
        symbol = symbol.strip().upper()
        if not symbol:
            self._json({"error": "no symbol"}, 400)
            return
        rng = self._one(params, "range", "1y") or "1y"
        interval = self._one(params, "interval", "1d") or "1d"

        out: dict = {"symbol": symbol, "range": rng, "interval": interval}

        # Snapshot row first — it has the fundamentals the chart doesn't.
        df = self._frame()
        if df is not None and "symbol" in df.columns:
            match = df[df["symbol"] == symbol]
            if not match.empty:
                out["stats"] = screen.to_records(match.head(1))[0]

        # Live quote, so the header is current even if the snapshot is old.
        try:
            live = yahoo.provider.quotes([symbol])
            if live:
                q = live[0]
                label, ext_price, ext_pct = q.extended()
                out["quote"] = {
                    "price": q.price, "change": q.change,
                    "change_pct": q.change_pct, "volume": q.volume,
                    "day_high": q.day_high, "day_low": q.day_low,
                    "prev_close": q.prev_close, "market_cap": q.market_cap,
                    "lag_seconds": q.lag_seconds,
                    "market_state": q.market_state,
                    "pre_price": q.pre_price,
                    "pre_change_pct": q.pre_change_pct,
                    "post_price": q.post_price,
                    "post_change_pct": q.post_change_pct,
                    "ext_label": label, "ext_price": ext_price,
                    "ext_change_pct": ext_pct,
                    "name": (q.extra or {}).get("longName")
                            or (q.extra or {}).get("shortName"),
                }
        except ProviderError as exc:
            out["quote_error"] = str(exc)

        # Extended hours are only meaningful on an intraday chart; a daily
        # bar already contains them.
        prepost = self._one(params, "prepost", "1") != "0" and interval.endswith("m")
        try:
            bars = yahoo.provider.bars(symbol, rng=rng, interval=interval,
                                       prepost=prepost)
            out["bars"] = {"t": bars.timestamps, "o": bars.open,
                           "h": bars.high, "l": bars.low, "c": bars.close,
                           "v": bars.volume}
            out["sessions"] = bars.sessions
            out["prepost"] = prepost
        except ProviderError as exc:
            out["bars_error"] = str(exc)
            out["bars"] = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []}

        # EDGAR filings are best-effort: plenty of symbols are ETFs or
        # foreign issuers with no CIK, and that is not an error.
        try:
            cik = sec.ticker_map().get(symbol)
            out["filings"] = (sec.recent_filings(cik, limit=6) if cik else [])
        except Exception as exc:  # noqa: BLE001
            out["filings"] = []
            out["filings_error"] = str(exc)

        self._json(_clean(out))

    def _save_screen(self):
        body = self._body()
        action = str(body.get("action", "save"))
        name = str(body.get("name", "")).strip()
        if not name:
            self._json({"error": "need a name"}, 400)
            return
        if action == "delete":
            store.delete_screen(name)
        else:
            expr = str(body.get("expr", "")).strip()
            df = self._frame()
            if df is not None:
                screen.mask(df, expr)   # validate before it is persisted
            store.save_screen(name, expr)
        self._json({"ok": True, "screens": store.list_screens()})

    def _events(self):
        """SSE: refresh progress plus a status heartbeat."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.state.subscribe()
        try:
            self._sse("status", self.state.status())
            while True:
                try:
                    event, data = q.get(timeout=15)
                except queue.Empty:
                    event, data = "status", self.state.status()
                self._sse(event, data)
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            self.state.unsubscribe(q)
            self.close_connection = True

    def _sse(self, event: str, data: dict) -> None:
        frame = (f"event: {event}\n"
                 f"data: {json.dumps(_clean(data), ensure_ascii=False)}\n\n")
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()


def _clean(obj):
    """NaN/Infinity are not JSON. Replace them with null before they reach
    json.dumps(allow_nan=False) and blow up a whole response."""
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def make_server(host: str = "127.0.0.1", port: int = 8900,
                config: dict | None = None) -> ThreadingHTTPServer:
    """Build (don't start) the server — also the tests' entry point."""
    state = AppState(config or load_config())
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


APP_BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def open_window(url: str) -> None:
    """A chromeless window, so it reads as a terminal and not a web page."""
    import subprocess
    for exe in APP_BROWSERS:
        if Path(exe).is_file():
            try:
                subprocess.Popen([exe, f"--app={url}",
                                  "--window-size=1600,980"])
                return
            except OSError:
                continue
    webbrowser.open(url)


def main(argv=None) -> int:
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    config = load_config()
    port = int(config.get("port", 8900))
    if "--port" in argv:
        i = argv.index("--port")
        if i + 1 < len(argv):
            port = int(argv[i + 1])

    try:
        server = make_server(port=port, config=config)
    except OSError:
        url = f"http://127.0.0.1:{port}"
        print(f"QuantPilot already running  ->  {url}")
        open_window(url)
        return 0

    state: AppState = server.RequestHandlerClass.state
    screen.seed_presets()
    state.start_stream()
    state.pin_watchlist()
    state.clock.start()

    meta = store.snapshot_meta()
    url = f"http://127.0.0.1:{port}"
    print(f"\n  QuantPilot  ->  {url}")
    if meta:
        age = (time.time() - meta["snapshot_ts"]) / 60
        print(f"  snapshot: {meta['rows']} symbols, {age:.0f} min old")
    else:
        print("  no snapshot yet — building the universe in the background")
    print(f"  stream:   {'websocket' if state.stream.available else 'polling (yfinance/websockets missing)'}")
    print("  (Ctrl+C to stop)\n")

    if universe.is_stale():
        state.start_refresh(note="boot")

    try:
        from .tray import start_tray
        start_tray(url, state)
    except Exception as exc:  # noqa: BLE001 — the tray is decoration
        print(f"  (tray unavailable: {exc})")

    if "--no-browser" not in argv:
        threading.Timer(0.4, open_window, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
