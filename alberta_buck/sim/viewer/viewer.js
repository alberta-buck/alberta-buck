// The sim viewer (ORGANIC-SCALE.org, tracks T13 / T14): one vector, one
// clock.  Left: every collected metric as panes.  Right: one tab per agent
// class, per-agent panes with the agent's telemetry pens and its action
// log.  Pure model-building functions live at the top and are exported for
// node tests; the DOM code below runs only in a browser.
//
// Telemetry (alberta_buck/sim/TELEMETRY.md):
//   v1  meta.telemetry.agents[] = roster of agents that opted in
//       (id "Class#idx", cls, idx, stride, knobs); frame.ag[id] = state.
//   v2  the roster lists EVERY agent (telemetry: true/false) and frame.ag[id]
//       may carry "acts": [{t, fn|kind, ok, err, why}] recorded at the chain
//       send and at the agent's own decision notes.
"use strict";

// ---------------------------------------------------------------- model --

// Class -> the ctr/frame prefix of its class aggregates (v1 vectors carry
// these for populations that emit no per-agent record).
const CLASS_PREFIX = {
  BuckCreditDebtorAgent: ["bcd_"], FatCreditBorrowerAgent: ["fat_"],
  SaverAgent: ["saver_", "regime_saver"], DirectMintAgent: ["dm", "directMint"],
  DirectMintBuckAgent: ["dm"], ArrivingDMAgent: ["dm", "endog_depositor"],
  BootstrapDMAgent: ["dm"], BuckPoolInvestorAgent: ["bpi_"],
  BuckIssuerArbAgent: ["bia_"], DiscountBuckArbAgent: ["dba_"],
  DiscountBasketArbAgent: ["dbb_"], ExcursionArbAgent: ["exc_"],
  ExcursionCreditArbAgent: ["exc_"], ExcursionBasketArbAgent: ["exc_"],
  ExcursionBuckArbAgent: ["exc_"], WhaleRaidAgent: ["raid_"],
  CommodityRebalArbAgent: ["crb_"], UndertakingAgent: ["ut_"],
  FacilityAgent: ["fac_"], SeederAgent: ["sd_"], MonetaryOpsAgent: ["mo_"],
  MonetaryKeeperAgent: ["mk_"], FenceKeeperAgent: ["fk_"],
  PidKeeperAgent: ["pid_"], DirectorKeeperAgent: ["director"],
  BuckBasketRebalancerAgent: ["rebalance"], AnonymousArbAgent: ["cycle", "ub"],
  TokenAccumulatorAgent: ["invested", "iv_"], MarketMakerWhale: ["poolBal"],
};

// Canonical left-column panes: [title, [keys...]] in display order; keys
// absent from a vector are dropped, panes with no keys are dropped.
const CANONICAL = [
  ["bvib (basket value in BUCK)", ["basketVal"]],
  ["K (buckK)", ["buckK"]],
  ["K terms: price loop", ["pid_up", "pid_ui", "pid_ud"]],
  ["K terms: position loop", ["pid_q", "pid_qi", "pid_qd"]],
  ["K state (ppm)", ["pid_p", "pid_i", "pid_d", "pid_s", "pid_is", "pid_ds"]],
  ["shadow: aggregate position s / saturation", ["sh_s", "sh_sat", "sh_boost"]],
  ["shadow: books", ["sh_net", "sh_held", "sh_outstanding", "sh_offset", "sh_cap", "sh_offset_cap"]],
  ["supply / treasury", ["supply", "treasuryBuck", "dmOutstanding"]],
  ["BUCK/USD (slot0 vs balances)", ["buck_usd", "buck_usd_bal", "buckUsd"]],
  ["basket NAV", ["basketNav"]],
  ["stress fee", ["stressFeesPaid", "stressFeeExits"]],
];

function isNum(v) { return typeof v === "number" && Number.isFinite(v); }

