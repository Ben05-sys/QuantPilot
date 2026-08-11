"""Settings and the on-disk layout.

Follows the house convention: ~/.<appname>/ with JSON for small mutable
state and SQLite for anything bulk. Ports 8765 and 8790 already belong to
OfflinePilotX and AskMyFiles, so we take 8900.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

APP_NAME = "QuantPilot"

# The project was called PilotMarkets until 2026-07-31. The old data
# directory holds the append-only snapshot history, which is the one thing
# here that cannot be rebuilt — it accumulates a generation at a time and
# there is no way to back-fill a day you did not record. So the rename
# carries it across instead of silently starting from an empty database.

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8900,
    # How often the browser asks for fresh quotes on visible rows. This is
    # the fallback cadence — while the websocket is healthy prices arrive
    # pushed, and the poll only covers rows the stream has gone quiet on.
    # Three seconds against a two-second server-side cache, so a poll
    # nearly always costs a real round trip rather than returning what it
    # returned last time.
    "tick_seconds": 3,
    # Backoff once the exchange reports it is closed.
    "closed_tick_seconds": 60,
    # A universe snapshot older than this is considered stale on boot.
    "universe_max_age_hours": 12,
    # Contact address sent to SEC in the User-Agent. They require a real
    # one; a browser User-Agent gets you a 403.
    #
    # Deliberately empty in the source. This string is transmitted to a
    # third party on every EDGAR request, so whoever is running the copy
    # has to put their own address in — a hard-coded one would quietly
    # identify the author on someone else's machine, and any rate-limit
    # complaint would land on the wrong person. Set it in
    # ~/.quantpilot/config.json or via QUANTPILOT_SEC_CONTACT.
    # Everything except the SEC filings panel works without it.
    "sec_contact": "",
    # Rows returned to the grid per screen run. The grid virtualizes, but
    # there is no point shipping 7000 rows over the wire.
    "screen_limit": 500,
    "provider": "auto",
}


def data_dir() -> Path:
    """~/.quantpilot, migrating ~/.pilotmarkets into it once if it is there.

    `PILOTMARKETS_HOME` is still honoured after `QUANTPILOT_HOME` so an
    existing install, script or test keeps working through the rename.
    """
    override = os.environ.get("QUANTPILOT_HOME") or os.environ.get(
        "PILOTMARKETS_HOME")
    if override:
        d = Path(override)
        d.mkdir(parents=True, exist_ok=True)
        return d

    d = Path.home() / ".quantpilot"
    legacy = Path.home() / ".pilotmarkets"
    # Only when the new one does not exist yet: if both are present the
    # user made the new one deliberately and moving over it would clobber
    # whichever is newer. Idempotent — after the first run `d` exists and
    # this is one `exists()` call.
    if not d.exists() and legacy.is_dir():
        try:
            shutil.move(str(legacy), str(d))
            print(f"[config] moved {legacy} -> {d} (project renamed)")
        except OSError as exc:
            # Better to run against the old directory than to lose the
            # history or refuse to start.
            print(f"[config] could not migrate {legacy}: {exc}")
            legacy.mkdir(parents=True, exist_ok=True)
            return legacy
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "market.db"


def cache_path() -> Path:
    return data_dir() / "httpcache.db"


def config_path() -> Path:
    return data_dir() / "config.json"


def load_config() -> dict:
    """Defaults <- config.json <- environment. A broken config file is
    reported and ignored rather than being fatal."""
    cfg = dict(DEFAULTS)
    p = config_path()
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[config] ignoring unreadable {p}: {exc}")
    for key in cfg:
        env = os.environ.get(f"QUANTPILOT_{key.upper()}")
        if env is None:
            env = os.environ.get(f"PILOTMARKETS_{key.upper()}")
        if env is not None:
            cfg[key] = (env == "1" if isinstance(cfg[key], bool)
                        else type(cfg[key])(env))
    return cfg


def save_config(cfg: dict) -> None:
    config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")
