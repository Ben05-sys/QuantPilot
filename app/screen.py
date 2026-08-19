"""The screening engine — text expression -> boolean mask over the universe.

`store.screens` has always stored a text `expr`; this is the thing that
evaluates it. Two callers, one mechanism: the command line passes an
expression the user typed, and the Finviz-style dropdown bar compiles its
state down to the same string via `filters_to_expr()`. So anything the
dropdowns can express is saveable, and anything saved can be typed.

Expressions are parsed with `ast` and walked under an allowlist. `eval()`
is never called on user text and neither is `DataFrame.query`, which
executes attribute access and would happily run `@__import__`. A rejected
expression names the token that killed it:

    mcap > 10b and chg > 3 and relvol > 2
    sector == "Technology" and pe < 25 and from52high > -5
    2 < pe < 30 and price / sma50 > 1.05
"""

from __future__ import annotations

import ast
import re
import time

import numpy as np
import pandas as pd

from . import store

# ---- vocabulary ----

# Real columns, from the snapshot table plus what universe.derive() adds.
DERIVED = ["rel_volume", "rel_volume_raw", "rel_volume_10d", "session_fraction",
           "pct_from_52w_high", "pct_from_52w_low", "pct_52w_range",
           "range_52w_width",
           "pct_from_sma50", "pct_from_sma200", "earnings_days",
           "earnings_when", "peg", "mktcap", "chg", "is_adr",
           "rs_sector", "sector_change_pct",
           "dollar_volume", "avg_dollar_volume", "day_range_pct", "gap_pct",
           "gap_filled", "gap_held", "true_range", "atr_pct", "sma_spread",
           "intraday_pct", "earnings_yield", "eps_growth", "payout_ratio",
           "turnover_pct", "volume_trend", "pe_spread", "rs_market",
           "ext_confirms"]

# Joined onto the frame from the corporate calendar (app/calendars.py)
# rather than read from the snapshot, plus what derive() computes from
# them. Listed separately from DERIVED because they are a different kind
# of fact with a different failure mode: these are null until the
# calendar's first background refresh lands, and null again for any
# company with nothing scheduled inside its window. Neither is "pays no
# dividend", and no default may be substituted for either.
CALENDAR = ["ex_div_ts", "div_record_ts", "div_pay_ts", "div_declared_ts",
            "div_amount", "div_annual_amount",
            "ex_div_days", "div_record_days", "div_pay_days",
            "div_drop_pct", "div_yield_upcoming", "ipo_age_years"]

COLUMNS = [n for n, _ in store.UNIVERSE_COLUMNS] + DERIVED + CALENDAR

TEXT_COLUMNS = {"symbol", "name", "sector", "industry", "country",
                "exchange", "quote_type", "currency", "market_state",
                "ext_label"}

# Fields that move tick by tick. Filtering these against a stale snapshot
# answers a question about the past, so they get re-priced from live quotes
# before the predicate runs.
LIVE_COLUMNS = {
    "price", "change", "change_pct", "volume", "rel_volume", "market_cap",
    "day_high", "day_low", "pre_price", "pre_change_pct", "post_price",
    "post_change_pct", "ext_price", "ext_change_pct", "ext_label",
    "market_state", "chg", "mktcap", "rs_sector", "sector_change_pct",
    "rs_market",
    # Derived from price, so they go stale with it.
    "pct_from_52w_high", "pct_from_52w_low", "pct_52w_range",
    "pct_from_sma50", "pct_from_sma200",
    # Dollar volume moves with both of its factors; the day-range position
    # moves with all three of its.
    "dollar_volume", "avg_dollar_volume", "day_range_pct",
    # Built from today's open, which a stale snapshot may not have yet.
    "gap_pct", "gap_filled", "gap_held", "intraday_pct",
    # Tracks whichever extended print is live right now.
    "ext_confirms",
    # Built from today's volume, which only rises as the session runs.
    "rel_volume_10d", "turnover_pct",
    # Built from today's high/low, which only widen as the session runs.
    "true_range", "atr_pct",
    # The dividend against today's price. The payment is fixed weeks in
    # advance and the price is not, so these two move for exactly the same
    # reason `pct_from_sma50` does — and the whole point of asking is the
    # size of the drop relative to where the stock is *now*.
    "div_drop_pct", "div_yield_upcoming",
}

