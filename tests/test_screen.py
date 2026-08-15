#!/usr/bin/env python3
"""Screening engine tests. No network, no database — a synthetic frame.

    python tests/test_screen.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from app import screen
from app.screen import ScreenError

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
    """Six rows chosen so every filter has both a hit and a miss."""
    return pd.DataFrame([
        # symbol  sector        price   chg   vol      mcap   pe    relvol
        ("NVDA", "Technology",  184.22,  1.84, 182e6, 4.51e12, 52.1, 1.20,
         -1.0, 12.0, 0.0, 3.0),
        ("AAPL", "Technology",  241.90, -0.31,  44e6, 3.62e12, 36.4, 0.80,
         -8.0, 5.0, 0.55, 45.0),
        ("XOM",  "Energy",      118.40,  3.42,  21e6, 5.10e11, 14.2, 2.40,
         -2.0, 3.0, 3.20, 2.0),
        ("PENNY", "Health Care",   0.42, -7.10, 900e3, 4.10e7, np.nan, 6.10,
         -78.0, -20.0, 0.0, np.nan),
        ("JNJ",  "Health Care", 162.10,  0.12,   6e6, 3.90e11, 24.8, 0.60,
         -4.0, 1.0, 3.05, 90.0),
        ("SPY",  None,          739.09,  0.42,  38e6, np.nan,  np.nan, 0.90,
         -0.5, 8.0, 1.10, np.nan),
    ], columns=["symbol", "sector", "price", "change_pct", "volume",
                "market_cap", "pe", "rel_volume", "pct_from_52w_high",
                "pct_from_sma50", "dividend_yield", "earnings_days"])


def syms(df):
    return sorted(df["symbol"].tolist())


def main():
    df = frame()
    # The aliases the evaluator leans on.
    df["mktcap"] = df["market_cap"]
    df["chg"] = df["change_pct"]
    df["sma50"] = df["price"] / (1 + df["pct_from_sma50"] / 100)
    df["sma200"] = df["sma50"] * 0.95
    df["eps_ttm"] = df["price"] / df["pe"]
    df["quote_type"] = ["EQUITY"] * 5 + ["ETF"]
    df["industry"] = ["Semiconductors", "Computer Manufacturing",
                      "Integrated oil", "Biotechnology: Laboratory",
                      "Pharmaceutical", None]

    print("\nliterals and aliases")
    check("suffix 10b expands",
          "10000000000.0" in screen.normalize("mcap > 10b"),
          screen.normalize("mcap > 10b"))
    check("suffix 500k expands",
          "500000.0" in screen.normalize("vol > 500k"))
    check("percent literal dropped",
          screen.normalize("chg > 3%") == "chg > 3")
    check("sma50 is not mangled by the suffix rule",
          screen.normalize("fromsma50 > 0") == "fromsma50 > 0",
          screen.normalize("fromsma50 > 0"))
    check("CAPS AND lowered", "and" in screen.normalize("chg>1 AND vol>1k"))
    check("single = becomes ==",
          screen.normalize('sector = "Energy"') == 'sector == "Energy"')
    check("alias mcap -> market_cap", screen.resolve("mcap") == "market_cap")
    check("alias relvol -> rel_volume", screen.resolve("RelVol") == "rel_volume")

    print("\nnumeric filters")
    check("mcap > 10b", syms(df[screen.mask(df, "mcap > 10b")]) ==
          ["AAPL", "JNJ", "NVDA", "XOM"],
          syms(df[screen.mask(df, "mcap > 10b")]))
    check("chg > 3 and relvol > 2",
          syms(df[screen.mask(df, "chg > 3 and relvol > 2")]) == ["XOM"])
    check("chained 2 < pe < 30",
          syms(df[screen.mask(df, "2 < pe < 30")]) == ["JNJ", "XOM"],
          syms(df[screen.mask(df, "2 < pe < 30")]))
    check("arithmetic price / sma50 > 1.05",
          "NVDA" in syms(df[screen.mask(df, "price / sma50 > 1.05")]))
    check("negative threshold from52high > -5",
          syms(df[screen.mask(df, "from52high > -5")]) ==
          ["JNJ", "NVDA", "SPY", "XOM"],
          syms(df[screen.mask(df, "from52high > -5")]))
    check("not operator",
          "PENNY" not in syms(df[screen.mask(df, "not (price < 1)")]))
    check("or operator",
          syms(df[screen.mask(df, "chg > 3 or chg < -5")]) ==
          ["PENNY", "XOM"])
    check("empty expression selects all",
          int(screen.mask(df, "").sum()) == len(df))

    print("\nmissing values")
    check("NaN pe excluded from pe < 25",
          syms(df[screen.mask(df, "pe < 25")]) == ["JNJ", "XOM"],
          syms(df[screen.mask(df, "pe < 25")]))
    check("NaN mcap excluded from mcap > 0",
          "SPY" not in syms(df[screen.mask(df, "mcap > 0")]))
    check("unknown column yields no rows, not a crash",
          int(screen.mask(df.drop(columns=["pe"]), "pe > 0").sum()) == 0)

    print("\ntext filters")
    check("sector equality is case-insensitive",
          syms(df[screen.mask(df, 'sector == "technology"')]) ==
          ["AAPL", "NVDA"])
    check("sector in list",
          syms(df[screen.mask(df, 'sector in ("Energy", "Health Care")')]) ==
          ["JNJ", "PENNY", "XOM"],
          syms(df[screen.mask(df, 'sector in ("Energy", "Health Care")')]))
    check("substring search in industry",
          syms(df[screen.mask(df, '"semi" in industry')]) == ["NVDA"])
    check("!= keeps rows with a null sector out of the wrong bucket",
          "AAPL" not in syms(df[screen.mask(df, 'sector != "Technology"')]))
    check("quote_type filter",
          syms(df[screen.mask(df, 'type == "ETF"')]) == ["SPY"])

    print("\nrejected expressions")
    for bad, why in [
        ('__import__("os").system("dir")', "call"),
        ("df.drop(columns=['pe'])", "attribute"),
        ("price.__class__", "dunder attribute"),
        ("[x for x in price]", "comprehension"),
        ("price if pe else vol", "conditional"),
        ("lambda x: x", "lambda"),
        ("open('secrets.txt')", "builtin call"),
        ("price[0]", "subscript"),
        ("nosuchfield > 3", "unknown field"),
        ("price >", "syntax error"),
    ]:
        try:
            screen.mask(df, bad)
            check(f"rejects {why}", False, f"{bad!r} was accepted")
        except ScreenError:
            check(f"rejects {why}", True)
        except Exception as exc:  # noqa: BLE001
            check(f"rejects {why} cleanly", False,
                  f"raised {type(exc).__name__} instead of ScreenError: {exc}")

    print("\nrun(): sort and paginate")
    page, total = screen.run(df, "mcap > 0", sort="market_cap",
                             descending=True, limit=2)
    check("total counts all matches", total == 5, f"total={total}")
    check("page is limited", len(page) == 2)
    check("sorted by market cap desc",
          page["symbol"].tolist() == ["NVDA", "AAPL"],
          page["symbol"].tolist())
    page2, _ = screen.run(df, "mcap > 0", sort="market_cap", limit=2, offset=2)
    check("offset pages forward",
          page2["symbol"].tolist() == ["XOM", "JNJ"],
          page2["symbol"].tolist())
    asc, _ = screen.run(df, "", sort="price", descending=False, limit=1)
    check("ascending sort", asc["symbol"].tolist() == ["PENNY"])
    nan_last, _ = screen.run(df, "", sort="pe", descending=False, limit=6)
    check("NaN sorts last in both directions",
          nan_last["symbol"].tolist()[0] == "XOM",
          nan_last["symbol"].tolist())

    print("\ndropdown bar")
    expr = screen.filters_to_expr({"cap": "Large >10B", "chg": "Up 3%+"})
    check("filters compile to an expression",
          "mcap > 10b" in expr and "chg > 3" in expr, expr)
    check("compiled expression evaluates",
          syms(df[screen.mask(df, expr)]) == ["XOM"], expr)
    check("Any is ignored",
          screen.filters_to_expr({"cap": "Any", "chg": "Up 3%+"}) == "chg > 3")
    check("empty selection means no filter",
          screen.filters_to_expr({}) == "")
    check("every preset parses",
          all(isinstance(screen.mask(df, e), pd.Series)
              for _, e in screen.PRESETS))
    check("every dropdown option parses",
          all(isinstance(screen.mask(df, frag), pd.Series)
              for _, _, opts in screen.FILTER_SPECS
              for _, frag in opts))
    check("describe splits top-level ands",
          screen.describe("mcap > 10b and chg > 3") ==
          ["mcap > 10b", "chg > 3"],
          screen.describe("mcap > 10b and chg > 3"))
    check("describe keeps a parenthesised or intact",
          screen.describe("(chg > 3 or chg < -3) and vol > 1m") ==
          ["chg > 3 or chg < -3", "vol > 1m"],
          screen.describe("(chg > 3 or chg < -3) and vol > 1m"))

    print("\nADR detection")
    from app.universe import derive
    adr_df = derive(pd.DataFrame({
        "symbol": ["BABA", "SONY", "ADSE", "AAPL", "SHEL", "SPOT", "NONE"],
        "name": ["Alibaba Group Holding Limited American Depositary Shares",
                 "Sony Group Corporation American Depositary Shares",
                 # The trap: 'ADS-TEC' contains ADS but is an Irish
                 # ordinary-share listing, not a depositary receipt.
                 "ADS-TEC ENERGY PLC Ordinary Shares",
                 "Apple Inc. Common Stock",
                 "Shell PLC American Depositary Shares (each representing two)",
                 "Spotify Technology S.A. Ordinary Shares",
                 None],
        "country": ["China", "Japan", "Ireland", "United States",
                    "Netherlands", "Luxembourg", None],
        "price": [150.0, 25.0, 8.0, 240.0, 70.0, 500.0, 1.0],
        "change_pct": [2.0, 1.0, -1.0, 0.5, 3.0, -2.0, 0.0],
        "volume": [5e6, 2e6, 1e5, 40e6, 3e6, 1e6, 5e5],
        "market_cap": [275e9, 133e9, 3e8, 3.6e12, 240e9, 100e9, 1e7],
        "avg_volume_3m": [4e6] * 7, "week52_high": [200.0] * 7,
        "week52_low": [50.0] * 7, "sma50": [140.0] * 7, "sma200": [130.0] * 7,
        "earnings_ts": [np.nan] * 7,
    }))
    adrs = sorted(adr_df[adr_df["is_adr"]]["symbol"].tolist())
    check("ADRs are found", adrs == ["BABA", "SHEL", "SONY"], adrs)
    check("ADS-TEC is not an ADR — 'ADS' in a company name is not a "
          "security type", "ADSE" not in adrs)
    check("ordinary shares of a foreign company are not ADRs",
          "SPOT" not in adrs)
    check("a US common stock is not an ADR", "AAPL" not in adrs)
    check("a null name does not crash the match",
          bool(adr_df.loc[adr_df.symbol == "NONE", "is_adr"].iloc[0]) is False)
    check("`adr` is usable bare in an expression",
          sorted(adr_df[screen.mask(adr_df, "adr")]["symbol"]) == adrs)
    check("`not adr` inverts it",
          "AAPL" in adr_df[screen.mask(adr_df, "not adr")]["symbol"].tolist())
    check("ADR + country composes",
          adr_df[screen.mask(adr_df, 'adr and country == "China"')
                 ]["symbol"].tolist() == ["BABA"])
    print("\nsector relative strength")
    from app.universe import derive as _derive
    sec = _derive(pd.DataFrame({
        "symbol": ["A", "B", "HALTED"], "name": ["A", "B", "H"],
        "sector": ["Technology"] * 3,
        # A and B agree the sector is up 10%. HALTED has no change_pct —
        # a partial refresh or a trading halt — but carries most of the
        # sector's market cap.
        "price": [100.0] * 3, "change_pct": [10.0, 10.0, np.nan],
        "volume": [1e6] * 3, "avg_volume_3m": [1e6] * 3,
        "market_cap": [1e9, 1e9, 8e9],
        "week52_high": [120.0] * 3, "week52_low": [80.0] * 3,
        "sma50": [100.0] * 3, "sma200": [100.0] * 3,
        "earnings_ts": [np.nan] * 3,
    }))
    secrow = dict(zip(sec["symbol"], sec.to_dict("records"), strict=True))
    check("a name with no change_pct does not drag the sector average "
          "toward flat just by having a market cap",
          abs(secrow["A"]["sector_change_pct"] - 10.0) < 1e-9,
          secrow["A"]["sector_change_pct"])
    check("a stock in line with its sector reads zero relative strength",
          abs(secrow["A"]["rs_sector"]) < 1e-9, secrow["A"]["rs_sector"])
    check("a halted name with no change_pct gets no relative strength either",
          pd.isna(secrow["HALTED"]["rs_sector"]))

    check("alias is_adr also resolves", screen.resolve("is_adr") == "is_adr")
    check("ADRs narrow before re-pricing — domicile does not drift",
          "is_adr" in screen.STATIC_SAFE)

    print("\ntime-adjusted relative volume")
    import datetime
    from zoneinfo import ZoneInfo

    from app import universe as U
    ET = ZoneInfo("America/New_York")
    OPEN = datetime.datetime(2026, 7, 28, 9, 30, tzinfo=ET)
    def at(h, m):
        return U.session_fraction(
            datetime.datetime(2026, 7, 28, h, m, tzinfo=ET))

    def after_open(minutes):
        return U.session_fraction(OPEN + datetime.timedelta(minutes=minutes))

    check("nothing expected before the open — a closed market compares "
          "full day to full day", at(8, 0) == 1.0)
    check("half the day by roughly 13:00",
          0.40 < at(13, 0) < 0.55, at(13, 0))
    check("the curve only rises",
          all(after_open(m) <= after_open(m + 30) + 1e-9
              for m in range(30, 360, 30)))
    check("volume is front-loaded, not spread evenly — an even split would "
          "put 23% on the board by 11:00",
          at(11, 0) > 0.23, at(11, 0))
    check("the divisor is floored, so the first minutes can't report 250x",
          at(9, 32) >= U.MIN_SESSION_FRACTION, at(9, 32))
    check("after the close it is the whole day again", at(17, 0) == 1.0)

    def vol_frame(state):
        return pd.DataFrame({
            "symbol": ["X"], "name": ["X Corp"], "sector": ["Technology"],
            "price": [100.0], "change_pct": [1.0],
            "volume": [500_000.0], "avg_volume_3m": [1_000_000.0],
            # Half its three-month average, but twice its last two weeks:
            # the case the 3m ratio alone reads as a quiet day.
            "avg_volume_10d": [250_000.0],
            "market_cap": [1e10], "week52_high": [150.0],
            "week52_low": [50.0], "sma50": [95.0], "sma200": [90.0],
            "earnings_ts": [np.nan], "market_state": [state]})

    live = vol_frame("REGULAR")
    adjusted = derive(live)
    check("during the session, relvol is scaled by the elapsed fraction",
          adjusted["rel_volume"].iloc[0] >= adjusted["rel_volume_raw"].iloc[0],
          (adjusted["rel_volume"].iloc[0], adjusted["rel_volume_raw"].iloc[0]))
    check("the raw ratio is kept alongside it",
          abs(adjusted["rel_volume_raw"].iloc[0] - 0.5) < 1e-9,
          adjusted["rel_volume_raw"].iloc[0])

    closed = derive(vol_frame("CLOSED"))
    check("a closed market is not adjusted",
          abs(closed["rel_volume"].iloc[0] - 0.5) < 1e-9,
          closed["rel_volume"].iloc[0])
    check("a market holiday cannot inflate readings — the venue's state "
          "decides, not the wall clock",
          closed["session_fraction"].iloc[0] == 1.0)

    # 500k against a 250k ten-day average is 2.0 — the same tape the
    # three-month ratio calls 0.5. Both readings are true; they answer
    # different questions, which is the point of carrying both.
    check("the ten-day ratio is volume over the 10-day average",
          abs(closed["rel_volume_10d"].iloc[0] - 2.0) < 1e-9,
          closed["rel_volume_10d"].iloc[0])
    check("and it disagrees with the three-month one, as it should",
          closed["rel_volume"].iloc[0] < 1 < closed["rel_volume_10d"].iloc[0],
          (closed["rel_volume"].iloc[0], closed["rel_volume_10d"].iloc[0]))
    check("during the session it is scaled by the elapsed fraction too",
          adjusted["rel_volume_10d"].iloc[0] >= 2.0,
          adjusted["rel_volume_10d"].iloc[0])
    check("relvol10 resolves", screen.resolve("relvol10") == "rel_volume_10d")
    check("today's volume only rises, so it is never prefiltered on a "
          "stale snapshot",
          "rel_volume_10d" in screen.LIVE_COLUMNS
          and "rel_volume_10d" not in screen.STATIC_SAFE)
    check("a snapshot taken before the 10-day column existed derives "
          "rather than raising",
          derive(pd.DataFrame({
              "symbol": ["X"], "name": ["X"], "sector": ["T"], "price": [1.0],
              "change_pct": [0.0], "volume": [1.0], "avg_volume_3m": [1.0],
              "market_cap": [1.0], "week52_high": [1.0], "week52_low": [1.0],
              "sma50": [1.0], "sma200": [1.0], "earnings_ts": [np.nan],
          }))["rel_volume_10d"].isna().all())

    print("\nearnings timing")
    def when_for(hour, minute=0):
        ts = datetime.datetime(2026, 7, 29, hour, minute,
                               tzinfo=ET).timestamp()
        f = pd.DataFrame({
            "symbol": ["X"], "name": ["X"], "sector": ["T"], "price": [10.0],
            "change_pct": [0.0], "volume": [1.0], "avg_volume_3m": [1.0],
            "market_cap": [1.0], "week52_high": [1.0], "week52_low": [1.0],
            "sma50": [1.0], "sma200": [1.0], "earnings_ts": [ts]})
        return derive(f)["earnings_when"].iloc[0]
    check("08:30 ET is before the open", when_for(8, 30) == "BMO",
          when_for(8, 30))
    check("16:00 ET is after the close", when_for(16, 0) == "AMC",
          when_for(16, 0))
    check("11:00 ET is during market hours", when_for(11, 0) == "DMH")
    check("09:29 is still before the open", when_for(9, 29) == "BMO")
    check("a missing date stays null rather than defaulting",
          derive(pd.DataFrame({
              "symbol": ["X"], "name": ["X"], "sector": ["T"], "price": [1.0],
              "change_pct": [0.0], "volume": [1.0], "avg_volume_3m": [1.0],
              "market_cap": [1.0], "week52_high": [1.0], "week52_low": [1.0],
              "sma50": [1.0], "sma200": [1.0], "earnings_ts": [np.nan],
          }))["earnings_when"].iloc[0] is None)
    check("`when` is screenable", screen.resolve("when") == "earnings_when")

    print("\ndollar volume and day range")
    dv = derive(pd.DataFrame({
        # A $2 stock and a $200 stock on identical share volume: the pair
        # the whole metric exists to tell apart.
        "symbol": ["CHEAP", "DEAR", "FLAT", "NOTRADE"],
        "name": ["C", "D", "F", "N"], "sector": ["T"] * 4,
        "price": [2.0, 200.0, 50.0, 10.0],
        "change_pct": [0.0] * 4,
        "volume": [40e6, 40e6, 1e6, 0.0],
        "avg_volume_3m": [20e6, 10e6, 2e6, 1e6],
        "market_cap": [1e9] * 4,
        # CHEAP is being sold into the low, DEAR is closing on its high,
        # FLAT never moved, NOTRADE sits a third of the way up.
        "day_low": [1.9, 190.0, 50.0, 9.0],
        "day_high": [2.5, 200.0, 50.0, 12.0],
        # CHEAP gapped up 5%, DEAR gapped down 3%, FLAT and NOTRADE opened
        # unchanged from yesterday's close.
        "open": [2.1, 194.0, 50.0, 10.0],
        "prev_close": [2.0, 200.0, 50.0, 10.0],
        "week52_high": [3.0, 250.0, 60.0, 15.0],
        "week52_low": [1.0, 100.0, 40.0, 8.0],
        "sma50": [2.0, 200.0, 50.0, 10.0],
        "sma200": [2.0, 200.0, 50.0, 10.0],
        "earnings_ts": [np.nan] * 4,
    }))
    row = dict(zip(dv["symbol"], dv.to_dict("records"), strict=True))
    check("dollar volume is price x volume",
          row["CHEAP"]["dollar_volume"] == 80e6, row["CHEAP"]["dollar_volume"])
    check("same share volume, 100x the money",
          row["DEAR"]["dollar_volume"] == 8e9, row["DEAR"]["dollar_volume"])
    check("average dollar volume prices the average day",
          row["DEAR"]["avg_dollar_volume"] == 2e9)
    check("dvol resolves", screen.resolve("dvol") == "dollar_volume")
    check("turnover resolves too", screen.resolve("TurnOver") == "dollar_volume")
    check("avgdvol resolves", screen.resolve("avgdvol") == "avg_dollar_volume")
    check("dvol screens on money, not shares",
          syms(dv[screen.mask(dv, "dvol > 1b")]) == ["DEAR"],
          syms(dv[screen.mask(dv, "dvol > 1b")]))
    check("suffix works on dollar amounts",
          "1000000000.0" in screen.normalize("dvol > 1b"))

    check("sold into the low reads near 0",
          abs(row["CHEAP"]["day_range_pct"] - 100 / 6) < 1e-9,
          row["CHEAP"]["day_range_pct"])
    check("closing on the high reads 100",
          abs(row["DEAR"]["day_range_pct"] - 100.0) < 1e-9,
          row["DEAR"]["day_range_pct"])
    check("a zero-width range is null, not 50",
          row["FLAT"]["day_range_pct"] != row["FLAT"]["day_range_pct"],
          row["FLAT"]["day_range_pct"])
    check("dayrange resolves", screen.resolve("dayrange") == "day_range_pct")
    check("dayrange screens", syms(dv[screen.mask(dv, "dayrange > 25")]) ==
          ["DEAR", "NOTRADE"],
          syms(dv[screen.mask(dv, "dayrange > 25")]))
    check("a frame with no high/low derives rather than raising",
          derive(pd.DataFrame({
              "symbol": ["X"], "name": ["X"], "sector": ["T"], "price": [1.0],
              "change_pct": [0.0], "volume": [1.0], "avg_volume_3m": [1.0],
              "market_cap": [1.0], "week52_high": [1.0], "week52_low": [1.0],
              "sma50": [1.0], "sma200": [1.0], "earnings_ts": [np.nan],
          }))["day_range_pct"].isna().all())

    print("\n52-week range position")
    check("sitting at the midpoint of the range reads 50",
          abs(row["CHEAP"]["pct_52w_range"] - 50.0) < 1e-9,
          row["CHEAP"]["pct_52w_range"])
    check("closer to the low end of a wide range reads well under 50",
          abs(row["DEAR"]["pct_52w_range"] - 200 / 3) < 1e-9,
          row["DEAR"]["pct_52w_range"])
    check("distinct from pct_from_52w_high: NOTRADE is 33% off its high "
          "but only 29% up its range, because the range itself is wide",
          abs(row["NOTRADE"]["pct_52w_range"] - 200 / 7) < 1e-9,
          row["NOTRADE"]["pct_52w_range"])
    check("pos52 resolves", screen.resolve("pos52") == "pct_52w_range")
    check("pct_52w_range needs a live price, so it isn't prefiltered on a stale one",
          "pct_52w_range" in screen.LIVE_COLUMNS
          and "pct_52w_range" not in screen.STATIC_SAFE)

    print("\n52-week range width")
    check("DEAR's range is 150% of its low: (250-100)/100",
          abs(row["DEAR"]["range_52w_width"] - 150.0) < 1e-9,
          row["DEAR"]["range_52w_width"])
    check("NOTRADE's narrower range reads smaller even though its position "
          "in the range is similar to CHEAP's",
          row["NOTRADE"]["range_52w_width"] < row["CHEAP"]["range_52w_width"],
          (row["NOTRADE"]["range_52w_width"], row["CHEAP"]["range_52w_width"]))
    check("width52 resolves", screen.resolve("width52") == "range_52w_width")
    check("range_52w_width depends only on the two slow endpoints, so it "
          "narrows safely stale",
          "range_52w_width" in screen.STATIC_SAFE
          and "range_52w_width" not in screen.LIVE_COLUMNS)

    print("\ngap percent")
    check("gapped up 5%", abs(row["CHEAP"]["gap_pct"] - 5.0) < 1e-9,
          row["CHEAP"]["gap_pct"])
    check("gapped down 3%", abs(row["DEAR"]["gap_pct"] - (-3.0)) < 1e-9,
          row["DEAR"]["gap_pct"])
    check("opened flat is a real zero, not null",
          row["FLAT"]["gap_pct"] == 0.0, row["FLAT"]["gap_pct"])
    check("gap resolves", screen.resolve("gap") == "gap_pct")
    check("gap screens", syms(dv[screen.mask(dv, "gap > 3")]) == ["CHEAP"],
          syms(dv[screen.mask(dv, "gap > 3")]))
    check("gap needs a live open, so it isn't prefiltered on a stale one",
          "gap_pct" in screen.LIVE_COLUMNS and "gap_pct" not in screen.STATIC_SAFE)
    check("a frame with no open/prev_close derives rather than raising",
          derive(pd.DataFrame({
              "symbol": ["X"], "name": ["X"], "sector": ["T"], "price": [1.0],
              "change_pct": [0.0], "volume": [1.0], "avg_volume_3m": [1.0],
              "market_cap": [1.0], "week52_high": [1.0], "week52_low": [1.0],
              "sma50": [1.0], "sma200": [1.0], "earnings_ts": [np.nan],
          }))["gap_pct"].isna().all())

    print("\ngap filled")
    check("gap up retraded through the prior close: filled",
          row["CHEAP"]["gap_filled"] is True, row["CHEAP"]["gap_filled"])
    check("gap down retraded back up through the prior close: filled",
          row["DEAR"]["gap_filled"] is True, row["DEAR"]["gap_filled"])
    check("no gap at all reads null, not False",
          row["FLAT"]["gap_filled"] is None, row["FLAT"]["gap_filled"])
    check("gap up that never came back down reads not filled",
          derive(pd.DataFrame({
              "symbol": ["X"], "name": ["X"], "sector": ["T"], "price": [1.0],
              "change_pct": [0.0], "volume": [1.0], "avg_volume_3m": [1.0],
              "market_cap": [1.0], "week52_high": [1.0], "week52_low": [1.0],
              "sma50": [1.0], "sma200": [1.0], "earnings_ts": [np.nan],
              "open": [11.0], "prev_close": [10.0],
              "day_low": [10.5], "day_high": [11.5],
          }))["gap_filled"].iloc[0] is False)
    check("gapfilled resolves", screen.resolve("gapfilled") == "gap_filled")
    check("gap_filled needs today's high/low, so it isn't prefiltered stale",
          "gap_filled" in screen.LIVE_COLUMNS
          and "gap_filled" not in screen.STATIC_SAFE)

    print("\nintraday percent")
    check("gapped up but sold off since the open reads negative",
          abs(row["CHEAP"]["intraday_pct"] - (-100 / 21)) < 1e-9,
          row["CHEAP"]["intraday_pct"])
    check("distinct from chg: CHEAP is flat on the day yet moved intraday",
          row["CHEAP"]["chg"] == 0.0 and row["CHEAP"]["intraday_pct"] != 0.0,
          (row["CHEAP"]["chg"], row["CHEAP"]["intraday_pct"]))
    check("gapped down but ran since the open reads positive",
          abs(row["DEAR"]["intraday_pct"] - 300 / 97) < 1e-9,
          row["DEAR"]["intraday_pct"])
    check("opened at price is a real zero, not null",
          row["FLAT"]["intraday_pct"] == 0.0, row["FLAT"]["intraday_pct"])
    check("intraday resolves", screen.resolve("intraday") == "intraday_pct")
    check("sinceopen resolves too", screen.resolve("sinceopen") == "intraday_pct")
    check("intraday needs a live price and today's open, so it isn't prefiltered stale",
          "intraday_pct" in screen.LIVE_COLUMNS
          and "intraday_pct" not in screen.STATIC_SAFE)
    check("a frame with no open derives rather than raising",
          derive(pd.DataFrame({
              "symbol": ["X"], "name": ["X"], "sector": ["T"], "price": [1.0],
              "change_pct": [0.0], "volume": [1.0], "avg_volume_3m": [1.0],
              "market_cap": [1.0], "week52_high": [1.0], "week52_low": [1.0],
              "sma50": [1.0], "sma200": [1.0], "earnings_ts": [np.nan],
          }))["intraday_pct"].isna().all())

    print("\ntrue range")
    tr = derive(pd.DataFrame({
        "symbol": ["NORMAL", "GAPPED"], "name": ["N", "G"], "sector": ["T"] * 2,
        "price": [82.0, 82.0], "change_pct": [0.0] * 2,
        "volume": [1e6] * 2, "avg_volume_3m": [1e6] * 2, "market_cap": [1e9] * 2,
        # NORMAL trades a plain 4-point bar. GAPPED opens far below
        # yesterday's close and then sits still — a bar that looks tiny
        # (high-low = 4) but only because most of the move already
        # happened overnight.
        "day_high": [84.0, 82.0], "day_low": [80.0, 78.0],
        "prev_close": [83.0, 100.0], "open": [83.0, 80.0],
        "week52_high": [90.0, 120.0], "week52_low": [70.0, 70.0],
        "sma50": [82.0, 82.0], "sma200": [82.0, 82.0],
        "earnings_ts": [np.nan] * 2,
    }))
    trrow = dict(zip(tr["symbol"], tr.to_dict("records"), strict=True))
    check("ordinary day: true range is just the high/low bar",
          trrow["NORMAL"]["true_range"] == 4.0, trrow["NORMAL"]["true_range"])
    check("gap day: true range captures the overnight gap the bar misses",
          trrow["GAPPED"]["true_range"] == 22.0, trrow["GAPPED"]["true_range"])
    check("tr resolves", screen.resolve("tr") == "true_range")
    check("true range needs today's high/low, so it isn't prefiltered on a stale one",
          "true_range" in screen.LIVE_COLUMNS
          and "true_range" not in screen.STATIC_SAFE)
    check("a frame with no high/low derives rather than raising",
          derive(pd.DataFrame({
              "symbol": ["X"], "name": ["X"], "sector": ["T"], "price": [1.0],
              "change_pct": [0.0], "volume": [1.0], "avg_volume_3m": [1.0],
              "market_cap": [1.0], "week52_high": [1.0], "week52_low": [1.0],
              "sma50": [1.0], "sma200": [1.0], "earnings_ts": [np.nan],
          }))["true_range"].isna().all())

    print("\natr percent")
    check("ordinary day as a percent of price",
          abs(trrow["NORMAL"]["atr_pct"] - 4 / 82 * 100) < 1e-9,
          trrow["NORMAL"]["atr_pct"])
    check("gap day reads far larger once scaled by price",
          abs(trrow["GAPPED"]["atr_pct"] - 22 / 82 * 100) < 1e-9,
          trrow["GAPPED"]["atr_pct"])
    check("atr resolves", screen.resolve("atr") == "atr_pct")
    check("atr_pct needs today's range and price, so it isn't prefiltered "
          "on a stale one",
          "atr_pct" in screen.LIVE_COLUMNS
          and "atr_pct" not in screen.STATIC_SAFE)

    print("\nsma spread")
    ss = derive(pd.DataFrame({
        "symbol": ["CROSSED", "BELOW"], "name": ["C", "B"], "sector": ["T"] * 2,
        "price": [100.0, 100.0], "change_pct": [0.0] * 2,
        "volume": [1e6] * 2, "avg_volume_3m": [1e6] * 2, "market_cap": [1e9] * 2,
        "week52_high": [110.0] * 2, "week52_low": [90.0] * 2,
        "sma50": [110.0, 95.0], "sma200": [100.0, 100.0],
        "earnings_ts": [np.nan] * 2,
    }))
    ssrow = dict(zip(ss["symbol"], ss.to_dict("records"), strict=True))
    check("above the 200-day: spread is positive and sized",
          abs(ssrow["CROSSED"]["sma_spread"] - 10.0) < 1e-9,
          ssrow["CROSSED"]["sma_spread"])
    check("below the 200-day: spread is negative",
          abs(ssrow["BELOW"]["sma_spread"] - (-5.0)) < 1e-9,
          ssrow["BELOW"]["sma_spread"])
    check("smaspread resolves", screen.resolve("smaspread") == "sma_spread")
    check("sma_spread depends only on two slow-moving averages, so it "
          "narrows safely stale",
          "sma_spread" in screen.STATIC_SAFE
          and "sma_spread" not in screen.LIVE_COLUMNS)

    print("\nearnings yield")
    ey = derive(pd.DataFrame({
        "symbol": ["EARNER", "LOSER"], "name": ["E", "L"], "sector": ["T"] * 2,
        "price": [50.0, 50.0], "change_pct": [0.0] * 2,
        "volume": [1e6] * 2, "avg_volume_3m": [1e6] * 2, "market_cap": [1e9] * 2,
        "week52_high": [60.0] * 2, "week52_low": [40.0] * 2,
        "sma50": [50.0] * 2, "sma200": [50.0] * 2,
        "eps_ttm": [2.5, -1.0], "pe": [20.0, np.nan],
        "earnings_ts": [np.nan] * 2,
    }))
    eyrow = dict(zip(ey["symbol"], ey.to_dict("records"), strict=True))
    check("profitable name: yield is eps over price, as a percent",
          abs(eyrow["EARNER"]["earnings_yield"] - 5.0) < 1e-9,
          eyrow["EARNER"]["earnings_yield"])
    check("loss-making name: yield stays negative where P/E goes undefined",
          abs(eyrow["LOSER"]["earnings_yield"] - (-2.0)) < 1e-9,
          eyrow["LOSER"]["earnings_yield"])
    check("eyield resolves", screen.resolve("eyield") == "earnings_yield")
    check("earnings yield tracks eps, not price, so it narrows safely stale",
          "earnings_yield" in screen.STATIC_SAFE
          and "earnings_yield" not in screen.LIVE_COLUMNS)

    print("\neps growth")
    eg = derive(pd.DataFrame({
        "symbol": ["GROWER", "RECOVERING"], "name": ["G", "R"], "sector": ["T"] * 2,
        "price": [50.0, 50.0], "change_pct": [0.0] * 2,
        "volume": [1e6] * 2, "avg_volume_3m": [1e6] * 2, "market_cap": [1e9] * 2,
        "week52_high": [60.0] * 2, "week52_low": [40.0] * 2,
        "sma50": [50.0] * 2, "sma200": [50.0] * 2,
        "eps_ttm": [2.0, -1.0], "eps_forward": [2.5, -0.5],
        "earnings_ts": [np.nan] * 2,
    }))
    egrow = dict(zip(eg["symbol"], eg.to_dict("records"), strict=True))
    check("profitable name: growth is forward over trailing, as a percent",
          abs(egrow["GROWER"]["eps_growth"] - 25.0) < 1e-9,
          egrow["GROWER"]["eps_growth"])
    check("loss narrowing toward zero still reads as growth, not decline",
          abs(egrow["RECOVERING"]["eps_growth"] - 50.0) < 1e-9,
          egrow["RECOVERING"]["eps_growth"])
    check("epsgrowth resolves", screen.resolve("epsgrowth") == "eps_growth")
    check("eps growth tracks two estimates, not price, so it narrows safely stale",
          "eps_growth" in screen.STATIC_SAFE
          and "eps_growth" not in screen.LIVE_COLUMNS)

    print("\npeg")
    pg = derive(pd.DataFrame({
        "symbol": ["GROWER", "DECLINER"], "name": ["G", "D"], "sector": ["T"] * 2,
        "price": [50.0, 50.0], "change_pct": [0.0] * 2,
        "volume": [1e6] * 2, "avg_volume_3m": [1e6] * 2, "market_cap": [1e9] * 2,
        "week52_high": [60.0] * 2, "week52_low": [40.0] * 2,
        "sma50": [50.0] * 2, "sma200": [50.0] * 2,
        "pe": [20.0, 15.0], "eps_ttm": [2.0, 2.0], "eps_forward": [2.5, 1.5],
        "earnings_ts": [np.nan] * 2,
    }))
    pgrow = dict(zip(pg["symbol"], pg.to_dict("records"), strict=True))
    check("growing name: peg is P/E over the growth rate eps_growth supplies",
          abs(pgrow["GROWER"]["peg"] - 0.8) < 1e-9, pgrow["GROWER"]["peg"])
    check("shrinking estimates: peg stays null rather than flip sign into "
          "a fake bargain",
          np.isnan(pgrow["DECLINER"]["peg"]), pgrow["DECLINER"]["peg"])
    check("peg tracks the same slow-moving inputs as pe and eps_growth, so "
          "it narrows safely stale",
          "peg" in screen.STATIC_SAFE and "peg" not in screen.LIVE_COLUMNS)

    print("\npe spread")
    ps = derive(pd.DataFrame({
        "symbol": ["CHEAP", "LOSER"], "name": ["C", "L"], "sector": ["T"] * 2,
        "price": [50.0] * 2, "change_pct": [0.0] * 2,
        "volume": [1e6] * 2, "avg_volume_3m": [1e6] * 2, "market_cap": [1e9] * 2,
        "week52_high": [60.0] * 2, "week52_low": [40.0] * 2,
        "sma50": [50.0] * 2, "sma200": [50.0] * 2,
        "pe": [10.0, -5.0], "earnings_ts": [np.nan] * 2,
    }))
    psrow = dict(zip(ps["symbol"], ps.to_dict("records"), strict=True))
    # Sole positive P/E in its sector, so it *is* the average: spread is 0.
    check("the only profitable name in its sector is its own average",
          abs(psrow["CHEAP"]["pe_spread"]) < 1e-9, psrow["CHEAP"]["pe_spread"])
    check("a loss-making name has no multiple to compare, so it stays null",
          np.isnan(psrow["LOSER"]["pe_spread"]), psrow["LOSER"]["pe_spread"])
    check("pespread resolves", screen.resolve("pespread") == "pe_spread")
    check("pe spread tracks pe, itself static-safe, so it narrows safely stale",
          "pe_spread" in screen.STATIC_SAFE
          and "pe_spread" not in screen.LIVE_COLUMNS)

    print("\npayout ratio")
    pr = derive(pd.DataFrame({
        "symbol": ["COVERED", "LOSER"], "name": ["C", "L"], "sector": ["T"] * 2,
        "price": [50.0, 50.0], "change_pct": [0.0] * 2,
        "volume": [1e6] * 2, "avg_volume_3m": [1e6] * 2, "market_cap": [1e9] * 2,
        "week52_high": [60.0] * 2, "week52_low": [40.0] * 2,
        "sma50": [50.0] * 2, "sma200": [50.0] * 2,
        "eps_ttm": [4.0, -1.0], "dividend_rate": [1.0, 1.0],
        "earnings_ts": [np.nan] * 2,
    }))
    prrow = dict(zip(pr["symbol"], pr.to_dict("records"), strict=True))
    check("profitable name: payout is dividend over trailing eps, as a percent",
          abs(prrow["COVERED"]["payout_ratio"] - 25.0) < 1e-9,
          prrow["COVERED"]["payout_ratio"])
    check("loss-making name: payout stays null rather than a fake positive number",
          np.isnan(prrow["LOSER"]["payout_ratio"]), prrow["LOSER"]["payout_ratio"])
    check("payout resolves", screen.resolve("payout") == "payout_ratio")
    check("payout ratio tracks dividend and trailing eps, not price, so it "
          "narrows safely stale",
          "payout_ratio" in screen.STATIC_SAFE
          and "payout_ratio" not in screen.LIVE_COLUMNS)

    print("\nturnover")
    tv = derive(pd.DataFrame({
        "symbol": ["ACTIVE", "NOFLOAT"], "name": ["A", "N"], "sector": ["T"] * 2,
        "price": [50.0, 50.0], "change_pct": [0.0] * 2,
        "volume": [2e6, 1e6], "avg_volume_3m": [1e6] * 2, "market_cap": [1e9] * 2,
        "week52_high": [60.0] * 2, "week52_low": [40.0] * 2,
        "sma50": [50.0] * 2, "sma200": [50.0] * 2,
        "shares_outstanding": [40e6, 0.0],
        "earnings_ts": [np.nan] * 2,
    }))
    tvrow = dict(zip(tv["symbol"], tv.to_dict("records"), strict=True))
    check("turnover is today's volume over shares outstanding, as a percent",
          abs(tvrow["ACTIVE"]["turnover_pct"] - 5.0) < 1e-9,
          tvrow["ACTIVE"]["turnover_pct"])
    check("zero shares outstanding stays null rather than a division blow-up",
          np.isnan(tvrow["NOFLOAT"]["turnover_pct"]), tvrow["NOFLOAT"]["turnover_pct"])
    check("turnoverpct resolves", screen.resolve("turnoverpct") == "turnover_pct")
    check("turnover tracks today's volume, which only rises, so it needs "
          "live re-pricing before it narrows a screen",
          "turnover_pct" in screen.LIVE_COLUMNS
          and "turnover_pct" not in screen.STATIC_SAFE)

    print("\nvolume trend")
    vt = derive(pd.DataFrame({
        "symbol": ["HOT", "QUIET"], "name": ["H", "Q"], "sector": ["T"] * 2,
        "price": [50.0, 50.0], "change_pct": [0.0] * 2,
        "volume": [1e6] * 2, "market_cap": [1e9] * 2,
        "week52_high": [60.0] * 2, "week52_low": [40.0] * 2,
        "sma50": [50.0] * 2, "sma200": [50.0] * 2,
        "avg_volume_10d": [3e6, 0.5e6], "avg_volume_3m": [2e6, 2e6],
        "earnings_ts": [np.nan] * 2,
    }))
    vtrow = dict(zip(vt["symbol"], vt.to_dict("records"), strict=True))
    check("recent pace running above the quarterly baseline",
          abs(vtrow["HOT"]["volume_trend"] - 150.0) < 1e-9,
          vtrow["HOT"]["volume_trend"])
    check("recent pace running below the quarterly baseline",
          abs(vtrow["QUIET"]["volume_trend"] - 25.0) < 1e-9,
          vtrow["QUIET"]["volume_trend"])
    check("voltrend resolves", screen.resolve("voltrend") == "volume_trend")
    check("volume trend tracks two rolling averages, not today's tape, so "
          "it narrows safely stale",
          "volume_trend" in screen.STATIC_SAFE
          and "volume_trend" not in screen.LIVE_COLUMNS)

    # Today's dollar volume only rises, so prefiltering on a stale one would
    # drop the names that have since crossed the line. The average is a
    # standing fact about the name and narrows safely.
    static, live = screen.split_live("avgdvol > 20m and dvol > 100m")
    check("average dollar volume narrows before re-pricing",
          static == "avgdvol > 20000000.0", static)
    check("today's dollar volume waits for live quotes",
          live == "dvol > 100000000.0", live)
    check("day range position is live",
          "day_range_pct" in screen.LIVE_COLUMNS)
    check("today's dollar volume is never prefiltered",
          "dollar_volume" not in screen.STATIC_SAFE)

    dvol_filter = screen.filters_to_expr({"dvol": "Over $10M"})
    check("the dropdown compiles", dvol_filter == "avgdvol > 10m", dvol_filter)
    check("and what it compiles to is screenable",
          syms(dv[screen.mask(dv, dvol_filter)]) == ["CHEAP", "DEAR", "FLAT"],
          syms(dv[screen.mask(dv, dvol_filter)]))
    # Every preset has to parse, or it lands in the saved list as a screen
    # that errors the moment anyone clicks it.
    bad = []
    for name, expr in screen.PRESETS:
        try:
            screen.mask(dv, expr)
        except ScreenError as exc:
            bad.append(f"{name}: {exc}")
    check("every preset still parses", not bad, bad)

    print("\ncountry grouping")
    cs = screen.country_summary(adr_df)
    check("every row with a country is placed",
          cs["placed"] == 6, cs["placed"])
    check("rows with no country are counted, not bucketed",
          cs["unplaced"] == 1, cs["unplaced"])
    check("placed plus unplaced is the whole frame",
          cs["placed"] + cs["unplaced"] == cs["total"])
    check("no liquidity floor on the map — small markets stay whole",
          len(cs["countries"]) == 6, len(cs["countries"]))
    by_name = {c["name"]: c for c in cs["countries"]}
    check("group carries a count", by_name["China"]["count"] == 1)
    check("group carries market cap",
          abs(by_name["China"]["market_cap"] - 275e9) < 1)
    check("group carries breadth",
          by_name["China"]["advancing"] == 1
          and by_name["China"]["declining"] == 0)
    check("countries are ranked by change",
          [c["name"] for c in cs["countries"]][0] == "Netherlands",
          [c["name"] for c in cs["countries"]])

    print("\ncap-weighted averages")
    weighted = derive(pd.DataFrame({
        "symbol": ["BIG", "TINY"], "sector": ["Technology"] * 2,
        "country": ["United States"] * 2, "name": ["Big Co", "Tiny Co"],
        "price": [100.0, 1.0], "change_pct": [1.0, -50.0],
        "volume": [1e7, 1e7], "market_cap": [1e12, 1e6],
        "avg_volume_3m": [1e7] * 2, "week52_high": [200.0] * 2,
        "week52_low": [1.0] * 2, "sma50": [90.0] * 2, "sma200": [80.0] * 2,
        "earnings_ts": [np.nan] * 2,
    }))
    grp = screen.group_summary(weighted, "sector")[0]
    check("a microcap collapse does not drag the sector down",
          abs(grp["change_pct"] - 1.0) < 0.01, grp["change_pct"])
    check("a plain mean would have said otherwise",
          abs((1.0 + -50.0) / 2 - grp["change_pct"]) > 20)

    print("\nserialization")
    recs = screen.to_records(df, ["symbol", "pe", "market_cap"])
    check("NaN becomes null", recs[3]["pe"] is None, recs[3])
    check("floats survive", abs(recs[0]["market_cap"] - 4.51e12) < 1)
    import json
    check("output is JSON-serializable",
          json.loads(json.dumps(recs))[0]["symbol"] == "NVDA")

    summary = screen.market_summary(df)
    check("summary counts advancers", summary["advancing"] == 4,
          summary["advancing"])
    check("summary counts decliners", summary["declining"] == 2)
    check("summary ranks sectors",
          [s["sector"] for s in summary["sectors"]][0] == "Energy",
          summary["sectors"])

    print(f"\n{PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
