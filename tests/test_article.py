"""The article reader's address check, which is the whole security story.

The reader takes a story id and looks the URL up, so the obvious attack —
handing it an address — is already closed. What is left is subtler and is
what this file is about: the URL comes from a third-party news feed, and
the server on the other end of it gets to answer "go and fetch this other
address instead". A check that only inspects the address you started with
is not a check at all under those conditions.

No network. Address literals are used everywhere a public host is needed,
so nothing here depends on DNS, and the one unresolvable case uses .invalid
(RFC 2606), which is guaranteed never to resolve.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["QUANTPILOT_HOME"] = tempfile.mkdtemp(prefix="qp-article-")

from app import article, net  # noqa: E402

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


class FakeResponse:
    def __init__(self, status, location=None, body=b"<html>ok</html>"):
        self.status_code = status
        self.headers = {"location": location} if location else {}
        self.content = body
        self.text = body.decode("utf-8", "replace")
        self.url = ""

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308)


class FakeSession:
    """Replays a scripted list of responses and records where it was sent."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.visited = []

    def get(self, url, **kw):
        self.visited.append(url)
        return self._responses.pop(0)


class NullCache:
    def get(self, url, ttl):
        return None

    def put(self, url, body):
        pass


def main():
    print("addresses that must never be fetched")
    # Every one of these is this machine, this network, or the metadata
    # service — in the spellings an attacker would actually reach for.
    for url in [
        "http://127.0.0.1:8900/api/status",     # the terminal's own API
        "http://2130706433/",                   # 127.0.0.1 as an integer
        "http://0177.0.0.1/",                   # octal
        "http://127.1/",                        # short form
        "http://[::1]/",                        # IPv6 loopback
        "http://[::ffff:127.0.0.1]/",           # IPv4-mapped IPv6
        "http://localhost/x",
        "http://something.localhost/x",
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/",
        "http://0.0.0.0/",
        "http://100.64.0.1/",                   # carrier-grade NAT
        "http://[fc00::1]/",                    # IPv6 unique local
        "http://[fe80::1]/",                    # IPv6 link local
        "file:///etc/passwd",                   # not even http
        "ftp://example.com/x",
        "http://no-such-host.invalid/x",        # unresolvable: fails closed
    ]:
        check(f"refuses {url}", not article._safe(url))

    print("\naddresses that are ordinary news")
    for url in ["https://93.184.216.34/story.html", "http://8.8.8.8/x",
                "https://[2606:4700:4700::1111]/a"]:
        check(f"allows {url}", article._safe(url))

    print("\nredirects are checked on every hop, not just the first")
    saved_session, saved_cache = net.session, net.cache
    try:
        net.cache = lambda: NullCache()

        # The feed's URL is a real publisher; the publisher answers with a
        # redirect to the terminal's own API. Following that would make the
        # reader a proxy for anything on this machine.
        sess = FakeSession([FakeResponse(302, "http://127.0.0.1:8900/api/screen")])
        net.session = lambda: sess
        try:
            net.fetch("https://93.184.216.34/story.html", ttl=0, retries=1,
                      redirect_guard=article._safe)
            check("a redirect to loopback is refused", False, "it was followed")
        except net.FetchError as exc:
            check("a redirect to loopback is refused", True)
            check("and the error names the address it would not follow",
                  "127.0.0.1" in str(exc.url), exc.url)
        check("nothing was fetched beyond the first hop",
              sess.visited == ["https://93.184.216.34/story.html"], sess.visited)

        # Publishers legitimately redirect — http to https, amp to canonical.
        # Those still have to work, or the reader is useless.
        sess = FakeSession([
            FakeResponse(301, "https://93.184.216.34/story"),
            FakeResponse(200, body=b"<html>the article</html>"),
        ])
        net.session = lambda: sess
        body = net.fetch("http://93.184.216.34/story", ttl=0, retries=1,
                         redirect_guard=article._safe)
        check("an ordinary redirect is still followed", b"the article" in body)
        check("both hops were requested", len(sess.visited) == 2, sess.visited)

        # A chain that never lands is a chain that has to be cut.
        sess = FakeSession([FakeResponse(302, f"https://8.8.8.{n}/x")
                            for n in range(1, 12)])
        net.session = lambda: sess
        try:
            net.fetch("https://8.8.8.8/x", ttl=0, retries=1,
                      redirect_guard=article._safe)
            check("an endless redirect chain is cut", False, "it kept going")
        except net.FetchError:
            check("an endless redirect chain is cut", True)
        check("and cut at the documented limit",
              len(sess.visited) <= net.MAX_REDIRECTS + 1, sess.visited)
    finally:
        net.session, net.cache = saved_session, saved_cache

    print("\nthe SEC contact address goes to the SEC and nowhere else")
    check("sec.gov is the SEC", net._is_sec("sec.gov"))
    check("so is a subdomain", net._is_sec("www.sec.gov"))
    check("notsec.gov is not", not net._is_sec("notsec.gov"))
    check("nor is sec.gov.example.com", not net._is_sec("sec.gov.example.com"))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
