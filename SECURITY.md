# Security policy

## Reporting a vulnerability

Please report privately, not in a public issue:
**[open a security advisory](https://github.com/Ben05-sys/QuantPilot/security/advisories/new)**.

You will get an acknowledgement within a few days. This is a single-maintainer
project, so please allow a reasonable window for a fix before disclosing —
90 days is the default, sooner if the fix is out earlier. Credit is given in
the advisory unless you would rather stay anonymous.

## Supported versions

`main` only. There are no maintenance branches: fixes land on `main` and are
tagged, and the fix for anything reported will be in the next tag.

## Threat model

QuantPilot runs on your own machine, binds loopback, and holds no credentials
— there is no account, no API key and no vendor. What it does have is an HTTP
server, an expression parser fed by user text, and a fetcher that retrieves
pages on your behalf. Those are the three places where a bug is a security
bug rather than a rendering one, and each is deliberately constrained:

- **The server** binds `127.0.0.1` only, checks that the `Host` header names a
  loopback address so a DNS-rebinding page cannot reach it, and requires a
  per-launch token that is injected into the served page and echoed on every
  API call. The SSE stream takes the same token as a query parameter, because
  `EventSource` cannot set request headers.
- **The expression engine** parses with `ast` and walks the tree under a node
  allowlist. `eval()` is never called on user text, and neither is
  `DataFrame.query`, which executes attribute access and would happily run
  `@__import__`.
- **The article reader** takes a story id, never a URL. A route that fetched
  whatever address it was handed would be an open proxy running on your
  machine, usable to probe your own network. Only stories the server has
  already served as news can be fetched; the host is resolved and every
  address it stands for is checked against loopback, private, link-local and
  carrier ranges; and the check runs again on **every redirect hop**, because
  the publisher on the other end is allowed to answer "fetch this instead".
  `tests/test_article.py` holds the whole table, in the spellings that
  matter — `2130706433` and `[::ffff:127.0.0.1]` are also loopback.
- **Third-party reach** is one embed: the YouTube player in the live news
  view, built by script only when you open that view and a channel is on air.
  `tests/test_server.py` enumerates every permitted external host, so the set
  cannot quietly widen.

### In scope

Anything that lets a web page you happen to have open reach the terminal;
anything that escapes the expression allowlist; anything that turns the
article reader into a general fetcher; anything that writes outside
`~/.quantpilot`; and any way the served page can be made to load a third
party not on the permitted list.

### Out of scope

- **Upstream data being wrong or stale.** Report it as a bug — it matters,
  but it is not a vulnerability.
- **Anything requiring an attacker who is already running code as you.** The
  per-launch token is in your own process and the database is a file in your
  home directory; a local attacker with your privileges has already won.
- **Running the terminal on a public interface.** It binds loopback and
  refuses non-loopback `Host` headers by design. Deliberately putting it
  behind a proxy that reaches the internet is a configuration you have chosen,
  and the AGPL network clause then applies to you as well.
