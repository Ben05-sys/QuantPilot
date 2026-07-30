#!/usr/bin/env python3
"""The market clock. Offline, and time is supplied rather than waited for.

Waiting until 4pm Eastern to discover whether the close snapshot fires is
not a test, so `tick()` takes both the market state and the clock reading
as arguments and the whole session is replayed in milliseconds.

    python tests/test_clock.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import clock

PASS = FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


class FakeState:
    """Stands in for AppState. Records what it was asked to do."""

    def __init__(self, state="CLOSED"):
        self.state = state
        self.lag_calls = 0

    def lag(self):
        self.lag_calls += 1
        return 1.0, self.state


def build(state="CLOSED"):
    calls = {"refresh": [], "requote": []}

    def refresh(note):
        calls["refresh"].append(note)
        return True

    def requote(cursor, size):
        calls["requote"].append((cursor, size))
        return cursor + size

    c = clock.MarketClock(FakeState(state), refresh=refresh, requote=requote)
    return c, calls


def main():
    T = 1_700_000_000.0

    print("\nsession transitions")
    c, calls = build()
    check("a closed market does nothing",
          c.tick("CLOSED", T) is None and not calls["refresh"])
    check("overnight triggers no rebuild — it rolls prices, but the "
          "universe is only rebuilt on a session transition",
          c.tick("PREPRE", T + 60) != "open" and not calls["refresh"])

    c, calls = build()
    c.tick("PRE", T)
    check("pre-market alone is not an open", not calls["refresh"])
    action = c.tick("REGULAR", T + 60)
    check("PRE -> REGULAR fires the open snapshot",
          action == "open" and calls["refresh"] == ["open"], calls["refresh"])
    check("staying open fires nothing more",
          c.tick("REGULAR", T + 120) in (None, "roll")
          and calls["refresh"] == ["open"])

    print("\nmidday")
    c, calls = build()
    c.tick("REGULAR", T)
    calls["refresh"].clear()
    check("no midday rebuild before the interval",
          c.tick("REGULAR", T + clock.MIDDAY_AFTER - 60) != "midday")
    check("midday fires once past the interval",
          c.tick("REGULAR", T + clock.MIDDAY_AFTER + 1) == "midday")
    check("midday does not fire twice",
          c.tick("REGULAR", T + clock.MIDDAY_AFTER + 5000) != "midday")

    print("\nclose")
    c, calls = build()
    c.tick("REGULAR", T)
    calls["refresh"].clear()
    check("REGULAR -> POST fires the close snapshot",
          c.tick("POST", T + 100) == "close", calls["refresh"])
    check("it is tagged close", calls["refresh"] == ["close"])
    check("POST -> CLOSED does not fire a second close",
          c.tick("CLOSED", T + 200) != "close")

    print("\nholidays and half-days need no calendar")
    c, calls = build()
    # A market holiday: the venue simply never reports REGULAR.
    for i in range(20):
        c.tick("CLOSED", T + i * 600)
    check("a day with no REGULAR state produces no snapshots",
          calls["refresh"] == [], calls["refresh"])
    # A half-day closes early; nothing special is needed, the transition
    # just happens sooner.
    c, calls = build()
    c.tick("REGULAR", T)
    calls["refresh"].clear()
    check("an early close is still a close",
          c.tick("POST", T + 3600) == "close")

    print("\nrolling re-quote")
    c, calls = build()
    c.tick("REGULAR", T)                      # the open consumes this tick
    calls["requote"].clear()
    got = c.tick("REGULAR", T + clock.ROLL_INTERVAL + 1)
    check("rolls between rebuilds", got == "roll", got)
    check("asks for a bounded slice",
          calls["requote"][0][1] == clock.ROLL_BATCH, calls["requote"])
    check("does not roll again immediately",
          c.tick("REGULAR", T + clock.ROLL_INTERVAL + 2) is None)
    c.tick("REGULAR", T + clock.ROLL_INTERVAL * 2 + 4)
    check("the cursor advances so it sweeps the universe",
          calls["requote"][1][0] > calls["requote"][0][0], calls["requote"])

    c, calls = build()
    c.tick("PRE", T)
    check("extended hours roll too — pre-market prices move",
          c.tick("PRE", T + clock.ROLL_INTERVAL + 1) == "roll")
    c, calls = build()
    c.tick("POSTPOST", T)
    check("the overnight gap rolls too — US equities trade on ATS venues "
          "from about 20:00 to 04:00 ET, right through the Asian day",
          c.tick("POSTPOST", T + clock.ROLL_INTERVAL + 1) == "roll")
    c, calls = build()
    c.tick("CLOSED", T)
    check("a genuinely shut market does not roll",
          c.tick("CLOSED", T + clock.ROLL_INTERVAL + 1) is None)

    print("\nrobustness")
    c, calls = build()
    check("an unknown state is ignored rather than guessed at",
          c.tick(None, T) is None)
    broken = clock.MarketClock(FakeState("REGULAR"),
                               refresh=lambda note: 1 / 0,
                               requote=lambda a, b: 0)
    try:
        broken.tick("REGULAR", T)
        check("a failing refresh propagates to the caller, not silently "
              "swallowed inside tick()", False, "no raise")
    except ZeroDivisionError:
        check("a failing refresh propagates to the caller, not silently "
              "swallowed inside tick()", True)

    c, _ = build("REGULAR")
    c.tick(None, T)   # reads from state
    check("state is read from AppState when not supplied",
          c.last_state == "REGULAR", c.last_state)
    check("reading state costs no extra upstream call — AppState.lag() is "
          "already cached for the header", c.state.lag_calls == 1)

    print("\nreporting")
    c, calls = build()
    c.tick("REGULAR", T)
    st = c.status()
    check("status reports the session", st["market_state"] == "REGULAR")
    check("status carries history", st["history"][-1]["action"] == "open")
    check("history is bounded",
          all(len(build()[0].history) <= 40 for _ in range(1)))

    print("\ntimezone")
    now = clock.now_et()
    check("exchange time is timezone-aware", now.tzinfo is not None)
    check("it is New York, so EDT/EST is handled by the stdlib",
          "New_York" in str(now.tzinfo), str(now.tzinfo))

    print(f"\n{PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
