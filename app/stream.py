"""Real-time tick stream.

Yahoo pushes trades over a websocket as protobuf; `yfinance` ships the
client and the generated message types, so this is a thin wrapper around
`yf.WebSocket` rather than a protocol implementation.

Measured 2026-07-28 during pre-market: 30 ticks in 45 seconds across five
liquid symbols, sub-second timestamps. Extended-hours prints arrive on the
same feed, which is the whole reason for preferring it to polling.

Two rules this module exists to enforce:

  - The subscription follows the viewport. Nobody needs 7,000 symbols
    streaming; they need the forty rows on screen.
  - A dead socket must be visible. If the stream drops, `healthy` goes
    false and the caller falls back to polling — a grid that silently
    stops updating while still labelled LIVE is worse than one that admits
    it is polling.
"""

from __future__ import annotations

import random
import threading
import time

# Ticks older than this are dropped from the cache: a price from twenty
# minutes ago is not a live quote and should not be served as one.
TICK_TTL = 900.0

# Yahoo drops connections that subscribe to too much at once.
MAX_SYMBOLS = 500

_BACKOFF = [1, 2, 5, 10, 20, 30, 60]

# Consecutive failures before the stream stands down entirely. Yahoo
# throttles under repeated reconnects, so retrying harder makes it worse.
COOLDOWN_AFTER = 6
COOLDOWN = 600.0


