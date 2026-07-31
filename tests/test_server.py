#!/usr/bin/env python3
"""Server tests: boot on an ephemeral port and exercise every route.

Runs against a synthetic snapshot in a throwaway home, so the offline
routes are deterministic. The routes that must reach an upstream (quotes,
indices, ticker) are marked LIVE and are reported as skipped rather than
failed when there is no network.

    python tests/test_server.py
"""

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="quantpilot-srv-"))
os.environ["QUANTPILOT_HOME"] = str(TMP)

from app import screen, server, store, universe  # noqa: E402

PASS = FAIL = SKIP = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def skip(label, why):
    global SKIP
    SKIP += 1
    print(f"  skip {label}  ({why})")


SECTORS = ["Technology", "Energy", "Health Care", "Finance"]


def seed():
    """A small universe with enough shape for every filter to bite."""
    rows = []
    for i in range(40):
        rows.append({
            "symbol": f"S{i:02d}", "name": f"Sample Corp {i}",
            "sector": SECTORS[i % 4], "industry": f"Industry {i % 7}",
            "country": "United States", "exchange": "NASDAQ",
            "quote_type": "EQUITY" if i else "ETF", "currency": "USD",
            "price": 10.0 + i * 3, "change": i * 0.05,
            "change_pct": (i % 11) - 4.0,
            "volume": 1e5 * (i + 1), "avg_volume_3m": 5e5,
            "market_cap": 1e8 * (i + 1) ** 2,
            "pe": None if i % 9 == 0 else 8.0 + i,
            "week52_high": 20.0 + i * 3, "week52_low": 5.0 + i,
            "sma50": 9.0 + i * 3, "sma200": 8.0 + i * 3,
            "dividend_yield": (i % 5) * 1.1,
            "eps_ttm": 1.0 + i * 0.1,
            "earnings_ts": None,
        })
    return store.write_snapshot(rows, elapsed=0.1, note="test")


