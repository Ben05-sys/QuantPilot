#!/usr/bin/env python3
"""Option chain shaping. Offline — the provider is stubbed.

What's tested is the decisions this project made: pairing calls and puts
by strike, centring the window on the money rather than taking the first
N strikes, ranking unusual activity by size rather than by ratio, and
never losing the `delayed` flag.

    python tests/test_options.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import options
from app.providers import cboe
from app.providers.base import ProviderError

PASS = FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


SPOT = 100.0


def fixture():
    """Strikes 50..150 on two expiries, shaped like Cboe's payload."""
    contracts = []
    for expiry in ("2026-08-07", "2026-09-18"):
        for strike in range(50, 155, 5):
            for kind in ("call", "put"):
                # Two deliberate hotspots on the near expiry, of different
                # sizes, so ranking is actually exercised: the 120 call is
                # the bigger trade, the 90 put has the higher ratio.
                big = (strike == 120 and kind == "call"
                       and expiry == "2026-08-07")
                small = (strike == 90 and kind == "put"
                         and expiry == "2026-08-07")
                contracts.append({
                    "contract": f"T{expiry}{kind[0].upper()}{strike}",
                    "expiry": expiry, "type": kind, "strike": float(strike),
                    "bid": 1.0, "ask": 1.4, "last": 1.2,
                    "volume": (9000.0 if big else 2000.0 if small
                               else (60.0 if kind == "call" else 30.0)),
                    "open_interest": (300.0 if big else 20.0 if small
                                      else 1000.0),
                    "iv": 0.31,
                    "delta": 0.5 if kind == "call" else -0.5,
                    "gamma": 0.02, "theta": -0.05, "vega": 0.11, "rho": 0.01,
                    "theo": 1.19,
                })
    return {"symbol": "TEST", "price": SPOT, "iv30": 28.5,
            "expiries": ["2026-08-07", "2026-09-18"],
            "contracts": contracts, "delayed": True, "source": "cboe"}


def main():
    original = cboe.chain
    cboe.chain = lambda symbol, ttl=600: fixture()
    try:
        return run()
    finally:
        cboe.chain = original


def run():
    print("\nchain shaping")
    c = options.chain("TEST")
    check("defaults to the nearest expiry", c["expiry"] == "2026-08-07",
          c["expiry"])
    check("every expiry is offered", len(c["expiries"]) == 2)
    check("calls and puts are paired on one row",
          all(r["call"] and r["put"] for r in c["rows"]),
          [r["strike"] for r in c["rows"] if not (r["call"] and r["put"])])
    check("strikes ascend",
          [r["strike"] for r in c["rows"]] ==
          sorted(r["strike"] for r in c["rows"]))
    check("greeks survive",
          c["rows"][0]["call"]["gamma"] == 0.02
          and c["rows"][0]["put"]["delta"] == -0.5)
    check("spread is computed", abs(c["rows"][0]["call"]["spread"] - 0.4) < 1e-9)
    check("mid is computed", abs(c["rows"][0]["call"]["mid"] - 1.2) < 1e-9)
    check("delayed rides on the payload", c["delayed"] is True)
    check("source is named", c["source"] == "cboe")

    print("\nthe window is centred on the money")
    narrow = options.chain("TEST", strikes=3)
    ks = [r["strike"] for r in narrow["rows"]]
    check("window is bounded", len(ks) == 7, len(ks))
    check("spot is inside the window", min(ks) <= SPOT <= max(ks), ks)
    check("window straddles spot rather than starting at the lowest strike",
          min(ks) > 50, ks)
    check("a chain shorter than the window is returned whole",
          len(options.chain("TEST", strikes=999)["rows"]) == 21)

    print("\nexpiry selection")
    far = options.chain("TEST", expiry="2026-09-18")
    check("an explicit expiry is honoured", far["expiry"] == "2026-09-18")
    check("an unknown expiry falls back to the nearest, not an error",
          options.chain("TEST", expiry="1999-01-01")["expiry"] == "2026-08-07")

    print("\nput/call ratios")
    s = c["stats"]
    check("volume ratio is computed", s["all"]["pc_volume"] is not None)
    check("open-interest ratio is computed too — they disagree and the "
          "difference is the point", s["all"]["pc_oi"] is not None)
    check("ratios are per-expiry as well as overall",
          s["expiry"]["call_volume"] < s["all"]["call_volume"])
    check("contract count is reported", s["contracts"] == 84, s["contracts"])

    print("\nunusual activity")
    u = options.unusual("TEST")
    check("finds the hotspot", u["rows"][0]["strike"] == 120.0,
          u["rows"][0]["strike"])
    check("it is a call", u["rows"][0]["type"] == "call")
    check("ratio is computed", abs(u["rows"][0]["vol_oi"] - 30.0) < 1e-9)
    check("two hotspots are found", u["total"] == 2, u["total"])
    check("ranked by size, not by ratio — the 90 put has a 100x ratio and "
          "still ranks second behind 9,000 contracts",
          [r["volume"] for r in u["rows"]] == [9000.0, 2000.0],
          [r["volume"] for r in u["rows"]])
    check("the higher-ratio contract really is the smaller one",
          u["rows"][1]["vol_oi"] > u["rows"][0]["vol_oi"])
    check("quiet contracts are excluded",
          all(r["volume"] >= options.MIN_UNUSUAL_VOLUME for r in u["rows"]))
    check("contracts below the ratio floor are excluded",
          all(r["vol_oi"] >= 1.0 for r in u["rows"]))
    check("notional is computed at the mid",
          abs(u["rows"][0]["notional"] - 1.2 * 9000 * 100) < 1e-6,
          u["rows"][0]["notional"])
    check("moneyness is relative to spot",
          abs(u["rows"][0]["moneyness"] - 20.0) < 1e-9,
          u["rows"][0]["moneyness"])
    check("delayed rides on this payload too", u["delayed"] is True)
    check("a high floor excludes everything rather than erroring",
          options.unusual("TEST", min_volume=1e9)["rows"] == [])
    check("limit is honoured", len(options.unusual("TEST", limit=1)["rows"]) == 1)
    check("total counts matches before the limit",
          options.unusual("TEST", limit=1)["total"] > 1)

    print("\nempty chain")
    empty = dict(fixture())
    empty["contracts"] = []
    cboe.chain = lambda symbol, ttl=600: empty
    try:
        options.chain("TEST")
        check("an empty chain raises ProviderError", False, "no raise")
    except ProviderError:
        check("an empty chain raises ProviderError", True)
    except Exception as exc:  # noqa: BLE001
        check("an empty chain raises ProviderError", False,
              f"raised {type(exc).__name__}")

    print(f"\n{PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
