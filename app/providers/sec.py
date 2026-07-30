"""SEC EDGAR — authoritative fundamentals and the live insider feed.

The gotcha that trips nearly everyone, verified here: SEC returns 403 for
a browser User-Agent and 200 for a declarative contact string. net.py
routes *.sec.gov to the contact UA automatically.

Published limit is 10 requests/second across all sec.gov subdomains;
net.py holds us to 5.

What EDGAR is good for: ground truth. What it is not good for: freshness.
XBRL fundamentals lag the filing calendar by 40-90 days no matter who you
are, so live ratios come from Yahoo and EDGAR is the audit trail.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from .. import net
from .base import ProviderError

TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
FORM4_RSS = ("https://www.sec.gov/cgi-bin/browse-edgar"
             "?action=getcurrent&type=4&output=atom&count=100")


def ticker_map(ttl: float = 7 * 24 * 3600) -> dict[str, int]:
    """SYMBOL -> CIK. ~10k entries, 798 KB, changes rarely."""
    try:
        raw = net.fetch(TICKERS, ttl=ttl, timeout=60)
    except net.FetchError as exc:
        raise ProviderError(f"sec tickers: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"sec tickers: bad json ({exc})") from exc
    out = {}
    # Shipped as {"0": {...}, "1": {...}} rather than a list.
    for row in (data.values() if isinstance(data, dict) else data):
        t = (row.get("ticker") or "").strip().upper()
        cik = row.get("cik_str")
        if t and isinstance(cik, int):
            out[t] = cik
    if not out:
        raise ProviderError("sec tickers: empty map")
    return out


def submissions(cik: int, ttl: float = 3600) -> dict:
    """Filing history — used by the filing-diff feature to find the current
    and prior 10-K/10-Q for a company."""
    try:
        return json.loads(net.fetch(SUBMISSIONS.format(cik=cik), ttl=ttl,
                                    timeout=60))
    except net.FetchError as exc:
        raise ProviderError(f"sec submissions {cik}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"sec submissions {cik}: bad json ({exc})") from exc


def recent_filings(cik: int, forms: tuple[str, ...] = ("10-K", "10-Q"),
                   limit: int = 8) -> list[dict]:
    """Most recent filings of the given types, newest first."""
    sub = submissions(cik)
    recent = (sub.get("filings") or {}).get("recent") or {}
    cols = ("accessionNumber", "filingDate", "reportDate", "form",
            "primaryDocument")
    n = len(recent.get("form") or [])
    out = []
    for i in range(n):
        form = (recent.get("form") or [None] * n)[i]
        if form not in forms:
            continue
        row = {c: (recent.get(c) or [None] * n)[i] for c in cols}
        acc = (row["accessionNumber"] or "").replace("-", "")
        row["url"] = (f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
                      f"{row['primaryDocument']}") if acc else None
        row["cik"] = cik
        out.append(row)
        if len(out) >= limit:
            break
    return out


def company_facts(cik: int, ttl: float = 24 * 3600) -> dict:
    """Full XBRL history. Large (single-MB) per company."""
    try:
        return json.loads(net.fetch(COMPANYFACTS.format(cik=cik), ttl=ttl,
                                    timeout=90))
    except net.FetchError as exc:
        if exc.status == 404:
            raise ProviderError(f"sec: no XBRL facts for CIK {cik}") from exc
        raise ProviderError(f"sec companyfacts {cik}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"sec companyfacts {cik}: bad json ({exc})") from exc


FTS = ("https://efts.sec.gov/LATEST/search-index"
       "?q=%22{name}%22&forms={forms}")

# "APPLE INC  (AAPL)  (CIK 0000320193)" — the aggregation buckets carry the
# ticker already, which is the whole reason this is usable.
_ENTITY = re.compile(r"^(?P<name>.*?)\s+\((?P<ticker>[A-Z0-9.\-]{1,10})\)\s+"
                     r"\(CIK (?P<cik>\d+)\)")


def related(company: str, forms: str = "10-K", limit: int = 25,
            ttl: float = 7 * 24 * 3600) -> list[dict]:
    """Companies that name `company` in their own annual reports.

    This is the closest a free source gets to a supply-chain graph.
    Vendors sell curated supplier lists because building one is manual
    work; what EDGAR gives instead is every company that disclosed a
    relationship material enough to write down, with the filing attached
    so the claim can be read rather than trusted.

    It does **not** distinguish supplier from customer from competitor —
    Harley-Davidson names Tesla, and it is neither's supplier. The caller
    labels these honestly; guessing here would be worse than not knowing.

    Verified 2026-07-29: Apple returns Liquidmetal, Qualcomm, Intel and
    InterDigital; Tesla returns Aptiv, QuantumScape and ChargePoint.
    """
    name = (company or "").strip()
    if not name:
        return []
    url = FTS.format(name=quote(name), forms=forms)
    try:
        payload = json.loads(net.fetch(url, ttl=ttl, retries=2, timeout=60))
    except net.FetchError as exc:
        raise ProviderError(f"sec full-text search: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"sec full-text search: bad json ({exc})") from exc

    buckets = (payload.get("aggregations", {})
               .get("entity_filter", {}).get("buckets", []))
    out = []
    for bucket in buckets:
        match = _ENTITY.match(bucket.get("key", ""))
        if not match:
            continue
        ticker = match.group("ticker").upper()
        if ticker in ("NONE", ""):
            continue
        out.append({
            "symbol": ticker,
            "company": match.group("name").strip(),
            "cik": int(match.group("cik")),
            "filings": int(bucket.get("doc_count") or 0),
        })
        if len(out) >= limit:
            break
    return out


def latest_fact(facts: dict, tag: str, unit: str = "USD") -> float | None:
    """Most recent reported value for a us-gaap concept.

    Note the trap: companyfacts returns every amount from every filing,
    including restatements of the same period. Sorting by filing date and
    taking the last is the honest reading of 'latest'.
    """
    try:
        series = facts["facts"]["us-gaap"][tag]["units"][unit]
    except (KeyError, TypeError):
        return None
    dated = [s for s in series if s.get("end") and s.get("val") is not None]
    if not dated:
        return None
    dated.sort(key=lambda s: (s.get("end"), s.get("filed") or ""))
    try:
        return float(dated[-1]["val"])
    except (TypeError, ValueError):
        return None
