"""Settings and the on-disk layout.

Follows the house convention: ~/.<appname>/ with JSON for small mutable
state and SQLite for anything bulk. Ports 8765 and 8790 already belong to
OfflinePilotX and AskMyFiles, so we take 8900.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "PilotMarkets"

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8900,
    # How often the browser asks for fresh quotes on visible rows.
    "tick_seconds": 5,
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
    # ~/.pilotmarkets/config.json or via PILOTMARKETS_SEC_CONTACT.
    # Everything except the SEC filings panel works without it.
    "sec_contact": "",
    # Rows returned to the grid per screen run. The grid virtualizes, but
    # there is no point shipping 7000 rows over the wire.
    "screen_limit": 500,
    "provider": "auto",
}


def data_dir() -> Path:
    d = Path(os.environ.get("PILOTMARKETS_HOME", Path.home() / ".pilotmarkets"))
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
        env = os.environ.get(f"PILOTMARKETS_{key.upper()}")
        if env is not None:
            cfg[key] = type(cfg[key])(env) if not isinstance(cfg[key], bool) else env == "1"
    return cfg


def save_config(cfg: dict) -> None:
    config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")
