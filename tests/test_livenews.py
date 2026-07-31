"""Live news: channel probing and, mostly, ticker extraction.

The extraction is the part worth testing hard. It reads free text and
asserts which companies it is about, and the failure that matters is not
missing a mention — it is confidently tagging the wrong stock, because a
rail that calls "the obesity data" a semiconductor story stops being worth
reading. So most of what follows is about what must *not* match.

No network: `channels()` is exercised against a stubbed fetch. The probe's
job is to be honest about who is on air, including when the answer is
nobody.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["QUANTPILOT_HOME"] = tempfile.mkdtemp(prefix="qp-livenews-")

import pandas as pd  # noqa: E402

from app import livenews  # noqa: E402

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def frame():
    return pd.DataFrame({
        "symbol": ["NVDA", "MSFT", "AAPL", "AMZN", "LLY", "NVO", "AMD",
                   "INTC", "DAIO", "MASS", "OPEN", "TSLA"],
        "name": ["NVIDIA Corporation Common Stock",
                 "Microsoft Corporation Common Stock",
                 "Apple Inc. Common Stock",
                 "Amazon.com Inc. Common Stock",
                 "Eli Lilly and Company Common Stock",
                 "Novo Nordisk A/S Common Stock",
                 "Advanced Micro Devices Inc. Common Stock",
                 "Intel Corporation Common Stock",
                 "Data I/O Corporation Common Stock",
                 "908 Devices Inc. Common Stock",
                 "Opendoor Technologies Inc. Common Stock",
                 "Tesla Inc. Common Stock"],
        "change_pct": [2.6, 15.5, -0.4, 1.1, -4.5, 0.1, 3.3,
                       -1.2, -1.7, 0.8, 9.9, 3.5],
        "price": [100.0] * 12,
    })


def syms(text, df=None):
    return sorted(h["symbol"] for h in livenews.mentioned(text, df or frame()))


def main():
    print("company names resolve to tickers")
    check("a plain name", syms("Nvidia beat on earnings") == ["NVDA"])
    check("two in one sentence",
          syms("Nvidia beat and Microsoft guided higher") == ["MSFT", "NVDA"])
    check("a two-word name", syms("Eli Lilly raised guidance") == ["LLY"])
    check("a foreign name", syms("Novo Nordisk fell on the data") == ["NVO"])
    check("a dot-com name resolves on the word people say",
          syms("Amazon reports after the close") == ["AMZN"])
    check("a three-word name", syms("Advanced Micro Devices took share")
          == ["AMD"])

    print("\ncashtags")
    check("$TSLA", syms("watching $TSLA into the print") == ["TSLA"])
    check("a cashtag resolves a name that plain text must not",
          syms("$OPEN squeezed") == ["OPEN"])
    check("lowercase cashtag still resolves", syms("$nvda ripping") == ["NVDA"])

    print("\nwhat must NOT match — the whole point")
    # Every one of these tagged the wrong stock before the guard existed.
    check("an ordinary word that is also a company",
          syms("he was open to a deal") == [], syms("he was open to a deal"))
    check("'data' is not Data I/O",
          syms("they moved on the obesity data") == [],
          syms("they moved on the obesity data"))
    check("a generic noun inside a longer name is not its own company",
          "MASS" not in syms("Advanced Micro Devices took share"),
          syms("Advanced Micro Devices took share"))
    check("a bare uppercase word is not a ticker",
          syms("the CEO said OPEN was ON the table") == [],
          syms("the CEO said OPEN was ON the table"))
    check("an empty string", syms("") == [])
    check("text about nothing", syms("no companies here at all") == [])

    print("\ndegrades rather than raises")
    check("a None frame", livenews.mentioned("Nvidia", None) == [])
    check("an empty frame", livenews.mentioned("Nvidia", pd.DataFrame()) == [])
    check("a frame with no name column",
          livenews.mentioned("Nvidia", pd.DataFrame({"symbol": ["NVDA"]})) == [])

    print("\nwhat comes back")
    hits = livenews.mentioned("Nvidia and Eli Lilly", frame())
    check("the mover leads", hits[0]["symbol"] == "LLY",
          [h["symbol"] for h in hits])          # -4.5 outranks +2.6
    check("how it was found is reported",
          {h["how"] for h in hits} == {"name"})
    check("the move rides along", hits[0]["change_pct"] == -4.5)
    nan = frame()
    nan.loc[0, "change_pct"] = float("nan")
    got = livenews.mentioned("Nvidia", nan)
    check("NaN stays null rather than becoming zero",
          got[0]["change_pct"] is None, got)

    print("\ncompany roots")
    check("suffixes are stripped",
          livenews._company_root("Microsoft Corporation Common Stock")
          == "microsoft")
    check("dot-com is stripped",
          livenews._company_root("Amazon.com Inc.") == "amazon")
    check("a two-word root survives",
          livenews._company_root("Eli Lilly and Company") == "eli lilly")

    print("\nchannel probing")
    live_html = '"isLiveNow":true "videoId":"abcdefghijk" ' \
                '<meta name="title" content="Markets Live"> "viewCount":"4200"'
    livenews.net.fetch = lambda *a, **k: live_html.encode()
    d = livenews.channels()
    check("every channel is reported", len(d["channels"]) == len(livenews.CHANNELS))
    check("all live when the page says so", d["live_count"] == len(livenews.CHANNELS))
    check("a default is chosen", d["default"] is not None)
    check("the video id is picked up",
          d["channels"][0]["video_id"] == "abcdefghijk")
    check("viewers are picked up", d["channels"][0]["viewers"] == 4200)

    livenews.net.fetch = lambda *a, **k: b"just a channel page, nobody on air"
    d = livenews.channels()
    check("nothing live is a real answer, not an error", d["live_count"] == 0)
    check("and there is no default to open",
          d["default"] is None, d["default"])

    def boom(*a, **k):
        raise RuntimeError("upstream down")
    livenews.net.fetch = boom
    d = livenews.channels()
    check("a dead upstream does not take the view down",
          d["live_count"] == 0 and len(d["channels"]) == len(livenews.CHANNELS))
    check("and the reason is reported rather than swallowed",
          d["channels"][0]["error"] == "RuntimeError",
          d["channels"][0]["error"])

    print("\nembed url")
    u = livenews.embed_url("abcdefghijk")
    check("points at the official embed on the no-cookie host",
          u.startswith("https://www.youtube-nocookie.com/embed/abcdefghijk"), u)
    check("never the cookie-setting host", "//www.youtube.com/" not in u, u)
    check("does not autoplay", "autoplay" not in u, u)

    print(f"\n{PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
