"""The dashboard: one static page from one run of tools/measure.py.

    PYTHONPATH=. python -m tools.dashboard MEASURE.json OUT_DIR

Writes OUT_DIR/index.html (self-contained: the numbers ride in the page, no outside
code) and OUT_DIR/latest.json (the measurement). Nothing is read from an earlier run and
nothing is kept for a later one: each run's page is its own computation. Only the
workflow (.github/workflows/measure.yml) publishes it.
"""
from __future__ import annotations

import argparse
import json
import os


def render(result: dict) -> str:
    payload = json.dumps(result, separators=(",", ":"), default=int).replace("</", "<\\/")
    return PAGE.replace("__PAYLOAD__", payload)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("measure")
    parser.add_argument("out_dir")
    args = parser.parse_args(argv)
    with open(args.measure, encoding="utf-8") as f:
        result = json.load(f)
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(render(result))
    with open(os.path.join(args.out_dir, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, default=int)
    print(f"wrote {args.out_dir}/index.html")
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jump measured</title>
<style>
:root{
  color-scheme:light;
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10); --series:#2a78d6;
  --good:#0ca30c; --warning:#8f5c0d; --critical:#d03b3b; --wash:#2a78d614;
}
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme="light"])){
    color-scheme:dark;
    --page:#0d0d0d; --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10); --series:#3987e5;
    --warning:#fab219; --wash:#3987e51f;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --page:#0d0d0d; --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10); --series:#3987e5;
  --warning:#fab219; --wash:#3987e51f;
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink2);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px 64px}
header{padding:32px 0 8px}
h1{margin:0;color:var(--ink);font-size:clamp(1.6rem,4vw,2.2rem);letter-spacing:-.02em}
h2{margin:0 0 4px;color:var(--ink);font-size:1.2rem}
h3{margin:18px 0 6px;color:var(--ink);font-size:.98rem}
p{margin:0}
.meta{color:var(--muted);font-size:.84rem;margin-top:6px}
.meta b{color:var(--ink2);font-weight:600}
a{color:var(--series)}
section{margin-top:40px}
.note{color:var(--muted);font-size:.85rem;max-width:84ch;margin-bottom:12px}
.dim{color:var(--muted)}
.panel{background:var(--surface);border:1px solid var(--ring);border-radius:6px;padding:16px}
.tiles{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));margin-top:20px}
.tile .k{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.06em}
.tile .v{color:var(--ink);font-size:2rem;font-weight:700;line-height:1.15;margin-top:4px}
.tile .w{color:var(--muted);font-size:.82rem;margin-top:4px}
.status{display:inline-flex;gap:6px;align-items:center;font-weight:600}
.status.ok{color:var(--good)} .status.bad{color:var(--critical)} .status.warn{color:var(--warning)}
.ctl{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.ctl label{font-size:.82rem;color:var(--muted)}
select,input{font:inherit;font-size:.85rem;color:var(--ink);background:var(--surface);
  border:1px solid var(--ring);border-radius:4px;padding:5px 8px}
.tablebox{overflow:auto;max-height:640px;background:var(--surface);border:1px solid var(--ring);border-radius:6px}
table{border-collapse:collapse;width:100%;font-size:.86rem;min-width:520px}
th,td{padding:6px 10px;border-bottom:1px solid var(--grid);text-align:left;white-space:nowrap}
th{position:sticky;top:0;background:var(--surface);color:var(--muted);font-weight:600;
  font-size:.74rem;text-transform:uppercase;letter-spacing:.04em;z-index:1}
th.s{cursor:pointer} th.s:hover{color:var(--ink)}
th[data-dir="-1"]::after{content:" ↓"} th[data-dir="1"]::after{content:" ↑"}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
td.name{color:var(--ink)}
td.long{white-space:normal;min-width:260px}
tbody tr:hover td{background:var(--wash)}
tbody tr:last-child td{border-bottom:0}
caption{caption-side:bottom;text-align:left;padding:8px 10px;color:var(--muted);font-size:.8rem}
.chart svg{display:block;width:100%;height:auto;overflow:visible}
.chart text{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.chart .grid{stroke:var(--grid);stroke-width:1}
.chart .base{stroke:var(--axis);stroke-width:1}
.chart .bar{fill:var(--series)}
.chart .hit{fill:transparent;cursor:crosshair}
.chart .hit:hover + .bar, .chart .bar:hover{opacity:.75}
#tip{position:fixed;pointer-events:none;z-index:10;background:var(--surface);color:var(--ink);
  border:1px solid var(--ring);border-radius:4px;padding:6px 8px;font-size:.78rem;
  box-shadow:0 4px 16px rgba(0,0,0,.12);opacity:0;white-space:nowrap}
.grid2{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
footer{margin-top:48px;color:var(--muted);font-size:.82rem;max-width:90ch}
.theme{float:right;margin-top:4px}
.saw{color:var(--good)} .out{color:var(--muted)}
</style>
</head>
<body>
<div class="wrap">
<header>
  <select class="theme" id="theme" aria-label="Theme">
    <option value="">system theme</option><option value="light">light</option><option value="dark">dark</option>
  </select>
  <h1>Jump, measured</h1>
  <p class="meta" id="meta"></p>
  <p class="meta" id="meta2"></p>
  <p class="meta" id="checks"></p>
  <p class="meta" id="behind"></p>
  <div class="tiles" id="tiles"></div>
</header>

<section id="volume">
  <h2>Volume, the detector alone</h2>
  <p class="note">An event is an instrument's flags inside 24 hours from its first, worded by
  its rarest; it counts in the window its first reading was found in. A push rings at once;
  a note row goes into the weekly note. Every reading counts here: no vote. What the votes
  change is below, inside the windows the bot votes in.</p>
  <div class="tablebox"><table id="t-volume"></table></div>
</section>

<section id="votes">
  <h2>The votes, taken again by this code</h2>
  <p class="note" id="votes-note"></p>
  <div class="tiles" id="vote-tiles"></div>
  <h3>By window</h3>
  <div class="tablebox"><table id="t-window"></table></div>
  <div class="grid2">
    <div><h3>By source</h3><div class="tablebox"><table id="t-source"></table></div></div>
    <div><h3>By block</h3><div class="tablebox"><table id="t-vblock"></table></div></div>
  </div>
  <h3>What the votes change, by instrument</h3>
  <div class="tablebox"><table id="t-effect"></table></div>
  <h3>Every vote</h3>
  <div class="ctl">
    <label for="vq">Instrument</label><input id="vq" type="search" placeholder="filter">
    <select id="vv" aria-label="Verdict"><option value="">every verdict</option>
      <option>real</option><option>uncertain</option><option value="not_real">not real</option>
      <option>overnight</option></select>
    <select id="vf" aria-label="Size"><option value="">every candidate</option>
      <option value="flag">flagged (at the bottom level or more)</option></select>
  </div>
  <div class="tablebox"><table id="t-votes"></table></div>
</section>

<section id="split">
  <h2>By block and by instrument, the detector alone</h2>
  <div class="ctl">
    <label for="search">Instrument</label>
    <input id="search" type="search" placeholder="filter">
    <select id="block" aria-label="Block"><option value="">all blocks</option></select>
  </div>
  <div class="tablebox"><table id="t-block"></table></div>
  <div class="tablebox" style="margin-top:12px"><table id="t-inst"></table></div>
</section>

<section id="archive">
  <h2>The whole archive, by year</h2>
  <p class="note">Every year since the first stored bar, scored by this code, the detector
  alone (no year before the voted one has votes). Instruments start in different years, so
  each year names the instrument-years it rests on; compare years per instrument-year. The
  last year is partial.</p>
  <div class="panel chart" id="c-archive"></div>
  <div class="tablebox" style="margin-top:12px"><table id="t-archive"></table></div>
</section>

<section id="biggest">
  <div class="grid2">
    <div>
      <h2>The biggest hours of each record</h2>
      <p class="note">Each instrument's top 0.01% of settled hours by size of move, and the word
      the detector gave each - the hours nobody would want missed.</p>
      <div class="tablebox"><table id="t-big"></table></div>
    </div>
    <div>
      <h2>Checks</h2>
      <p class="note">The page's own counts, added across. A check that fails stops the page
      from being published.</p>
      <div class="tablebox"><table id="t-checks"></table></div>
    </div>
  </div>
</section>

<section id="limits">
  <h2>Provider limits</h2>
  <p class="note">Each keyed provider's limit and how it is shared
  (<code>config/provider_limits.csv</code>), against what was asked: the usage files
  (<code>data/jump/usage</code>, written by the hourly run and the history walks), the last 30
  days of a day's limit and the last two months of a month's. Twelve Data counts credits, one
  per symbol. An hour's or a minute's limit is the provider's own 429, shown with its shares.
  A day or month past 80% of its limit goes to the health chat.</p>
  <div class="tablebox"><table id="t-limits"></table></div>
</section>

<section id="coverage">
  <h2>Coverage</h2>
  <p class="note">Each instrument's stored history as measured. A last bar well before the
  measurement means its newest hours were not in the store this run had.</p>
  <div class="tablebox"><table id="t-cover"></table></div>
</section>

<footer>Measured by <code>tools/measure.py</code> - the metrics built cold by
<code>jump.pipeline</code>, scored by <code>jump.jumps.run</code>, a push by
<code>routing.PUSH_TIERS</code>, the votes by <code>jump.verify</code> on the voters' bars
fetched by <code>tools/voter_bars.py</code> - and drawn by <code>tools/dashboard.py</code>,
in one run of <code>.github/workflows/measure.yml</code>. Nothing is kept between runs; the
bot reads none of it.</footer>
</div>
<div id="tip" role="status" aria-live="polite"></div>
<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
(function(){
const M = JSON.parse(document.getElementById("payload").textContent);
const V = M.votes;
const WORDS = ["noticeable","high","major","extreme"];
const VERDICTS = ["real","uncertain","not_real","overnight"];
const VNAME = {real:"real", uncertain:"uncertain", not_real:"not real", overnight:"overnight"};
const W = M.windows.map(w => w.name);
const $ = id => document.getElementById(id);
const fmt = (n, d=0) => n == null ? "—" : Number(n).toLocaleString("en-US",
  {minimumFractionDigits:d, maximumFractionDigits:d});
const n0 = v => fmt(v);
const day = t => t == null ? "—" : new Date(t*1000).toISOString().slice(0,10);
const hour = t => t == null ? "—" : new Date(t*1000).toISOString().slice(0,16).replace("T"," ");
const pct = m => m == null ? "outage" : (m >= 0 ? "+" : "") + (m*100).toFixed(2) + "%";
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const repo = "https://github.com/conjuror123/Market-alert";

// theme
const th = $("theme");
try { th.value = localStorage.getItem("theme") || ""; } catch(e) {}
const setTheme = v => { if (v) document.documentElement.dataset.theme = v;
  else delete document.documentElement.dataset.theme; };
setTheme(th.value);
th.onchange = () => { setTheme(th.value); try { localStorage.setItem("theme", th.value); } catch(e) {} };

// header
const meta = M.meta;
$("meta").innerHTML = `Commit <a href="${repo}/commit/${esc(meta.commit)}"><b>${esc(String(meta.commit).slice(0,7))}</b></a>
  on ${esc(meta.branch)} · ${esc(meta.subject)} · measured ${esc(meta.measured_at)}`;
$("meta2").innerHTML = `<b>${meta.instruments}</b> instruments · levels <b>${meta.levels.join(" / ")}σ</b>
  · pushed: ${esc(meta.push_words.join(", "))} · half-year window ${meta.window_days} days
  · measured to ${hour(meta.end)} UTC · newest bar ${hour(meta.last_bar)}`;
const failed = M.checks.filter(c => !c.ok);
const behind = meta.behind || [];
$("behind").innerHTML = behind.length ? `<span class="status warn">⚠ ${behind.length} instrument(s)
  with no bar in the 4 days to the measurement - their newest hours were not in the store this
  run had (a market holiday does this too):</span> ${behind.map(b => `${esc(b.ticker)} (${esc(b.provider)}, last ${hour(b.last)})`).join(", ")}` : "";
const partial = V && V.fetched.failed.length;
$("checks").innerHTML = (failed.length
  ? `<span class="status bad">✕ ${failed.length} of ${M.checks.length} checks failed</span>`
  : `<span class="status ok">✓ ${M.checks.length} of ${M.checks.length} checks held</span>`)
  + (V ? (partial ? ` · <span class="status warn">⚠ ${V.fetched.failed.length} voter fetch(es) failed: the votes are partial</span>` : " · every voter's bars fetched")
       : ` · <span class="status warn">⚠ no voters' bars: no votes this run</span>`);

// tiles
const vd = M.volume, y0 = vd[W[0]], y1 = vd[W[1]];
const tiles = [
  ["Pushes a week", fmt(y0.pushes_a_week,2), `${W[0]}, the detector alone`],
  ["Note rows a week", fmt(y0.rows_a_week,2), "the detector alone"],
  ["Busiest week's pushes", fmt(y0.busiest_week.pushes), `week to ${y0.busiest_week.week_to}`],
  ["The year before", fmt(y1.pushes_a_week,2),
   `pushes a week · ${fmt(y1.rows_a_week,2)} rows · busiest ${fmt(y1.busiest_week.pushes)}`],
];
const tile = t => `<div class="panel tile"><div class="k">${t[0]}</div>
  <div class="v">${t[1]}</div><div class="w">${t[2]}</div></div>`;
$("tiles").innerHTML = tiles.map(tile).join("");

// tooltip
const tip = $("tip");
function showTip(html, ev){ tip.innerHTML = html; tip.style.opacity = 1;
  const x = Math.min(ev.clientX + 12, innerWidth - tip.offsetWidth - 8);
  tip.style.left = x + "px"; tip.style.top = (ev.clientY + 14) + "px"; }
function hideTip(){ tip.style.opacity = 0; }

// a sortable table from column specs: [key, label, numeric, render, cellClass]
function table(el, cols, rows, caption){
  let key = null, dir = -1;
  function draw(){
    const r = rows.slice();
    if (key){ const c = cols.find(c => c[0] === key);
      r.sort((a,b) => { const x = a[key], y = b[key];
        return (c[2] ? ((x ?? -1e18) - (y ?? -1e18)) : String(x).localeCompare(String(y))) * dir; }); }
    el.innerHTML = `<thead><tr>${cols.map(c => `<th class="s${c[2] ? " n" : ""}" data-k="${c[0]}"
      ${key === c[0] ? `data-dir="${dir}"` : ""}>${c[1]}</th>`).join("")}</tr></thead>
      <tbody>${r.map(row => `<tr>${cols.map((c,i) => `<td class="${c[2] ? "n" : ""}${i === 0 ? " name" : ""}${c[4] ? " " + c[4] : ""}">${
        c[3] ? c[3](row[c[0]], row) : esc(row[c[0]])}</td>`).join("")}</tr>`).join("")}</tbody>
      ${caption ? `<caption>${caption}</caption>` : ""}`;
    el.querySelectorAll("th.s").forEach(h => h.onclick = () => {
      dir = key === h.dataset.k ? -dir : -1; key = h.dataset.k; draw(); });
  }
  draw();
}

// volume
table($("t-volume"),
  [["window","Window"],["pushes","Pushes",true,n0],["pw","a week",true,v => fmt(v,2)],
   ["rows","Note rows",true,n0],["rw","a week",true,v => fmt(v,2)],
   ["busiest","Busiest week",true,(v,r) => `${fmt(v)} <span style="color:var(--muted)">to ${r.bweek}</span>`],
   ...WORDS.map(w => [w, w, true, n0])],
  W.map(w => { const v = vd[w]; return {window: w, pushes: v.pushes, rows: v.rows,
    pw: v.pushes_a_week, rw: v.rows_a_week, busiest: v.busiest_week.pushes,
    bweek: v.busiest_week.week_to, ...v.by_word}; }),
  "Pushes are the events worded " + esc(M.meta.push_words.join(", ")) + "; note rows the rest. Busiest week: Saturday to Friday, by when each event started.");

// votes
const days = d => fmt(d) + (d === 1 ? " day" : " days");
if (V){
  const T = V.tables, E = V.effect;
  $("votes-note").innerHTML = `The votes the live pass would hold at ${hour(M.meta.end)} UTC: each
    instrument's moves at the candidate line or more inside its recount window - as long as the
    closest-reaching of its sources still serves the hour; past it the bot never votes again -
    put to its sources by this code exactly as the live pass votes, on their bars fetched
    ${hour(V.fetched.at)} UTC (${esc(V.fetched.span)}). A source with no bar - or no price - at either
    of the move's two bars is an outage, left out; with nobody left the move is uncertain.
    Voted again with its own record until the verdicts settle, as over the bot's runs.` +
    (V.fetched.failed.length ? `<br><span class="status warn">⚠ Not fetched: ${V.fetched.failed.map(esc).join("; ")}</span>` : "");
  $("vote-tiles").innerHTML = [["Votes", fmt(T.votes), `${fmt(T.flagged)} at the bottom level or more`],
    ...VERDICTS.map(v => [VNAME[v], fmt(T.by_verdict[v]), `${fmt(T.flagged_by_verdict[v])} of the flagged`]),
    ["Pushes inside the windows", `${fmt(E.detector.pushes)} → ${fmt(E.voted.pushes)}`, "the detector alone → with the votes"],
    ["Note rows inside the windows", `${fmt(E.detector.rows)} → ${fmt(E.voted.rows)}`, "the detector alone → with the votes"]]
    .map(tile).join("");
  table($("t-window"), [["window","Recount window",true,days],["n","Instruments",true,n0],
    ["votes","Votes",true,n0], ...VERDICTS.map(v => [v, VNAME[v], true, n0]),
    ["answers_a_vote","Sources answering a vote",true,v => fmt(v,2)],
    ["names","Instruments","",null,"long"]],
    T.by_window.map(w => ({...w, window: w.days, n: w.instruments.length, names: w.instruments.join(", ")})),
    "An instrument's window is the reach of the closest-reaching of its sources; a source's answer is its own move over the move's two bars.");
  table($("t-source"), [["source","Source"],["asked","Asked",true,n0],["saw","Saw it",true,n0],
    ["did_not","Did not",true,n0],["outage","Outage",true,n0]],
    Object.entries(T.by_source).map(([k,v]) => ({source:k, ...v})),
    "An outage: no bar, or no price, at either of the move's two bars.");
  table($("t-vblock"), [["block","Block"],...VERDICTS.map(v => [v, VNAME[v], true, n0])],
    Object.entries(T.by_block).map(([k,v]) => ({block:k, ...v})));
  const erows = Object.entries(E.by_instrument).map(([k,v]) => ({ticker: k, window: v.window_days,
    dp: v.detector.pushes, vp: v.voted.pushes, dr: v.detector.rows, vr: v.voted.rows,
    change: (v.voted.pushes - v.detector.pushes) + (v.voted.rows - v.detector.rows)}));
  table($("t-effect"), [["ticker","Instrument"],["window","Window",true,days],
    ["dp","Pushes, detector",true,n0],["vp","with the votes",true,n0],
    ["dr","Rows, detector",true,n0],["vr","with the votes",true,n0]],
    erows.filter(r => r.change !== 0 || r.dp || r.dr),
    `Inside each instrument's window, and the day before it (an event runs 24 hours). Outside the windows the votes change nothing: ${fmt(E.outside.detector.pushes)} pushes and ${fmt(E.outside.detector.rows)} rows either way. Instruments with no event in their window are left out.`);
  const vlist = () => {
    const q = $("vq").value.trim().toLowerCase(), ver = $("vv").value, fl = $("vf").value;
    const rows = V.list.slice().sort((a,b) => b.hour - a.hour).filter(r => (!q || r.ticker.toLowerCase().includes(q)) && (!ver || r.verdict === ver)
      && (!fl || (r.z != null && Math.abs(r.z) >= M.meta.levels[0])))
      .map(r => ({...r, az: r.z == null ? null : Math.abs(r.z),
        said: r.answers.map(a => `<span class="${a.move == null ? "out" : a.saw ? "saw" : ""}">${esc(a.source)} ${pct(a.move)}${a.saw ? " ✓" : ""}</span>`).join(", ")}));
    table($("t-votes"), [["hour","Hour (UTC)",true,hour],["ticker","Instrument"],["block","Block"],
      ["check","Reading",false,v => v === "open" ? "gap" : "hour"],["az","|z|",true,v => fmt(v,1)],
      ["stored","Stored move",true,pct],["verdict","Verdict",false,v => VNAME[v] || esc(v)],
      ["said","Each source",false,v => v,"long"]], rows,
      `${rows.length} of ${V.list.length} votes. ✓ saw it.`);
  };
  ["vq","vv","vf"].forEach(id => $(id).addEventListener("input", vlist));
  vlist();
} else {
  $("votes").innerHTML = `<h2>The votes</h2><p class="note">No voters' bars this run: no votes were taken.</p>`;
}

// block and instrument tables
const cover = M.coverage;
const blocks = [...new Set(cover.map(c => c.block))].sort();
$("block").innerHTML += blocks.map(b => `<option>${esc(b)}</option>`).join("");
function splits(){
  const q = $("search").value.trim().toLowerCase(), b = $("block").value;
  const ws = W;
  const brows = blocks.map(bl => { const row = {block: bl, members: cover.filter(c => c.block === bl).length};
    ws.forEach((w,i) => { const x = M.by_block[w][bl] || {pushes:0, rows:0};
      row["p"+i] = x.pushes; row["r"+i] = x.rows; }); return row; });
  table($("t-block"), [["block","Block"],["members","Instruments",true],
    ...ws.flatMap((w,i) => [["p"+i, "Pushes, "+w, true, n0], ["r"+i, "Rows, "+w, true, n0]])], brows);
  const irows = cover.filter(c => (!b || c.block === b) &&
      (!q || (c.ticker + " " + (c.display||"") + " " + c.label).toLowerCase().includes(q)))
    .map(c => { const row = {ticker: c.ticker, name: c.display || c.label, block: c.block,
      provider: c.provider, first: c.first};
      ws.forEach((w,i) => { const x = M.by_instrument[w][c.ticker] || {pushes:0, rows:0};
        row["p"+i] = x.pushes; row["r"+i] = x.rows; }); return row; });
  table($("t-inst"), [["ticker","Instrument"],["name","Name"],["block","Block"],["provider","Feed"],
    ...ws.flatMap((w,i) => [["p"+i, "Pushes, "+w, true, n0], ["r"+i, "Rows, "+w, true, n0]]),
    ["first","History from",true,day]], irows,
    `${irows.length} of ${cover.length} instruments. An instrument whose history starts inside a window counts only the part it had.`);
}
["search","block"].forEach(id => $(id).addEventListener("input", splits));
splits();

// archive
const arows = M.archive.filter(y => y.instrument_years >= 0.1).map(y => ({
  year: y.year + (y.partial ? " (to " + day(M.meta.end) + ")" : ""), y: y.year,
  iy: y.instrument_years, events: y.events, pushes: y.pushes,
  epi: y.events / y.instrument_years, ppi: y.pushes / y.instrument_years}));
barChart($("c-archive"), arows.map(x => ({label: String(x.y), y: x.ppi,
  tip: `<b>${x.year}</b><br>${fmt(x.pushes)} pushes, ${fmt(x.events)} events<br>${fmt(x.iy,1)} instrument-years<br>${fmt(x.ppi,2)} pushes per instrument-year`})),
  "pushes per instrument-year, detector alone");
table($("t-archive"), [["year","Year"],["iy","Instrument-years",true,v => fmt(v,1)],
  ["events","Events",true,n0],["pushes","Pushes",true,n0],
  ["epi","Events per instrument-year",true,v => fmt(v,2)],["ppi","Pushes per instrument-year",true,v => fmt(v,2)]],
  arows, "Instrument-years: the years of history the basket's instruments had in that year, summed.");

// biggest hours
const big = M.biggest_hours;
table($("t-big"), [["word","Word"],["hours","Hours",true]],
  [...WORDS, "not flagged"].map(w => ({word: w, hours: big.by_word[w]})),
  `${fmt(big.total)} hours across ${big.by_instrument.length} instruments.`);

// checks
table($("t-checks"), [["ok","",false,v => v ? '<span class="status ok">✓ held</span>' : '<span class="status bad">✕ failed</span>'],
  ["name","Check"],["detail","Compared"]], M.checks);

// provider limits
const lim = M.limits || [];
const share = (c, l) => c == null || !l ? "–" : `${n0(c)} <span class="dim">(${Math.round(100 * c / l)}%)</span>`;
if (lim.length) table($("t-limits"), [["provider","Provider"],["window","Window"],["limit","Limit",true,n0],
  ["live","Live",true,n0],["manual","Manual",true,n0],["dashboard","Dashboard",true,n0],["votes","Votes",true,n0],
  ["latest","Latest",true,(v, r) => r.latestCalls == null ? "–" : `${share(r.latestCalls, r.limit)} <span class="dim">${esc(r.latestPeriod)}</span>`],
  ["peak","Peak",true,(v, r) => r.peakCalls == null ? "–" : `${share(r.peakCalls, r.limit)} <span class="dim">${esc(r.peakPeriod)}</span>`],
  ["counted","Counted",true,n0],["note","Note",false,null,"long"]],
  lim.map(l => ({provider: l.provider, window: l.window, limit: l.limit, live: l.live, manual: l.manual,
    dashboard: l.dashboard, votes: l.votes, note: l.note, counted: l.periods_counted,
    latest: l.latest ? l.latest.calls : null, latestCalls: l.latest ? l.latest.calls : null,
    latestPeriod: l.latest ? l.latest.period : "", peak: l.peak ? l.peak.calls : null,
    peakCalls: l.peak ? l.peak.calls : null, peakPeriod: l.peak ? l.peak.period : ""})));
else $("limits").insertAdjacentHTML("beforeend", '<p class="note">No limits measured this run.</p>');

// coverage
table($("t-cover"), [["ticker","Instrument"],["name","Name"],["block","Block"],["provider","Feed"],
  ["first","First bar",true,day],["last","Last bar",true,hour],["years","Years",true,v => fmt(v,1)],["hours","Hours",true,n0]],
  cover.map(c => ({ticker: c.ticker, name: c.display || c.label, block: c.block, provider: c.provider,
    first: c.first, last: c.last, hours: c.hours, years: c.first ? (c.last - c.first) / 31557600 : null})));

// a bar chart: one series, thin bars with rounded ends, a tooltip on hover
function niceMax(v){ if (!(v > 0)) return 1; const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1,2,2.5,5,10]) if (v <= m*p) return m*p; return 10*p; }
function barChart(el, pts, label){
  const w = 1100, h = 260, l = 44, r = 12, t = 14, b = 28, iw = w - l - r, ih = h - t - b;
  const max = niceMax(Math.max(...pts.map(p => p.y || 0)));
  const band = iw / Math.max(pts.length, 1), bw = Math.min(24, band - 2);
  let s = "";
  for (let i = 0; i <= 4; i++){ const y = t + ih - ih*i/4;
    s += `<line class="grid" x1="${l}" x2="${w - r}" y1="${y}" y2="${y}"/>
      <text x="${l - 6}" y="${y + 4}" text-anchor="end">${fmt(max*i/4, max < 4 ? 1 : 0)}</text>`; }
  s += `<line class="base" x1="${l}" x2="${w - r}" y1="${t + ih}" y2="${t + ih}"/><text x="${l}" y="${t - 2}">${esc(label)}</text>`;
  pts.forEach((p, i) => { const x = l + i*band + (band - bw)/2, hh = (p.y || 0) / max * ih;
    const y = t + ih - hh, rr = Math.min(4, hh);
    const tick = pts.length > 12 ? "’" + p.label.slice(-2) : p.label;
    s += `<rect class="hit" data-i="${i}" x="${l + i*band}" y="${t}" width="${band}" height="${ih}"/>`;
    s += `<path class="bar" d="M${x},${t + ih} V${y + rr} Q${x},${y} ${x + rr},${y} H${x + bw - rr} Q${x + bw},${y} ${x + bw},${y + rr} V${t + ih} Z" pointer-events="none"/>`;
    if (pts.length <= 30 || i % 2 === 0) s += `<text x="${x + bw/2}" y="${h - 8}" text-anchor="middle">${esc(tick)}</text>`; });
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(label)}">${s}</svg>`;
  el.querySelectorAll(".hit").forEach(hn => { const p = pts[+hn.dataset.i];
    hn.onmousemove = e => showTip(p.tip, e); hn.onmouseleave = hideTip; });
}
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
