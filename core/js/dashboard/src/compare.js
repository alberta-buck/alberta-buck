// Comparing runs that are identical but for the settings under study (alberta_buck/sim/compare.py).
// Two ways in: a comparison's index (its axes, and which run is which), where one axis is varied and
// every other held at a chosen value; or any two runs, whose manifests say which settings differ.
// Either way the runs' frames are drawn on the same charts, one line per run, with the first day each
// run departs from the first (the reference) marked, and a table of the differences.
//
// The functions here are pure (node --test reads them: core/js/test/dashboard-compare.test.js); the
// CompareTab at the end is the page's.

import { TimeChart } from "./chart.js";
import { RAW, format, startupShareMark } from "./series.js";
import { runOptions } from "./run-picker.js";

export const MAX_RUNS = 6;     // one categorical hue per run, never cycled: more values, fewer at once

const num = (v) => (v === null || v === undefined || !Number.isFinite(Number(v)) ? null : Number(v));
const buck = (raw) => (raw === null || raw === undefined ? null : Number(raw) / RAW);
const get = (f, path) => path.reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), f);

// The observables a pair of runs is compared on, as in compare.py's WATCHED.
const WATCHED = [["control", "k"], ["control", "bvib"], ["market", "buck_usd"], ["supply", "total"]];

// What each Compare chart draws for one run's frame.
export const METRICS = [
  { id: "k", title: "K", unit: "ratio", value: (f) => num(f?.control?.k) },
  { id: "bvib", title: "BVIB", unit: "ratio", reference: 1, value: (f) => num(f?.control?.bvib) },
  { id: "bvib_nominal", title: "BVIB quoted (nominal)", unit: "ratio", reference: 1, value: (f) => num(f?.control?.bvib_nominal) },
  { id: "buck_usd", title: "BUCK/USD", unit: "ratio", reference: 1, value: (f) => num(f?.market?.buck_usd) },
  { id: "supply", title: "BUCK supply", unit: "buck", value: (f) => buck(f?.supply?.total) },
  { id: "lien", title: "Basket lien", unit: "buck", value: (f) => buck(f?.supply?.basket_lien) },
  { id: "principal", title: "Household principal", unit: "buck", value: (f) => buck(f?.supply?.household_principal) },
  ...["investor", "debtor", "saver"].map((role) => ({
    id: role, title: `${role[0].toUpperCase()}${role.slice(1)}s active`, unit: "count",
    value: (f) => num(f?.population?.roles?.[role]?.active),
  })),
  { id: "refused", title: "Households refused", unit: "count", value: (f) => num(f?.health?.errors) },
];

// The runs of a comparison with `vary` free and every other axis at `held`'s value, in the varied
// axis's order (its reference first).
export const clipFrames = (frames, endDay) => endDay == null ? frames : frames.filter((frame) => frame.day <= endDay);

export function selection(index, vary, held = {}) {
  const axis = index.axes.find((a) => a.name === vary);
  if (!axis) return [];
  const order = axis.values.map((v) => v.label);
  return index.runs
    .filter((r) => index.axes.every((a) => a.name === vary || r.values[a.name] === (held[a.name] ?? a.reference)))
    .sort((a, b) => order.indexOf(a.values[vary]) - order.indexOf(b.values[vary]))
    .map((r) => ({ ...r, id: r.id, label: r.values[vary] }));
}

// Every setting of a resolved experiment as dotted keys (the manifest's "experiment"; its source path
// is where it was read, not a setting).
export function flatten(obj, prefix = "", out = {}) {
  for (const [k, v] of Object.entries(obj ?? {})) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (!prefix && k === "source") continue;
    if (v && typeof v === "object" && !Array.isArray(v)) flatten(v, key, out);
    else out[key] = JSON.stringify(v);
  }
  return out;
}

