#!/usr/bin/env python3
"""Freeze a running terminal into a static page anyone can open.

    python tools/build_demo.py            # ~90s of tape, during US hours
    python tools/build_demo.py --seconds 30 --no-tape

QuantPilot is loopback-only and always will be, so nobody can look at it
without installing Python first. This builds the next best thing: the real
`app/web/index.html`, unmodified, with the network replaced by fixtures
captured from a real server and a recording of the real tape played back
underneath it. Sorting, scrolling, the heatmap, the ticker view and the
prices moving are all genuinely working — they are just working against a
recording.

**Frozen data only.** Yahoo's terms prohibit automated access, which is
defensible for one person's local terminal and is not defensible for
something served from a public URL. A recording of one afternoon, stamped
with its date and labelled a recording on the page, is a screenshot that
moves — not a data service. There is no live call anywhere in the output;
`window.fetch` is replaced outright, so a missing fixture fails visibly
here rather than quietly reaching Yahoo from a stranger's browser.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import screen, server, stream, universe  # noqa: E402

OUT = ROOT / "docs" / "demo"
WEB = ROOT / "app" / "web" / "index.html"

# Rows the grid is handed. It asks for 2,000 in one call and virtualizes
# the rest, so this is the whole screener as far as the page is concerned.
ROWS = 2000

# Symbols deep enough into the fixture to get a ticker view and news —
# roughly the first screenful, so clicking a visible row lands on a chart.
# The tape covers far more; these are the ones with a year of bars behind
# them, and each costs about 20KB of the download.
DETAIL = 30

# How much of the universe the recorded tape subscribes to. The grid shows
# forty rows at a time, but scrolling should keep ticking.
TAPE_SYMBOLS = 300


def get(base: str, token: str, path: str, timeout: int = 90):
    req = urllib.request.Request(base + path, headers={"X-PM-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"    ! {path} -> {type(exc).__name__}: {exc}")
        return None


def record_tape(symbols: list[str], seconds: float) -> tuple[list[dict], float | None]:
    """Listen to the real feed and write down what came out.

    Stored as offsets from the first print rather than wall-clock times,
    so the replay can loop without a discontinuity at the seam.
    """
    def note(t: dict) -> None:
        """One print, as small as it can be and still be the same print.

        Short keys and no nulls: this file is committed, and a tape of
        several thousand prints written out longhand is most of the
        download for someone who only wants to look at the thing.
        """
        e = {"t": t["at"], "s": t["symbol"], "p": round(t["price"], 4)}
        for key, short, digits in (("change", "c", 4),
                                   ("change_pct", "cp", 4),
                                   ("volume", "v", 0),
                                   ("day_high", "h", 4),
                                   ("day_low", "l", 4)):
            v = t.get(key)
            if v is not None:
                e[short] = round(v, digits) if digits else int(v)
        events.append(e)

    events: list[dict] = []
    ts = stream.TickStream(on_tick=note)
    if not ts.available:
        print("    ! yfinance/websockets missing — no tape")
        return [], None
    ts.subscribe(symbols[:TAPE_SYMBOLS])
    ts.start()

    # The clock starts at the first print, not at connect. Yahoo can take
    # the better part of a minute to hand over the socket, and counting
    # that as tape produced a recording that was two thirds dead air —
    # a demo that sat motionless for as long as anyone would look at it.
    print("    waiting for the first print...", end="\r")
    opened = time.time()
    while not events and time.time() - opened < 120:
        time.sleep(0.5)
    if not events:
        ts.stop()
        print("    ! no prints in two minutes — is the market open?")
        return [], None

    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(1.0)
        print(f"    {len(events):5d} prints, "
              f"{deadline - time.time():4.0f}s left   ", end="\r")
    latency = ts.latency()
    ts.stop()

    base = events[0]["t"]
    for e in events:
        e["t"] = round(e["t"] - base, 3)
    covered = len({e["s"] for e in events})
    lag = f", feed {latency:.1f}s behind" if latency else ""
    print(f"    {len(events)} prints over {events[-1]['t']:.0f}s across "
          f"{covered} symbols{lag}          ")
    return events, latency


def alias_map(df, rows: list[dict]) -> dict:
    """Every field name the command line accepts -> a key the rows carry.

    Resolved through the real engine rather than transcribed, so the demo
    understands exactly the vocabulary the terminal does. Two wrinkles it
    has to absorb:

      - `resolve` accepts anything in COLUMNS, but the grid payload is a
        subset of the frame — `atr_pct` is a column and is not shipped.
        Names that land outside the payload are dropped, and the shim says
        so rather than returning an empty screen as though nothing matched.
      - the frame carries alias *columns* (`chg` beside `change_pct`), and
        those are not shipped either. Rather than hard-coding the pairs,
        they are found by asking which shipped column holds the same
        values — so a new alias column needs no change here.
    """
    if not rows:
        return {}
    keys = set(rows[0])
    out: dict[str, str] = {}
    duplicate: dict[str, str] = {}
    for name in sorted(set(screen.COLUMNS) | set(screen.ALIASES)):
        try:
            canon = screen.resolve(name)
        except Exception:  # noqa: BLE001 — not every alias is a column
            continue
        if canon in keys:
            out[name] = canon
            continue
        if canon not in duplicate:
            duplicate[canon] = ""
            if canon in df.columns:
                for other in keys:
                    if other in df.columns and df[canon].equals(df[other]):
                        duplicate[canon] = other
                        break
        if duplicate[canon]:
            out[name] = duplicate[canon]
    return out


def build(seconds: float, tape: bool) -> None:
    df = universe.load(force=True)
    if df.empty:
        sys.exit("no universe snapshot — run `python pilot.py --refresh` first")
    print(f"universe: {len(df):,} symbols")

    srv = server.make_server(port=0)
    port = srv.server_address[1]
    token = srv.RequestHandlerClass.state.token
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"capturing from {base}")

    fixtures: dict[str, object] = {}

    def capture(name: str, path: str, timeout: int = 90):
        print(f"  {name}")
        payload = get(base, token, path, timeout)
        if payload is not None:
            fixtures[name] = payload
        return payload

    capture("filters", "/api/filters")
    status = capture("status", "/api/status")

    # The screen the page opens on, live-priced — this is the one request
    # whose answer the demo has to be able to vary, so it is captured with
    # every column the grid can sort by and re-sorted in the browser.
    rows = capture("screen",
                   f"/api/screen?expr=&filters=%7B%7D&sort=market_cap"
                   f"&dir=desc&limit={ROWS}&offset=0", timeout=300)

    capture("heatmap", "/api/heatmap?expr=&filters=%7B%7D&limit=500")
    capture("world", "/api/world")
    capture("indices", "/api/indices")
    capture("futures", "/api/futures")
    capture("earnings", "/api/earnings?days=7")
    capture("market", "/api/market")
    capture("screens", "/api/screens")
    capture("diffs", "/api/diffs")
    capture("livenews", "/api/livenews")
    capture("watchlist", "/api/watchlist")

    symbols = [r["symbol"] for r in ((rows or {}).get("rows") or [])]
    if not symbols:
        sys.exit("the screen came back empty — nothing to build a demo from")

    tickers, news = {}, {}
    for sym in symbols[:DETAIL]:
        print(f"  ticker {sym}")
        d = get(base, token, f"/api/ticker/{sym}?range=1y&interval=1d")
        if d:
            tickers[sym] = d
        n = get(base, token, f"/api/news?symbol={sym}&count=8")
        if n:
            news[sym] = n
    fixtures["tickers"] = tickers
    fixtures["news"] = news

    recorded, tape_latency = (record_tape(symbols, seconds) if tape
                              else ([], None))

    captured_at = time.time()
    meta = {
        "captured_at": captured_at,
        "captured_label": datetime.fromtimestamp(
            captured_at, UTC).strftime("%d %b %Y, %H:%M UTC"),
        "market_state": (status or {}).get("market_state"),
        "session": (status or {}).get("session"),
        "rows": len((rows or {}).get("rows") or []),
        "universe": (status or {}).get("rows"),
        "tape_prints": len(recorded),
        "tape_seconds": round(recorded[-1]["t"], 1) if recorded else 0,
        # Measured over the recording itself, not read off the status
        # fixture — that one is captured before the socket has delivered
        # a single print, so it is always null and the badge would show
        # STREAM with no number where the honest answer exists.
        "stream_latency": tape_latency,
        # Everything the browser-side screener needs to understand an
        # expression, taken from the engine rather than restated: a second
        # copy of the alias table would drift and start quietly answering
        # a different question than the real one.
        "aliases": alias_map(df, (rows or {}).get("rows") or []),
        "text_columns": sorted(screen.TEXT_COLUMNS),
        "suffixes": screen._MULT,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "data.js").write_text(
        "window.DEMO_DATA=" + json.dumps(
            {"meta": meta, "fixtures": fixtures, "tape": recorded},
            separators=(",", ":"), default=str) + ";\n",
        encoding="utf-8")

    shutil.copy(ROOT / "tools" / "demo_shim.js", OUT / "shim.js")

    html = WEB.read_text(encoding="utf-8")
    html = html.replace(
        "<head>",
        '<head>\n<!-- Built by tools/build_demo.py. The page below is '
        'app/web/index.html verbatim; these two scripts stand in for the '
        'server. -->\n'
        '<script src="data.js"></script>\n<script src="shim.js"></script>',
        1)
    (OUT / "index.html").write_text(html, encoding="utf-8")

    total = sum(p.stat().st_size for p in OUT.glob("*"))
    print(f"\n{OUT}")
    for p in sorted(OUT.glob("*")):
        print(f"  {p.name:12s} {p.stat().st_size/1024:8.0f} KB")
    print(f"  {'total':12s} {total/1024:8.0f} KB")
    print(f"\ncaptured {meta['captured_label']} · {meta['rows']} rows · "
          f"{meta['tape_prints']} prints over {meta['tape_seconds']}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=90.0,
                    help="how much tape to record (default 90)")
    ap.add_argument("--no-tape", action="store_true",
                    help="skip the recording; prices will not move")
    args = ap.parse_args()
    build(args.seconds, not args.no_tape)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
