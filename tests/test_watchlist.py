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
print("4 passed, 0 failed")