// The settings that differ between two runs: [{ key, a, b }], from their manifests.
export function settingsDiff(a, b) {
  const fields = (m) => ({ ...flatten(m?.experiment),
    ...flatten({ runtime: m?.engine, upkeep: m?.settings?.arm, treatment_day: m?.settings?.warmup_days }) });
  const fa = fields(a), fb = fields(b);
  const keys = [...new Set([...Object.keys(fa), ...Object.keys(fb)])].sort();
  const out = keys.filter((k) => fa[k] !== fb[k]).map((key) => ({ key, a: fa[key] ?? "(unset)", b: fb[key] ?? "(unset)" }));
  const sa = a?.scenario?.seed, sb = b?.scenario?.seed;
  if (sa !== sb && !out.some((d) => d.key === "scenario.seed")) out.push({ key: "scenario.seed", a: String(sa), b: String(sb) });
  return out;
}

export function treatmentText(arm, day) {
  const label = { active: "Active upkeep", maintenance: "Strict maintenance" }[arm];
  return label && Number.isInteger(day) ? `${label} from day ${day}` : null;
}

// The first day two runs differ: on the watched observables, and anywhere in the frame (null: never
// over the days both recorded).
export function divergence(a, b) {
  const byDay = new Map(b.map((f) => [f.day, f]));
  const out = { watched: null, any: null, days: 0 };
  for (const f of a) {
    const g = byDay.get(f.day);
    if (!g) break;
    out.days++;
    if (out.any === null && JSON.stringify(f) !== JSON.stringify(g)) out.any = f.day;
    if (out.watched === null && WATCHED.some((p) => JSON.stringify(get(f, p)) !== JSON.stringify(get(g, p)))) out.watched = f.day;
    if (out.any !== null && out.watched !== null) break;
  }
  return out;
}

// Return percentages are unavailable until their explicitly recorded baseline exists.
export function roi(value, baseline) {
  const v = num(value), b = num(baseline);
  return v !== null && b !== null && Number.isFinite(v) && Number.isFinite(b) && b > 0 ? 100 * (v / b - 1) : null;
}
const shareBaseline = (f) => startupShareMark(f) ?? f?.basket?.share_price;

// One run's headline figures (compare.py's headline, the ones the table shows).
export function headline(frames, run = {}) {
  if (!frames.length) return { days: 0 };
  const last = frames[frames.length - 1];
  const ks = frames.map((f) => num(f.control?.k)).filter((v) => v !== null);
  const bv = frames.map((f) => num(f.control?.bvib)).filter((v) => v !== null);
  const mean = (xs) => (xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : null);
  return {
    days: frames.length, k_end: num(last.control?.k), k_mean: mean(ks),
    bvib_gap_mean: mean(bv.map((x) => Math.abs(x - 1))), buck_usd_end: num(last.market?.buck_usd),
    supply_end: buck(last.supply?.total), lien_end: buck(last.supply?.basket_lien),
    arrived: Object.keys(last.population?.roles ?? {}).length && Object.values(last.population.roles).every((r) => num(r.arrived) !== null) ? Object.values(last.population.roles).reduce((s, r) => s + num(r.arrived), 0) : null,
    refused_days: frames.every((f) => num(f.health?.errors) !== null) ? frames.reduce((s, f) => s + num(f.health.errors), 0) : null,
    share_roi: roi(last.basket?.share_price, shareBaseline(frames[0])),
    passive_roi: roi(last.basket?.hold_index, frames[0].basket?.hold_index),
    treatment_roi: run.treatment_day != null && last.day >= run.treatment_day ? roi(last.basket?.share_price, run.baseline_share_price) : null,
  };
}

export const COLUMNS = [
  { key: "share_roi", label: "Share return from opening mark", unit: "pct" },
  { key: "passive_roi", label: "Passive return from first close", unit: "pct" },
  { key: "treatment_roi", label: "Share return from pre-switch", unit: "pct" },
  { key: "k_end", label: "K at end", unit: "ratio" }, { key: "k_mean", label: "Mean K", unit: "ratio" },
  { key: "bvib_gap_mean", label: "Mean |BVIB - 1|", unit: "ratio" }, { key: "buck_usd_end", label: "BUCK/USD at end", unit: "ratio" },
  { key: "supply_end", label: "Supply at end", unit: "buck" }, { key: "lien_end", label: "Basket lien at end", unit: "buck" },
  { key: "arrived", label: "Households arrived", unit: "count" }, { key: "refused_days", label: "Refusals", unit: "count" },
];

