#!/usr/bin/env python3
"""Storage tests against a throwaway database.

QUANTPILOT_HOME is redirected before app.config is imported, so this
never touches the real ~/.quantpilot.

    python tests/test_store.py
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="quantpilot-test-"))
os.environ["QUANTPILOT_HOME"] = str(TMP)

from app import screen, store  # noqa: E402

PASS = FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def rows(n, base=100.0):
    return [{"symbol": f"T{i}", "name": f"Test {i}", "sector": "Technology",
             "price": base + i, "change_pct": i * 0.1,
             "market_cap": 1e9 * (i + 1), "volume": 1e6}
            for i in range(n)]


def main():
    print(f"\nusing {TMP}")

    print("\nsnapshots")
    check("no snapshot on a fresh database",
          store.latest_snapshot_ts() is None)
    ts1 = store.write_snapshot(rows(5), elapsed=1.5, note="first")
    check("write returns a timestamp", isinstance(ts1, float) and ts1 > 0)
    check("latest points at it", store.latest_snapshot_ts() == ts1)
    meta = store.snapshot_meta()
    check("meta records the row count", meta["rows"] == 5, meta)
    check("meta records the note", meta["note"] == "first")

    read = store.read_snapshot()
    check("round-trips every row", len(read) == 5)
    check("round-trips values",
          sorted(r["symbol"] for r in read) ==
          ["T0", "T1", "T2", "T3", "T4"])
    check("preserves floats", abs(read[0]["market_cap"] - 1e9) < 1)

    time.sleep(0.01)
    ts2 = store.write_snapshot(rows(3, base=200.0), note="second")
    check("snapshots are append-only, not overwritten",
          len(store.snapshot_list()) == 2)
    check("latest moves to the new generation",
          store.latest_snapshot_ts() == ts2)
    check("the old generation is still readable",
          len(store.read_snapshot(ts1)) == 5)
    check("point-in-time values are the old ones",
          store.read_snapshot(ts1)[0]["price"] == 100.0)

    for _ in range(4):
        time.sleep(0.005)
        store.write_snapshot(rows(2))
    check("six generations stored", len(store.snapshot_list()) == 6)
    store.prune_snapshots(keep=2)
    check("prune keeps exactly N", len(store.snapshot_list()) == 2,
          len(store.snapshot_list()))
    check("prune drops the pruned rows too",
          store.read_snapshot(ts1) == [])

    print("\nsaved screens")
    store.save_screen("Momo", "chg > 3 and relvol > 2")
    check("saved", len(store.list_screens()) == 1)
    check("readable by name",
          store.get_screen("Momo")["expr"] == "chg > 3 and relvol > 2")
    store.save_screen("Momo", "chg > 5")
    check("re-saving updates in place",
          len(store.list_screens()) == 1
          and store.get_screen("Momo")["expr"] == "chg > 5")
    store.record_run("Momo", ["AAPL", "NVDA"])
    time.sleep(0.01)
    store.record_run("Momo", ["NVDA", "TSLA"])
    runs = store.last_runs("Momo", limit=2)
    check("runs recorded newest first",
          runs[0]["symbols"] == ["NVDA", "TSLA"], runs)
    check("membership diff is computable",
          set(runs[0]["symbols"]) - set(runs[1]["symbols"]) == {"TSLA"})
    store.delete_screen("Momo")
    check("delete removes the screen", store.get_screen("Momo") is None)
    check("delete removes its runs", store.last_runs("Momo") == [])

    print("\npresets")
    added = screen.seed_presets()
    check("presets seeded", added == len(screen.PRESETS), added)
    check("seeding twice adds nothing", screen.seed_presets() == 0)
    store.save_screen(screen.PRESETS[0][0], "chg > 99")
    screen.seed_presets()
    check("seeding never clobbers an edited screen",
          store.get_screen(screen.PRESETS[0][0])["expr"] == "chg > 99")

    print("\npositions")
    store.upsert_position("nvda", 10, 120.5, note="core")
    check("symbol is normalized to upper case",
          store.positions()[0]["symbol"] == "NVDA")
    store.upsert_position("NVDA", 25, 130.0)
    check("upsert updates rather than duplicating",
          len(store.positions()) == 1 and store.positions()[0]["shares"] == 25)
    store.delete_position("NVDA")
    check("delete works", store.positions() == [])

    print("\nnothing personal ships in the source")
    # This string is transmitted to SEC on every EDGAR request. A hard-coded
    # address would identify whoever built the copy, on someone else's
    # machine, and any rate-limit complaint would reach the wrong person.
    from app import config as appconfig
    from app import net
    from app.providers.base import ProviderError
    default = appconfig.DEFAULTS["sec_contact"]
    check("the shipped default carries no address", default == "", default)
    check("and no e-mail is hiding anywhere else in DEFAULTS",
          not [v for v in appconfig.DEFAULTS.values()
               if isinstance(v, str) and "@" in v],
          appconfig.DEFAULTS)

    try:
        net.sec_ua()
        check("an unset contact refuses to build a User-Agent", False,
              "it built one")
    except net.ContactRequired as exc:
        check("an unset contact refuses to build a User-Agent", True)
        check("and the message says where to put it",
              "sec_contact" in str(exc) and "config.json" in str(exc), exc)
    # Subclassing FetchError is what turns this into a message in the
    # filings panel instead of a traceback, at every existing call site.
    check("it degrades to a clean provider error, not a crash",
          issubclass(net.ContactRequired, net.FetchError))
    try:
        sec_map_error = ""
        from app.providers import sec as sec_provider
        sec_provider.ticker_map()
    except ProviderError as exc:
        sec_map_error = str(exc)
    except Exception as exc:  # noqa: BLE001
        sec_map_error = f"RAW {type(exc).__name__}"
    check("an SEC call without a contact reports rather than raises",
          "contact address" in sec_map_error, sec_map_error)

    print(f"\n{PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        try:
            store.conn().close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(TMP, ignore_errors=True)
    raise SystemExit(code)
