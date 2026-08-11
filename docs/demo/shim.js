/* The server, replaced by a recording.
 *
 * Loaded ahead of app/web/index.html's own script, which is shipped
 * unmodified — the page has no idea it is a demo. Everything below is
 * either answering a request from a captured fixture or replaying the
 * tape that tools/build_demo.py recorded off the real feed.
 *
 * `window.fetch` is replaced outright rather than wrapped. A fixture we
 * forgot must fail here, visibly, and never fall through to the real
 * Yahoo endpoints from a stranger's browser: this page is public and
 * those endpoints are not ours to hand out.
 */
(function () {
  "use strict";

  const D = window.DEMO_DATA;
  if (!D) { console.error("demo data missing"); return; }
  const F = D.fixtures, META = D.meta;

  window.PM_TOKEN = "demo";

  /* ---------- clocks ----------
   * Ages on screen are rendered from timestamps, so a recording opened
   * three weeks later would read "snapshot 21d old · STALE" and look
   * broken rather than frozen. Every captured timestamp is shifted
   * forward by the same amount, which makes the page show the ages it
   * showed while it was being recorded. That is a claim about the
   * recording, not about now — which is why the banner says so in the
   * first line, unmissably, before anyone reads a price. */
  const SHIFT = (Date.now() / 1000) - META.captured_at;
  const TIME_KEYS = new Set(["snapshot_ts", "as_of", "server_time",
    "build_at", "captured_at", "published", "earnings_ts", "quote_time",
    "time", "at", "start", "end"]);

  function shift(v) {
    if (Array.isArray(v)) return v.map(shift);
    if (v && typeof v === "object") {
      const o = {};
      for (const k of Object.keys(v)) {
        o[k] = TIME_KEYS.has(k) && typeof v[k] === "number" && v[k] > 1e9
          ? v[k] + SHIFT : shift(v[k]);
      }
      return o;
    }
    return v;
  }

  /* ---------- the screener ----------
   * The one request whose answer has to vary: sorting and filtering are
   * most of what the terminal is. Rows are held here and screened in the
   * browser, against the same alias table and column names the Python
   * engine uses — dumped into data.js at build time rather than restated,
   * because a second copy would drift and start answering a slightly
   * different question than the real one. */
  const ALL = (F.screen && F.screen.rows) || [];
  const ALIASES = META.aliases || {};
  const TEXT = new Set(META.text_columns || []);
  const SUFFIX = META.suffixes || {};

  // Undefined for anything the recording cannot answer, so an unknown
  // field is reported rather than quietly matching nothing — an empty
  // grid reads as "no stock qualifies", which would be a wrong answer
  // where "the demo doesn't ship that column" is the true one.
  let unknown = "";
  function resolve(f) {
    const k = String(f).toLowerCase();
    const c = ALIASES[k];
    if (!c) unknown = k;
    return c;
  }

  function num(tok) {
    const m = /^(-?[\d.]+)([a-z]?)$/i.exec(String(tok).trim());
    if (!m) return null;
    const mult = m[2] ? SUFFIX[m[2].toLowerCase()] : 1;
    if (m[2] && !mult) return null;
    const v = parseFloat(m[1]);
    return Number.isFinite(v) ? v * (mult || 1) : null;
  }

  /* A comparison, or a bare word treated as a substring match on the
   * name — deliberately the same shorthand the real command line takes. */
  function predicate(text) {
    const m = /^\s*([a-z_0-9]+)\s*(>=|<=|==|!=|>|<)\s*(.+?)\s*$/i.exec(text);
    if (!m) {
      const q = text.trim().replace(/^["']|["']$/g, "").toLowerCase();
      if (!q) return null;
      return r => String(r.symbol || "").toLowerCase().includes(q)
        || String(r.name || "").toLowerCase().includes(q);
    }
    const field = resolve(m[1]), op = m[2];
    if (!field) return null;
    let raw = m[3].trim().replace(/^["']|["']$/g, "");
    if (TEXT.has(field)) {
      const want = raw.toLowerCase();
      return r => {
        const got = String(r[field] === null || r[field] === undefined
          ? "" : r[field]).toLowerCase();
        return op === "!=" ? got !== want : got === want;
      };
    }
    const want = num(raw);
    if (want === null) return null;
    return r => {
      const got = r[field];
      if (got === null || got === undefined || Number.isNaN(got)) return false;
      switch (op) {
        case ">": return got > want;
        case "<": return got < want;
        case ">=": return got >= want;
        case "<=": return got <= want;
        case "==": return got === want;
        case "!=": return got !== want;
      }
      return false;
    };
  }

  /* Splits on `and`/`or` at depth zero, recursing into parentheses. Not
   * the real ast engine — that one is Python and refuses anything it
   * cannot prove safe. This one only ever runs over an array in the
   * viewer's own tab, and anything it fails to parse is reported rather
   * than guessed at. */
  function compile(expr) {
    const text = (expr || "").trim();
    if (!text) return () => true;
    const parts = [], ops = [];
    let depth = 0, cur = "";
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (ch === "(") depth++;
      if (ch === ")") depth--;
      if (depth === 0) {
        const rest = text.slice(i).toLowerCase();
        const kw = rest.startsWith(" and ") ? "and"
          : rest.startsWith(" or ") ? "or" : null;
        if (kw) { parts.push(cur); ops.push(kw); cur = ""; i += kw.length + 1; continue; }
      }
      cur += ch;
    }
    parts.push(cur);
    const fns = parts.map(p => {
      const t = p.trim();
      if (t.startsWith("(") && t.endsWith(")")) return compile(t.slice(1, -1));
      return predicate(t);
    });
    if (fns.some(f => !f)) return null;
    return r => {
      let acc = fns[0](r);
      for (let i = 0; i < ops.length; i++) {
        acc = ops[i] === "and" ? (acc && fns[i + 1](r)) : (acc || fns[i + 1](r));
      }
      return acc;
    };
  }

  let warned = false;
  function runScreen(params) {
    const expr = params.get("expr") || "";
    let filters = {};
    try { filters = JSON.parse(params.get("filters") || "{}"); } catch (e) { }
    const chosen = Object.values(filters).filter(Boolean);
    // The dropdowns compile to expression fragments on the server, and
    // that table is not shipped here. Say so once, plainly, rather than
    // returning the unfiltered universe as though it had been screened.
    if (chosen.length && !warned) {
      warned = true;
      setTimeout(() => window.say && window.say(
        "demo: the dropdown filters compile on the server — the command "
        + "line works here, try  mcap > 500b and chg > 1", true), 1200);
    }
    unknown = "";
    const fn = compile(expr);
    if (!fn) {
      const why = unknown
        ? `demo: "${unknown}" is a real field, but the recording only ships `
          + "the columns the grid displays"
        : "demo: this recording understands comparisons, and/or and "
          + "parentheses — the full engine is Python, and local";
      setTimeout(() => window.say && window.say(why, true), 100);
    }
    let rows = fn ? ALL.filter(fn) : ALL.slice();
    const sort = resolve(params.get("sort") || "market_cap") || "market_cap";
    const dir = (params.get("dir") || "desc") === "desc" ? -1 : 1;
    rows.sort((a, b) => {
      const x = a[sort], y = b[sort];
      const xm = x === null || x === undefined || Number.isNaN(x);
      const ym = y === null || y === undefined || Number.isNaN(y);
      // Missing sorts last in both directions: a blank P/E is not the
      // cheapest stock in the market. The real engine's rule, kept.
      if (xm && ym) return 0;
      if (xm) return 1;
      if (ym) return -1;
      if (typeof x === "string" || typeof y === "string") {
        return String(x).localeCompare(String(y)) * dir;
      }
      return (x - y) * dir;
    });
    const offset = parseInt(params.get("offset") || "0", 10);
    const limit = parseInt(params.get("limit") || "500", 10);
    return {
      rows: rows.slice(offset, offset + limit),
      total: rows.length,
      universe: META.universe || ALL.length,
      expr: expr,
      // "static" is the one the count line renders without a claim about
      // where the prices came from. The alternatives would each say
      // something untrue here: "live" announces a re-pricing round trip
      // that did not happen, and "snapshot" warns the screen was run on
      // stale prices as though that were a fault rather than the point.
      // The banner has already said what these prices are.
      priced: "static",
      as_of: META.captured_at,
      elapsed_ms: 0,
    };
  }

  /* ---------- routing ---------- */
  // A path the recording cannot answer has to come back as a failure, not
  // as a 200 carrying an `error` key. `api()` only throws on !ok, so a
  // 200 would be handed to a view as though it were data and the view
  // would render undefined fields or throw somewhere unrelated.
  function miss(message) { const e = new Error(message); e.miss = true; return e; }

  function route(path, params) {
    if (path === "/api/screen") return runScreen(params);
    if (path === "/api/subscribe") return { ok: true, stream: streamStatus(), quotes: {} };
    if (path === "/api/quotes") return { quotes: {}, as_of: Date.now() / 1000, stream: streamStatus() };
    if (path === "/api/status") return status();
    if (path.startsWith("/api/ticker/")) {
      const sym = decodeURIComponent(path.slice("/api/ticker/".length));
      if (F.tickers[sym]) return F.tickers[sym];
      throw miss(`demo: no chart recorded for ${sym} — the recording `
        + `carries charts for the first ${Object.keys(F.tickers).length} rows`);
    }
    if (path === "/api/news") {
      const sym = params.get("symbol");
      return F.news[sym] || { symbol: sym, items: [], source: "demo" };
    }
    if (path === "/api/watchlist") return F.watchlist || { watchlist: [] };
    if (path === "/api/refresh") {
      return { started: false, error: "the demo is a recording — nothing to rebuild" };
    }
    const name = path.replace("/api/", "");
    if (F[name] !== undefined) return F[name];
    throw miss("demo: nothing recorded for " + path);
  }

  function status() {
    const s = JSON.parse(JSON.stringify(F.status || {}));
    s.stream = streamStatus();
    return s;
  }

  function streamStatus() {
    return {
      available: true, connected: true, healthy: true,
      subscribed: subscribed.size,
      pinned: 0, failures: 0, last_tick_age: 0,
      // The delay the feed actually had while this was recorded. It is
      // not this page's delay — the page has none, it is a recording —
      // but zero would be the one number that is certainly a lie.
      latency: META.stream_latency,
      samples: META.tape_prints, error: "",
    };
  }

  // Seeded from the recording rather than filled as prints arrive, so the
  // badge reads STREAM 99 on load instead of STREAM 0 for the first few
  // seconds. These are the symbols the tape actually carries, which is
  // the same claim the live badge makes about its subscription.
  const subscribed = new Set((D.tape || []).map(e => e.s));

  window.fetch = function (url, opts) {
    const u = new URL(url, location.href);
    let payload, status = 200;
    try {
      payload = route(u.pathname, u.searchParams);
    } catch (err) {
      if (!err.miss) console.error("demo route failed", u.pathname, err);
      payload = { error: err.miss ? err.message : "demo: " + err.message };
      status = err.miss ? 404 : 500;
    }
    // The screen response is built here and already carries live clocks;
    // everything else is a captured fixture whose timestamps need moving.
    const body = JSON.stringify(
      status === 200 && u.pathname !== "/api/screen" ? shift(payload) : payload);
    return Promise.resolve(new Response(body, {
      status: status, headers: { "Content-Type": "application/json" },
    }));
  };

  /* ---------- the tape ----------
   * Real prints, in the order and at the intervals they actually arrived,
   * looped. The page receives them exactly as it receives live ones. */
  const TAPE = D.tape || [];
  const SPAN = TAPE.length ? TAPE[TAPE.length - 1].t + 1 : 0;

  window.EventSource = function () {
    const listeners = {};
    this.addEventListener = (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); };
    this.close = () => { };
    const emit = (ev, data) => {
      for (const fn of (listeners[ev] || [])) {
        try { fn({ data: JSON.stringify(data) }); } catch (e) { console.error(e); }
      }
    };
    setTimeout(() => emit("status", shift(status())), 0);
    setInterval(() => emit("status", shift(status())), 5000);

    if (!TAPE.length) return;
    let i = 0, epoch = Date.now();
    // Batched on the same 50ms window the server coalesces on, so the
    // replay repaints the way the real thing does.
    setInterval(() => {
      const now = (Date.now() - epoch) / 1000;
      if (now > SPAN) { i = 0; epoch = Date.now(); return; }
      const quotes = {};
      let n = 0;
      while (i < TAPE.length && TAPE[i].t <= now) {
        const e = TAPE[i++];
        const q = { price: e.p, time: Date.now() / 1000, at: Date.now() / 1000 };
        if (e.c !== null && e.c !== undefined) q.change = e.c;
        if (e.cp !== null && e.cp !== undefined) q.change_pct = e.cp;
        if (e.v !== null && e.v !== undefined) q.volume = e.v;
        if (e.h !== null && e.h !== undefined) q.day_high = e.h;
        if (e.l !== null && e.l !== undefined) q.day_low = e.l;
        quotes[e.s] = q;
        subscribed.add(e.s);
        n++;
      }
      if (n) emit("ticks", { quotes: quotes, as_of: Date.now() / 1000 });
    }, 50);
  };

  /* ---------- the label ----------
   * First thing on the page, before any price. The terminal's one rule is
   * that it never claims to be live when it is not, and a public URL
   * showing a moving tape is exactly where that rule earns its keep. */
  document.addEventListener("DOMContentLoaded", () => {
    const bar = document.createElement("div");
    bar.id = "demoBar";
    bar.innerHTML =
      '<b>DEMO</b> — a recording of the real terminal, captured '
      + META.captured_label
      + (META.tape_prints
        ? ' · ' + META.tape_prints.toLocaleString() + ' real prints replaying'
        : '')
      + '. Prices are from that moment and are <b>not live</b>. '
      + 'Sorting, the command line, the heatmap and the charts all work. '
      + '<a href="https://github.com/Ben05-sys/QuantPilot">Run it yourself '
      + 'for the live feed →</a>';
    document.body.insertBefore(bar, document.body.firstChild);
    const css = document.createElement("style");
    css.textContent = `
      #demoBar{background:#2a1c00;color:#ffb000;border-bottom:1px solid #5a3d00;
        padding:5px 10px;font:11px/1.5 ui-monospace,Menlo,Consolas,monospace;
        text-align:center}
      #demoBar b{color:#ffd166}
      #demoBar a{color:#ffd166}`;
    document.head.appendChild(css);
  });
})();
