# Changelog

Notable changes to QuantPilot. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`0.x` means the expression language, the field names and the stored schema
can still change between minor versions. Anything that would invalidate a
saved screen will be called out here under **Changed** rather than buried.

## [Unreleased]

### Added

- **An ex-dividend calendar** (`Alt+D`, or `div`). What goes ex over the
  next six weeks, grouped by exchange day, carrying all four dates a
  dividend has — declared, ex, record and payment — plus the cash amount,
  that amount as a percent of today's price, the indicated annual yield
  and the payout ratio.
- **An IPO board** (`Alt+I`, or `ipo`), split into upcoming, priced, filed
  and withdrawn, with the range a deal is asking, the share count and the
  offer size. Deliberately not part of the screener: a company that has
  not listed has no price, no volume and no market cap, and every screener
  row is supposed to be a line you can trade. The ones that have listed
  link through to the chart.
- **Both calendars are part of the expression language**, which is the
  point of joining them to the universe rather than serving them as two
  more read-only tables. `exdiv` is days until the stock trades without
  its next payment, negative once it has, alongside `divamt`, `divdrop`,
  `fwdyield`, `exdate`, `recorddate`, `paydate` and `ipoage`. So
  `0 < exdiv < 7 and payout < 75` is a screen, and so is
  `not (0 < exdiv < 7)` — an ex-date is a scheduled gap down, and a
  momentum screen that does not exclude one is partly measuring dividends.
  A new **Ex-Dividend** dropdown compiles to the same expressions.
- A fourth clock, in `app/calendars.py`. The dividend calendar is one call
  per exchange day against a host limited to two a second, so a six-week
  window costs about twenty seconds — more than a whole universe rebuild,
  for data that changes once a day. It refreshes on a background thread
  and the screener joins whatever is known; before the first refresh every
  dividend column is null, which is not the same fact as "pays nothing"
  and renders as an em dash.

