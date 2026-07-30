#!/usr/bin/env python3
"""Screen membership diffs, against a throwaway database.

    python tests/test_diffs.py
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="pilotmarkets-diff-"))
os.environ["PILOTMARKETS_HOME"] = str(TMP)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app import diffs, store  # noqa: E402
from app.universe import derive  # noqa: E402

PASS = FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def frame(changes):
    """`changes` maps symbol -> change_pct."""
    symbols = list(changes)
    df = pd.DataFrame({
        "symbol": symbols,
        "name": [f"{s} Corp" for s in symbols],
        "sector": ["Technology"] * len(symbols),
        "change_pct": [changes[s] for s in symbols],
        "price": [100.0] * len(symbols),
        "volume": [5e6] * len(symbols),
        "avg_volume_3m": [1e6] * len(symbols),
        "market_cap": [5e10] * len(symbols),
        "week52_high": [150.0] * len(symbols),
        "week52_low": [50.0] * len(symbols),
        "sma50": [95.0] * len(symbols),
        "sma200": [90.0] * len(symbols),
        "earnings_ts": [np.nan] * len(symbols),
        "market_state": ["CLOSED"] * len(symbols),
    })
    return derive(df)


def main():
    print(f"\nusing {TMP}")
    store.save_screen("Movers", "chg > 3")

    print("\nfirst run")
    diffs.record_all(frame({"AAA": 5.0, "BBB": 4.0, "CCC": 1.0}))
    d = diffs.for_screen("Movers")
    check("members are recorded", d["count"] == 2, d)
    check("a single run reports no arrivals — with nothing to compare "
          "against, calling every member 'entered' would be a lie",
          d["entered"] == [] and d["exited"] == [], d)
    check("run count is honest", d["runs"] == 1)

    print("\nsecond run")
    time.sleep(0.01)
    diffs.record_all(frame({"AAA": 5.0, "BBB": 1.0, "CCC": 6.0, "DDD": 9.0}))
    d = diffs.for_screen("Movers")
    check("new matches are reported as entered",
          d["entered"] == ["CCC", "DDD"], d["entered"])
    check("dropped matches are reported as exited",
          d["exited"] == ["BBB"], d["exited"])
    check("unchanged members appear in neither",
          "AAA" not in d["entered"] and "AAA" not in d["exited"])
    check("count reflects the latest run", d["count"] == 3, d["count"])
    check("the comparison point is timestamped", d["since"] is not None)

    print("\nthird run, nothing changed")
    time.sleep(0.01)
    diffs.record_all(frame({"AAA": 5.0, "CCC": 6.0, "DDD": 9.0}))
    d = diffs.for_screen("Movers")
    check("a quiet interval reports no movement",
          d["entered"] == [] and d["exited"] == [], d)

    print("\nmany screens")
    store.save_screen("Big", "mcap > 1b")
    store.save_screen("Third", "chg > 100")
    result = diffs.record_all(frame({"AAA": 5.0}))
    check("every screen is recorded", result["recorded"] == 3, result)
    check("nothing failed", result["failed"] == [], result["failed"])
    all_of = diffs.all_screens()
    check("all_screens covers them", len(all_of["screens"]) == 3)

    print("\nrobustness")
    store.save_screen("Bad", "nosuchfield > 1")
    result = diffs.record_all(frame({"AAA": 5.0}))
    check("a broken saved screen is skipped, not fatal",
          result["recorded"] == 3 and len(result["failed"]) == 1, result)
    check("the broken one is named by name",
          result["failed"][0]["name"] == "Bad", result["failed"])
    check("the others still recorded",
          diffs.for_screen("Movers")["runs"] >= 2)
    check("an unknown screen is empty rather than an error",
          diffs.for_screen("NeverSaved")["runs"] == 0)
    check("an empty frame records nothing",
          diffs.record_all(frame({"AAA": 1.0}).iloc[0:0])["recorded"] == 0)

    print(f"\n{PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        try:
            store.conn().close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(TMP, ignore_errors=True)
    raise SystemExit(code)
