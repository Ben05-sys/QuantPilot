#!/usr/bin/env python3
"""Corporate calendar tests. No network — Nasdaq's payloads, pasted.

What is tested here is everything this project decided about the two
calendars: that an exchange date stays an exchange date from a machine
nine and a half hours ahead of it, that a date nobody has set yet stays
null instead of becoming an epoch, that the dividend columns exist on the
frame whether or not the calendar has ever loaded, and that the screener
can narrow on a declared ex-date without re-pricing but never on the drop,
which is a fraction of today's price.

    python tests/test_calendars.py
"""

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app import calendars, screen  # noqa: E402
from app.providers import nasdaq  # noqa: E402

PASS = FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


# One row of each, exactly as the endpoints returned them on 2026-08-11.
DIVIDEND_ROW = {
    "companyName": "Arrow Financial Corporation Common Stock",
    "symbol": "AROW",
    "dividend_Ex_Date": "8/11/2026",
    "payment_Date": "8/25/2026",
    "record_Date": "8/11/2026",
    "dividend_Rate": 0.3,
    "indicated_Annual_Dividend": 1.2,
    "announcement_Date": "7/22/2026",
}

IPO_UPCOMING_ROW = {
    "dealID": "1395669-118793",
    "proposedTickerSymbol": "LYNX",
    "companyName": "Lyntris Inc.",
    "proposedExchange": "NYSE",
    "proposedSharePrice": "19.00-22.00",
    "sharesOffered": "24,000,000",
    "expectedPriceDate": "8/19/2026",
    "dollarValueOfSharesOffered": "$607,200,000",
}


def frame():
    """Four rows: two with a dividend scheduled, one payer with nothing in
    the window, one that has never paid.

    Carries the columns `derive` needs rather than only the ones this
    suite asserts on — it runs the real `derive`, because the point is
    that these fields survive the same pipeline every other column does.
    """
    # `payout_ratio` is derived from dividend_rate over trailing EPS, not
    # stored, so it is set through its inputs — writing the ratio directly
    # would be silently overwritten by derive() and the assertions below
    # would be testing a column the real pipeline never produces.
    df = pd.DataFrame([
        ("KO", "Coca-Cola", 71.0, 3.1e11, 1.48),
        ("PEP", "PepsiCo", 142.0, 1.9e11, 1.64),
        ("JNJ", "Johnson & Johnson", 162.0, 3.9e11, 1.10),
        ("NVDA", "NVIDIA", 184.0, 4.5e12, 0.04),
    ], columns=["symbol", "name", "price", "market_cap", "dividend_rate"])
    df["dividend_yield"] = [2.9, 4.0, 3.0, 0.02]
    df["sector"] = ["Consumer Staples", "Consumer Staples", "Health Care",
                    "Technology"]
    for col, val in [("change_pct", 0.0), ("change", 0.0),
                     ("volume", 1e7), ("avg_volume_3m", 1.2e7),
                     ("week52_high", 200.0), ("week52_low", 60.0),
                     ("sma50", 150.0), ("sma200", 140.0),
                     ("earnings_ts", np.nan), ("ipo_year", np.nan),
                     ("eps_ttm", 2.0)]:
        df[col] = val
    return df