- **A demo anyone can open**, at
  [ben05-sys.github.io/QuantPilot](https://ben05-sys.github.io/QuantPilot/).
  `app/web/index.html` verbatim, with the network replaced by fixtures
  captured from a real server and a couple of minutes of the real tape
  replaying underneath. Sorting, the command line, the heatmap, the world
  map and the charts all work. Built locally by `tools/build_demo.py` and
  committed; the workflow only copies it, and the page makes no request to
  anyone when you open it.
- **The `STREAM` badge states the delay it is measuring** — the median gap
  between the venue's timestamp and the print arriving, over the last two
  hundred prints, rather than a green light that means "a socket is open".
- **`atr_pct` — true range as a percentage of price**, so the same reading
  compares across price levels. Screenable, aliased `atr`, and classified
  live rather than static-safe: it is built from today's range and today's
  price, so prefiltering it against a stale snapshot would answer a question
  about yesterday.
- **A [contributor licence agreement](CLA.md).** It asks one thing beyond the
  AGPL — permission to license the project commercially as well — and grants
  a licence rather than taking an assignment, so contributors keep the
  copyright in their own work. Without one, a single merged pull request from
  someone later unreachable pins the project to AGPL-only permanently, which
  is a decision worth making on purpose.

### Changed

- **Ticks flush to the browser on arrival.** There was a flat
  `sleep(0.7)` between the socket and the event stream, so every price on
  screen was held back by up to 700ms with the number already in hand —
  about a fifth of the total delay. Measured over a regular session, what
  the terminal adds went from ~350ms on average to a median of 2ms, with a
  worst case of 51ms. Yahoo's own 2.9s is unchanged and is not ours to fix.
- **The market-state probe runs on its own thread.** It costs an upstream
  round trip and whichever thread found the cache expired paid for it,
  which meant one batch of prices every five seconds left a quarter of a
  second late.
- **Visible rows are polled against a 2s cache rather than a 10s one**, and
  the poll now runs at 3s. The single cache TTL was tuned for bulk
  screening, so every second poll of the rows on screen was answered from
  cache and the fallback ran at half its advertised rate. Bulk screening
  keeps the longer TTL — it costs ten upstream chunks, not one.

### Fixed

- **A special dividend no longer reports a yield of zero.** Nasdaq returns
  an indicated annual of `0` for an irregular payment, and dividing that
  by the price rendered a stock paying five cents this week as yielding
  0.00%. It is null now — the payment is real and simply not annualisable.
- **Volume, relative volume and the day's range now move with the price.**
  Yahoo puts the running session totals on every print and the tick handler
  kept four fields out of thirteen, so while the stream was up those
  columns sat frozen at whatever the last snapshot recorded.
- **A print is assigned to its own session, not to SPY's.** The venue's
  `marketState` is one global answer sampled from one symbol, and it was
  wrong at both edges: a regular-session trade reported after the bell was
  filed as extended-hours, and an overnight ATS print could overwrite
  `LAST` whenever the clock still said `REGULAR`.
- **A cached tick no longer outranks a newer quote.** Prints are kept for
  fifteen minutes because for a thin name the last trade really is the
  price — but that cached tick was overwriting a quote fetched a moment
  ago, so a row could report a quarter-hour-old price while the header
  honestly read `LAG 2s`. Whichever of the two the venue timestamps say is
  later now wins.
- **Rows the stream has gone quiet on are polled again.** The poll stopped
  entirely while the socket was healthy, which is only an argument about
  the *feed*: outside the most active few hundred names a stock can go
  minutes between trades, and those rows sat at their snapshot price for as
  long as you looked at them, under a badge reporting the feed as live.
- **A halted name no longer drags its sector's average toward flat.**
  `rs_sector` zero-filled a missing `change_pct` while still counting that
  row's full market-cap weight in the denominator, so a name with no reading
  today — a trading halt, or one absent from a partial refresh — was treated
  as "flat" rather than "unknown". On a sector with two names genuinely up
  10% and a halted large-cap third, the average read 2%, and every other
  name's relative strength was then measured against that wrong baseline.
  Missing rows now carry no weight at all, the same way every other field
  here refuses to turn "missing" into "zero".

## [0.1.1] — 2026-08-01

Security release. 0.1.0 stood for about an hour; anyone who cloned it should
take this instead.

### Security

- **The article reader now re-checks the address on every redirect hop.** It
  validated the URL it was given and then let the HTTP layer follow redirects
  unexamined, so a publisher in the news feed — or anyone who could get a link
  into it — could answer with `302 http://127.0.0.1:8900/…` and have the
  terminal fetch it. Reported by CodeQL as a partial SSRF; the guard now runs
  on each hop and the chain is cut at five.
- **The blocked-address check understands addresses rather than text.** It was
  a regex over the hostname, which meant `127.0.0.1` was blocked while
  `2130706433`, `0177.0.0.1`, `127.1` and `[::ffff:127.0.0.1]` — the same
  machine, differently spelled — were not, and `0.0.0.0`, IPv6 link-local and
  unique-local, and the 100.64/10 carrier range were never listed at all.
  Names are resolved and every address they stand for is checked; a name that
  does not resolve now fails closed.
- **The SEC contact address can no longer be sent anywhere but the SEC.** The
  agent carrying it was selected with `host.endswith("sec.gov")`, which also
  matches `notsec.gov`.
- **A symbol went into one Yahoo URL unencoded.** `snapshot_lag` was the only
  call of six that did not percent-encode, so a symbol carrying `?` or `/`
  could rewrite the request it was meant to be a parameter of.
- `tests/test_article.py` covers all of the above, and runs in CI.

### Added

- CodeQL analysis, `ruff` in CI against a pinned version, the live news and
  article suites in the matrix, and community health files — SECURITY.md with
  the threat model, a code of conduct, issue forms and a PR checklist.

### Fixed

- The header said `PILOT MARKETS`: the rename had reached the repository, the
  docs and the data directory but not the page itself.
- The index strip's timestamp stretched across the futures strip, because its
  CSS rule had lost its declaration block and the selector ran on into the
  next one. It also rendered in the system locale next to a 24-hour ET clock.

## [0.1.0] — 2026-08-01

First tagged release. The terminal has been working for a while; this is the
point at which it is worth someone else's afternoon.

### Added

- **Screener** over the whole listed US universe — about 7,000 symbols —
  filtered locally in pandas with no network in the hot path. Fifteen
  Finviz-style dropdowns plus a command line, both compiling to the same
  expression string, so anything you can click you can type, save and reload.
- **Expression language** parsed with `ast` under a node allowlist. `eval()`
  is never called on user text, and neither is `DataFrame.query`. Suffixes
  (`k m b t`), chained comparisons, membership and substring `in`,
  case-insensitive text, arithmetic between fields.
- **Time-adjusted relative volume** — volume so far against what a normal
  session would have traded *by now*, so momentum screens return something
  before noon. `relvolraw` keeps the unadjusted ratio.
- **Heatmap**, squarified treemap by sector; **world map** of 60 countries;
  **ADR** view; **earnings** calendar split BMO/AMC.
- **Ticker detail** — candles with SMA50/SMA200 and volume, 1D through MAX,
  extended-hours sessions shaded, 52-week range, SEC 10-K/10-Q links, news.
- **Extended and overnight sessions** as first-class data: pre-market,
  after-hours and the 20:00–04:00 ET overnight tape reach the `EXT%` column
  and the filter bar. The regular-session `LAST` is never overwritten with a
  print from a different session.
- **Market news** ranked by hotness — recency times the size of the move the
  story is about — with the ticker and its move on every row.
- **Live news** (`F10`): the US finance networks embedded from their own
  YouTube channels, with liveness read per channel rather than assumed, and
  the tickers the current headlines are about priced alongside.
- **Screen diffs**, **alerts**, **watchlist with P&L**, **relative strength**
  (`rs`), **chart comparison**, **related companies** from SEC annual-report
  mentions, and an **in-terminal article reader** that takes a story id.
- **Append-only snapshots** keyed by timestamp, which is what makes screen
  membership diffs and point-in-time history possible at all.
- **Windows conveniences**, each degrading to a printed line elsewhere: tray
  icon, `Ctrl+Alt+M` hotkey, `--install-shortcut`.
- **CI** on Linux, macOS and Windows across Python 3.11 and 3.13, plus a
  jsdom smoke test that boots the whole UI, and CodeQL analysis.

### Security

- The server binds loopback only, verifies the `Host` header against loopback
  addresses so a DNS-rebinding page cannot reach it, and requires a
  per-launch token echoed on every API call.
- The article reader accepts a story id, never a URL, and re-checks the
  resolved host against loopback and private ranges — a route that fetched
  any address it was handed would be an open proxy on the user's machine.
- `tests/test_server.py` enumerates every permitted external host, so the
  page's third-party reach cannot quietly widen beyond the YouTube embed.

### Licence

- AGPL-3.0. The network clause is the point: a modified version run as a
  service other people can reach has to publish its source.

[Unreleased]: https://github.com/Ben05-sys/QuantPilot/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Ben05-sys/QuantPilot/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Ben05-sys/QuantPilot/releases/tag/v0.1.0
