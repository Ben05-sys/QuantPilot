/* Boot the terminal in jsdom and assert it survives.
 *
 * `node --check` only catches syntax errors. The failure that actually
 * bites is a runtime throw at load: one bad line kills every handler
 * registered after it, and the page renders but does nothing. That is
 * indistinguishable from a working UI until you click something.
 *
 * jsdom has no canvas, so every getContext() call returns a stub here.
 * This tests wiring, not pixels.
 *
 *   node tests/ui_smoke.js
 */
const fs = require("fs");
const path = require("path");

// jsdom is a dev-only dependency and deliberately not vendored here. Try
// the usual places in order, then give up quietly — a machine without it
// skips this suite rather than failing it. JSDOM_PATH overrides.
const CANDIDATES = [
  process.env.JSDOM_PATH,
  "jsdom",                                            // installed normally
  path.join(__dirname, "node_modules", "jsdom"),
  // A sibling checkout that already has it, whatever the checkout is called.
  path.join(__dirname, "..", "..", "OfflinePilotX",
            "tests", "node_modules", "jsdom"),
].filter(Boolean);

let JSDOM;
for (const candidate of CANDIDATES) {
  try {
    ({ JSDOM } = require(candidate));
    break;
  } catch (e) { /* try the next one */ }
}
if (!JSDOM) {
  console.log("  skip  jsdom not installed — `npm i jsdom` in tests/ to "
              + "run the UI smoke suite");
  process.exit(0);
}

const HTML = path.join(__dirname, "..", "app", "web", "index.html");
let html = fs.readFileSync(HTML, "utf8");
html = html.replace("<head>", '<head><script>window.PM_TOKEN="test";</script>');

let pass = 0, fail = 0;
function check(label, ok, detail) {
  if (ok) { pass++; console.log("  ok   " + label); }
  else { fail++; console.log("  FAIL " + label + (detail ? "  " + detail : "")); }
}

// A canvas stub. Every 2d-context method the terminal calls is a no-op;
// measureText has to return something with a width or drawChart divides
// by undefined.
const ctxStub = new Proxy({}, {
  get(_, key) {
    if (key === "measureText") return () => ({ width: 40 });
    if (key === "createLinearGradient") {
      return () => ({ addColorStop() {} });
    }
    if (key === "canvas") return { width: 800, height: 600 };
    return typeof key === "string" ? () => {} : undefined;
  },
  set() { return true; },
});

const errors = [];
const dom = new JSDOM(html, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  // localStorage needs a real origin; on the default about:blank jsdom
  // throws a SecurityError on every access.
  url: "http://127.0.0.1:8900/",
  beforeParse(win) {
    win.HTMLCanvasElement.prototype.getContext = () => ctxStub;
    win.devicePixelRatio = 1;
    // The page fetches on boot; jsdom has no server. Every call rejects,
    // which is itself worth testing — the UI must survive a dead backend.
    win.fetch = () => Promise.reject(new Error("no server in smoke test"));
    win.EventSource = function () {
      this.addEventListener = () => {};
      this.close = () => {};
    };
    win.addEventListener("error", (e) =>
      errors.push(e.error ? (e.error.stack || e.error.message) : e.message));
  },
});

const win = dom.window;
const doc = win.document;