// The table: one row per run, its figures and their differences from the first run's, and the day it
// first departs from the first run.
export function rows(runs) {
  const ref = runs[0];
  const refFigures = ref ? headline(ref.frames, ref) : null;
  return runs.map((r, i) => {
    const h = headline(r.frames, r);
    const comparable = !!(ref?.frames.length && r.frames.length &&
      ref.frames[0].day === r.frames[0].day && ref.frames.at(-1).day === r.frames.at(-1).day);
    const diff = {};
    for (const c of COLUMNS) {
      const a = h[c.key], b = refFigures?.[c.key];
      diff[c.key] = i === 0 || !comparable || a === null || b === null || a === undefined || b === undefined ? null : a - b;
    }
    return { label: r.label, id: r.id, figures: h, diff, comparable, diverges: i === 0 ? null : divergence(r.frames, ref.frames) };
  });
}

// The runs' frames as one frame a day ({ day, runs: { label: frame } }) and a chart spec per metric
// with one series per run, so the ordinary TimeChart draws them.
export function merge(runs) {
  const days = new Map();
  for (const r of runs) for (const f of r.frames) {
    if (!days.has(f.day)) days.set(f.day, { day: f.day, runs: {} });
    days.get(f.day).runs[r.label] = f;
  }
  return [...days.values()].sort((a, b) => a.day - b.day);
}

// A marked share in USDC: the BUCK-denominated share mark times that day's
// recorded full-value BUCK bid. No executable holder sale is implied.
export function bidMark(frame) {
  const share = num(frame?.basket?.share_price), bid = num(frame?.market?.book?.buck_bid);
  return share !== null && bid !== null && share > 0 && bid > 0 ? share * bid : null;
}
export function commonBidBaseline(runs) {
  const candidates = runs.map((run) => ({run, marks: new Map(run.frames.map((f) => [f.day, bidMark(f)]).filter(([, v]) => v !== null))})).filter((x) => x.marks.size);
  if (!candidates.length) return {day: null, marks: new Map()};
  const days = [...candidates[0].marks.keys()].sort((a, b) => a - b);
  const day = days.find((d) => candidates.every((x) => x.marks.has(d)));
  return day === undefined ? {day: null, marks: new Map()} : {day, marks: new Map(candidates.map((x) => [x.run.id, x.marks.get(day)]))};
}

export function specs(runs) {
  const commonBid = commonBidBaseline(runs);
  const roiMetrics = [
    { id: "share-bid-roi", title: "Share return in USDC at the market BUCK bid", unit: "pct", kind: "line",
      note: "Same-day BUCK share mark multiplied by the recorded market BUCK bid, rebased to the earliest common day with both observations for the available runs. Missing bids stay unavailable. This is a mark, not the proceeds from redeeming and selling a holder position; fees, slippage and exit restrictions are excluded.",
      value: (f, r) => f && commonBid.day !== null && f.day >= commonBid.day ? roi(bidMark(f), commonBid.marks.get(r.id)) : null },
    { id: "share-roi", title: "Share return from opening mark", unit: "pct", kind: "line",
      note: "Uses the mark after startup when recorded, or the first close.",
      value: (f, r) => roi(f?.basket?.share_price, shareBaseline(r.frames[0])) },
    { id: "passive-roi", title: "Passive TOKEN return from first close", unit: "pct", kind: "line", value: (f, r) => roi(f?.basket?.hold_index, r.frames[0]?.basket?.hold_index) },
    { id: "treatment-roi", title: "Share return from pre-switch mark", unit: "pct", kind: "line", value: (f, r) => f?.day >= r.treatment_day ? roi(f?.basket?.share_price, r.baseline_share_price) : null },
  ];
  return [...roiMetrics, ...METRICS].map((m) => ({
    id: m.id, title: m.title, unit: m.unit, kind: "line", reference: m.reference, note: m.note,
    series: runs.map((r) => ({ key: r.label, label: r.label, value: (f) => m.value(f.runs[r.label], r) })),
  }));
}

