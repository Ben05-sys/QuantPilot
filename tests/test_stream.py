#!/usr/bin/env python3
"""Streaming and live-pricing tests. No network, no websocket.

The socket itself is Yahoo's and yfinance's problem; what's tested here is
everything this project decided: which fields are safe to narrow on before
re-pricing, that a partial quote never blanks out good data, that stale
ticks are dropped rather than served, and that a dead stream reports
itself as dead instead of leaving a frozen grid labelled LIVE.

    python tests/test_stream.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from app import screen, stream

PASS = FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def frame():
    """A snapshot deliberately out of date: these were the prices hours
    ago, and the live quotes below disagree."""
    df = pd.DataFrame([
        ("NVDA", "Technology", 180.0, 1.0, 100e6, 4.5e12, 52.0),
        ("AAPL", "Technology", 240.0, -0.5, 40e6, 3.6e12, 36.0),
        ("XOM", "Energy", 118.0, 0.2, 20e6, 5.1e11, 14.0),
        ("JNJ", "Health Care", 162.0, 0.1, 6e6, 3.9e11, 24.0),
    ], columns=["symbol", "sector", "price", "change_pct", "volume",
                "market_cap", "pe"])
    for col, val in [("avg_volume_3m", 50e6), ("week52_high", 300.0),
                     ("week52_low", 100.0), ("sma50", 170.0),
                     ("sma200", 160.0), ("earnings_ts", np.nan),
                     ("pre_change_pct", np.nan), ("post_change_pct", np.nan),
                     ("ext_change_pct", np.nan), ("ext_price", np.nan),
                     ("pre_price", np.nan), ("post_price", np.nan),
                     ("day_high", np.nan), ("day_low", np.nan),
                     ("change", 0.0), ("market_state", "REGULAR")]:
        df[col] = val
    df["ext_label"] = None
    df["name"] = ["NVIDIA Corp", "Apple Inc.", "Exxon Mobil", "J&J"]
    return df


def main():
    from app.universe import derive
    df = derive(frame())

    print("\nstatic / live split")
    cases = [
        ("mcap > 10b and chg > 3", "mcap > 10000000000.0", "chg > 3"),
        ('sector == "Energy" and pe < 20', None, ""),
        ("chg > 3", "", "chg > 3"),
        ("extchg > 5 and mcap > 1b", "mcap > 1000000000.0", "extchg > 5"),
    ]
    for expr, want_static, want_live in cases:
        static, live = screen.split_live(expr)
        if want_static is not None:
            check(f"static half of {expr!r}", static == want_static,
                  f"got {static!r}")
        check(f"live half of {expr!r}", live == want_live, f"got {live!r}")

    check("market cap narrows before re-pricing",
          "market_cap" in screen.STATIC_SAFE)
    check("change never narrows before re-pricing",
          "change_pct" not in screen.STATIC_SAFE)
    check("volume never narrows — it only rises during a session",
          "volume" not in screen.STATIC_SAFE)
    check("one live field contaminates the whole conjunct",
          screen.split_live("mcap > 1b and chg > 2")[0] == "mcap > 1000000000.0")
    check("a mixed single condition is treated as live",
          screen.split_live("price / sma50 > 1.1")[0] == "")
    check("fields_used resolves aliases",
          screen.fields_used("mcap > 1b and relvol > 2") ==
          {"market_cap", "rel_volume"},
          screen.fields_used("mcap > 1b and relvol > 2"))

    print("\nlive overlay")
    quotes = {
        "NVDA": {"price": 196.5, "change_pct": 9.2, "volume": 180e6},
        "AAPL": {"price": 336.9, "change_pct": 1.16},   # no volume: partial
        "XOM": {"ext_change_pct": 4.4, "ext_label": "PRE", "ext_price": 123.2},
    }
    live = screen.apply_quotes(df, quotes)
    check("price is overwritten",
          float(live.loc[live.symbol == "NVDA", "price"].iloc[0]) == 196.5)
    check("change is overwritten",
          float(live.loc[live.symbol == "NVDA", "change_pct"].iloc[0]) == 9.2)
    check("a partial quote does not blank the snapshot",
          float(live.loc[live.symbol == "AAPL", "volume"].iloc[0]) == 40e6,
          live.loc[live.symbol == "AAPL", "volume"].iloc[0])
    check("an unquoted symbol keeps its snapshot values",
          float(live.loc[live.symbol == "JNJ", "price"].iloc[0]) == 162.0)
    check("extended fields land",
          float(live.loc[live.symbol == "XOM", "ext_change_pct"].iloc[0]) == 4.4)
    check("extended label lands",
          live.loc[live.symbol == "XOM", "ext_label"].iloc[0] == "PRE")
    # 180M traded against a 50M average. rel_volume itself is scaled by
    # how far into the session we are, so the raw ratio is the stable
    # thing to assert on.
    check("derived columns are recomputed from the live price",
          abs(float(live.loc[live.symbol == "NVDA",
                             "rel_volume_raw"].iloc[0]) - 3.6) < 0.01,
          live.loc[live.symbol == "NVDA", "rel_volume_raw"].iloc[0])
    check("and the session adjustment is applied on top",
          float(live.loc[live.symbol == "NVDA", "rel_volume"].iloc[0])
          >= float(live.loc[live.symbol == "NVDA", "rel_volume_raw"].iloc[0]))
    check("empty quotes are a no-op",
          screen.apply_quotes(df, {}) is df)

    print("\nthe screen the whole thing exists for")
    stale = df[screen.mask(df, "chg > 3")]
    fresh = live[screen.mask(live, "chg > 3")]
    check("stale snapshot finds nothing", list(stale["symbol"]) == [],
          list(stale["symbol"]))
    check("live prices find the mover", list(fresh["symbol"]) == ["NVDA"],
          list(fresh["symbol"]))

    print("\ntick stream")
    ts = stream.TickStream()
    check("probes for the streaming stack",
          isinstance(ts.available, bool))
    st = ts.status()
    check("reports not connected before start", st["connected"] is False)
    check("an unstarted stream is never healthy", st["healthy"] is False)
    check("nothing subscribed yet", st["subscribed"] == 0)

    ts.subscribe(["nvda", " aapl ", ""])
    check("symbols are normalized",
          ts._wanted == {"NVDA", "AAPL"}, ts._wanted)
    ts.subscribe(["NVDA", "AAPL"])
    check("resubscribing the same set is a no-op",
          ts._wanted == {"NVDA", "AAPL"})
    ts.subscribe([f"S{i}" for i in range(stream.MAX_SYMBOLS + 50)])
    check("what actually goes to the socket is capped",
          len(ts._target()) == stream.MAX_SYMBOLS, len(ts._target()))

    print("\npinned symbols (the watchlist)")
    ts.pin(["AAPL", "NVDA"])
    check("pinned symbols are in the target set",
          {"AAPL", "NVDA"} <= ts._target())
    check("still capped with pins",
          len(ts._target()) == stream.MAX_SYMBOLS, len(ts._target()))
    ts.subscribe([f"Z{i}" for i in range(stream.MAX_SYMBOLS + 50)])
    check("a full viewport cannot crowd the watchlist out — this is the "
          "one panel you would not notice going stale",
          {"AAPL", "NVDA"} <= ts._target())
    ts.pin([])
    check("unpinning drops them from the target",
          not ({"AAPL", "NVDA"} & ts._target()))
    ts.subscribe(["AAPL"])
    check("pinning is idempotent", (ts.pin(["X"]), ts.pin(["X"]))[1] is None)

    print("\nbackoff and cooldown")
    check("cooldown threshold is set", stream.COOLDOWN_AFTER >= 3)
    check("cooldown is long enough to matter", stream.COOLDOWN >= 300)
    check("backoff grows", stream._BACKOFF == sorted(stream._BACKOFF))
    check("status exposes the failure count", "failures" in ts.status())
    check("status exposes the pinned count", "pinned" in ts.status())

    # The real payload shape: protobuf's MessageToDict renders int64 as a
    # string, so `time` arrives quoted. An earlier version of this test
    # passed an int, which is how a crash in the tick handler survived
    # into a running server.
    ts._handle({"id": "NVDA", "price": 196.5, "change_percent": 9.2,
                "time": "1785232190000"})
    snap = ts.snapshot()
    check("a tick is recorded", "NVDA" in snap)
    check("price is carried", snap["NVDA"]["price"] == 196.5)
    check("a string timestamp in milliseconds becomes seconds",
          abs(snap["NVDA"]["time"] - 1785232190) < 1, snap["NVDA"]["time"])
    ts._handle({"id": "AMD", "price": "459.23", "time": "1785232190000"})
    check("a string price is accepted",
          ts.snapshot()["AMD"]["price"] == 459.23)
    check("snapshot filters by symbol",
          set(ts.snapshot(["NVDA"])) == {"NVDA"})
    ts._handle({"id": "BAD"})
    check("a tick with no price is ignored", "BAD" not in ts.snapshot())
    ts._handle({"id": "JUNK", "price": "n/a"})
    check("an unparseable price is ignored", "JUNK" not in ts.snapshot())
    ts._handle({"id": "NOTIME", "price": 5.0})
    check("a tick with no timestamp still lands", "NOTIME" in ts.snapshot())

    ts._ticks["OLD"] = {"symbol": "OLD", "price": 1.0,
                        "at": time.time() - stream.TICK_TTL - 5}
    check("stale ticks are dropped, not served", "OLD" not in ts.snapshot())

    print("\nwhat else the print was carrying")
    # Yahoo puts the running session totals on every tick and this handler
    # used to keep four fields out of thirteen. While the stream was up a
    # row's volume, relative volume and day range therefore stayed frozen
    # at the last snapshot: the price moved and everything derived from it
    # did not. Field names are Yahoo's, verified against a live feed.
    ts._handle({"id": "AMD", "price": 475.75, "change": 5.5,
                "change_percent": 1.17, "day_volume": "10784039",
                "day_high": 478.0, "day_low": 469.5, "last_size": "100",
                "exchange": "NMS", "market_hours": 1,
                "time": "1785232190000"})
    got = ts.snapshot()["AMD"]
    check("the day's volume arrives with the price", got["volume"] == 10784039)
    check("so does the day's range",
          got["day_high"] == 478.0 and got["day_low"] == 469.5, got)
    check("and the size of the print", got["last_size"] == 100)
    check("and where it traded", got["exchange"] == "NMS")

    print("\nevery print states its own session")
    for code, name in stream.MARKET_HOURS.items():
        ts._handle({"id": f"H{code}", "price": 10.0, "market_hours": code,
                    "time": "1785232190000"})
        check(f"market_hours {code} is {name}",
              ts.snapshot()[f"H{code}"]["session"] == name)
    # Same conversion that quotes int64 will quote this one day.
    ts._handle({"id": "STRH", "price": 10.0, "market_hours": "2",
                "time": "1785232190000"})
    check("a session that arrived as a string still reads",
          ts.snapshot()["STRH"]["session"] == "POST")
    ts._handle({"id": "NOH", "price": 10.0, "time": "1785232190000"})
    check("an absent session is null, not a guess",
          ts.snapshot()["NOH"]["session"] is None)
    ts._handle({"id": "ODDH", "price": 10.0, "market_hours": 99,
                "time": "1785232190000"})
    check("an unrecognised session is null too",
          ts.snapshot()["ODDH"]["session"] is None)

    print("\nthe delay is measured, not assumed")
    fresh = stream.TickStream()
    check("no ticks means no latency to report, not zero",
          fresh.latency() is None)
    check("status says so as well", fresh.status()["latency"] is None)
    now_ms = time.time() * 1000.0
    for offset in (1000, 3000, 5000):      # 1s, 3s, 5s behind
        fresh._handle({"id": f"L{offset}", "price": 1.0,
                       "time": str(int(now_ms - offset))})
    check("the median of the sample is what is reported",
          abs(fresh.latency() - 3.0) < 0.5, fresh.latency())
    check("status carries it for the badge",
          abs(fresh.status()["latency"] - 3.0) < 0.5)
    check("and how many prints it rests on",
          fresh.status()["samples"] == 3)
    fresh._handle({"id": "NOTS", "price": 1.0})
    check("a tick with no venue timestamp is not sampled — it would "
          "score a flattering zero", fresh.status()["samples"] == 3)
    check("the window is bounded",
          fresh._latencies.maxlen == stream.LATENCY_WINDOW)

    ts.connected = True
    ts.last_tick_at = 0.0
    check("an open socket that never ticked is not healthy",
          ts.status()["healthy"] is False)
    ts.last_tick_at = time.time()
    check("connected and ticking is healthy", ts.status()["healthy"] is True)
    ts.last_tick_at = time.time() - 300
    check("connected but gone silent is not healthy",
          ts.status()["healthy"] is False)

    check("backoff is bounded",
          stream._BACKOFF[-1] <= 60 and stream._BACKOFF[0] >= 1)

    print(f"\n{PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
