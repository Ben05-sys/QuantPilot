/* The parts of the terminal you can check without looking at it.
 *
 * `ui_smoke.js` drives the views — it clicks through the chrome and proves
 * the handlers are alive. This suite goes underneath that, at the four
 * places where a silent regression would be expensive and invisible:
 *
 *   formatting      a cell that reads "NaN" or "0" where the terminal has
 *                   no number is house rule 2 broken on screen, and it
 *                   looks exactly like a real figure to whoever is trading
 *                   against it.
 *   virtualization  "7,000 rows scroll like 40" is a load-bearing claim.
 *                   Lose the windowing and the terminal still works on the
 *                   six-row frames every other test uses, then dies on a
 *                   real universe.
 *   the token       every API call carries X-PM-Token. That is the whole
 *                   reason another website cannot drive this server, and
 *                   nothing else asserts it from the browser side.
 *   escaping        company names come from an upstream. `esc` is the only
 *                   thing between a name and the DOM.
 *
 * Plus the small stateful interactions ui_smoke does not reach: sorting by
 * a column header, the watchlist dot, and command history.
 *
 *   node tests/ui_units.js
 */
const fs = require("fs");
const path = require("path");

// Same lookup as ui_smoke.js: a machine without jsdom skips rather than
// fails. Kept standalone on purpose — the suites are plain scripts and
// run top to bottom, and a shared harness would be the only import in
// the tests directory.
const CANDIDATES = [
  process.env.JSDOM_PATH,
  "jsdom",
  path.join(__dirname, "node_modules", "jsdom"),
  path.join(__dirname, "..", "..", "OfflinePilotX",
            "tests", "node_modules", "jsdom"),
].filter(Boolean);

let JSDOM;
for (const candidate of CANDIDATES) {
  try { ({ JSDOM } = require(candidate)); break; } catch (e) { /* next */ }
}
if (!JSDOM) {
  console.log("  skip  jsdom not installed — `npm i jsdom` in tests/ to "
              + "run the UI unit suite");
  process.exit(0);
}

const HTML = path.join(__dirname, "..", "app", "web", "index.html");
let html = fs.readFileSync(HTML, "utf8");
html = html.replace("<head>", '<head><script>window.PM_TOKEN="test-token";</script>');

let pass = 0, fail = 0;
function check(label, ok, detail) {
  if (ok) { pass++; console.log("  ok   " + label); }
  else { fail++; console.log("  FAIL " + label + (detail ? "  " + detail : "")); }
}

const ctxStub = new Proxy({}, {
  get(_, key) {
    if (key === "measureText") return () => ({ width: 40 });
    if (key === "createLinearGradient") return () => ({ addColorStop() {} });
    if (key === "canvas") return { width: 800, height: 600 };
    return typeof key === "string" ? () => {} : undefined;
  },
  set() { return true; },
});

// Every fetch is recorded so the token assertions have something to read,
// and answers with an empty-but-valid screen payload so the handlers under
// test run to completion instead of falling into their error paths.
const calls = [];
const errors = [];
const dom = new JSDOM(html, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  url: "http://127.0.0.1:8900/",
  beforeParse(win) {
    win.HTMLCanvasElement.prototype.getContext = () => ctxStub;
    win.devicePixelRatio = 1;
    win.fetch = (url, opts) => {
      calls.push({ url: String(url), opts: opts || {} });
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({
          // Shaped like the real routes, because these calls succeed here.
          // ui_smoke rejects every fetch and so never reaches the success
          // path; boot() walks straight into buildFilters(filters.filters)
          // and would throw on a payload that only looks close enough.
          filters: [], presets: [], screens: [], columns: [], fields: [],
          rows: [], total: 0, universe: 0, expr: "", chips: [],
          items: [], symbols: [], channels: [], sectors: [], countries: [],
          positions: [], indices: [], futures: [],
        }),
      });
    };
    win.EventSource = function () {
      this.addEventListener = () => {}; this.close = () => {};
    };
    win.addEventListener("error", (e) =>
      errors.push(e.error ? (e.error.stack || e.error.message) : e.message));
  },
});

const win = dom.window;
const doc = win.document;
const ev = (js) => win.eval(js);

