#!/usr/bin/env python3
"""Market movers, against a synthetic frame. python tests/test_news.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from app import news  # noqa: E402

FAIL = 0


def check(label, condition):
    global FAIL
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    FAIL += 0 if condition else 1


top = news.movers(pd.DataFrame({
    "symbol": ["AAA", "BBB", "CCC"],
    "name": ["Alpha Corp", "Beta Inc", "Gamma LLC"],
    "market_cap": [5e10, 5e10, 1e8],  # CCC too small to qualify
    "volume": [1e6, 1e6, 1e6],
    "change_pct": [8.0, -3.0, 20.0],
}))
check("only liquid names qualify", len(top) == 2)
check("biggest absolute move leads", top[0]["symbol"] == "AAA")
# Regression: used to read `r.name_`, a column itertuples never makes.
check("the name rides along, not null", top[0]["name"] == "Alpha Corp")
check("second mover's name is populated too", top[1]["name"] == "Beta Inc")
print(f"\n{4 - FAIL} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