// The charts' markers: the day each run departs from the reference, one marker a day.
export function departures(table) {
  const days = new Map();
  for (const r of table) {
    if (!r.diverges || r.diverges.any === null) continue;
    const day = r.diverges.watched ?? r.diverges.any;
    days.set(day, [...(days.get(day) ?? []), r.label]);
  }
  return [...days].sort((a, b) => a[0] - b[0])
    .map(([day, labels]) => ({ day, label: `${labels.join(", ")} ${labels.length > 1 ? "depart" : "departs"}` }));
}

export function dayText(d) {
  if (!d) return "";
  if (d.any === null) return d.days ? `never (${d.days} days)` : "no common days";
  return `day ${d.watched ?? d.any}${d.watched === null ? " (not in K, BVIB, price or supply)" : ""}`;
}

// --- the page --------------------------------------------------------------------------------- //
const html = (tag, attrs = {}, text = null) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== null) node.textContent = text;
  return node;
};

// A difference keeps its sign, and a small ratio its digits: a K one part in a million apart is still apart.
export function signed(v, unit) {
  if (v === null || v === undefined) return "";
  const sign = v > 0 ? "+" : v < 0 ? "-" : "±", m = Math.abs(v);
  if (unit === "ratio" && m > 0 && m < 0.01) return `${sign}${m.toPrecision(2)}`;
  return `${sign}${format(m, unit)}`;
}

// Empty means latest; reject negatives, fractions, unsafe integers and malformed URLs.
export function parseThroughDay(value) {
  if (value == null || value === "") return null;
  if (!/^\d+$/.test(String(value))) return NaN;
  const n = Number(value);
  return Number.isSafeInteger(n) ? n : NaN;
}
export function effectiveEndDay(requested, maximum) {
  if (maximum == null) return requested;
  return requested == null ? maximum : Math.min(requested, maximum);
}

export class CompareTab {
  // source: { comparisons(), comparison(name), runs(), manifest(id), frames(id) }
  constructor(root, bus, source) {
    this.root = root;
    this.bus = bus;
    this.source = source;
    const query = new URL(location.href).searchParams;
    const requestedDay = parseThroughDay(query.get("day"));
    this.state = { mode: "comparison", name: query.get("comparison"), vary: null, held: {}, a: null, b: null,
      through: Number.isNaN(requestedDay) ? null : requestedDay };
    this.controls = html("div", { class: "compare-controls" });
    this.question = html("p", { class: "note" });
    this.differs = html("div", { class: "compare-differs" });
    this.table = html("div", { class: "table-wrap" });
    this.grid = html("div", { class: "charts" });
    this.problem = html("p", { class: "problem", role: "alert", hidden: "" });
    root.replaceChildren(this.controls, this.question, this.problem, this.differs, this.table, this.grid);
    this.cache = new Map();
    this.loading = false;
  }

  async show(refresh = false) {
    if (this.loading) return;
    this.loading = true;
    try {
      this.list = await this.source.comparisons();
      this.runList = await this.source.runs();
    } catch (e) {
      this.loading = false;
      return this.fail(`Could not list comparisons: ${e.message}`);
    }
    this.cache.clear();
    if (!this.state.name && this.list.length) this.state.name = this.list[0].name;
    if (!this.list.length && !this.state.name) this.state.mode = "pair";
    this.state.a ??= this.runList[1]?.id ?? this.runList[0]?.id ?? null;
    this.state.b ??= this.runList[0]?.id ?? null;
    try { await this.draw(refresh); } finally { this.loading = false; }
  }

  fail(text) {
    this.problem.hidden = false;
    this.problem.textContent = text;
  }

  select(label, options, value, onChange) {
    const wrap = html("label", { class: "compare-pick" });
    wrap.append(document.createTextNode(`${label} `));
    const s = html("select");
    for (const [v, text] of options) {
      const o = html("option", { value: v }, text);
      if (v === value) o.selected = true;
      s.append(o);
    }
    s.addEventListener("change", () => onChange(s.value));
    wrap.append(s);
    return wrap;
  }