// Scale a raw series into display units by magnitude: 1e18-scaled
// quantities (bvib, K, pid terms) -> x1e-18; 6-dec money -> x1e-6 (USD/BUCK).
function unitOf(key, sample) {
  if (/^(basketVal|buckK|pid_|sh_s$|sh_sat$|sh_boost$|sh_value$)/.test(key)) return [1e-18, "x1"];
  const m = sample.filter(isNum).map(Math.abs).sort((a, b) => a - b);
  if (!m.length) return [1, ""];
  const med = m[Math.floor(m.length / 2)];
  if (med >= 1e15) return [1e-18, "x1"];
  if (med >= 1e5) return [1e-6, "M6"];
  return [1, ""];
}

function frameKeysNumeric(frames) {
  const keys = new Set();
  for (const f of frames.slice(0, 50).concat(frames.slice(-50))) {
    for (const k of Object.keys(f)) {
      const v = f[k];
      if (isNum(v)) keys.add(k);
      else if (Array.isArray(v) && v.length && isNum(v[0])) keys.add(k);
    }
  }
  return [...keys].sort();
}

// Build {key: Float64Array|null[] per frame} for scalar keys and expanded
// per-token keys for list-valued keys ("spotBuck[0]" ...).
function buildSeries(frames, keys, tokens) {
  const n = frames.length, out = {};
  for (const k of keys) {
    const first = frames.find(f => f[k] != null);
    if (first && Array.isArray(first[k])) {
      const width = first[k].length;
      for (let i = 0; i < width; i++) {
        const name = `${k}[${(tokens && tokens[i]) ? tokens[i] : i}]`;
        const arr = new Array(n).fill(null);
        for (let j = 0; j < n; j++) { const v = frames[j][k]; if (v && isNum(v[i])) arr[j] = v[i]; }
        out[name] = arr;
      }
    } else {
      const arr = new Array(n).fill(null);
      for (let j = 0; j < n; j++) { const v = frames[j][k]; if (isNum(v)) arr[j] = v; }
      out[k] = arr;
    }
  }
  return out;
}

function groupByPrefix(keys) {
  const groups = {};
  for (const k of keys) {
    const m = k.match(/^([a-z]+)_/) || k.match(/^([a-z]+)[A-Z]/);
    const g = m ? m[1] : "misc";
    (groups[g] = groups[g] || []).push(k);
  }
  return groups;
}

// The per-class view: roster ids by class, blank instances for classes the
// scenario populated but the roster does not carry, and each class's
// aggregate keys.
function buildClasses(meta, frames, allKeys) {
  const tel = (meta && meta.telemetry) || {};
  const roster = tel.agents || [];
  const byCls = {};
  for (const a of roster) (byCls[a.cls] = byCls[a.cls] || []).push(a);
  const pop = ((meta && meta.experiment && meta.experiment.scenario) || {}).agents || {};
  const classes = new Set([...Object.keys(byCls), ...Object.keys(pop)]);
  // classes only visible through their aggregates
  for (const [cls, prefs] of Object.entries(CLASS_PREFIX)) {
    if (allKeys.some(k => prefs.some(p => k.startsWith(p)))) {
      if (pop[cls] || byCls[cls]) classes.add(cls);
    }
  }
  const out = [];
  for (const cls of [...classes].sort()) {
    const agents = (byCls[cls] || []).slice().sort((a, b) => a.idx - b.idx);
    const count = pop[cls] != null ? Number(pop[cls]) : agents.length;
    const prefs = CLASS_PREFIX[cls] || [];
    const aggKeys = allKeys.filter(k => prefs.some(p => k.startsWith(p)));
    const blanks = Math.max(0, count - agents.length);
    // v2 names every instance; telemetry:false entries are NAMED blanks
    // (no state pens; they may still carry acts).
    const withState = agents.filter(a => a.telemetry !== false).length;
    out.push({ cls, count, agents, blanks, aggKeys, withState,
               telemetry: withState ? (tel.version || 1) : (agents.length ? (tel.version || 1) : 0) });
  }
  return out;
}

