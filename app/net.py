"""Rate-limited, disk-cached HTTP.

Ported from an earlier project's HTTP layer (httpx -> requests), with two
additions this project needs: per-host User-Agent selection and a
cookie-bearing session, because the upstreams disagree about what a polite
client looks like.

The rules every upstream here is owed: declare who we are, stay under the
published rate limits, and never re-fetch something we already have.

Two empirically-verified gotchas, both of which bite people:
  - SEC returns 403 for a browser User-Agent and 200 for a declarative
    contact string. It is the opposite of what you would guess.
  - Yahoo returns 429 with no User-Agent at all, and 401 on /v7/ without a
    cookie+crumb pair.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from . import config

# A browser string for the endpoints that expect one.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def sec_ua() -> str:
    """SEC asks for a descriptive string with contact info. Sending a
    browser User-Agent here earns a 403."""
    contact = (config.load_config().get("sec_contact") or "").strip()
    if not contact:
        raise ContactRequired(
            "SEC filings need a contact address. Put your email in "
            f"{config.config_path()} as \"sec_contact\", or set "
            "QUANTPILOT_SEC_CONTACT. Everything else works without it.")
    return f"QuantPilot/0.1 ({contact})"


class RateLimiter:
    """Simple token bucket, shared across threads."""

    def __init__(self, per_second: float):
        self._min_interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = self._last + self._min_interval - now
            if sleep_for > 0:
                time.sleep(sleep_for)
                now = time.monotonic()
            self._last = now


# Per-host limits. SEC publishes 10/s; we take half of that.
_LIMITERS: dict[str, RateLimiter] = {
    "www.sec.gov": RateLimiter(5),
    "data.sec.gov": RateLimiter(5),
    "efts.sec.gov": RateLimiter(4),
    "api.nasdaq.com": RateLimiter(2),
    "query1.finance.yahoo.com": RateLimiter(8),
    "query2.finance.yahoo.com": RateLimiter(8),
    "cdn.cboe.com": RateLimiter(2),
}
_DEFAULT_LIMITER = RateLimiter(4)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _is_sec(host: str) -> bool:
    # Exactly sec.gov or a subdomain of it. `endswith("sec.gov")` also
    # matches `notsec.gov`, and the SEC agent carries the user's own contact
    # address — a string this project goes out of its way not to hand to
    # anyone who did not ask for it.
    return host == "sec.gov" or host.endswith(".sec.gov")


def _ua_for(url: str) -> str:
    return sec_ua() if _is_sec(_host(url)) else BROWSER_UA


class Cache:
    """URL -> response body with a per-call TTL.

    Filings and universe snapshots are effectively immutable once written,
    so a long TTL is safe. Live quotes pass ttl=0 and skip it entirely.
    """

    def __init__(self, path=None):
        self._path = path or config.cache_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS cache ("
                "  key TEXT PRIMARY KEY,"
                "  url TEXT NOT NULL,"
                "  body BLOB NOT NULL,"
                "  fetched_at REAL NOT NULL"
                ")"
            )

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()

    def get(self, url: str, ttl: float) -> bytes | None:
        if ttl <= 0:
            return None
        row = self._conn().execute(
            "SELECT body, fetched_at FROM cache WHERE key = ?", (self._key(url),)
        ).fetchone()
        if row is None:
            return None
        body, fetched_at = row
        if time.time() - fetched_at > ttl:
            return None
        return body

    def put(self, url: str, body: bytes) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, url, body, fetched_at) "
            "VALUES (?,?,?,?)",
            (self._key(url), url, body, time.time()),
        )
        conn.commit()


_cache: Cache | None = None
_cache_lock = threading.Lock()
_session: requests.Session | None = None
_session_lock = threading.Lock()


def cache() -> Cache:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = Cache()
        return _cache


def session() -> requests.Session:
    """One shared session so Yahoo's auth cookie survives across calls."""
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            s.headers.update({"Accept-Encoding": "gzip, deflate"})
            _session = s
        return _session