# Fields safe to narrow with *before* re-pricing. The distinction is not
# "does this change" — market cap changes with every tick — but "would a
# few hours of drift change the answer". Nobody screening `mcap > 10b`
# cares that the boundary moved 2% since lunch; someone screening
# `chg > 3` cares enormously. Volume is deliberately absent: it only ever
# rises during a session, so prefiltering on a stale value would drop the
# very names whose volume has since crossed the threshold.
STATIC_SAFE = {
    "symbol", "name", "sector", "industry", "country", "exchange",
    "quote_type", "currency", "ipo_year", "market_cap", "mktcap",
    "shares_outstanding", "pe", "forward_pe", "pb", "eps_ttm", "eps_forward",
    "book_value", "dividend_yield", "dividend_rate", "week52_high",
    "week52_low", "week52_change_pct", "sma50", "sma200", "avg_volume_3m",
    "avg_volume_10d", "analyst_rating", "earnings_ts", "earnings_days",
    "earnings_when", "peg", "prev_close", "is_adr",
    # Both inputs are themselves slow-moving averages a snapshot old by a
    # few hours does not change; unlike `pct_from_sma50` it does not also
    # depend on today's price.
    "sma_spread",
    # The width of the 52-week high/low band, unlike `pct_52w_range` above,
    # never touches today's price — only the two endpoints, which move on
    # their own slow schedule.
    "range_52w_width",
    # Same drift tolerance as `pe`, whose reciprocal this is: trailing EPS
    # barely moves intraday, so a few hours of price drift does not change
    # which side of a yield threshold a name falls on.
    "earnings_yield",
    # Both inputs are analyst estimates that update on their own schedule,
    # not with the tape — same drift tolerance as the `eps_ttm`/`eps_forward`
    # they're built from.
    "eps_growth",
    # Dividend rate and trailing EPS are both slow-moving fundamentals, not
    # today's tape — same drift tolerance as `earnings_yield`, whose
    # denominator this shares.
    "payout_ratio",
    # avg_dollar_volume is NOT here, though it looks like a standing fact:
    # it multiplies avg_volume_3m (slow) by `price` (live), so prefiltering
    # on a stale snapshot answers with yesterday's price baked into today's
    # threshold — the same two-clocks-in-one-number mistake `pct_52w_range`
    # avoids by staying off this set. It's in LIVE_COLUMNS instead, same as
    # `dollar_volume`, and gets re-priced before a screen sees it.
    # Two rolling averages, same drift tolerance as `sma_spread`. Today's
    # raw `volume` is deliberately absent, same reasoning as the line above.
    "volume_trend",
    # Built entirely from `pe`, itself already static-safe.
    "pe_spread",
    # The corporate calendar. A declared dividend's four dates and its
    # amount are fixed the moment it is announced and do not move again,
    # so they are the most static fields here — safe to narrow on before
    # anything is re-priced. The day counts are recomputed against the
    # clock on every load, not carried in the snapshot, so they cannot go
    # stale the way a stored figure would.
    "ex_div_ts", "div_record_ts", "div_pay_ts", "div_declared_ts",
    "div_amount", "div_annual_amount",
    "ex_div_days", "div_record_days", "div_pay_days",
    "ipo_age_years",
}