// Per-agent pens: {id: {field: array per frame}} for numeric fields of
// frame.ag[id]; acts: {id: [{day, t, ...}]} in time order.
function buildAgents(frames, roster) {
  const n = frames.length, pens = {}, acts = {};
  const ids = new Set(roster.map(a => a.id));
  for (let j = 0; j < n; j++) {
    const ag = frames[j].ag; if (!ag) continue;
    for (const [id, rec] of Object.entries(ag)) {
      ids.add(id);
      if (!rec || typeof rec !== "object") continue;
      for (const [k, v] of Object.entries(rec)) {
        if (k === "acts") {
          const lst = acts[id] = acts[id] || [];
          for (const a of (v || [])) lst.push(Object.assign({ day: frames[j].day != null ? frames[j].day : j }, a));
        } else if (isNum(v)) {
          const p = pens[id] = pens[id] || {};
          (p[k] = p[k] || new Array(n).fill(null))[j] = v;
        }
      }
    }
  }
  return { ids: [...ids], pens, acts };
}

// The k acts nearest the current day (ties by tick), in time order.
function nearestActs(list, day, k = 3) {
  if (!list || !list.length) return [];
  return list.map(a => [Math.abs(a.day - day) * 8 + (a.t || 0) / 8, a])
    .sort((x, y) => x[0] - y[0]).slice(0, k).map(x => x[1])
    .sort((x, y) => (x.day - y.day) || ((x.t || 0) - (y.t || 0)));
}

function buildModel(vec) {
  const frames = vec.frames || [];
  const tokens = vec.tokens || [];
  const meta = vec.meta || {};
  const keys = frameKeysNumeric(frames);
  const series = buildSeries(frames, keys, tokens);
  const allKeys = Object.keys(series);
  const days = frames.map((f, i) => (f.day != null ? f.day : i));
  const canonical = CANONICAL.map(([t, ks]) => [t, ks.filter(k => k in series)]).filter(x => x[1].length);
  const used = new Set(canonical.flatMap(x => x[1]));
  const groups = groupByPrefix(allKeys.filter(k => !used.has(k)));
  const classes = buildClasses(meta, frames, allKeys);
  const roster = ((meta.telemetry || {}).agents) || [];
  const agents = buildAgents(frames, roster);
  return { frames, days, tokens, meta, series, canonical, groups, classes, roster, agents,
           version: (meta.telemetry || {}).version || 0 };
}

if (typeof module !== "undefined") {
  module.exports = { buildModel, buildClasses, buildAgents, buildSeries, nearestActs,
                     frameKeysNumeric, unitOf, groupByPrefix, CLASS_PREFIX, CANONICAL };
}

// ------------------------------------------------------------------ UI --