  async frames(id) {
    if (!this.cache.has(id)) this.cache.set(id, this.source.frames(id));
    return this.cache.get(id);
  }

  async draw(refresh = false) {
    this.problem.hidden = true;
    const st = this.state;
    const modes = [["comparison", "A comparison"], ["pair", "Any two runs"]];
    const controls = [this.select("Compare", modes, st.mode, (v) => { st.mode = v; this.draw(); })];
    let runs = [], index = null;
    if (st.mode === "comparison") {
      if (!this.list.length) return this.fail("No comparisons yet. Run an economy replay or matched basket study with sim-workbench.");
      controls.push(this.select("Comparison", this.list.map((c) => [c.name, c.name]), st.name, (v) => {
        Object.assign(st, { name: v, vary: null, held: {} }); const url = new URL(location.href); url.searchParams.set("comparison", v); history.replaceState(null, "", url); this.draw();
      }));
      index = this.list.find((c) => c.name === st.name);
      if (!index) return this.fail(`Comparison ${st.name} is not available yet; this page will retry.`);
      if (!index.axes?.length) return this.fail(`${index.name}: ${index.error ?? "no axes"}`);
      st.vary ??= index.axes[0].name;
      controls.push(this.select("Vary", index.axes.map((a) => [a.name, a.name]), st.vary, (v) => { st.vary = v; this.draw(); }));
      for (const a of index.axes) {
        if (a.name === st.vary) continue;
        controls.push(this.select(`Hold ${a.name} at`, a.values.map((v) => [v.label, v.label]), st.held[a.name] ?? a.reference,
          (v) => { st.held[a.name] = v; this.draw(); }));
      }
      runs = selection(index, st.vary, st.held).slice(0, MAX_RUNS);
      const axis = index.axes.find((a) => a.name === st.vary);
      const sentence = (t) => (/[.?!]$/.test(t) ? t : `${t}.`);
      this.question.textContent = [index.question && sentence(index.question), axis?.note ? sentence(`${axis.name}: ${axis.note}`) : null,
        `${index.days ?? "All"} days of ${index.experiment}.`].filter(Boolean).join(" ");
    } else {
      const opts = runOptions(this.runList);
      controls.push(this.select("Run", opts, st.a, (v) => { st.a = v; this.draw(); }));
      controls.push(this.select("against", opts, st.b, (v) => { st.b = v; this.draw(); }));
      runs = [{ id: st.b, label: "reference" }, { id: st.a, label: "run" }].filter((r) => r.id);
      this.question.textContent = "Any two runs: the settings that differ between them, and what differs in their days.";
    }
    const effective = effectiveEndDay(st.through, index?.end_day);
    const dayLabel = html("label", { class: "compare-pick" });
    dayLabel.append(document.createTextNode("Through day "));
    const dayInput = html("input", { type: "number", min: "0", step: "1", placeholder: "Latest", "aria-label": "Through day", style: "width: 6.5em" });
    if (index?.end_day != null) dayInput.max = String(index.end_day);
    if (st.through != null) dayInput.value = String(effective);
    dayInput.addEventListener("change", () => {
      const day = parseThroughDay(dayInput.value);
      if (Number.isNaN(day) || (index?.end_day != null && day != null && day > index.end_day)) {
        dayInput.setCustomValidity(index?.end_day != null ? `Enter a whole day from 0 to ${index.end_day}.` : "Enter a non-negative whole day.");
        dayInput.reportValidity();
        return;
      }
      dayInput.setCustomValidity("");
      st.through = day;
      const url = new URL(location.href);
      if (day == null) url.searchParams.delete("day"); else url.searchParams.set("day", String(day));
      history.replaceState(null, "", url);
      this.draw();
    });
    dayLabel.append(dayInput);
    controls.push(dayLabel);
    if (st.through != null && index?.end_day != null && st.through > index.end_day) {
      this.question.textContent += ` Limited to day ${index.end_day} by this comparison.`;
    }
    await Promise.all(runs.map(async (r) => {
      const info = this.runList.find((item) => item.id === r.id);
      r.status = info?.status ?? "not started";
      try { const all = info ? await this.frames(r.id) : []; r.frames = clipFrames(all, effective); }
      catch (e) { r.frames = []; r.status = `unavailable: ${e.message}`; }
    }));
    const { updated: _updated, ...stableIndex } = index ?? {};
    const signature = JSON.stringify({index: stableIndex, runs});
    if (refresh && signature === this.signature) return;
    this.signature = signature;
    this.controls.replaceChildren(...controls);
    await this.drawDiffers(index, runs);
    this.drawTable(runs);
    this.drawCharts(runs);
  }