# Short names, because nobody wants to type pct_from_52w_high at a prompt.
ALIASES = {
    "cap": "market_cap", "mcap": "market_cap", "marketcap": "market_cap",
    "last": "price", "close": "price", "chgpct": "change_pct",
    "vol": "volume", "avgvol": "avg_volume_3m", "avgvol10": "avg_volume_10d",
    "relvol": "rel_volume", "relativevolume": "rel_volume",
    "relvol10": "rel_volume_10d", "rvol10": "rel_volume_10d",
    "shares": "shares_outstanding", "float": "shares_outstanding",
    "eps": "eps_ttm", "fpe": "forward_pe", "pricetobook": "pb",
    "eyield": "earnings_yield", "earningsyield": "earnings_yield",
    "epsgrowth": "eps_growth", "growth": "eps_growth",
    "payout": "payout_ratio", "payoutratio": "payout_ratio",
    "div": "dividend_yield", "divyield": "dividend_yield",
    "hi52": "week52_high", "lo52": "week52_low",
    "high52": "week52_high", "low52": "week52_low",
    "perf52": "week52_change_pct",
    "from52high": "pct_from_52w_high", "from52low": "pct_from_52w_low",
    "pos52": "pct_52w_range", "range52pos": "pct_52w_range",
    "width52": "range_52w_width", "range52width": "range_52w_width",
    "fromsma50": "pct_from_sma50", "fromsma200": "pct_from_sma200",
    "sma50pct": "pct_from_sma50", "sma200pct": "pct_from_sma200",
    "smaspread": "sma_spread", "crossdist": "sma_spread",
    "rating": "analyst_rating", "earnings": "earnings_days",
    "ipo": "ipo_year", "type": "quote_type", "state": "market_state",
    "lag": "lag_seconds",
    # Extended hours.
    "prechg": "pre_change_pct", "premarket": "pre_change_pct",
    "postchg": "post_change_pct", "ah": "post_change_pct",
    "afterhours": "post_change_pct", "aftermarket": "post_change_pct",
    "extchg": "ext_change_pct", "ext": "ext_change_pct",
    "preprice": "pre_price", "postprice": "post_price",
    "extprice": "ext_price", "session": "ext_label",
    "confirms": "ext_confirms",
    "adr": "is_adr", "depositary": "is_adr",
    "relvolraw": "rel_volume_raw", "rawrelvol": "rel_volume_raw",
    "dvol": "dollar_volume", "dollarvol": "dollar_volume",
    "dollarvolume": "dollar_volume", "turnover": "dollar_volume",
    "avgdvol": "avg_dollar_volume", "avgdollarvol": "avg_dollar_volume",
    "liquidity": "avg_dollar_volume",
    "turnoverpct": "turnover_pct", "floatturnover": "turnover_pct",
    "voltrend": "volume_trend", "volumetrend": "volume_trend",
    "dayrange": "day_range_pct", "rangepos": "day_range_pct",
    "inrange": "day_range_pct", "gap": "gap_pct",
    "intraday": "intraday_pct", "sinceopen": "intraday_pct",
    "gapfilled": "gap_filled", "fillsthegap": "gap_filled",
    "gapheld": "gap_held", "heldthegap": "gap_held",
    "tr": "true_range", "truerange": "true_range",
    "atr": "atr_pct", "atrpct": "atr_pct",
    "when": "earnings_when", "earningswhen": "earnings_when",
    "rs": "rs_sector", "vssector": "rs_sector",
    "sectorchg": "sector_change_pct",
    "rsmkt": "rs_market", "vsmarket": "rs_market", "rsmarket": "rs_market",
    "pespread": "pe_spread",
    # The dividend calendar. `exdiv` is the one people reach for — days
    # until the stock trades without its next payment — so it gets the
    # shortest name, and the raw timestamps sit behind `*date` spellings
    # for anyone who wants to sort or bracket by the date itself.
    "exdiv": "ex_div_days", "exdivdays": "ex_div_days",
    "daystoex": "ex_div_days", "exdate": "ex_div_ts",
    "recorddate": "div_record_ts", "recorddays": "div_record_days",
    "paydate": "div_pay_ts", "paydays": "div_pay_days",
    "declared": "div_declared_ts", "declareddate": "div_declared_ts",
    # The payment itself, and the two ways of sizing it. `divamt` is the
    # cash per share; `divdrop` is that as a percent of today's price —
    # the size of the gap to expect on the ex-date. Deliberately not
    # `div`, which is already the trailing annual yield: two numbers that
    # differ by a factor of four are worth two names.
    "divamt": "div_amount", "divamount": "div_amount",
    "divdrop": "div_drop_pct", "exdrop": "div_drop_pct",
    "divannual": "div_annual_amount", "annualdiv": "div_annual_amount",
    "fwdyield": "div_yield_upcoming", "divyieldfwd": "div_yield_upcoming",
    "ipoage": "ipo_age_years", "yearslisted": "ipo_age_years",
}

# Bare words that read as values, not columns.
CONSTANTS = {"true": True, "false": False, "none": None, "nan": np.nan}

# Sort keys the UI exposes. Anything in COLUMNS is valid; this is only the
# default column order for the grid.
DEFAULT_COLUMNS = ["symbol", "name", "price", "change_pct", "volume",
                   "rel_volume", "market_cap", "pe", "sector", "industry"]


class ScreenError(ValueError):
    """A malformed or disallowed expression. The message is shown verbatim
    in the terminal's status bar, so it has to read like English."""


# ---- literals ----

# 10b / 2.5m / 500k / 1.2t, but only where the number stands alone, so
# `sma50` and `week52_high` keep their digits.
_SUFFIX = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([kmbt])\b", re.IGNORECASE)
_MULT = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}

# Percent literals: `chg > 3%` is the same as `chg > 3`.
_PERCENT = re.compile(r"(?<=[\d)])\s*%")


def normalize(expr: str) -> str:
    """Rewrite the shorthand a screener user expects into valid Python."""
    text = _PERCENT.sub("", expr)
    text = _SUFFIX.sub(
        lambda m: repr(float(m.group(1)) * _MULT[m.group(2).lower()]), text)
    # `AND`/`OR`/`NOT` typed in caps, and the C-style spellings.
    text = re.sub(r"(?<![\w.])(AND|OR|NOT)(?![\w.])",
                  lambda m: m.group(1).lower(), text)
    text = re.sub(r"&&", " and ", text)
    text = re.sub(r"\|\|", " or ", text)
    # A single `=` where a comparison is meant.
    text = re.sub(r"(?<![=!<>])=(?!=)", "==", text)
    return text.strip()