if (typeof window !== "undefined") {
  const $ = (s, el = document) => el.querySelector(s);
  const state = { model: null, idx: 0, charts: [], leftCharts: [], rightCharts: [], playing: null, activeTab: null, loadSeq: 0, aborter: null };
  function destroy(list) { for (const u of list) { try { u.destroy(); } catch (_) {} } list.length = 0; state.charts = state.leftCharts.concat(state.rightCharts); }
  function status(msg, isErr) { const s = $("#status"); if (s) { s.textContent = msg || ""; s.className = "row mono " + (isErr ? "err" : "muted"); } }

  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  const cursorPlugin = () => ({
    hooks: { draw: u => {
      const m = state.model; if (!m) return;
      const x = u.valToPos(m.days[state.idx], "x", true);
      const ctx = u.ctx; ctx.save(); ctx.strokeStyle = "rgba(220,60,60,0.9)"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, u.bbox.top); ctx.lineTo(x, u.bbox.top + u.bbox.height); ctx.stroke(); ctx.restore();
    } }
  });

  const PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b", "#e377c2", "#17becf", "#7f7f7f", "#bcbd22"];

  function makeChart(container, title, seriesMap, keys, opts = {}) {
    if (typeof uPlot === "undefined") { container.appendChild(el("div", "err", "uPlot not loaded: run `make viewer-vendor` (copies node_modules/uplot/dist into viewer/vendor)")); return null; }
    const m = state.model;
    const data = [m.days];
    const series = [{ label: "day" }];
    keys.forEach((k, i) => {
      const raw = seriesMap[k]; const [scale, unit] = opts.unit || unitOf(k, raw);
      data.push(raw.map(v => (v == null ? null : v * scale)));
      series.push({ label: unit ? `${k} (${unit})` : k, stroke: PALETTE[i % PALETTE.length], width: 1, spanGaps: true, points: { show: false } });
    });
    const box = el("div", "pane"); box.appendChild(el("div", "pane-title", title)); container.appendChild(box);
    const u = new uPlot({ width: box.clientWidth - 8 || 520, height: opts.height || 140, series,
      cursor: { sync: { key: "sim" } }, legend: { show: true }, plugins: [cursorPlugin()],
      axes: [{ label: "day" }, { size: 60 }], scales: { x: { time: false } } }, data, box);
    (opts.column === "right" ? state.rightCharts : state.leftCharts).push(u);
    state.charts = state.leftCharts.concat(state.rightCharts);
    return u;
  }

  function renderLeft() {
    const m = state.model, left = $("#left"); destroy(state.leftCharts); left.innerHTML = "";
    for (const [title, keys] of m.canonical) makeChart(left, title, m.series, keys);
    const groups = Object.entries(m.groups).sort();
    const sel = $("#groups"); sel.innerHTML = "";
    for (const [g, keys] of groups) {
      const lab = el("label"); const cb = el("input"); cb.type = "checkbox"; cb.dataset.group = g;
      cb.checked = false; lab.appendChild(cb); lab.appendChild(document.createTextNode(` ${g} (${keys.length})`)); sel.appendChild(lab);
      cb.addEventListener("change", () => { renderGroup(g, keys, cb.checked); });
    }
  }

  function renderGroup(g, keys, on) {
    const left = $("#left");
    const existing = $(`[data-pane-group="${g}"]`, left);
    if (existing) { for (const u of state.leftCharts.filter(u => existing.contains(u.root))) { try { u.destroy(); } catch (_) {} } state.leftCharts = state.leftCharts.filter(u => !existing.contains(u.root)); state.charts = state.leftCharts.concat(state.rightCharts); existing.remove(); }
    if (!on) return;
    const wrap = el("div"); wrap.dataset.paneGroup = g; left.appendChild(wrap);
    // chunk keys into panes of <= 6 pens
    for (let i = 0; i < keys.length; i += 6) makeChart(wrap, `${g}: ${keys.slice(i, i + 6).join(", ")}`, state.model.series, keys.slice(i, i + 6));
  }

  function renderTabs() {
    const m = state.model, tabs = $("#tabs"), body = $("#agents"); destroy(state.rightCharts); tabs.innerHTML = ""; body.innerHTML = "";
    for (const c of m.classes) {
      const b = el("button", "tab", `${c.cls} x${c.count}${c.withState ? "" : " (no state)"}`);
      b.addEventListener("click", () => { state.activeTab = c.cls; renderTabs(); });
      if (state.activeTab === c.cls) b.classList.add("active");
      tabs.appendChild(b);
    }
    if (!state.activeTab && m.classes.length) { state.activeTab = m.classes[0].cls; }
    const c = m.classes.find(x => x.cls === state.activeTab); if (!c) return;
    const head = el("div", "class-head");
    head.appendChild(el("div", "class-title", `${c.cls}: ${c.count} instance${c.count === 1 ? "" : "s"}; ${c.withState} with state telemetry, ${c.agents.length - c.withState} named without state, ${c.blanks} unnamed blank (schema v${m.version || 1})`));
    body.appendChild(head);
    if (c.aggKeys.length) {
      const agg = el("div"); body.appendChild(agg);
      for (let i = 0; i < c.aggKeys.length; i += 6) makeChart(agg, `${c.cls} aggregates: ${c.aggKeys.slice(i, i + 6).join(", ")}`, m.series, c.aggKeys.slice(i, i + 6), { height: 120, column: "right" });
    } else body.appendChild(el("div", "note", "no class aggregates in this vector"));
    for (const a of c.agents) {
      const pane = el("div", "agent"); body.appendChild(pane);
      const pens = m.agents.pens[a.id] || {};
      const keys = Object.keys(pens);
      pane.appendChild(el("div", "agent-title", `${a.id}  stride ${a.stride || 1}  knobs: ${JSON.stringify(a.knobs || {}).slice(0, 160)}`));
      if (keys.length) makeChart(pane, `${a.id}`, pens, keys, { height: 120, column: "right" });
      else pane.appendChild(el("div", "note", a.telemetry === false ? "named instance, no state telemetry (its acts below, if any)" : "no per-frame state in this vector"));
      const log = el("div", "acts"); log.dataset.agent = a.id; pane.appendChild(log);
      renderActs(log, a.id);
    }
    if (c.blanks) {
      const bl = el("details", "blanks"); const sm = el("summary", null, `${c.blanks} instance${c.blanks === 1 ? "" : "s"} without telemetry (blank frames)`); bl.appendChild(sm);
      const list = el("div", "blank-list");
      for (let i = 0; i < c.blanks; i++) { const f = el("div", "blank"); f.textContent = `${c.cls}#? (${i + 1}/${c.blanks}) -- no telemetry in this vector`; list.appendChild(f); }
      bl.appendChild(list); body.appendChild(bl);
    }
  }

  function renderActs(log, id) {
    const m = state.model; log.innerHTML = "";
    const lst = m.agents.acts[id];
    if (!lst || !lst.length) { log.appendChild(el("div", "note", m.version >= 2 ? "no acts recorded" : "no action log (telemetry v1)")); return; }
    const day = m.days[state.idx];
    for (const a of nearestActs(lst, day, 3)) {
      const row = el("div", "act" + (a.day === day ? " now" : ""));
      const kind = a.kind || a.fn || "?";
      const ok = a.ok === false ? " FAILED" : "";
      row.textContent = `d${a.day} t${a.t != null ? a.t : "-"}  ${kind}${ok}${a.err ? " " + a.err : ""}${a.why ? "  why " + JSON.stringify(a.why) : ""}${a.args ? " " + JSON.stringify(a.args) : ""}`;
      log.appendChild(row);
    }
  }

  function setIdx(i) {
    const m = state.model; if (!m) return;
    state.idx = Math.max(0, Math.min(m.frames.length - 1, i));
    $("#slider").value = state.idx; $("#day").textContent = `day ${m.days[state.idx]} (${state.idx + 1}/${m.frames.length})`;
    for (const u of state.charts) { try { u.redraw(false); } catch (_) {} }
    for (const log of document.querySelectorAll(".acts")) renderActs(log, log.dataset.agent);
    const f = m.frames[state.idx];
    $("#readout").textContent = ["basketVal", "buckK", "supply", "buck_usd"].filter(k => k in f).map(k => `${k}=${fmt(k, f[k])}`).join("   ");
  }
  function fmt(k, v) { if (!isNum(v)) return String(v); const [s] = unitOf(k, [v]); const x = v * s; return Math.abs(x) >= 1000 ? x.toLocaleString(undefined, { maximumFractionDigits: 0 }) : x.toPrecision(6); }

  function load(vec, name) {
    destroy(state.leftCharts); destroy(state.rightCharts); state.activeTab = null;
    if (state.playing) { clearInterval(state.playing); state.playing = null; $("#play").textContent = "play"; }
    try { state.model = buildModel(vec); }
    catch (e) { status(`could not build the model from ${name}: ${e.message}`, true); return; }
    status(`loaded ${name}`);
    $("#title").textContent = `${name}: ${state.model.frames.length} frames, telemetry v${state.model.version || "1 (none)"}, ${state.model.roster.length} agents in roster, ${state.model.classes.length} classes`;
    $("#slider").max = Math.max(0, state.model.frames.length - 1);
    renderLeft(); renderTabs(); setIdx(state.model.frames.length - 1);
  }

  async function loadUrl(url) {
    if (!url) return;
    const seq = ++state.loadSeq;
    if (state.aborter) { try { state.aborter.abort(); } catch (_) {} }
    const ctl = state.aborter = (typeof AbortController !== "undefined") ? new AbortController() : null;
    $("#title").textContent = `loading ${url} ...`; status("");
    try {
      const r = await fetch(url, ctl ? { signal: ctl.signal } : undefined);
      if (!r.ok) { status(`failed: HTTP ${r.status} for ${url}`, true); return; }
      const text = await r.text();
      if (seq !== state.loadSeq) return;            // a newer load superseded this one
      const vec = text.trimStart().startsWith("{") ? JSON.parse(text) : jsonl(text);
      load(vec, url);
    } catch (e) {
      if (e && e.name === "AbortError") return;
      status(`could not load ${url}: ${e.message}`, true);
    }
  }
  function loadFile(f) {
    if (!f) return;
    const rd = new FileReader();
    rd.onerror = () => status(`could not read ${f.name}`, true);
    rd.onload = () => { try { const txt = rd.result; load(txt.trimStart().startsWith("{") ? JSON.parse(txt) : jsonl(txt), f.name); } catch (e) { status(`${f.name}: ${e.message}`, true); } };
    rd.readAsText(f);
  }
  function jsonl(text) { // frames one per line; an optional first line {"meta":...}
    const lines = text.split("\n").filter(Boolean).map(l => JSON.parse(l));
    const head = lines[0] && lines[0].meta ? lines.shift() : {};
    return { meta: head.meta || {}, tokens: head.tokens || [], frames: lines };
  }

  window.addEventListener("DOMContentLoaded", async () => {
    $("#slider").addEventListener("input", e => setIdx(Number(e.target.value)));
    $("#prev").addEventListener("click", () => setIdx(state.idx - 1));
    $("#next").addEventListener("click", () => setIdx(state.idx + 1));
    $("#play").addEventListener("click", () => {
      if (state.playing) { clearInterval(state.playing); state.playing = null; $("#play").textContent = "play"; return; }
      $("#play").textContent = "pause";
      state.playing = setInterval(() => { if (state.idx >= state.model.frames.length - 1) setIdx(0); else setIdx(state.idx + 1); }, 120);
    });
    document.addEventListener("keydown", e => { if (e.key === "ArrowLeft") setIdx(state.idx - 1); if (e.key === "ArrowRight") setIdx(state.idx + 1); });
    $("#file").addEventListener("change", e => loadFile(e.target.files[0]));
    document.body.addEventListener("dragover", e => e.preventDefault());
    document.body.addEventListener("drop", e => { e.preventDefault(); loadFile(e.dataTransfer.files[0]); });
    window.addEventListener("error", e => status(`error: ${e.message}`, true));
    window.addEventListener("unhandledrejection", e => status(`error: ${(e.reason && e.reason.message) || e.reason}`, true));
    const q = new URLSearchParams(location.search);
    try { const r = await fetch("/data/index.json"); if (r.ok) { const idx = await r.json(); const sel = $("#vectors"); for (const v of idx) { const o = el("option", null, `${v.path} (${(v.size / 1048576).toFixed(1)} MB)`); o.value = "/data/" + v.path; sel.appendChild(o); } sel.addEventListener("change", () => { if (sel.value) loadUrl(sel.value); }); } } catch (_) {}
    if (q.get("v")) loadUrl(q.get("v"));
  });
}