def main():
    print("\nexchange dates stay exchange dates")
    ts = nasdaq._date("8/11/2026")
    # Parsed as New York midnight. Read back in New York it is the 11th at
    # 00:00 — the assertion that would fail if it were parsed as local
    # midnight from India, where it would land on the 10th at 14:30 ET.
    back = datetime.fromtimestamp(ts, nasdaq.EASTERN)
    check("M/D/YYYY parses to exchange midnight",
          (back.year, back.month, back.day, back.hour) == (2026, 8, 11, 0),
          back.isoformat())
    check("ISO form parses too",
          nasdaq._date("2026-08-11") == ts, nasdaq._date("2026-08-11"))
    for blank in ("", None, "N/A", "--", "TBD", "not a date"):
        check(f"{blank!r} is null, not an epoch",
              nasdaq._date(blank) is None, nasdaq._date(blank))

    print("\na price that may be a range")
    check("a filed range gives both ends",
          nasdaq._range("19.00-22.00") == (19.0, 22.0))
    check("a priced deal gives the same number twice",
          nasdaq._range("16.00") == (16.0, 16.0))
    check("a dollar sign is not part of the number",
          nasdaq._range("$10.00") == (10.0, 10.0))
    check("nothing offered is (None, None)",
          nasdaq._range("") == (None, None))
    check("a negative low is not read as a range",
          nasdaq._range("-5.00") == (-5.0, -5.0), nasdaq._range("-5.00"))

    print("\nthe dividend row Nasdaq actually sends")
    rows = nasdaq._parse_dividend_rows([DIVIDEND_ROW])
    row = rows[0]
    check("symbol survives", row["symbol"] == "AROW")
    check("all four dates are present",
          all(row[k] is not None for k in
              ("declared_ts", "ex_ts", "record_ts", "pay_ts")), row)
    check("declared comes before ex", row["declared_ts"] < row["ex_ts"])
    check("ex comes before payment", row["ex_ts"] < row["pay_ts"])
    check("this payment is not the annual figure",
          row["amount"] == 0.3 and row["annual_amount"] == 1.2)
    # Seen on the live board: a special or irregular dividend comes back
    # with an indicated annual of 0, and rendering that as "yield 0.00%"
    # beside a real five-cent payment says the stock pays nothing.
    special = nasdaq._parse_dividend_rows([
        dict(DIVIDEND_ROW, symbol="CLBK", dividend_Rate=0.05,
             indicated_Annual_Dividend=0)])[0]
    check("a special dividend keeps its amount",
          special["amount"] == 0.05)
    check("but an indicated annual of zero is null, not a yield of zero",
          special["annual_amount"] is None, special["annual_amount"])

    print("\nthe IPO row Nasdaq actually sends")
    parsed = nasdaq._parse_ipo_rows([IPO_UPCOMING_ROW], "upcoming")[0]
    check("symbol and venue survive",
          parsed["symbol"] == "LYNX" and parsed["exchange"] == "NYSE")
    check("the filed range is kept as a range",
          (parsed["price_low"], parsed["price_high"]) == (19.0, 22.0))
    check("shares parse through the thousands separators",
          parsed["shares"] == 24_000_000.0, parsed["shares"])
    check("the offer amount parses through the dollar sign",
          parsed["value"] == 607_200_000.0, parsed["value"])
    check("the expected date is the row's date",
          parsed["date_ts"] == nasdaq._date("8/19/2026"))

    print("\nthe columns exist whether or not the calendar loaded")
    # The failure this prevents: a column that is only sometimes present
    # turns every screen mentioning it into "unknown field" instead of
    # "nothing matched" — an error where the honest answer is an empty
    # grid. So `apply` guarantees the shape even with nothing fetched.
    calendars.dividends._data, calendars.dividends._at = None, 0.0
    empty = calendars.apply(frame())
    for col in calendars.DIVIDEND_COLUMNS:
        check(f"{col} exists with no calendar", col in empty.columns)
    check("and every value is null, not zero",
          empty["ex_div_ts"].isna().all())
    check("every calendar column is a known field to the engine",
          all(c in screen.COLUMNS for c in calendars.DIVIDEND_COLUMNS))

    print("\njoined onto the frame")
    now = time.time()
    calendars.dividends._data = {
        "KO": {"symbol": "KO", "ex_ts": now + 3 * 86400,
               "record_ts": now + 3 * 86400, "pay_ts": now + 20 * 86400,
               "declared_ts": now - 10 * 86400, "amount": 0.51,
               "annual_amount": 2.04},
        "PEP": {"symbol": "PEP", "ex_ts": now - 0.5 * 86400,
                "record_ts": now - 0.5 * 86400, "pay_ts": now + 14 * 86400,
                "declared_ts": now - 20 * 86400, "amount": 1.42,
                "annual_amount": 5.68},
    }
    calendars.dividends._at = now

    from app.universe import derive
    df = derive(calendars.apply(frame()))
    by = dict(zip(df["symbol"], df.to_dict("records"), strict=True))

    check("a scheduled name gets its dates",
          abs(by["KO"]["ex_div_days"] - 3) < 0.01, by["KO"]["ex_div_days"])
    check("one that went ex yesterday is negative, not zero — buying the "
          "morning after is a different trade",
          by["PEP"]["ex_div_days"] < 0, by["PEP"]["ex_div_days"])
    check("a payer with nothing in the window stays null, which is not "
          "the same fact as paying nothing",
          np.isnan(by["JNJ"]["ex_div_days"]))
    check("and so does a name that has never paid",
          np.isnan(by["NVDA"]["ex_div_days"]))

    check("the drop is this payment against today's price",
          abs(by["KO"]["div_drop_pct"] - (0.51 / 71.0 * 100)) < 1e-9,
          by["KO"]["div_drop_pct"])
    check("the forward yield is the annual figure, not four times the drop",
          abs(by["KO"]["div_yield_upcoming"] - (2.04 / 71.0 * 100)) < 1e-9)
    check("record and pay days are counted too",
          abs(by["KO"]["div_pay_days"] - 20) < 0.01)

    print("\nwhat may be narrowed before re-pricing")
    check("a declared ex-date does not move, so it narrows safely stale",
          "ex_div_days" in screen.STATIC_SAFE
          and "ex_div_days" not in screen.LIVE_COLUMNS)
    check("nor do the four dates themselves",
          all(c in screen.STATIC_SAFE for c in
              ("ex_div_ts", "div_record_ts", "div_pay_ts", "div_declared_ts")))
    check("nor the cash amount, which is fixed when it is declared",
          "div_amount" in screen.STATIC_SAFE)
    check("but the drop is a fraction of today's price, so it is live",
          "div_drop_pct" in screen.LIVE_COLUMNS
          and "div_drop_pct" not in screen.STATIC_SAFE)
    check("and so is the forward yield, for the same reason",
          "div_yield_upcoming" in screen.LIVE_COLUMNS)
    # The split hands back the halves as written, aliases intact — it is
    # deciding which conjuncts run when, not rewriting the expression.
    static, live = screen.split_live("exdiv < 7 and divdrop > 1")
    check("so a capture screen narrows on the date and re-prices the drop",
          static == "exdiv < 7" and live == "divdrop > 1", (static, live))
    check("and the halves name the columns the split was reasoning about",
          screen.fields_used("exdiv < 7 and divdrop > 1")
          == {"ex_div_days", "div_drop_pct"})

    print("\nthe vocabulary")
    for alias, column in [("exdiv", "ex_div_days"), ("daystoex", "ex_div_days"),
                          ("exdate", "ex_div_ts"), ("paydate", "div_pay_ts"),
                          ("recorddate", "div_record_ts"),
                          ("divamt", "div_amount"),
                          ("divdrop", "div_drop_pct"),
                          ("fwdyield", "div_yield_upcoming"),
                          ("ipoage", "ipo_age_years")]:
        check(f"{alias} -> {column}", screen.resolve(alias) == column,
              screen.resolve(alias))
    check("div still means the trailing annual yield — two numbers that "
          "differ by a factor of four keep two names",
          screen.resolve("div") == "dividend_yield")

    print("\nscreening on it")
    check("goes ex within a week",
          list(df[screen.mask(df, "0 < exdiv < 7")]["symbol"]) == ["KO"])
    check("went ex in the last few days",
          list(df[screen.mask(df, "-2 < exdiv < 0")]["symbol"]) == ["PEP"])
    check("a covered payer about to go ex",
          list(df[screen.mask(df, "0 < exdiv < 7 and payout < 80")]["symbol"])
          == ["KO"])
    check("a name with no dividend scheduled never matches a date screen",
          "NVDA" not in list(df[screen.mask(df, "exdiv < 100")]["symbol"]))
    check("the dropdown compiles",
          screen.filters_to_expr({"exdiv": "Next 7 days"}) == "0 < exdiv < 7",
          screen.filters_to_expr({"exdiv": "Next 7 days"}))
    check("the avoidance filter keeps names with no dividend at all — "
          "'nothing scheduled' passes 'nothing within 7 days'",
          "NVDA" in list(df[screen.mask(
              df, screen.filters_to_expr({"exdiv": "Avoid — none within 7d"})
          )]["symbol"]))

    print("\nthe board the view reads")
    board = calendars.upcoming_dividends(df, days=7)
    check("only what is inside the window", [r["symbol"] for r in board]
          == ["PEP", "KO"], [r["symbol"] for r in board])
    check("nearest first", board[0]["ex_div_days"] < board[1]["ex_div_days"])
    check("each row carries what the terminal knows, not just four dates",
          all(k in board[0] for k in
              ("price", "market_cap", "payout_ratio", "div_drop_pct")))
    check("NaN is rendered as null, never as a number",
          all(v is None or not (isinstance(v, float) and np.isnan(v))
              for v in board[0].values()))

    print("\nstaleness is reported, not hidden")
    calendars.dividends._at = time.time() - calendars.DIVIDEND_TTL - 1
    check("an old calendar says so", calendars.dividends.stale())
    calendars.dividends._at = time.time()
    check("a fresh one does not", not calendars.dividends.stale())
    calendars.dividends._data, calendars.dividends._at = None, 0.0
    check("never fetched counts as stale", calendars.dividends.stale())
    check("and has no age to report, rather than an age of zero",
          calendars.dividends.age is None)

    print(f"\n{PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
