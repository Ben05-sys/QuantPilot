"""Provider interface.

Every upstream sits behind this so it can be swapped. That is not
architectural decoration: the free quote sources here are undocumented
endpoints whose terms prohibit automated access, so the day one of them
closes or the day this needs to be legitimate, only one file changes.

Latency is a first-class field. It is *measured* per response (server
timestamp vs. local clock), never assumed, and the UI renders whatever
comes back. A terminal that claims "LIVE" over stale data is worse than
one that admits the delay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Quote:
    symbol: str
    price: float | None = None
    change: float | None = None
    change_pct: float | None = None
    volume: float | None = None
    market_cap: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    prev_close: float | None = None
    # Seconds between the venue's timestamp and our clock. None = unknown.
    lag_seconds: float | None = None
    # REGULAR / PRE / POST / PREPRE / POSTPOST / CLOSED / None
    market_state: str | None = None
    # Extended hours. `price` above is always the last *regular* session
    # print, so these are kept separate rather than folded into it — an
    # after-hours move is a different fact about a different session, and
    # merging the two is how a screener starts lying about the day's range.
    pre_price: float | None = None
    pre_change_pct: float | None = None
    pre_time: float | None = None
    post_price: float | None = None
    post_change_pct: float | None = None
    post_time: float | None = None
    source: str = ""
    extra: dict = field(default_factory=dict)

    def extended(self) -> tuple[str | None, float | None, float | None]:
        """The extended-hours session that matters *now* -> (label, price,
        change %). Which one is live is decided by the venue's own
        market_state, not by our clock.

        POSTPOST is the overnight gap after after-hours closes: the last
        post-market print is still the most recent trade, so it keeps being
        reported until the pre-market opens.
        """
        state = (self.market_state or "").upper()
        if state == "PRE" and self.pre_price is not None:
            return "PRE", self.pre_price, self.pre_change_pct
        if state in ("POST", "POSTPOST") and self.post_price is not None:
            return "AH", self.post_price, self.post_change_pct
        return None, None, None


@dataclass
class Bars:
    symbol: str
    timestamps: list[int]
    open: list[float | None]
    high: list[float | None]
    low: list[float | None]
    close: list[float | None]
    volume: list[float | None]
    interval: str = "1d"
    source: str = ""
    # Session boundaries as (start, end) epoch pairs, when extended hours
    # were requested. The chart is one continuous series, so without these
    # there is no way to tell a 4am print from a 10am one — and shading the
    # extended regions is the whole point of asking for them.
    sessions: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.timestamps)


class ProviderError(RuntimeError):
    """An upstream failed in a way the caller should surface, not crash on."""


class QuoteProvider(Protocol):
    name: str

    def quotes(self, symbols: list[str]) -> list[Quote]:
        ...

    def bars(self, symbol: str, rng: str = "1y", interval: str = "1d") -> Bars:
        ...


_REGISTRY: dict[str, QuoteProvider] = {}


def register(provider: QuoteProvider) -> QuoteProvider:
    _REGISTRY[provider.name] = provider
    return provider


def get(name: str) -> QuoteProvider:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ProviderError(
            f"unknown provider {name!r}; have {sorted(_REGISTRY)}") from None


def available() -> list[str]:
    return sorted(_REGISTRY)
