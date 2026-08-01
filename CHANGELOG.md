# Changelog

Notable changes to QuantPilot. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`0.x` means the expression language, the field names and the stored schema
can still change between minor versions. Anything that would invalidate a
saved screen will be called out here under **Changed** rather than buried.

## [Unreleased]

Nothing yet.

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