class TickStream:
    """A single background websocket, resubscribed as the viewport moves."""

    def __init__(self, on_tick=None):
        self._on_tick = on_tick
        self._ticks: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._wanted: set[str] = set()
        # Symbols that stay subscribed regardless of what is on screen —
        # the watchlist. Scrolling must not silently stop updating the one
        # panel you are not looking at.
        self._pinned: set[str] = set()
        self._subscribed: set[str] = set()
        self._ws = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._fail = 0
        self._connected_at = 0.0
        self.connected = False
        self.last_tick_at = 0.0
        self.error = ""
        self.available = _probe()

    # ---- lifecycle ----

    def start(self) -> bool:
        if not self.available or self._thread:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="pm-stream")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._close()

    def _close(self) -> None:
        ws, self._ws = self._ws, None
        self.connected = False
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 — closing a dead socket
                pass

    # ---- subscription ----

    def subscribe(self, symbols) -> None:
        """Replace the viewport set. Cheap and idempotent — the UI calls
        this on every scroll."""
        wanted = {s.strip().upper() for s in symbols if s and s.strip()}
        with self._lock:
            if wanted == self._wanted:
                return
            self._wanted = wanted
        # Push it now. Deferring this to the tick handler deadlocks: no
        # symbols are subscribed, so no tick ever arrives, so the handler
        # never runs to subscribe them.
        self._sync()

    def pin(self, symbols) -> None:
        """Symbols that survive a viewport change."""
        pinned = {s.strip().upper() for s in symbols if s and s.strip()}
        with self._lock:
            if pinned == self._pinned:
                return
            self._pinned = pinned
        self._sync()

    def _target(self) -> set[str]:
        """Pinned symbols first, so a long grid can never crowd the
        watchlist out of the subscription cap."""
        with self._lock:
            pinned = list(self._pinned)[:MAX_SYMBOLS]
            room = MAX_SYMBOLS - len(pinned)
            viewport = [s for s in self._wanted if s not in set(pinned)][:room]
        return set(pinned) | set(viewport)

    def _sync(self) -> None:
        """Push the target set at the socket. yfinance has no unsubscribe,
        so a shrinking set is handled by letting extra symbols keep
        arriving — they cost nothing and stop when the socket recycles."""
        wanted = self._target()
        with self._lock:
            ws = self._ws
            new = wanted - self._subscribed
        if not new or ws is None:
            return
        try:
            ws.subscribe(sorted(new))
        except Exception as exc:  # noqa: BLE001
            self.error = f"subscribe failed: {exc}"
            self._close()
            return
        with self._lock:
            self._subscribed |= new

    # ---- the socket thread ----

    def _run(self) -> None:
        import yfinance as yf

        while not self._stop.is_set():
            try:
                self._ws = yf.WebSocket(verbose=False)
                with self._lock:
                    self._subscribed = set()
                wanted = sorted(self._target())
                if wanted:
                    self._ws.subscribe(wanted)
                    with self._lock:
                        self._subscribed = set(wanted)
                self.connected = True
                self.error = ""
                self._connected_at = time.time()
                # listen() blocks until the socket closes.
                self._ws.listen(self._handle)
            except Exception as exc:  # noqa: BLE001 — any drop is a retry
                self.error = f"{type(exc).__name__}: {exc}"
            finally:
                self._close()

            if self._stop.is_set():
                return

            # A connection that lasted a while and then dropped is a
            # normal disconnect; the failure counter resets so a
            # long-running session doesn't inherit an hour-old backoff.
            if time.time() - self._connected_at > 120:
                self._fail = 0
            self._fail += 1

            # Yahoo throttles. Observed: healthy for a morning, then
            # keepalive timeouts on every reconnect. Hammering makes that
            # worse, so after a run of failures the stream stands down for
            # a cooldown and the UI falls back to polling — which it can,
            # because the badge tells it the truth.
            if self._fail >= COOLDOWN_AFTER:
                self.error = (f"{self._fail} consecutive failures — "
                              f"polling for {int(COOLDOWN / 60)} min")
                self._stop.wait(COOLDOWN)
                self._fail = 0
                continue

            base = _BACKOFF[min(self._fail - 1, len(_BACKOFF) - 1)]
            # Jitter, so a reconnect storm doesn't synchronise.
            self._stop.wait(base * (0.75 + random.random() * 0.5))

    def _handle(self, message: dict) -> None:
        symbol = message.get("id")
        price = _f(message.get("price"))
        if not symbol or price is None:
            return
        # Every numeric field goes through _f: protobuf's MessageToDict
        # renders int64 as a *string*, so `time` arrives as "1785232190000"
        # and any arithmetic on it raises. yfinance catches that and drops
        # the tick, which looks exactly like a market with no trades.
        millis = _f(message.get("time"))
        tick = {
            "symbol": symbol,
            "price": price,
            "change": _f(message.get("change")),
            "change_pct": _f(message.get("change_percent")),
            "time": (millis / 1000.0) if millis else time.time(),
            "at": time.time(),
        }
        with self._lock:
            self._ticks[symbol] = tick
        self.last_tick_at = tick["at"]
        if self._on_tick:
            try:
                self._on_tick(tick)
            except Exception:  # noqa: BLE001 — a bad consumer must not
                pass          # take the socket down

        # A subscription added while the socket was already listening only
        # takes effect when we push it, and this is the one thread allowed
        # to touch the socket.
        self._sync()

    # ---- reading ----

    def snapshot(self, symbols=None) -> dict:
        """Fresh ticks, keyed by symbol. Stale entries are omitted rather
        than aged — the caller falls back to a real quote for those."""
        cutoff = time.time() - TICK_TTL
        with self._lock:
            items = list(self._ticks.items())
        wanted = ({s.upper() for s in symbols} if symbols else None)
        return {sym: t for sym, t in items
                if t["at"] >= cutoff and (wanted is None or sym in wanted)}

    def status(self) -> dict:
        with self._lock:
            subscribed = len(self._subscribed)
            pinned = len(self._pinned)
        age = (time.time() - self.last_tick_at) if self.last_tick_at else None
        return {
            "available": self.available,
            "connected": self.connected,
            # Healthy means ticks are actually arriving. An open socket
            # that has never delivered one is not healthy — treating it as
            # such is how a grid ends up frozen while still labelled LIVE.
            # A quiet market legitimately produces no ticks, and the answer
            # there is the same: fall back to polling.
            "healthy": bool(self.connected and age is not None and age < 120),
            "subscribed": subscribed,
            "pinned": pinned,
            "failures": self._fail,
            "last_tick_age": age,
            "error": self.error,
        }


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _probe() -> bool:
    """Is the streaming stack importable? Missing optional deps degrade to
    polling instead of breaking the terminal."""
    try:
        import yfinance  # noqa: F401
        import websockets  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False