def reset_session() -> None:
    """Drop cookies. Used when Yahoo starts 401-ing and the crumb needs
    to be re-minted."""
    global _session
    with _session_lock:
        _session = None


class FetchError(RuntimeError):
    def __init__(self, url: str, status: int, body: str = ""):
        self.url = url
        self.status = status
        super().__init__(f"{status} fetching {url}: {body[:200]}")


class ContactRequired(FetchError):
    """No SEC contact address is configured.

    Raised rather than defaulted: SEC wants a real address, and inventing
    one — or reusing whoever happened to build the binary — is worse than
    saying plainly that the filings panel needs one line of setup.

    A FetchError subclass on purpose. Every SEC call already turns those
    into a clean ProviderError that the UI reports in place, so this
    arrives as a readable message in the filings panel instead of a
    traceback, without touching a single call site.
    """

    def __init__(self, message: str):
        RuntimeError.__init__(self, message)
        self.url = ""
        self.status = 0


DEFAULT_TTL = 24 * 3600


MAX_REDIRECTS = 5


def fetch(url: str, *, ttl: float = DEFAULT_TTL, retries: int = 3,
          headers: dict | None = None, timeout: float = 30.0,
          redirect_guard: Callable[[str], bool] | None = None) -> bytes:
    """GET a URL, honouring the cache and the per-host rate limit.

    `redirect_guard` makes the hops visible. Left as None, requests follows
    redirects itself, which is right for the providers: their hosts are ours
    to begin with. The article reader passes its own check, because there
    the starting URL came from a third-party feed — and a host check that
    only inspects the address you *started* with is not a check at all if
    the server on the other end is allowed to send you somewhere else.
    """
    requested = url
    cached = cache().get(requested, ttl)
    if cached is not None:
        return cached

    hdrs = dict(headers or {})
    sess = session()
    last_error: Exception | None = None

    # Redirects are the outer loop and retries the inner one: a hop is not a
    # failed attempt, and following three of them must not eat the retries a
    # flaky host is going to need.
    for _hop in range(MAX_REDIRECTS + 1):
        limiter = _LIMITERS.get(_host(url), _DEFAULT_LIMITER)
        # A caller-supplied agent wins and stays put across hops; otherwise
        # it is chosen per host, so a hop onto sec.gov still identifies us.
        hdrs["User-Agent"] = (headers or {}).get("User-Agent") or _ua_for(url)
        resp = None

        for attempt in range(retries):
            limiter.wait()
            try:
                resp = sess.get(url, headers=hdrs, timeout=timeout,
                                allow_redirects=redirect_guard is None)
            except requests.RequestException as exc:  # transport; retry
                last_error, resp = exc, None
                time.sleep(1.5 * (attempt + 1))
                continue

            if resp.status_code in (429, 503) or resp.status_code >= 500:
                time.sleep(2.0 * (attempt + 1))
                last_error, resp = FetchError(
                    url, resp.status_code, resp.text), None
                continue
            break

        if resp is None:
            break

        if redirect_guard is not None and resp.is_redirect:
            target = urljoin(url, resp.headers.get("location", ""))
            if not redirect_guard(target):
                raise FetchError(target, resp.status_code,
                                 "redirected somewhere we will not follow")
            url = target
            continue

        if resp.status_code == 200:
            if ttl > 0:
                # Keyed on what the caller asked for, so a redirect chain is
                # not walked again on every read.
                cache().put(requested, resp.content)
            return resp.content

        # 404 is a real answer for a lot of these APIs ("this company never
        # reported that concept"), not a transient failure.
        if resp.status_code == 404:
            raise FetchError(url, 404)

        raise FetchError(url, resp.status_code, resp.text)
    else:
        raise FetchError(url, 0, "too many redirects")

    if isinstance(last_error, FetchError):
        raise last_error
    raise FetchError(url, 0, str(last_error))


def fetch_json(url: str, *, ttl: float = DEFAULT_TTL, **kw) -> Any:
    return json.loads(fetch(url, ttl=ttl, **kw))
