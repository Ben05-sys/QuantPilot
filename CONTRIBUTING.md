# Contributing

Issues and pull requests are welcome. The codebase is small enough to read in
an afternoon — about 4,600 lines of Python and one HTML file — and every module
opens with a docstring explaining why it exists rather than what it does. The
`## Layout` section of the [README](README.md) is the map.

## Getting it running

```bash
pip install -r requirements.txt
python pilot.py
```

Runs on Linux, macOS and Windows; CI checks all three on every push. The first
launch builds the universe snapshot in the background, which takes 20–35
seconds for about 7,000 symbols. Data lives in `~/.quantpilot` and nothing is
written inside the repo.

Three conveniences are Windows-only and each degrades to a printed line rather
than an error: the tray icon, the `Ctrl+Alt+M` hotkey, and `--install-shortcut`.

## Tests

Plain scripts with asserts, no pytest. Run them from the repo root:

```bash
python tests/test_screen.py    python tests/test_store.py     python tests/test_clock.py
python tests/test_diffs.py     python tests/test_options.py   python tests/test_stream.py
python tests/test_server.py    python tests/test_livenews.py  python tests/test_article.py

cd tests && npm install jsdom && cd ..
node tests/ui_smoke.js       node tests/ui_units.js
```

Assertions pass today and that number should only go up. **Run them before
opening a PR.**

The two UI suites split by altitude, and both matter if you touch
`app/web/index.html`:

`ui_smoke.js` boots the whole terminal in jsdom and drives the views. It is
the only thing that catches a runtime throw at page load — the failure that
leaves the terminal rendering perfectly while every handler after the bad line
is quietly dead.

`ui_units.js` sits underneath that, on the four things a regression would hide
in: **formatting** (a cell must never read `NaN` or a stand-in zero where the
terminal has no number — house rule 2, on screen), **virtualization** (7,000
rows must stay a window of DOM nodes, or the terminal passes every six-row
test and dies on a real universe), **the token** on every API call, and
**escaping** of upstream strings on their way into the DOM.

If a change needs a test to prove it, write one that **fails before your change
and passes after**. If you cannot construct that test, the change is not
understood well enough to merge yet. Never weaken an existing assertion to get
a green run; if an assertion is genuinely wrong, say so in the PR and change it
deliberately.

## Style

```bash
pip install -r requirements-dev.txt
ruff check .
```

That is exactly what CI runs, and the configuration lives in `ruff.toml` so it
cannot drift from what you see locally. There is deliberately **no formatter**:
the source is hand-wrapped so comments sit against the lines they explain, and
`ruff format` would rewrap all of it in one unreviewable commit. Wrap at 79
columns like the code around you; 88 is the ceiling CI enforces, and the gap is
room for the occasional line that reads worse broken than long.

## House rules

These four are load-bearing. A PR that breaks one will be sent back even if the
code is good, because they are the reason the terminal can be trusted with
money on the line.

**1. Never claim live over stale data.** Quote lag is measured per response and
rendered — `LAG 4s`, `LAG 12m`, `CLOSED`. The stream badge reads `STREAM` only
while ticks are genuinely arriving and falls back to `POLL` otherwise. A grid
that has silently stopped updating while still labelled live is worse than one
that admits it is polling.

**2. A field we cannot compute stays null.** Nothing is imputed, guessed, or
defaulted into looking like a fact. Guard division by zero — see `safe()` in
`universe.derive()`. A stock that never traded has no position in the day's
range, and `NaN` is the honest answer, not `50`.

**3. Expressions are parsed, never evaluated.** `app/screen.py` walks an `ast`
under a node allowlist. Never `eval()`, and never `DataFrame.query`, which
executes attribute access and would happily run `@__import__`.

**4. The terminal is local.** The server binds loopback, checks the `Host`
header, and requires a per-launch token. The only third party the page contacts
is the YouTube embed in the live news view, and `tests/test_server.py` enumerates
every permitted external host so that cannot quietly widen. The article reader
takes a **story id, never a URL** — a route that fetched whatever address it was
handed would be an open proxy running on someone's machine.

Two more worth knowing:

- `config.DEFAULTS["sec_contact"]` must stay `""`. It goes to SEC in the
  User-Agent on every EDGAR request, so a hard-coded address would identify the
  author on a stranger's machine and any rate-limit complaint would land on the
  wrong person. `tests/test_store.py` guards this.
- Fifteen grid columns is the ceiling. New depth goes into detail-on-demand —
  the cursor strip under the screener — not a new column.

## Adding a screenable field

The most common change, and the most useful. `universe.derive()` computes it;
`screen.py` registers it. A field needs all four:

1. Computed in `derive()`, null when the inputs are missing.
2. Added to `DERIVED` in `app/screen.py`.
3. Given a short alias in `ALIASES` — nobody types `pct_from_52w_high`.
4. Classified as `LIVE_COLUMNS` or `STATIC_SAFE`.

That last one is the part people get wrong. `STATIC_SAFE` does not mean "this
never changes" — market cap changes with every tick. It means *"would a few
hours of drift change the answer?"*. Nobody screening `mcap > 10b` cares that
the boundary moved 2% since lunch; someone screening `chg > 3` cares enormously.
**Anything derived from today's price or volume is not static-safe.** Volume
only ever rises during a session, so prefiltering a stale value drops the very
names that have since crossed the threshold.

Add a synthetic-frame test with hand-computed expected values, the way
`tests/test_screen.py` already does for dollar volume and day range. Be
especially careful about sign, denominator, and percent-versus-ratio.

## Commit messages

Prose, in the register already in the repo — read `git log`. Say what changed
and why it matters to someone screening real stocks. The log is the most-read
documentation this project has; a reader who knows finance but not this
codebase should finish a message understanding why the change was right.

## Data sources

Every upstream sits behind `providers.base.QuoteProvider`, so replacing one is a
single file. Worth knowing before you build on Yahoo: their endpoints are
undocumented and their terms prohibit automated access and monetization. That is
fine for a single-user terminal on your own machine and **not** fine as the
basis of anything published or sold.

## Licence

The project is **AGPL-3.0** and stays that way. Anyone running a modified
version as a network service has to publish their source — that clause is the
whole reason for choosing AGPL over MIT.

Contributions also need a signed **[Contributor Licence Agreement](CLA.md)**,
which asks one thing beyond the AGPL: permission to license the project on
commercial terms as well. You keep the copyright in your work — the CLA is a
licence grant, not an assignment, so your code stays yours to use anywhere
else. Signing takes one line in your first pull request and covers everything
you contribute afterwards.

The reason it is worth your two minutes: without it, one merged pull request
from someone who later becomes unreachable pins the whole project to AGPL-only
permanently. That is a decision worth making deliberately rather than by
accident. CLA.md explains the rest.
