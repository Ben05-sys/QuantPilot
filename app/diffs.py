"""What changed in your screens while you weren't looking.

`store.record_run()` and `store.last_runs()` have existed since the first
commit and, until now, were called from nowhere. They are the reason the
snapshot table is append-only: with a record of which symbols matched a
screen at each run, "what entered Momentum since the open" is a set
difference rather than a research project.

This matters most for whoever is asleep during the session. The US market
runs 19:00–01:30 IST; a terminal that can say what happened is worth more
than one that has to be watched.
"""

from __future__ import annotations

import time

from . import screen, store, universe


def record_all(df=None) -> dict:
    """Run every saved screen and record what matched.

    Called after each scheduled refresh. A screen that fails to evaluate —
    a saved expression referencing a field that has since gone away — is
    skipped and named, never allowed to stop the others.
    """
    df = universe.load() if df is None else df
    if df is None or df.empty:
        return {"recorded": 0, "failed": []}

    recorded, failed = 0, []
    for row in store.list_screens():
        try:
            hits = df[screen.mask(df, row["expr"])]
            store.record_run(row["name"], hits["symbol"].dropna().tolist())
            recorded += 1
        except screen.ScreenError as exc:
            failed.append({"name": row["name"], "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            failed.append({"name": row["name"],
                           "error": f"{type(exc).__name__}: {exc}"})
    return {"recorded": recorded, "failed": failed, "at": time.time()}


def for_screen(name: str) -> dict:
    """Entered and exited between the two most recent runs."""
    runs = store.last_runs(name, limit=2)
    if not runs:
        return {"name": name, "entered": [], "exited": [], "runs": 0}
    current = set(runs[0]["symbols"])
    if len(runs) < 2:
        # One run is not a comparison. Reporting every current member as
        # "entered" would be a lie the first time you save a screen.
        return {"name": name, "entered": [], "exited": [],
                "runs": 1, "count": len(current),
                "since": None, "at": runs[0]["run_ts"]}
    previous = set(runs[1]["symbols"])
    return {
        "name": name,
        "entered": sorted(current - previous),
        "exited": sorted(previous - current),
        "count": len(current),
        "runs": len(runs),
        "since": runs[1]["run_ts"],
        "at": runs[0]["run_ts"],
    }


def alerts(limit: int = 40) -> list[dict]:
    """Arrivals worth telling someone about.

    Only entries, not exits: a symbol leaving a screen is rarely news, and
    firing on both would double the noise for half the value. A screen's
    first ever run never alerts — every member would be an "arrival", and
    a notification storm the first time you save a screen is how people
    learn to ignore notifications.
    """
    out = []
    for row in store.list_screens():
        diff = for_screen(row["name"])
        if diff["runs"] < 2 or not diff["entered"]:
            continue
        for symbol in diff["entered"][:limit]:
            out.append({"screen": row["name"], "symbol": symbol,
                        "at": diff["at"]})
    return out[:limit]


def all_screens() -> dict:
    """Every saved screen with its latest diff, busiest first."""
    out = [for_screen(row["name"]) for row in store.list_screens()]
    out.sort(key=lambda d: -(len(d["entered"]) + len(d["exited"])))
    return {"screens": out, "as_of": time.time()}