  async drawDiffers(index, runs) {
    const box = this.differs;
    box.replaceChildren(html("h3", {}, "What differs"));
    const list = html("ul", { class: "compare-list" });
    if (index) {
      const axis = index.axes.find((a) => a.name === this.state.vary);
      for (const r of runs) {
        const v = axis.values.find((x) => x.label === r.label);
        const sets = Object.entries(v?.set ?? {}).map(([k, x]) => `${k} = ${JSON.stringify(x)}`).join(", ");
        const treatment = treatmentText(r.values?.arm, r.treatment_day);
        const detail = [treatment, sets].filter(Boolean).join(" · ");
        list.append(html("li", {}, `${r.label}${r.label === axis.reference ? " (reference)" : ""}${detail ? ` · ${detail}` : ""}`));
      }
    } else if (runs.length === 2) {
      const [ma, mb] = await Promise.all(runs.map((r) => this.source.manifest(r.id).catch(() => null)));
      const diff = settingsDiff(mb, ma);
      for (const [run, manifest] of [[runs[0], ma], [runs[1], mb]]) {
        const treatment = treatmentText(manifest?.settings?.arm, manifest?.settings?.warmup_days);
        if (treatment) list.append(html("li", {}, `${run.label} · ${treatment}`));
      }
      if (!diff.length) list.append(html("li", {}, "Recorded settings agree."));
      for (const d of diff) list.append(html("li", {}, `${d.key}: ${d.a} (run) against ${d.b} (reference)`));
    }
    box.append(list);
  }

  drawTable(runs) {
    const t = html("table");
    const head = html("tr");
    for (const text of ["Run", "Status", "First differs", ...COLUMNS.map((c) => c.label)]) head.append(html("th", { scope: "col" }, text));
    t.append(html("thead"));
    t.firstChild.append(head);
    const body = html("tbody");
    for (const r of rows(runs)) {
      const tr = html("tr");
      tr.append(html("th", { scope: "row", title: r.id }, r.label));
      const source = runs.find((item) => item.id === r.id);
      tr.append(html("td", {}, `${source?.status ?? "unknown"} · ${source?.frames?.length ?? 0} days${source?.frames?.length ? ` (${source.frames[0].day}–${source.frames.at(-1).day})` : ""}${source?.provisional ? " · provisional" : ""}${r.diverges && !r.comparable ? " · different endpoints; no deltas" : ""}`));
      tr.append(html("td", {}, r.diverges ? dayText(r.diverges) : "reference"));
      for (const c of COLUMNS) {
        const td = html("td", { class: "num" }, format(r.figures[c.key], c.unit));
        if (r.diff[c.key] !== null) td.append(html("span", { class: "delta" }, ` ${signed(r.diff[c.key], c.unit)}`));
        tr.append(td);
      }
      body.append(tr);
    }
    t.append(body);
    this.table.replaceChildren(t);
  }

  drawCharts(runs) {
    for (const c of this.charts ?? []) c.unsubscribe?.();
    this.grid.replaceChildren();
    const frames = merge(runs);
    const marks = departures(rows(runs));
    this.charts = specs(runs).map((spec) => {
      const card = html("section", { class: "card", "aria-label": spec.title });
      this.grid.append(card);
      const chart = new TimeChart(card, spec, this.bus);
      chart.update(frames, marks);
      return chart;
    });
  }
}