setTimeout(() => {
  check("the page booted without throwing", errors.length === 0, errors[0]);

  /* ---------- formatting ---------- */
  // Every one of these renders straight into a grid cell. The contract is
  // house rule 2: a number we do not have is blank, never a stand-in.

  console.log("\nformatting — a missing number is blank, never a figure");
  for (const fn of ["nf", "fcap", "fvol", "fpct"]) {
    check(fn + "(null) is blank", ev(fn + "(null)") === "",
          JSON.stringify(ev(fn + "(null)")));
    check(fn + "(undefined) is blank", ev(fn + "(undefined)") === "",
          JSON.stringify(ev(fn + "(undefined)")));
  }
  check("fage(null) is blank", ev("fage(null)") === "");
  check("fmoney with nothing to show says so rather than printing $0",
        ev("fmoney(null)") === "—", ev("fmoney(null)"));

  // NaN is the case the server cannot always absorb. `to_records` turns it
  // into null on the way out of /api/screen, but applyTicks writes straight
  // into S.rows from the tick stream, and the strip does its own
  // arithmetic. A formatter is the last line of defence and must not paint
  // the string "NaN" into a market-cap cell.
  for (const fn of ["nf", "fcap", "fvol", "fpct"]) {
    const got = ev(fn + "(NaN)");
    check(fn + "(NaN) is blank, not the word NaN", got === "",
          JSON.stringify(got));
  }
  check("fage(NaN) is blank too", ev("fage(NaN)") === "", ev("fage(NaN)"));
  check("has(NaN) is false, so the strip refuses it too",
        ev("has(NaN)") === false);

  console.log("\nformatting — zero is a real number and must survive");
  check("nf(0) renders", ev("nf(0,2)") === "0.00", ev("nf(0,2)"));
  check("fcap(0) renders", ev("fcap(0)") === "0", ev("fcap(0)"));
  check("fvol(0) renders", ev("fvol(0)") === "0", ev("fvol(0)"));
  check("fpct(0) carries no sign", ev("fpct(0)") === "0.00%", ev("fpct(0)"));

  console.log("\nformatting — the magnitude boundaries");
  check("fcap 999 stays plain", ev("fcap(999)") === "999", ev("fcap(999)"));
  check("fcap 1e3 becomes K", ev("fcap(1000)") === "1.0K", ev("fcap(1000)"));
  check("fcap 1e6 becomes M", ev("fcap(1e6)") === "1.0M", ev("fcap(1e6)"));
  check("fcap 1e9 becomes B", ev("fcap(1e9)") === "1.00B", ev("fcap(1e9)"));
  check("fcap 1e12 becomes T", ev("fcap(1e12)") === "1.00T", ev("fcap(1e12)"));
  check("a 4.5T company reads 4.50T", ev("fcap(4.51e12)") === "4.51T",
        ev("fcap(4.51e12)"));
  check("fvol 182M reads 182.0M", ev("fvol(182e6)") === "182.0M",
        ev("fvol(182e6)"));
  check("fvol keeps sub-thousand exact", ev("fvol(900)") === "900",
        ev("fvol(900)"));

  console.log("\nformatting — sign is never lost");
  check("a negative cap keeps its sign", ev("fcap(-2.5e9)") === "-2.50B",
        ev("fcap(-2.5e9)"));
  check("a fall is signed", ev("fpct(-16.7)") === "-16.70%", ev("fpct(-16.7)"));
  check("a rise is explicitly signed", ev("fpct(3.42)") === "+3.42%",
        ev("fpct(3.42)"));
  check("fmoney keeps the minus outside the currency",
        ev("fmoney(-1.5e9)") === "-$1.50B", ev("fmoney(-1.5e9)"));

  console.log("\nformatting — colour classes follow the number, not the string");
  check("a rise is up", ev("cls(1)") === "up");
  check("a fall is dn", ev("cls(-1)") === "dn");
  check("unchanged is flat", ev("cls(0)") === "flat");
  check("missing is flat, not up", ev("cls(null)") === "flat");

  console.log("\nformatting — ages");
  check("under an hour reads in minutes", ev("fage(120)") === "2m",
        ev("fage(120)"));
  check("hours are one decimal under ten", ev("fage(7200)") === "2.0h",
        ev("fage(7200)"));
  check("past two days it reads in days", ev("fage(86400*3)") === "3d",
        ev("fage(86400*3)"));

  /* ---------- escaping ---------- */
  // Company names, sectors and industries arrive from an upstream and go
  // into innerHTML. esc is the only thing in between.

  console.log("\nescaping — an upstream string cannot become markup");
  check("angle brackets are neutralised",
        ev(`esc('<script>alert(1)</script>')`)
        === "&lt;script&gt;alert(1)&lt;/script&gt;",
        ev(`esc('<script>alert(1)</script>')`));
  check("quotes are escaped, so an attribute cannot be broken out of",
        ev(`esc('a" onload="x')`) === 'a&quot; onload=&quot;x',
        ev(`esc('a" onload="x')`));
  check("ampersands are escaped first, not doubled",
        ev(`esc('AT&T')`) === "AT&amp;T", ev(`esc('AT&T')`));
  check("null becomes empty, not the word null", ev("esc(null)") === "");

  ev(`
    S.view = "screen";
    S.rows = [{symbol:"EVIL", name:'<img src=x onerror="window.__pwned=1">',
               price:1, change_pct:0, sector:"T", industry:"I"}];
    S.cursor = -1; S.hover = -1;
    drawRows(true);
  `);
  check("a hostile company name renders as text, not an element",
        !doc.querySelector('.r[data-i="0"] img'));
  check("and no handler on it ever ran", ev("window.__pwned") === undefined);

  /* ---------- virtualization ---------- */
  // The claim in the README is that 7,000 rows scroll like 40. What makes
  // that true is that only a window of them is ever in the DOM.

  console.log("\nvirtualization — a full universe is not a full DOM");
  ev(`
    Object.defineProperty(wrap, "clientHeight", {value: 400, configurable: true});
    S.view = "screen";
    S.rows = [];
    for (let i = 0; i < 7000; i++) {
      S.rows.push({symbol:"S"+i, name:"Company "+i, price:i+1,
                   change_pct:0, volume:1e6, market_cap:1e9,
                   sector:"Technology", industry:"Semis"});
    }
    S.cursor = -1; S.hover = -1;
    wrap.scrollTop = 0;
    drawRows(true);
  `);
  const rowCount = () => doc.querySelectorAll("#gridWrap .r").length;
  const n0 = rowCount();
  check("7,000 rows produce well under a hundred row elements",
        n0 > 0 && n0 < 100, String(n0));
  check("the spacer still reserves the full scroll height",
        ev("spacer.style.height") === (7000 * 19) + "px",
        ev("spacer.style.height"));
  check("the first row rendered is the first row of the universe",
        !!doc.querySelector('.r[data-i="0"]'));
  check("the last row is nowhere near the DOM",
        !doc.querySelector('.r[data-i="6999"]'));

  ev(`wrap.scrollTop = 19 * 1000; drawRows(true);`);
  const n1 = rowCount();
  // Mid-list renders a few more than the top does: drawRows overscans 8
  // rows either side, and at scrollTop 0 the ones above index 0 clamp
  // away. What matters is that the window stays a window — bounded, and
  // the same size whether the universe behind it is 40 rows or 7,000.
  check("scrolling moves the window rather than growing it",
        n1 < 100 && Math.abs(n1 - n0) <= 8, n1 + " vs " + n0);
  check("the window follows the scroll position",
        !!doc.querySelector('.r[data-i="1000"]')
        && !doc.querySelector('.r[data-i="0"]'));
  const [from, to] = ev("JSON.stringify(visibleRange())")
    ? JSON.parse(ev("JSON.stringify(visibleRange())")) : [0, 0];
  check("visibleRange brackets the scroll position",
        from <= 1000 && to >= 1000, from + ".." + to);
  check("visibleSymbols is what the stream would subscribe to — a viewport, "
        + "not a universe",
        ev("visibleSymbols().length") < 100
        && ev("visibleSymbols()[0]").startsWith("S"),
        String(ev("visibleSymbols().length")));
  check("rows are positioned by index, so the scrollbar stays honest",
        doc.querySelector('.r[data-i="1000"]').style.top === (1000 * 19) + "px",
        doc.querySelector('.r[data-i="1000"]').style.top);

  /* ---------- the token ---------- */
  // House rule 4. Without this header the API refuses the call, which is
  // what stops another page in the same browser driving this server.

  console.log("\nevery API call carries the per-launch token");
  calls.length = 0;
  ev(`api("/api/screen?expr=");`);
  check("a GET carries X-PM-Token", calls.length === 1
        && calls[0].opts.headers["X-PM-Token"] === "test-token",
        JSON.stringify(calls[0] && calls[0].opts.headers));
  calls.length = 0;
  ev(`api("/api/screens", {body:{name:"x", expr:"chg > 1"}});`);
  check("a write carries it too",
        calls[0].opts.headers["X-PM-Token"] === "test-token");
  check("a body makes it a POST", calls[0].opts.method === "POST",
        String(calls[0].opts.method));
  check("and is sent as JSON",
        calls[0].opts.headers["Content-Type"] === "application/json");
  check("the body is serialised, not passed as an object",
        typeof calls[0].opts.body === "string", typeof calls[0].opts.body);
  check("nothing leaks the token into the URL",
        !calls[0].url.includes("test-token"), calls[0].url);

  /* ---------- sorting ---------- */

  console.log("\nsorting by a column header");
  ev(`S.sort = "market_cap"; S.dir = "desc"; buildHead();`);
  const head = (k) => doc.querySelector('#gridHead div[data-k="' + k + '"]');
  check("the sorted column is marked in the header",
        head("market_cap").textContent.includes("▾"),
        head("market_cap").textContent);
  head("market_cap").onclick();
  check("clicking the sorted column flips direction",
        ev("S.dir") === "asc" && ev("S.sort") === "market_cap", ev("S.dir"));
  check("and the arrow follows", head("market_cap").textContent.includes("▴"),
        head("market_cap").textContent);
  head("volume").onclick();
  check("clicking a new numeric column sorts it biggest-first",
        ev("S.sort") === "volume" && ev("S.dir") === "desc",
        ev("S.sort") + "/" + ev("S.dir"));
  head("name").onclick();
  check("but a text column sorts A-Z, which is the useful direction there",
        ev("S.sort") === "name" && ev("S.dir") === "asc",
        ev("S.sort") + "/" + ev("S.dir"));

  /* ---------- watchlist dot ---------- */

  console.log("\nthe watchlist dot keeps the grid and the panel agreeing");
  ev(`
    S.view = "screen";
    wrap.scrollTop = 0;          // the virtualization block left it mid-list
    S.watch.symbols = new Set(["BBB"]);
    S.rows = [{symbol:"AAA", name:"A", price:1, change_pct:0},
              {symbol:"BBB", name:"B", price:2, change_pct:0}];
    S.cursor = -1; S.hover = -1;
    drawRows(true);
  `);
  const tick = (i) => doc.querySelector('.r[data-i="' + i + '"] div[data-c="symbol"]');
  check("a watched symbol is dotted", tick(1).textContent.includes("•"),
        tick(1).textContent);
  check("an unwatched one is not", !tick(0).textContent.includes("•"),
        tick(0).textContent);
  check("and the row itself is marked for the stream to keep",
        doc.querySelector('.r[data-i="1"]').classList.contains("watched"));

  /* ---------- command history ---------- */

  console.log("\ncommand history");
  const input = doc.getElementById("cmdIn");
  const key = (k) => input.dispatchEvent(
    new win.KeyboardEvent("keydown", { key: k, bubbles: true }));
  ev(`S.history = []; S.hpos = -1;`);
  for (const cmd of ["heat", "map", "adr"]) {
    input.value = cmd;
    key("Enter");
  }
  check("commands are remembered", ev("S.history.length") >= 3,
        String(ev("S.history.length")));
  input.value = "";
  key("ArrowUp");
  check("ArrowUp recalls the most recent command", input.value === "adr",
        input.value);
  key("ArrowUp");
  check("and walks back through them", input.value === "map", input.value);
  key("ArrowDown");
  check("ArrowDown walks forward again", input.value === "adr", input.value);

  /* ---------- treemap geometry ---------- */
  // squarify is pure arithmetic and the heatmap is unreadable if it is
  // wrong, but nothing was checking the invariants.

  console.log("\ntreemap geometry");
  const geo = JSON.parse(ev(`JSON.stringify(squarify(
    [{value:50},{value:30},{value:15},{value:5}], 0, 0, 400, 300, []))`));
  check("every item gets a rectangle", geo.length === 4, String(geo.length));
  const area = geo.reduce((a, r) => a + r.w * r.h, 0);
  check("the rectangles fill the box, near enough",
        Math.abs(area - 400 * 300) / (400 * 300) < 0.02,
        area.toFixed(0) + " of " + (400 * 300));
  check("area is proportional to value — the biggest is ten times the "
        + "smallest", Math.abs((geo[0].w * geo[0].h) / (geo[3].w * geo[3].h) - 10)
        < 0.5, String((geo[0].w * geo[0].h) / (geo[3].w * geo[3].h)));
  check("nothing escapes the box", geo.every(r =>
        r.x >= -0.01 && r.y >= -0.01
        && r.x + r.w <= 400.01 && r.y + r.h <= 300.01));
  check("no rectangle is inside out",
        geo.every(r => r.w > 0 && r.h > 0));
  const zero = JSON.parse(ev(
    `JSON.stringify(squarify([{value:0},{value:-5}], 0,0,100,100, []))`));
  check("a sector with no market cap is dropped rather than drawn at zero "
        + "width", zero.length === 0, String(zero.length));

  console.log("\n" + pass + " passed, " + fail + " failed\n");
  dom.window.close();
  process.exit(fail ? 1 : 0);
}, 900);
