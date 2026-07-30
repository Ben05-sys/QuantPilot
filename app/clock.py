"""Keeping the snapshot fresh.

Two jobs, both driven by the exchange rather than by a calendar:

  - **Rebuild** the whole universe on session transitions (open, mid,
    close). Reading the venue's own `marketState` gets market holidays and
    half-days right for free — no transition fires on a day the exchange
    is shut, and no holiday table has to be maintained and then quietly
    rot.

  - **Roll** through the universe re-quoting it between rebuilds, so the
    snapshot never drifts far from the tape. A full rebuild also refetches
    the Nasdaq listing and rewrites a generation; a roll only refreshes
    prices, which is all that moves intraday.

Everything here is best-effort. A failed refresh is reported and retried
next tick; it never takes the terminal down, because a stale screener you
can still use beats a dead one.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import universe

EASTERN = ZoneInfo("America/New_York")

# How often the clock wakes to look at the world.
POLL_SECONDS = 30.0

# A mid-session rebuild this long after the open, so the fundamentals that
# only move on a full refresh (market cap, averages) don't sit all day at
# their 9:30 values.
MIDDAY_AFTER = 3 * 3600

# Symbols per rolling re-quote pass. One Yahoo batch is 200 and takes
# about 0.3s warm; a slice of five batches keeps each pass short enough
# that a shutdown isn't waiting on it.
ROLL_BATCH = 1000

# Don't roll faster than this — the point is freshness, not load.
ROLL_INTERVAL = 90.0

OPEN_STATES = ("REGULAR",)
# Everything outside the regular session where prints still happen. The
# overnight gaps are included deliberately: US equities trade on ATS
# venues from roughly 20:00 to 04:00 ET, so leaving PREPRE and POSTPOST
# out meant the frame sat untouched through the entire Asian day — which
# is exactly when someone in India is looking at it.
EXTENDED_STATES = ("PRE", "POST", "PREPRE", "POSTPOST")


def now_et() -> datetime:
    """Exchange time. Computed rather than assumed, so the EDT/EST shift
    that moves the US session around in every other timezone is handled by
    the standard library instead of by arithmetic."""
    return datetime.now(EASTERN)


class MarketClock:
    """Watches the session and keeps the snapshot current."""

    def __init__(self, state, refresh=None, requote=None, poll=POLL_SECONDS):
        # `state` is the server's AppState: it already polls marketState
        # every few seconds for the header, so reading it here adds no
        # upstream calls at all.
        self.state = state
        self._refresh = refresh or (lambda note: state.start_refresh(note=note))
        self._requote = requote or self._default_requote
        self._poll = poll
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.last_state: str | None = None
        self.opened_at: float | None = None
        self.did_midday = False
        self.last_roll = 0.0
        self.roll_cursor = 0
        self.history: list[dict] = []
        self.error = ""

    # ---- lifecycle ----

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="pm-clock")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — never kill the clock
                self.error = f"{type(exc).__name__}: {exc}"

    # ---- the decision ----

    def tick(self, market_state: str | None = None, now: float | None = None) -> str | None:
        """One pass. Returns the action taken, or None.

        Split out from the thread so it can be driven directly in tests —
        waiting until 4pm Eastern to find out whether the close snapshot
        fires is not a test.
        """
        now = now if now is not None else time.time()
        state = market_state if market_state is not None else self._market_state()
        previous, self.last_state = self.last_state, state
        if state is None:
            return None

        action = None
        opening = state in OPEN_STATES and previous not in OPEN_STATES
        closing = previous in OPEN_STATES and state not in OPEN_STATES

        if opening:
            # `previous is None` is the first tick after boot, not a real
            # transition — but if we boot mid-session the snapshot still
            # wants building, and start_refresh() is already guarded
            # against running two at once.
            self.opened_at = now
            self.did_midday = False
            action = "open"
        elif closing:
            action = "close"
        elif (state in OPEN_STATES and not self.did_midday
              and self.opened_at and now - self.opened_at >= MIDDAY_AFTER):
            self.did_midday = True
            action = "midday"

        if action:
            self._record(action, now)
            self._refresh(action)
            return action

        # Between rebuilds, keep prices moving.
        if (state in OPEN_STATES + EXTENDED_STATES
                and now - self.last_roll >= ROLL_INTERVAL):
            self.last_roll = now
            rolled = self._requote(self.roll_cursor, ROLL_BATCH)
            if rolled:
                self.roll_cursor = rolled
                self._record("roll", now)
                return "roll"
        return None

    def _market_state(self) -> str | None:
        try:
            _, state = self.state.lag()
            return (state or "").upper() or None
        except Exception:  # noqa: BLE001
            return None

    def _record(self, action: str, when: float) -> None:
        self.history.append({"action": action, "at": when,
                             "et": now_et().strftime("%Y-%m-%d %H:%M")})
        del self.history[:-40]

    # ---- rolling re-quote ----

    def _default_requote(self, cursor: int, size: int) -> int:
        """Re-quote a slice of the universe in place.

        Writes into the *existing* snapshot rather than appending a
        generation: a rolling price update is not a point-in-time
        observation, and treating it as one would fill the history with
        near-duplicate generations and destroy the thing that makes the
        append-only design worth having.
        """
        df = universe.load()
        if df is None or df.empty or "symbol" not in df.columns:
            return 0
        symbols = df["symbol"].dropna().tolist()
        if not symbols:
            return 0
        cursor = cursor % len(symbols)
        slice_ = symbols[cursor:cursor + size]
        if not slice_:
            return 0
        quotes = self.state.requote(slice_)
        if not quotes:
            return 0
        universe.apply_live(quotes)
        return (cursor + len(slice_)) % len(symbols)

    # ---- reporting ----

    def status(self) -> dict:
        return {
            "market_state": self.last_state,
            "opened_at": self.opened_at,
            "did_midday": self.did_midday,
            "roll_cursor": self.roll_cursor,
            "last_roll": self.last_roll or None,
            "history": self.history[-6:],
            "error": self.error,
        }