def get(url, token, expect=200):
    req = urllib.request.Request(url, headers={"X-PM-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def post(url, token, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"X-PM-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def main():
    print(f"\nusing {TMP}")
    seed()
    universe.load(force=True)

    srv = server.make_server(port=0)
    port = srv.server_address[1]
    token = srv.RequestHandlerClass.state.token
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"serving on {base}\n")

    print("page and auth")
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        html = r.read().decode("utf-8")
    check("index.html is served", r.status == 200 and "PILOT MARKETS" in html)
    check("the launch token is injected", f'window.PM_TOKEN="{token}"' in html)
    # The page still loads nothing third-party *at rest*. The live news view
    # is the one exception and it is deliberate: an embedded broadcast is
    # the whole feature. It is held to three conditions, all checked here,
    # because "self-contained" quietly becoming "self-contained except for
    # the bits nobody audited" is exactly how a local-only tool stops being
    # one. The iframe is built by script on demand, so opening the terminal
    # and never pressing F10 still reaches no third party.
    markup = html.split("<script")[0]
    check("no external resources in the markup itself",
          'src="http' not in markup and 'href="http' not in markup)
    external = set(re.findall(r'https?://([\w.-]+)', html))
    allowed = {"www.youtube-nocookie.com", "www.gnu.org",
               "github.com", "www.w3.org"}
    check("the only external hosts are the embed, the licence and the source",
          external <= allowed, sorted(external - allowed))
    check("the embed is on the no-cookie host, which sets no cookie "
          "until you press play",
          "youtube-nocookie.com" in html and "//www.youtube.com/embed" not in html)
    check("nothing reaches YouTube until the view is opened",
          'src="https://www.youtube-nocookie.com' not in markup)
    status, body = get(base + "/api/status", "wrong-token")
    check("a bad token is rejected", status == 403, body)
    status, body = get(base + "/api/nope", token)
    check("unknown route 404s", status == 404)

    print("\nstatus and filters")
    status, body = get(base + "/api/status", token)
    check("status 200", status == 200)
    check("status reports the row count", body["rows"] == 40, body.get("rows"))
    check("status reports snapshot age",
          isinstance(body["snapshot_age"], float))
    check("status reports a tick interval", body["tick_seconds"] > 0)

    status, body = get(base + "/api/filters", token)
    check("filters 200", status == 200)
    # Against the spec list, not a number typed here: the point is that the
    # endpoint ships every dropdown, and a literal count only ever fails
    # the next time one is added.
    check("every dropdown is described",
          len(body["filters"]) == len(screen.FILTER_SPECS),
          f'{len(body["filters"])} of {len(screen.FILTER_SPECS)}')
    check("dropdowns start with Any",
          all(f["options"][0] == "Any" for f in body["filters"]))
    check("presets are shipped", len(body["presets"]) >= 5)
    check("grid columns are shipped", "change_pct" in body["columns"])
    check("field vocabulary is shipped", "relvol" in body["fields"])
    check("dollar volume is in the vocabulary", "dvol" in body["fields"])
    check("day range position is in the vocabulary",
          "dayrange" in body["fields"])

    print("\nscreen")
    status, body = get(base + "/api/screen?limit=10", token)
    check("screen 200", status == 200)
    check("respects the limit", len(body["rows"]) == 10, len(body["rows"]))
    check("reports the universe size", body["universe"] == 40)
    check("sorted by market cap desc by default",
          body["rows"][0]["market_cap"] >= body["rows"][1]["market_cap"])
    check("rows are JSON-clean (no NaN)",
          all(r["pe"] is None or isinstance(r["pe"], (int, float))
              for r in body["rows"]))
    # The cursor detail strip answers an arrow key from the row it already
    # holds. It can only do that if these ride along with the screen.
    missing = [f for f in ("day_high", "day_low", "open", "prev_close",
                           "avg_volume_3m", "rel_volume_raw")
               if f not in body["rows"][0]]
    check("rows carry what the detail strip needs", not missing, missing)

    status, body = get(base + "/api/screen?expr=" + "chg%20%3E%202", token)
    check("expression filters", body["total"] < 40 and body["total"] > 0,
          body["total"])
    check("chips describe the expression", body["chips"] == ["chg > 2"],
          body["chips"])

    status, body = get(
        base + "/api/screen?filters=" + '{"cap":"Large %3E10B"}'.replace(
            " ", "%20"), token)
    check("dropdown filters compile server-side",
          status == 200 and "mcap > 10b" in body["expr"], body.get("expr"))

    status, body = get(
        base + "/api/screen?sort=price&dir=asc&limit=3", token)
    check("ascending sort",
          [r["price"] for r in body["rows"]] ==
          sorted(r["price"] for r in body["rows"]))

    status, body = get(base + "/api/screen?limit=5&offset=5", token)
    check("offset pages", body["offset"] == 5 and len(body["rows"]) == 5)

    status, body = get(base + "/api/screen?expr=__import__(%22os%22)", token)
    check("a hostile expression is rejected with 400", status == 400, body)
    check("the rejection is readable",
          "not allowed" in body.get("error", ""), body)
    status, body = get(base + "/api/screen?expr=nosuchfield%3E1", token)
    check("an unknown field is rejected with 400",
          status == 400 and "unknown field" in body.get("error", ""), body)

    print("\nheatmap")
    status, body = get(base + "/api/heatmap?limit=20", token)
    check("heatmap 200", status == 200)
    check("cells carry size and colour",
          all(c["market_cap"] for c in body["cells"]), body["cells"][:1])
    check("cells carry a sector to group by",
          all(c["sector"] for c in body["cells"]))
    check("breadth is summarised",
          body["summary"]["advancing"] + body["summary"]["declining"] > 0,
          body["summary"])
    check("sectors are ranked",
          len(body["summary"]["sectors"]) == 4,
          body["summary"]["sectors"])

    print("\nworld map")
    status, body = get(base + "/api/world", token)
    check("world 200", status == 200)
    check("countries are aggregated",
          len(body["countries"]) == 1, len(body["countries"]))
    check("placed plus unplaced is the universe",
          body["placed"] + body["unplaced"] == body["total"] == 40,
          body)
    c = body["countries"][0]
    check("a country carries a name", c["name"] == "United States")
    check("a country carries a count", c["count"] == 40)
    check("a country carries market cap", c["market_cap"] > 0)
    check("a country carries breadth",
          c["advancing"] + c["declining"] <= c["count"])
    status, body = get(base + "/api/world?scope=adr", token)
    check("adr scope is honoured", body["scope"] == "adr")
    check("adr scope narrows to nothing in this fixture",
          body["placed"] == 0, body["placed"])

    print("\nsaved screens")
    status, body = post(base + "/api/screens", token,
                        {"action": "save", "name": "t1", "expr": "chg > 3"})
    check("save 200", status == 200)
    check("save returns the list",
          any(s["name"] == "t1" for s in body["screens"]))
    status, body = post(base + "/api/screens", token,
                        {"action": "save", "name": "bad",
                         "expr": "__import__('os')"})
    check("a hostile expression is refused before it is stored",
          status == 400, body)
    status, body = get(base + "/api/screens", token)
    check("the refused screen was not stored",
          not any(s["name"] == "bad" for s in body["screens"]))
    status, body = post(base + "/api/screens", token,
                        {"action": "delete", "name": "t1"})
    check("delete works", not any(s["name"] == "t1" for s in body["screens"]))
    status, body = post(base + "/api/screens", token, {"action": "save"})
    check("a nameless save is a 400", status == 400)

    print("\nlive routes (need network)")
    status, body = get(base + "/api/quotes?symbols=AAPL,MSFT", token)
    if status == 200 and body.get("quotes"):
        check("quotes return the symbols asked for",
              set(body["quotes"]) <= {"AAPL", "MSFT"} and body["quotes"])
        q = list(body["quotes"].values())[0]
        check("quotes carry a price", isinstance(q["price"], (int, float)))
        check("quotes carry a measured lag",
              q["lag_seconds"] is None or q["lag_seconds"] >= 0)
    else:
        skip("quotes", body.get("error", f"status {status}"))

    status, body = get(base + "/api/quotes?symbols=", token)
    check("an empty symbol list is not an error",
          status == 200 and body["quotes"] == {})

    status, body = get(base + "/api/indices", token)
    if status == 200 and body.get("indices") and body["indices"][0]["price"]:
        check("index strip is populated", len(body["indices"]) == 10)
        check("indices are labelled",
              body["indices"][0]["label"] == "SPX")
    else:
        skip("indices", body.get("error", "no data"))

    status, body = get(base + "/api/ticker/AAPL?range=1mo&interval=1d", token)
    if status == 200 and body.get("bars", {}).get("t"):
        check("ticker returns bars", len(body["bars"]["t"]) > 10)
        check("bars are aligned",
              len(body["bars"]["t"]) == len(body["bars"]["c"]))
        check("ticker returns a live quote",
              body.get("quote", {}).get("price") is not None)
        check("ticker returns EDGAR filings",
              isinstance(body.get("filings"), list))
    else:
        skip("ticker", body.get("bars_error", f"status {status}"))

    status, body = get(base + "/api/ticker/ZZZZNOTREAL?range=1mo", token)
    check("an unknown symbol is handled, not crashed",
          status == 200 and not body.get("bars", {}).get("t"), status)

    status, body = get(base + "/api/news?symbol=AAPL&limit=5", token)
    if status == 200 and body.get("items"):
        check("news returns stories", len(body["items"]) <= 5)
        first = body["items"][0]
        check("a story has a headline and a link",
              bool(first.get("title")) and bool(first.get("link")))
        check("a story carries its age",
              first.get("age_seconds") is None or first["age_seconds"] >= 0)
        check("stories carry the tickers they are about",
              isinstance(first.get("related"), list))
        check("newest first",
              [i["published"] for i in body["items"]] ==
              sorted((i["published"] for i in body["items"]), reverse=True))
    else:
        skip("news", body.get("error", f"status {status}"))

    status, body = get(base + "/api/news?symbol=", token)
    check("news without a symbol is a 400", status == 400, status)

    print("\nSSE")
    req = urllib.request.Request(
        base + f"/api/events?token={token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            first = r.readline().decode()
            payload = r.readline().decode()
        check("event stream opens", first.strip() == "event: status", first)
        check("the first frame is a status snapshot",
              json.loads(payload.split("data: ", 1)[1])["rows"] == 40)
    except Exception as exc:  # noqa: BLE001
        check("event stream opens", False, str(exc))

    req = urllib.request.Request(base + "/api/events?token=wrong")
    try:
        urllib.request.urlopen(req, timeout=10)
        check("SSE rejects a bad token", False, "it was accepted")
    except urllib.error.HTTPError as exc:
        check("SSE rejects a bad token", exc.code == 403)

    print("\na closed market must not flash")
    # A websocket tick outside regular hours is an extended-hours print on
    # some ATS, not a new last price. Writing it into `price` replaces the
    # closing print and makes the row flash as though the stock just
    # traded — with the badge honestly reading CLOSED beside it.
    state = srv.RequestHandlerClass.state
    tick = {"symbol": "NVDA", "price": 191.5, "change": 1.5,
            "change_pct": 0.79, "time": 1.0, "at": 1.0}

    live = state.shape_tick(tick, "REGULAR")
    check("during the session a tick is the last price",
          live["price"] == 191.5 and live["change_pct"] == 0.79, live)

    for closed in ("PREPRE", "POSTPOST", "POST", "PRE", "CLOSED"):
        shaped = state.shape_tick(tick, closed)
        check(f"{closed}: the tick never becomes a last price",
              "price" not in shaped and "change_pct" not in shaped, shaped)
        check(f"{closed}: it lands in the extended fields instead",
              shaped["ext_price"] == 191.5
              and shaped["ext_change_pct"] == 0.79, shaped)

    # The regression itself: two routes reach the browser — the /api/quotes
    # poll and the SSE broadcast — and only the poll used to apply the rule,
    # so the streaming path flashed all night.
    state._lag_cache.update(at=time.time(), lag=30000.0, state="PREPRE")
    state._on_tick(tick)
    drained = state.drain_ticks()
    check("the broadcast path shapes ticks too, not just the poll",
          "price" not in drained["NVDA"]
          and drained["NVDA"]["ext_price"] == 191.5, drained)
    check("draining twice does not resend a tick", state.drain_ticks() == {})

    # Third route, same rule: /api/subscribe hands back whatever is already
    # ticking, and the client feeds it straight into applyTicks — so
    # scrolling the grid after hours used to flash rows at an ATS price.
    # `at` has to be now: snapshot() drops ticks past the TTL, which is the
    # right behaviour and would otherwise make this check pass vacuously.
    state.stream._ticks["NVDA"] = dict(tick, at=time.time())
    # Re-stamped: lag() only trusts its cache for five seconds, and the
    # checks above take longer than that on a cold provider.
    state._lag_cache.update(at=time.time(), lag=30000.0, state="PREPRE")
    status, body = post(base + "/api/subscribe", token, {"symbols": ["NVDA"]})
    got = (body.get("quotes") or {}).get("NVDA", {})
    check("subscribing after hours returns no last price either",
          status == 200 and "price" not in got and got.get("ext_price") == 191.5,
          got)

    state._lag_cache.update(at=time.time(), lag=1.0, state="REGULAR")
    state._on_tick(tick)
    check("and it still passes real prices through when the market is open",
          state.drain_ticks()["NVDA"]["price"] == 191.5)
    state.stream._ticks["NVDA"] = dict(tick, at=time.time())
    state._lag_cache.update(at=time.time(), lag=1.0, state="REGULAR")
    status, body = post(base + "/api/subscribe", token, {"symbols": ["NVDA"]})
    check("as does subscribing during the session",
          (body.get("quotes") or {}).get("NVDA", {}).get("price") == 191.5,
          body.get("quotes"))

    print("\nrefresh guard")
    state.refresh_lock.acquire()
    status, body = post(base + "/api/refresh", token, {})
    check("a second refresh is refused while one runs", status == 409, body)
    state.refresh_lock.release()

    srv.shutdown()
    print(f"\n{PASS} passed, {FAIL} failed, {SKIP} skipped\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    raise SystemExit(code)
