"""Reading an article inside the terminal.

Embedding the page directly is not possible: Yahoo sends
`X-Frame-Options: SAMEORIGIN` and a `frame-ancestors 'self'` policy, so an
iframe is refused. The payload's own `summary` is one sentence. So the
server fetches the page and extracts the body text.

**Only articles this server has already handed out can be fetched.** A
route that took a URL and fetched it would be an open proxy sitting on
your machine — anything that could reach the API could use it to probe
your network or a cloud metadata endpoint. Instead `providers.yahoo.news`
registers each story's id and link here, and the reader asks by id.

Extraction is best-effort across publishers. When it comes back thin, the
UI says so and offers the original rather than pretending a stub is the
article.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from urllib.parse import urlsplit

from . import net

# id -> (url, registered_at). Bounded and time-limited: this is a
# capability list, not a cache, and it should not grow forever.
_KNOWN: dict[str, tuple[str, float]] = {}
_LOCK = threading.Lock()
_TTL = 6 * 3600
_MAX = 4000

# A registered id still has to resolve to an address off this machine and
# off this network — belt and braces, so a poisoned feed entry cannot point
# the fetcher at something local.
#
# This was a regex over the text of the host, which is the wrong shape for
# the job: `127.0.0.1` is only one spelling of loopback. `2130706433` is the
# same address as an integer, `[::1]` and `[::ffff:127.0.0.1]` are the same
# address in IPv6, and none of `0.0.0.0`, `fe80::`, `fc00::` or the
# 100.64/10 carrier range were listed at all. Asking the `ipaddress` module
# instead covers every spelling of every one of them, including the
# 169.254.169.254 metadata address that makes SSRF worth doing.


def _addresses(host: str) -> list:
    """Every IP `host` stands for — itself if it is a literal, otherwise
    whatever DNS says. An empty list means the name did not resolve, and the
    fetch is left to fail on its own terms rather than being called unsafe.
    """
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    # The spellings `ipaddress` rejects and the network stack still accepts:
    # `2130706433`, `0177.0.0.1`, `127.1` are all loopback to inet_aton, and
    # a check that only understands dotted quads would wave them through.
    try:
        return [ipaddress.IPv4Address(socket.inet_aton(host))]
    except OSError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    return [ipaddress.ip_address(info[4][0]) for info in infos]

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def register(story_id: str, url: str) -> None:
    """Mark a story as fetchable. Called as news is served."""
    if not story_id or not url:
        return
    with _LOCK:
        _KNOWN[str(story_id)] = (url, time.time())
        if len(_KNOWN) > _MAX:
            cutoff = time.time() - _TTL
            for key in [k for k, (_, at) in _KNOWN.items() if at < cutoff]:
                _KNOWN.pop(key, None)


def url_for(story_id: str) -> str | None:
    with _LOCK:
        entry = _KNOWN.get(str(story_id))
    if not entry:
        return None
    url, at = entry
    return url if time.time() - at <= _TTL else None


class ArticleError(RuntimeError):
    pass


def _safe(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    # Fails closed: a name nobody can resolve is not a name we will fetch.
    # The article would not have loaded anyway, and the original is always
    # one click away in the reader.
    addresses = _addresses(host)
    return bool(addresses) and all(addr.is_global for addr in addresses)


def fetch(story_id: str, ttl: float = 1800) -> dict:
    """Extracted body text for a story previously served as news."""
    url = url_for(story_id)
    if not url:
        raise ArticleError("unknown article — open it from a news list first")
    if not _safe(url):
        raise ArticleError("refusing to fetch that address")

    try:
        # The same check on every hop. Validating only the address you
        # started with is not a check at all when the server on the other
        # end is allowed to answer "now go and fetch 127.0.0.1 instead".
        raw = net.fetch(url, ttl=ttl, retries=2, timeout=30,
                        headers={"User-Agent": CHROME_UA},
                        redirect_guard=_safe)
    except net.FetchError as exc:
        raise ArticleError(f"could not load the article ({exc.status})") from exc

    paragraphs, title = extract(raw)
    text = "\n\n".join(paragraphs)
    return {
        "id": str(story_id),
        "url": url,
        "host": urlsplit(url).hostname,
        "title": title,
        "paragraphs": paragraphs,
        "chars": len(text),
        # Under a few hundred characters means extraction found headers and
        # cookie banners, not an article. The UI shows the original instead
        # of presenting a stub as the story.
        "partial": len(text) < 400,
        "as_of": time.time(),
    }


def extract(html: bytes) -> tuple[list[str], str]:
    """Body paragraphs and the page title.

    BeautifulSoup arrives transitively with yfinance, so this adds no new
    dependency. If it is somehow absent the caller still gets an empty
    result and a link rather than an exception.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return [], ""

    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else "")
    for junk in soup(["script", "style", "nav", "header", "footer", "aside",
                      "form", "noscript", "iframe", "figure"]):
        junk.decompose()

    node = soup.find("article") or soup.body or soup
    out, seen = [], set()
    for para in node.find_all("p"):
        text = para.get_text(" ", strip=True)
        # Short lines are captions, bylines and disclaimers; duplicates are
        # the same story repeated in a sidebar.
        if len(text) < 60 or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out, title