def resolve(name: str) -> str:
    """Alias or column name -> canonical column name."""
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    if key not in COLUMNS:
        raise ScreenError(f"unknown field {name!r}")
    return key


# ---- evaluation ----

_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.USub, ast.UAdd, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.Name, ast.Load, ast.Constant, ast.Tuple,
    ast.List, ast.Set,
)


class _Evaluator(ast.NodeVisitor):
    """Walks the tree once, producing pandas objects. Anything not in the
    allowlist raises before it can be evaluated."""

    def __init__(self, df: pd.DataFrame):
        self.df = df

    def generic_visit(self, node):
        raise ScreenError(
            f"{type(node).__name__.lower()} is not allowed in a screen")

    def visit(self, node):
        if not isinstance(node, _ALLOWED_NODES):
            raise ScreenError(
                f"{type(node).__name__.lower()} is not allowed in a screen")
        return super().visit(node)

    def visit_Expression(self, node):
        return self.visit(node.body)

    def visit_Constant(self, node):
        if isinstance(node.value, (int, float, str, bool)) or node.value is None:
            return node.value
        raise ScreenError(f"unsupported literal {node.value!r}")

    def visit_Name(self, node):
        low = node.id.lower()
        if low in CONSTANTS:
            return CONSTANTS[low]
        col = resolve(node.id)
        if col not in self.df.columns:
            # An honest empty column beats a KeyError traceback: some
            # snapshots predate a field.
            return pd.Series(np.nan, index=self.df.index)
        return self.df[col]

    def visit_Tuple(self, node):
        return [self.visit(e) for e in node.elts]

    visit_List = visit_Tuple
    visit_Set = visit_Tuple

    def visit_UnaryOp(self, node):
        val = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            return ~_as_bool(val)
        if isinstance(node.op, ast.USub):
            return -val
        return +val

    def visit_BinOp(self, node):
        left, right = self.visit(node.left), self.visit(node.right)
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                # Division by zero is a missing value here, not an error.
                if isinstance(right, pd.Series):
                    right = right.replace(0, np.nan)
                elif right == 0:
                    return pd.Series(np.nan, index=self.df.index)
                return left / right
        except TypeError as exc:
            raise ScreenError(f"cannot combine those values ({exc})") from exc
        raise ScreenError("unsupported arithmetic")

    def visit_BoolOp(self, node):
        values = [_as_bool(self.visit(v)) for v in node.values]
        out = values[0]
        for nxt in values[1:]:
            out = (out & nxt) if isinstance(node.op, ast.And) else (out | nxt)
        return out

    def visit_Compare(self, node):
        # Chained comparisons (`2 < pe < 30`) are and-ed together, exactly
        # as Python reads them.
        result = None
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = self.visit(comparator)
            piece = self._compare(left, op, right)
            result = piece if result is None else (result & piece)
            left = right
        return result

    def _compare(self, left, op, right):
        # Text columns compare case-insensitively — nobody should have to
        # remember whether Nasdaq writes "Technology" or "technology".
        left_s, right_s = _fold(left), _fold(right)

        if isinstance(op, ast.In):
            return _contains(left_s, right_s)
        if isinstance(op, ast.NotIn):
            return ~_contains(left_s, right_s)

        try:
            if isinstance(op, ast.Eq):
                return _eq(left_s, right_s)
            if isinstance(op, ast.NotEq):
                return ~_eq(left_s, right_s)
            if isinstance(op, ast.Lt):
                return left_s < right_s
            if isinstance(op, ast.LtE):
                return left_s <= right_s
            if isinstance(op, ast.Gt):
                return left_s > right_s
            if isinstance(op, ast.GtE):
                return left_s >= right_s
        except TypeError as exc:
            raise ScreenError(f"cannot compare those values ({exc})") from exc
        raise ScreenError("unsupported comparison")


def _is_text(series: pd.Series) -> bool:
    """True for text columns under either dtype regime — pandas 3 gives
    these `str` dtype where pandas 2 gave `object`, and checking only for
    `object` silently disabled every text filter."""
    return (series.dtype == object
            or pd.api.types.is_string_dtype(series))


def _fold(value):
    """Lowercase strings and string Series so comparisons are insensitive."""
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, list):
        return [_fold(v) for v in value]
    if isinstance(value, pd.Series) and _is_text(value):
        return value.astype("string").str.lower()
    return value


def _eq(left, right):
    if isinstance(left, pd.Series):
        return (left == right).fillna(False)
    if isinstance(right, pd.Series):
        return (right == left).fillna(False)
    return bool(left == right)