setTimeout(() => {
  console.log("\nboot");
  check("no uncaught error while loading", errors.length === 0,
        errors[0]);

  console.log("\nchrome");
  for (const id of ["brand", "clock", "stateBadge", "lagBadge", "streamBadge",
                    "strip", "cmdIn", "gridHead", "gridWrap"]) {
    check("#" + id + " exists", !!doc.getElementById(id));
  }

  console.log("\nviews");
  for (const v of ["screen", "map", "ticker", "saved", "world", "adr",
                   "earnings", "news"]) {
    check("view-" + v + " exists", !!doc.getElementById("view-" + v));
  }
  const tabs = [...doc.querySelectorAll(".tab")].map(t => t.dataset.view);
  check("every tab points at a real view",
        tabs.every(v => v === "help" || doc.getElementById("view-" + v)),
        tabs.join(","));
  check("ten tabs in the rail", tabs.length === 10, String(tabs.length));
  check("the live news view is reachable and has a player and a rail",
        !!doc.getElementById("view-livenews") && !!doc.getElementById("lnPlayer")
        && !!doc.getElementById("lnMentions"));
  check("futures strip exists", !!doc.getElementById("futures"));
  check("watch panel exists", !!doc.getElementById("watchPanel"));

  console.log("\nnew controls are wired");
  for (const id of ["wBubbles", "wTiles", "wAll", "wAdr"]) {
    const el = doc.getElementById(id);
    check("#" + id + " has a click handler", !!(el && el.onclick));
  }
  for (const id of ["adrCountry", "adrSort"]) {
    const el = doc.getElementById(id);
    check("#" + id + " has a change handler", !!(el && el.onchange));
  }
  check("#world canvas exists", !!doc.getElementById("world"));
  check("#adrWrap exists", !!doc.getElementById("adrWrap"));

  console.log("\nswitching views does not throw");
  const before = errors.length;
  for (const v of ["world", "adr", "earnings", "news", "map", "ticker",
                   "saved", "screen"]) {
    const tab = doc.querySelector('.tab[data-view="' + v + '"]');
    if (tab) tab.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  }
  check("no error from clicking through every view",
        errors.length === before, errors[before]);

  console.log("\nlayout toggles do not throw");
  const before2 = errors.length;
  doc.getElementById("wTiles").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  doc.getElementById("wBubbles").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  check("bubbles/tiles toggle cleanly", errors.length === before2,
        errors[before2]);

  console.log("\ndirectional flash");
  // `const S` at script top level lives in the global *lexical* scope, so
  // it is not a window property — but a global eval can still see it.
  // This is the one feature whose entire point is which colour appears,
  // so it gets driven directly rather than merely wired.
  win.eval(`
    S.view = "screen";
    S.rows = [
      {symbol:"AAA", price:100, change_pct:1, name:"A", sector:"T"},
      {symbol:"BBB", price:200, change_pct:-1, name:"B", sector:"T"},
      {symbol:"CCC", price:300, change_pct:0, name:"C", sector:"T"},
    ];
    drawRows(true);
    applyTicks({
      AAA:{price:101},   // up
      BBB:{price:199},   // down
      CCC:{price:300},   // unchanged — a heartbeat, not a trade
    });
  `);
  const cellOf = (i, key) => doc.querySelector(
    '.r[data-i="' + i + '"] div[data-c="' + key + '"]');
  const up = cellOf(0, "price"), dn = cellOf(1, "price"), same = cellOf(2, "price");
  check("an uptick flashes green",
        !!up && up.classList.contains("tick") && up.classList.contains("up"),
        up && up.className);
  check("a downtick flashes red",
        !!dn && dn.classList.contains("tick") && dn.classList.contains("dn"),
        dn && dn.className);
  check("a repeated price does not flash at all",
        !!same && !same.classList.contains("tick"), same && same.className);
  check("the volume cell never flashes",
        !cellOf(0, "volume") || !cellOf(0, "volume").classList.contains("tick"));
  check("the whole row is not washed",
        !doc.querySelector('.r[data-i="0"]').classList.contains("tick"));
  check("the price actually updated", win.eval("S.rows[0].price") === 101);
  check("direction is cleared after painting, so it flashes once",
        win.eval("S.rows[0]._dir") === null,
        String(win.eval("S.rows[0]._dir")));

  // Outside regular hours the server strips `price` off a tick and sends
  // the print as an extended-hours move instead. Nothing may flash, and
  // the closing price must survive untouched.
  win.eval(`
    S.rows = [{symbol:"AAA", price:100, change_pct:1, ext_change_pct:0,
               name:"A", sector:"T"}];
    S.cursor = -1; S.hover = -1;
    drawRows(true);
    applyTicks({AAA:{ext_price:104, ext_change_pct:4,
                     ext_label:"OVERNIGHT", market_state:"PREPRE"}});
  `);
  check("an overnight print does not flash the price",
        !cellOf(0, "price").classList.contains("tick"),
        cellOf(0, "price").className);
  check("and does not overwrite the closing price",
        win.eval("S.rows[0].price") === 100,
        String(win.eval("S.rows[0].price")));
  check("the overnight move still shows in EXT%",
        win.eval("S.rows[0].ext_change_pct") === 4,
        String(win.eval("S.rows[0].ext_change_pct")));

  console.log("\nkeyboard navigation");
  win.eval(`
    S.view = "screen";
    S.rows = [{symbol:"AAA",price:1},{symbol:"BBB",price:2},
              {symbol:"CCC",price:3},{symbol:"DDD",price:4}];
    S.cursor = -1;
    drawRows(true);
  `);
  const key = (k) => doc.dispatchEvent(
    new win.KeyboardEvent("keydown", { key: k, bubbles: true }));
  key("ArrowDown");
  check("first ArrowDown lands on row 0", win.eval("S.cursor") === 0,
        String(win.eval("S.cursor")));
  key("ArrowDown"); key("ArrowDown");
  check("ArrowDown walks the grid", win.eval("S.cursor") === 2,
        String(win.eval("S.cursor")));
  key("ArrowUp");
  check("ArrowUp walks back", win.eval("S.cursor") === 1);
  key("End");
  check("End jumps to the last row",
        win.eval("S.cursor") === 3, String(win.eval("S.cursor")));
  key("Home");
  check("Home jumps to the first row", win.eval("S.cursor") === 0);
  for (let i = 0; i < 10; i++) key("ArrowUp");
  check("cursor cannot run off the top", win.eval("S.cursor") === 0);
  for (let i = 0; i < 30; i++) key("ArrowDown");
  check("cursor cannot run off the bottom", win.eval("S.cursor") === 3);
  check("the selection follows the cursor",
        win.eval("S.sel") === "DDD", win.eval("S.sel"));
  check("the cursor row is marked",
        !!doc.querySelector(".r.cur"));

  console.log("\ncursor detail strip");
  const strip = doc.getElementById("cstrip");
  check("#cstrip exists", !!strip);
  win.eval(`
    S.view = "screen";
    S.rows = [
      // 40M shares at $2 and 40M at $200: the same VOLUME column, two
      // completely different markets. The strip is where that shows.
      {symbol:"CHEAP", name:"C", price:2, change_pct:1, volume:40e6,
       avg_volume_3m:20e6, rel_volume:4, rel_volume_raw:2,
       day_low:1.9, day_high:2.5, open:2.4, prev_close:1.98},
      {symbol:"DEAR", name:"D", price:200, change_pct:-1, volume:40e6,
       avg_volume_3m:20e6, rel_volume:2, rel_volume_raw:2,
       day_low:190, day_high:200, open:195, prev_close:202},
      // No range and no averages — the row that must not throw.
      {symbol:"BARE", name:"B", price:5, change_pct:0, volume:null,
       avg_volume_3m:null, day_low:null, day_high:null},
    ];
    S.cursor = -1; S.hover = -1;
    drawRows(true); paintStrip();
  `);
  check("with no row picked it explains itself",
        /hover a row/.test(doc.getElementById("csLine1").textContent),
        doc.getElementById("csLine1").textContent);

  win.eval("S.cursor = 0; S.hover = -1; paintStrip();");
  let l1 = doc.getElementById("csLine1").textContent;
  let l2 = doc.getElementById("csLine2").textContent;
  check("the strip names the row", /CHEAP/.test(l1), l1);
  check("share volume is shown", /40\.0M/.test(l1), l1);
  check("dollar volume is shown, and it is money not shares",
        /\$80\.0M/.test(l1), l1);
  // rel_volume 4 over rel_volume_raw 2 means the server judged half the
  // session gone, so 2x raw volume is running at 4x the pace.
  check("pace is the server's time-adjusted figure, not raw",
        /400% of normal/.test(l1), l1);
  check("the typical day's money is shown for comparison",
        /\$40\.0M/.test(l1), l1);
  check("the day range is drawn", !!doc.getElementById("csBar"));
  const pos = () => doc.querySelector("#csLine2 .pos").textContent;
  check("sold into the low reads near zero", pos() === "17%", pos());

  win.eval("S.cursor = 1; paintStrip();");
  l1 = doc.getElementById("csLine1").textContent;
  l2 = doc.getElementById("csLine2").textContent;
  check("same share volume, a hundred times the money",
        /\$8\.00B/.test(l1), l1);
  check("closing on the high reads 100", pos() === "100%", pos());

  win.eval("S.cursor = 2; paintStrip();");
  check("a row with no range says so rather than drawing a fake one",
        /no range yet/.test(doc.getElementById("csLine2").textContent)
        && !doc.getElementById("csBar"),
        doc.getElementById("csLine2").textContent);

  // A print through the snapshot's high is the new high — otherwise the
  // bar pegs at 100% and the day's width is understated.
  win.eval(`S.cursor = 1; applyTicks({DEAR:{price:205, volume:60e6}});`);
  check("a tick through the high widens the range",
        win.eval("S.rows[1].day_high") === 205,
        String(win.eval("S.rows[1].day_high")));
  check("and the strip follows the tick",
        /\$12\.30B/.test(doc.getElementById("csLine1").textContent),
        doc.getElementById("csLine1").textContent);

  // Hover beats the cursor, then hands it back.
  win.eval("S.cursor = 0; setHover(1);");
  check("hover takes over the strip",
        /DEAR/.test(doc.getElementById("csLine1").textContent));
  check("and marks the hovered row", !!doc.querySelector(".r.hov"));
  win.eval("setHover(-1);");
  check("leaving the grid hands the strip back to the cursor",
        /CHEAP/.test(doc.getElementById("csLine1").textContent));
  check("and unmarks the row", !doc.querySelector(".r.hov"));
  check("no error from driving the strip", errors.length === 0, errors[0]);

  console.log("\npersistence");
  win.eval(`
    S.filters = {cap:"Large >10B"};
    S.expr = "chg > 3";
    S.sort = "volume"; S.dir = "asc";
    S.world.layout = "tiles"; S.world.scope = "adr";
    S.news.strict = true;
    saveUi(true);
  `);
  const raw = win.localStorage.getItem("quantpilot.ui.v1");
  check("state is written to localStorage", !!raw);
  const stored = JSON.parse(raw || "{}");
  check("filters are stored", stored.filters.cap === "Large >10B");
  check("expression is stored", stored.expr === "chg > 3");
  check("sort is stored", stored.sort === "volume" && stored.dir === "asc");
  check("map layout is stored", stored.world.layout === "tiles");
  check("news preference is stored", stored.newsStrict === true);
  check("news list exists", !!doc.getElementById("newsList"));
  check("state round-trips back in",
        win.eval("(function(){S.filters={};S.expr='';loadUi();"
                 + "return S.expr + '|' + S.filters.cap + '|' + S.sort;})()")
        === "chg > 3|Large >10B|volume");
  win.localStorage.setItem("quantpilot.ui.v1", "{not json");
  check("corrupt saved state is discarded, not fatal",
        win.eval("(function(){try{return loadUi()===null;}"
                 + "catch(e){return 'threw: '+e.message;}})()") === true);

  console.log("\narticle reader");
  check("reader overlay exists", !!doc.getElementById("reader"));
  check("it starts hidden",
        doc.getElementById("reader").style.display !== "block");
  win.eval(`
    S.news.symbol = "NVDA";
    S.news.items = [{uuid:"abc", title:"A headline", publisher:"Reuters",
                     link:"https://example.com/x", age_seconds:120,
                     related:["NVDA","AMD"]}];
    drawNews();
  `);
  const nrow = doc.querySelector("#newsList .nrow");
  check("a news row renders", !!nrow);
  check("the row shows other tagged tickers — so a roundup looks like one",
        !!nrow && nrow.textContent.includes("AMD"));
  nrow.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  check("clicking opens the reader in the terminal, not a new tab",
        doc.getElementById("reader").style.display === "block");
  check("the headline is shown while it loads",
        doc.getElementById("rdTitle").textContent === "A headline");
  check("the original stays one click away",
        doc.getElementById("rdLink").getAttribute("href")
        === "https://example.com/x");
  doc.dispatchEvent(new win.KeyboardEvent("keydown",
    { key: "Escape", bubbles: true }));
  check("Escape closes it",
        doc.getElementById("reader").style.display !== "block");

  console.log("\nsearch bar");
  check("search box exists", !!doc.getElementById("search"));
  win.eval(`
    S.search.results = [
      {symbol:"NVDA", name:"NVIDIA Corporation", exchange:"NASDAQ"},
      {symbol:"NVDX", name:"T-Rex 2X Long NVIDIA", exchange:"BATS"},
    ];
    S.search.cursor = -1;
    drawSearch();
  `);
  check("results render",
        doc.querySelectorAll("#searchDrop .sr").length === 2);
  check("the dropdown opens",
        doc.getElementById("searchDrop").style.display === "block");
  const sbox = doc.getElementById("search");
  sbox.dispatchEvent(new win.KeyboardEvent("keydown",
    { key: "ArrowDown", bubbles: true }));
  check("ArrowDown highlights the first result",
        win.eval("S.search.cursor") === 0, String(win.eval("S.search.cursor")));
  sbox.dispatchEvent(new win.KeyboardEvent("keydown",
    { key: "Escape", bubbles: true }));
  check("Escape clears it", win.eval("S.search.results.length") === 0);

  console.log("\nmarket news");
  win.eval(`
    S.mkt.items = [
      {uuid:"m1", title:"Hot one", publisher:"Reuters", link:"http://x/1",
       hot:82, age_seconds:600, lead:"CVLT", lead_change:-16.7,
       related:["CVLT"]},
      {uuid:"m2", title:"Broad market piece", publisher:"AP",
       link:"http://x/2", hot:20, age_seconds:7200, lead:null,
       lead_change:null, related:[]},
    ];
    S.mkt.movers = [{symbol:"CVLT", change_pct:-16.7}];
    drawMarketNews();
  `);
  const mrows = doc.querySelectorAll("#mktNews .mrow");
  check("market rows render", mrows.length === 2, String(mrows.length));
  check("the ticker sits on the news bar itself",
        mrows[0].textContent.includes("CVLT"));
  check("its move is shown next to it",
        mrows[0].textContent.includes("16.7"));
  check("a story about no single company reads MARKET",
        mrows[1].textContent.includes("MARKET"));
  check("a hot story is marked and a cold one is not",
        mrows[0].classList.contains("veryhot")
        && !mrows[1].classList.contains("veryhot"));
  mrows[0].dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  check("clicking a market story opens the reader",
        doc.getElementById("reader").style.display === "block");
  doc.dispatchEvent(new win.KeyboardEvent("keydown",
    { key: "Escape", bubbles: true }));

  console.log("\nrelated companies");
  check("related panel exists", !!doc.getElementById("relList"));
  win.eval(`
    S.rel.symbol = "AAPL";
    S.rel.rows = [
      {symbol:"QCOM", company:"QUALCOMM INC", filings:16, kind:"peer",
       change_pct:-4.2},
      {symbol:"LQMT", company:"LIQUIDMETAL TECHNOLOGIES", filings:21,
       kind:"chain", change_pct:null},
    ];
    drawRelated();
  `);
  const rels = doc.querySelectorAll("#relList .rel");
  check("related rows render", rels.length === 2, String(rels.length));
  check("a same-sector name is marked a peer",
        rels[0].textContent.includes("peer"));
  check("a different-sector name is marked supply chain",
        rels[1].textContent.includes("chain"));
  check("the filing count is shown", rels[0].textContent.includes("16"));

  console.log("\nchart compare");
  check("compare box exists", !!doc.getElementById("cmpSym"));
  win.eval(`S.compare.symbol = "SPY"; S.compare.bars = {c:[100,101,102]};`);
  check("compare state holds", win.eval("S.compare.symbol") === "SPY");
  doc.getElementById("cmpClear").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true }));
  check("clearing it empties the box",
        doc.getElementById("cmpSym").value === "");

  console.log("\nalerts");
  check("alert bell exists", !!doc.getElementById("alertBell"));
  check("alerts start off", win.eval("S.alerts.enabled") === false);
  win.eval(`
    fireAlerts([{screen:"Momentum", symbol:"NVDA", at:1},
                {screen:"Momentum", symbol:"AMD", at:1}]);
  `);
  check("arrivals are recorded", win.eval("S.alerts.list.length") === 2);
  check("the bell shows a count",
        doc.getElementById("alertBell").textContent.includes("2"));
  check("nothing pops up while alerts are off — no permission prompt on a "
        + "page you just opened", win.eval("S.alerts.enabled") === false);

  console.log("\nbuild stamp");
  check("build stamp element exists", !!doc.getElementById("buildInfo"));

  console.log("\ncommand line survives a dead backend");
  const before3 = errors.length;
  const input = doc.getElementById("cmdIn");
  for (const cmd of ["help", "adr", "map", "heat", "NVDA", "screen adr"]) {
    input.value = cmd;
    input.dispatchEvent(new win.KeyboardEvent("keydown",
      { key: "Enter", bubbles: true }));
  }
  check("no error from typing commands", errors.length === before3,
        errors[before3]);

  console.log("\n" + pass + " passed, " + fail + " failed\n");
  dom.window.close();
  process.exit(fail ? 1 : 0);
}, 900);
