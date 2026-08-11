# CLAUDE.md

Guidance for Claude Code working in this repository. The README explains
what QuantPilot is and `CONTRIBUTING.md` explains the house style; this
file is the short list of things that have actually gone wrong.

## Before you commit, run the linter

```bash
python -m ruff check .
```

**This is not optional and it is the single most common way this repo
breaks.** CI runs `ruff check .` as a required job over the whole tree,
`tests/` included, and a failure there turns the branch red even when all
six test matrix jobs pass. Between 7 and 11 August 2026 `main` was red
five days running for exactly this, every time a line over 88 columns in
a test file — and because Dependabot cuts its branches from `main`, every
open Dependabot PR went red too and looked like a dependency problem it
had nothing to do with.

Wrap at **79 columns** like the code around you. **88 is the ceiling CI
enforces**, and the gap is room for the occasional line that reads worse
broken than long — not a licence to stop wrapping. The long lines that
keep breaking it are `check("...")` descriptions in the test suites; a
description that does not fit is two string literals, not one long one.

There is deliberately **no formatter**. Do not run `ruff format` — the
source is hand-wrapped so comments sit against the lines they explain,
and reformatting would bury the history in one unreviewable commit.

## Run the tests

Plain scripts, no pytest. All of them are offline except where noted.

```bash
python tests/test_screen.py      python tests/test_store.py
python tests/test_clock.py       python tests/test_diffs.py
python tests/test_options.py     python tests/test_calendars.py
python tests/test_article.py     python tests/test_livenews.py
python tests/test_server.py      python tests/test_stream.py
node   tests/ui_smoke.js         # boots the page in jsdom
```

`tests/ui_smoke.js` is the only thing that catches a runtime throw at page
load, which renders a dead UI with no error anywhere else. Run it after
touching `app/web/index.html`.

## Pull first

A scheduled agent commits to `main` daily. Start with `git pull` or you
will be rebasing.

## The rules that make this thing trustworthy

Break one and the terminal stops being worth running:

- **Never claim live over stale data.** Latency is *measured* per response
  and rendered — `LAG 4s`, `LAG 12m`, `CLOSED`. The `STREAM` badge says
  `STREAM` only while ticks are genuinely arriving. A grid that has
  silently stopped updating while still labelled live is worse than one
  admitting it polls.
- **A field we cannot compute stays null.** Never zero, never a guess. An
  em dash is an answer; `0.00%` on a stock that pays a dividend is a lie.
  This has bitten twice: a special dividend whose indicated annual rate is
  `0`, and a stock with nothing scheduled inside the calendar window.
- **`LAST` is never overwritten by an extended-hours print.** Every route
  to the browser goes through `AppState.shape_tick`. If you add a fourth,
  it goes through there too.
- **Expressions are parsed with `ast` under an allowlist.** Never `eval`,
  never `DataFrame.query`.

## Two things that look like obvious optimisations and are not

- **The corporate calendar does not belong in the universe rebuild.**
  Nasdaq's dividend calendar is one call *per exchange day* and `net.py`
  limits that host to 2/s, so a six-week window costs more than a whole
  rebuild. It refreshes on its own thread in `app/calendars.py`.
- **`docs/demo/` is never built in CI.** It is a build artifact, produced
  locally by `tools/build_demo.py` during market hours and committed; the
  Pages workflow only copies it. Building it on a runner would put
  GitHub's infrastructure on Yahoo's undocumented endpoints on a schedule,
  which is the thing the README's caveat rules out.

## Yahoo's terms are the ceiling

Undocumented endpoints whose terms prohibit automated access and
monetization. Fine for a single-user local terminal, not fine as the basis
of anything published or sold. Everything sits behind
`providers.base.QuoteProvider` so a licensed feed is a one-file swap.