def _contains(left, right):
    """`sector in ("technology", "energy")` -> isin.
    `"lab" in industry`                     -> substring search."""
    if isinstance(right, pd.Series):          # needle in column
        if not isinstance(left, str):
            raise ScreenError("`in` on a column expects text on the left")
        return right.str.contains(left, na=False, regex=False)
    if isinstance(left, pd.Series):           # column in list
        values = right if isinstance(right, list) else [right]
        if isinstance(right, str):            # column in "some text"
            return pd.Series([right.find(v) >= 0 if isinstance(v, str) else False
                              for v in left], index=left.index)
        return left.isin(values).fillna(False)
    raise ScreenError("`in` needs a field on one side")


def _as_bool(value) -> pd.Series | bool:
    if isinstance(value, pd.Series):
        return value.fillna(False).astype(bool)
    return bool(value)


def mask(df: pd.DataFrame, expr: str) -> pd.Series:
    """Boolean mask for `expr` over `df`. Empty expression selects all."""
    text = normalize(expr or "")
    if not text:
        return pd.Series(True, index=df.index)
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ScreenError(f"could not parse: {exc.msg}") from exc
    result = _Evaluator(df).visit(tree)
    if not isinstance(result, pd.Series):
        # A constant expression like `1 < 2` — apply it to every row.
        return pd.Series(bool(result), index=df.index)
    return _as_bool(result)


