#!/usr/bin/env python3
"""Watchlist P&L, hand-computed. python tests/test_watchlist.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import watchlist  # noqa: E402

quotes = {"AAA": {"price": 50.0}, "BBB": {"price": 20.0}}
no_basis = watchlist.enrich(
    [{"symbol": "AAA", "shares": 10.0, "cost_basis": None}], quotes)[0]
assert no_basis["market_value"] == 500.0, "needs shares and price only"
assert no_basis["pnl"] is None, "no basis means no P&L, not a $0.00"
with_basis = watchlist.enrich(
    [{"symbol": "BBB", "shares": 4.0, "cost_basis": 15.0}], quotes)[0]
assert with_basis["market_value"] == 80.0
assert with_basis["pnl"] == (20.0 - 15.0) * 4.0

# A cost basis of exactly $0 (gifted or granted shares) is a real value,
# not a missing one — the dollar P&L is the full market value, even
# though the percent gain has no basis to be computed against.
zero_basis = watchlist.enrich(
    [{"symbol": "AAA", "shares": 10.0, "cost_basis": 0.0}], quotes)[0]
assert zero_basis["pnl"] == 500.0, "a $0 basis is known, not missing"
assert zero_basis["pnl_pct"] is None, "percent gain from $0 is undefined"
print("5 passed, 0 failed")