def _parse(expr: str):
    """Parse and structurally validate, returning the tree.

    The node allowlist has to run *before* any name is resolved, or
    `__import__("os")` gets reported as an unknown field rather than as
    the disallowed call it is. Same rejection either way, but only one of
    those messages tells you what you actually did wrong.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ScreenError(f"could not parse: {exc.msg}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ScreenError(
                f"{type(node).__name__.lower()} is not allowed in a screen")
    return tree


def fields_used(expr: str) -> set[str]:
    """Every column an expression touches, resolved to canonical names."""
    text = normalize(expr or "")
    if not text:
        return set()
    out = set()
    for node in ast.walk(_parse(text)):
        if isinstance(node, ast.Name) and node.id.lower() not in CONSTANTS:
            out.add(resolve(node.id))
    return out


def split_live(expr: str) -> tuple[str, str]:
    """Split an expression into (static, live) halves.

    A screen is usually a chain of and-ed conditions, some about facts that
    barely move in a day (market cap, sector, P/E) and some about the tape
    (change, volume, price). Only the second kind needs a live quote, and
    quoting 7,000 symbols to answer `chg > 3` is not worth 15 seconds. So
    the static half narrows the field first and only the survivors get
    re-priced.

    An expression that is not a top-level `and` chain can't be split
    safely, so it goes wholly to whichever side it belongs.
    """
    text = normalize(expr or "")
    if not text:
        return "", ""
    body = _parse(text).body

    parts = (list(body.values)
             if isinstance(body, ast.BoolOp) and isinstance(body.op, ast.And)
             else [body])
    static, live = [], []
    for node in parts:
        names = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id.lower() not in CONSTANTS:
                names.add(resolve(sub.id))
        # A conjunct narrows only if *every* field in it is drift-tolerant.
        # One live field contaminates the whole condition.
        static_ok = bool(names) and names <= STATIC_SAFE
        (static if static_ok else live).append(ast.unparse(node))
    return " and ".join(static), " and ".join(live)


# Quote fields that overwrite their snapshot counterpart, when present.
_OVERLAY_NUMERIC = ("price", "change", "change_pct", "volume", "market_cap",
                    "day_high", "day_low", "pre_price", "pre_change_pct",
                    "post_price", "post_change_pct", "ext_price",
                    "ext_change_pct")
_OVERLAY_TEXT = ("ext_label", "market_state")


def apply_quotes(df: pd.DataFrame, quotes: dict) -> pd.DataFrame:
    """Overlay live quotes onto a snapshot frame and recompute what derives
    from price.

    A missing field leaves the snapshot value alone: a partial quote
    response must not blank out data we already had.
    """
    if df.empty or not quotes:
        return df
    # Local import: keeps screen.py importable without the provider stack.
    from .universe import derive
    out = df.copy()
    symbols = out["symbol"]

    # `field=field` binds the loop variable at definition time. The lambda is
    # consumed by .map() inside this iteration so late binding would not bite
    # today, but it is one refactor away from doing so.
    for field in _OVERLAY_NUMERIC:
        fresh = pd.to_numeric(
            symbols.map(lambda s, field=field: (quotes.get(s) or {}).get(field)),
            errors="coerce")
        if fresh.notna().any():
            out[field] = fresh.where(fresh.notna(),
                                     out[field] if field in out else np.nan)
    for field in _OVERLAY_TEXT:
        fresh = symbols.map(
            lambda s, field=field: (quotes.get(s) or {}).get(field))
        if fresh.notna().any():
            out[field] = fresh.where(fresh.notna(),
                                     out[field] if field in out else None)
    return derive(out)


def run(df: pd.DataFrame, expr: str = "", sort: str = "market_cap",
        descending: bool = True, limit: int = 500,
        offset: int = 0) -> tuple[pd.DataFrame, int]:
    """Screen, sort, paginate. Returns (page, total matched)."""
    if df.empty:
        return df, 0
    hits = df[mask(df, expr)]
    total = len(hits)
    if sort:
        col = resolve(sort)
        if col in hits.columns:
            # Missing values sort last in both directions; a blank P/E is
            # not "the cheapest stock in the market".
            hits = hits.sort_values(col, ascending=not descending,
                                    na_position="last", kind="stable")
    return hits.iloc[offset:offset + limit], total


# ---- the dropdown bar ----

# Each filter is (id, label, [(option label, expression fragment), ...]).
# "" as a fragment means "Any". The UI renders these; the server is the
# single source of truth so the two can never drift apart.
FILTER_SPECS = [
    ("sector", "Sector", [("Any", "")] + [
        (s, f'sector == "{s}"') for s in [
            "Technology", "Health Care", "Finance", "Energy",
            "Consumer Discretionary", "Consumer Staples", "Industrials",
            "Basic Materials", "Real Estate", "Telecommunications",
            "Utilities", "Miscellaneous"]]),
    ("cap", "Market Cap", [
        ("Any", ""), ("Mega >200B", "mcap > 200b"),
        ("Large >10B", "mcap > 10b"), ("Mid 2-10B", "2b < mcap < 10b"),
        ("Small 300M-2B", "300m < mcap < 2b"),
        ("Micro <300M", "mcap < 300m")]),
    ("price", "Price", [
        ("Any", ""), ("Under $5", "price < 5"), ("$5 to $20", "5 < price < 20"),
        ("$20 to $50", "20 < price < 50"), ("$50 to $200", "50 < price < 200"),
        ("Over $200", "price > 200")]),
    ("pe", "P/E", [
        ("Any", ""), ("Profitable", "pe > 0"), ("Under 15", "0 < pe < 15"),
        ("Under 25", "0 < pe < 25"), ("Over 50", "pe > 50"),
        ("No earnings", "eps < 0")]),
    ("chg", "Change", [
        ("Any", ""), ("Up", "chg > 0"), ("Up 3%+", "chg > 3"),
        ("Up 5%+", "chg > 5"), ("Down", "chg < 0"), ("Down 3%+", "chg < -3"),
        ("Down 5%+", "chg < -5")]),
    ("relvol", "Rel Volume", [
        ("Any", ""), ("Over 1.5", "relvol > 1.5"), ("Over 2", "relvol > 2"),
        ("Over 5", "relvol > 5"), ("Under 0.5", "relvol < 0.5")]),
    ("vol", "Volume", [
        ("Any", ""), ("Over 100K", "vol > 100k"), ("Over 500K", "vol > 500k"),
        ("Over 1M", "vol > 1m"), ("Over 10M", "vol > 10m")]),
    # Written against the *average*, not today's figure. A dropdown that
    # filtered on `dvol` would empty the grid every morning, because at
    # 09:45 nothing has traded $20M yet. Typed screens can still ask for
    # today's: `dvol > 500m`.
    ("dvol", "Dollar Vol", [
        ("Any", ""), ("Over $1M", "avgdvol > 1m"),
        ("Over $10M", "avgdvol > 10m"), ("Over $50M", "avgdvol > 50m"),
        ("Over $200M", "avgdvol > 200m"), ("Under $1M", "avgdvol < 1m")]),
    ("range52", "52W Range", [
        ("Any", ""), ("At high (<2%)", "from52high > -2"),
        ("Near high (<10%)", "from52high > -10"),
        ("Near low (<10%)", "from52low < 10"),
        ("Down 50%+", "from52high < -50")]),
    ("sma", "Moving Avg", [
        ("Any", ""), ("Above SMA50", "fromsma50 > 0"),
        ("Above SMA200", "fromsma200 > 0"),
        ("Above both", "fromsma50 > 0 and fromsma200 > 0"),
        ("Below both", "fromsma50 < 0 and fromsma200 < 0"),
        ("Golden cross", "sma50 > sma200")]),
    ("div", "Dividend", [
        ("Any", ""), ("Pays", "div > 0"), ("Over 2%", "div > 2"),
        ("Over 4%", "div > 4"), ("None", "div == 0 or div != div")]),
    ("exdiv", "Ex-Dividend", [
        ("Any", ""),
        ("Goes ex today", "-1 < exdiv < 1"),
        ("Next 3 days", "0 < exdiv < 3"),
        ("Next 7 days", "0 < exdiv < 7"),
        ("Next 30 days", "0 < exdiv < 30"),
        ("Went ex last 7d", "-7 < exdiv < 0"),
        # Capture screens want the payment to be worth the trade and the
        # company to be able to afford it. A payout over 100% is a
        # dividend funded from somewhere other than this year's earnings.
        ("Drop over 1%", "0 < exdiv < 30 and divdrop > 1"),
        ("Covered — payout under 75%", "0 < exdiv < 30 and 0 < payout < 75"),
        # The mirror of the earnings avoidance filter, and needed for the
        # same reason: an ex-date is a scheduled gap down, so a momentum
        # screen that does not exclude it is measuring the dividend.
        ("Avoid — none within 7d", "not (0 < exdiv < 7)")]),
    ("earnings", "Earnings", [
        ("Any", ""), ("Today", "-1 < earnings < 1"),
        ("Next 3 days", "0 < earnings < 3"),
        ("Next 7 days", "0 < earnings < 7"),
        ("Next 30 days", "0 < earnings < 30"),
        ("Reported last 7d", "-7 < earnings < 0"),
        ("Before the open", 'when == "BMO" and 0 < earnings < 14'),
        ("After the close", 'when == "AMC" and 0 < earnings < 14'),
        # The one people actually need: keep a screen clear of anything
        # about to be repriced by a print you didn't know was coming.
        ("Avoid — none within 7d", "not (0 < earnings < 7)")]),
    ("ext", "Ext Hours", [
        ("Any", ""), ("Moving", "extchg != 0"),
        ("Up 2%+", "extchg > 2"), ("Up 5%+", "extchg > 5"),
        ("Down 2%+", "extchg < -2"), ("Down 5%+", "extchg < -5"),
        ("Pre-market", 'session == "PRE"'), ("After hours", 'session == "AH"')]),
    ("country", "Country", [("Any", "")] + [
        (c, f'country == "{c}"') for c in [
            "United States", "China", "Canada", "Israel", "United Kingdom",
            "Singapore", "Hong Kong", "Brazil", "Taiwan", "Japan",
            "Australia", "India", "Germany", "France", "Switzerland",
            "Netherlands", "Ireland", "South Korea", "Mexico", "Greece",
            "Sweden", "Spain", "Italy", "Denmark", "Argentina",
            "South Africa", "Bermuda", "Cayman Islands", "Luxembourg"]]),
    ("adr", "ADR", [
        ("Any", ""), ("ADRs only", "adr"), ("Exclude ADRs", "not adr"),
        ("Foreign-domiciled", 'country != "United States"')]),
    ("type", "Type", [
        ("Any", ""), ("Common stock", 'type == "EQUITY"'),
        ("ETF", 'type == "ETF"'), ("Exclude funds", 'type != "ETF"')]),
]

_SPEC_BY_ID = {fid: (label, opts) for fid, label, opts in FILTER_SPECS}


def filters_to_expr(selected: dict) -> str:
    """{"cap": "Large >10B", "chg": "Up 3%+"} -> one and-ed expression."""
    parts = []
    for fid, choice in (selected or {}).items():
        spec = _SPEC_BY_ID.get(fid)
        if not spec or not choice:
            continue
        frag = dict(spec[1]).get(choice, "")
        if frag:
            parts.append(f"({frag})" if " or " in frag or " and " in frag
                         else frag)
    return " and ".join(parts)


def describe(expr: str) -> list[str]:
    """Split an expression into chips for the filter bar."""
    text = (expr or "").strip()
    if not text:
        return []
    depth, current, chips = 0, "", []
    tokens = re.split(r"(\s+and\s+|\s+or\s+|[()])", text, flags=re.IGNORECASE)
    for tok in tokens:
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        if depth == 0 and re.fullmatch(r"\s+and\s+", tok or "", re.IGNORECASE):
            if current.strip():
                chips.append(current.strip())
            current = ""
        else:
            current += tok or ""
    if current.strip():
        chips.append(current.strip())
    return [c.strip("() ") for c in chips if c.strip("() ")]


# ---- presets ----

PRESETS = [
    ("Momentum", "chg > 3 and relvol > 2 and price > 5 and vol > 500k"),
    ("Unusual Volume", "relvol > 3 and vol > 1m and mcap > 300m"),
    ("Near 52w High", "from52high > -3 and mcap > 2b and vol > 500k"),
    ("Large Cap Value", "mcap > 10b and 0 < pe < 15 and div > 1"),
    ("Oversold", "from52high < -40 and mcap > 1b and fromsma50 < -10"),
    ("Golden Cross", "sma50 > sma200 and fromsma50 > 0 and mcap > 2b"),
    ("Earnings This Week", "0 < earnings < 7 and mcap > 2b"),
    ("Dividend Payers", "div > 3 and mcap > 5b and 0 < pe < 30"),
    ("Beating Its Sector", "rs > 3 and mcap > 2b and vol > 500k"),
    # Closing on the high, on real money — the two conditions a continuation
    # setup needs and that a percentage change alone cannot express.
    ("Closing at the Highs", "dayrange > 90 and chg > 2 and dvol > 20m"),
    ("Real Money Moving", "dvol > 100m and relvol > 2 and avgdvol > 20m"),
    ("Extended Movers", "extchg > 3 and mcap > 1b"),
    ("Gapping Down", "extchg < -4 and mcap > 1b"),
    ("China ADRs", 'adr and country == "China"'),
    ("European ADRs", 'adr and country in ("United Kingdom", "France", '
                      '"Germany", "Netherlands", "Switzerland", "Spain", '
                      '"Italy", "Sweden", "Denmark", "Ireland")'),
    ("ADR Movers", "adr and chg > 3 and vol > 100k"),
]


def seed_presets() -> int:
    """Install the starter screens once. Never overwrites an edited one."""
    existing = {s["name"] for s in store.list_screens()}
    added = 0
    for name, expr in PRESETS:
        if name not in existing:
            store.save_screen(name, expr)
            added += 1
    return added


# ---- serialization ----

def to_records(df: pd.DataFrame, columns: list[str] | None = None) -> list[dict]:
    """DataFrame -> JSON-safe rows. NaN becomes null, not the string 'NaN',
    which is what json.dumps would otherwise emit and what would then break
    JSON.parse in the browser."""
    if df.empty:
        return []
    cols = [c for c in (columns or df.columns) if c in df.columns]
    out = []
    for row in df[cols].itertuples(index=False, name=None):
        rec = {}
        for col, val in zip(cols, row, strict=True):
            if val is None or (isinstance(val, float) and val != val):
                rec[col] = None
            elif isinstance(val, (np.integer,)):
                rec[col] = int(val)
            elif isinstance(val, (np.floating,)):
                rec[col] = None if np.isnan(val) else float(val)
            elif isinstance(val, np.bool_):
                rec[col] = bool(val)
            else:
                rec[col] = val
        out.append(rec)
    return out


def group_summary(df: pd.DataFrame, column: str,
                  liquid_only: bool = True) -> list[dict]:
    """Aggregate by `column`: count, total market cap, and a cap-weighted
    average change.

    Cap-weighted rather than a plain mean, because the average of 5,000
    tickers is dominated by microcaps nobody trades and would say the
    market fell on a day the index rose. Rows with no value in `column`
    are dropped, never bucketed into "Other" — a fake group is worse than
    a missing one, and the caller reports the shortfall.
    """
    if df.empty or column not in df.columns or "change_pct" not in df.columns:
        return []
    part_of = df[df["volume"].fillna(0) > 10_000] if liquid_only else df
    part_of = part_of.dropna(subset=[column])
    out = []
    for name, part in part_of.groupby(column):
        weights = part["market_cap"].fillna(0)
        total = float(weights.sum())
        changes = part["change_pct"].fillna(0)
        avg = (float((changes * weights).sum() / total) if total > 0
               else float(changes.mean() or 0))
        out.append({
            column: name,
            "name": str(name),
            "change_pct": avg,
            "count": int(len(part)),
            "market_cap": total,
            "advancing": int((part["change_pct"] > 0).sum()),
            "declining": int((part["change_pct"] < 0).sum()),
        })
    out.sort(key=lambda g: -g["change_pct"])
    return out


def market_summary(df: pd.DataFrame) -> dict:
    """Advancers/decliners and sector breadth for the status bar."""
    if df.empty or "change_pct" not in df.columns:
        return {"advancing": 0, "declining": 0, "unchanged": 0, "sectors": []}
    chg = df[df["volume"].fillna(0) > 10_000]["change_pct"]
    return {
        "advancing": int((chg > 0).sum()),
        "declining": int((chg < 0).sum()),
        "unchanged": int((chg == 0).sum()),
        "sectors": group_summary(df, "sector"),
        "as_of": time.time(),
    }


def country_summary(df: pd.DataFrame) -> dict:
    """Per-country aggregates for the world map, plus what didn't fit.

    No liquidity floor here, unlike sector breadth: a country's total
    market cap means every company domiciled there, and screening out the
    thinly traded ones would understate small markets by more than half.
    `unplaced` is then exactly the rows with no country at all.
    """
    countries = group_summary(df, "country", liquid_only=False)
    placed = sum(c["count"] for c in countries)
    return {
        "countries": countries,
        "placed": placed,
        "unplaced": int(len(df)) - placed,
        "total": int(len(df)),
        "as_of": time.time(),
    }
