// Comparing runs that are identical but for the settings under study (alberta_buck/sim/compare.py).
// Two ways in: a comparison's index (its axes, and which run is which), where one axis is varied and
// every other held at a chosen value; or any two runs, whose manifests say which settings differ.
// Either way the runs' frames are drawn on the same charts, one line per run, with the first day each
// run departs from the first (the reference) marked, and a table of the differences.
//
// The functions here are pure (node --test reads them: core/js/test/dashboard-compare.test.js); the
// CompareTab at the end is the page's.

import { TimeChart } from "./chart.js";
import { RAW, format } from "./series.js";

export const MAX_RUNS = 6;     // one categorical hue per run, never cycled: more values, fewer at once

const num = (v) => (v === null || v === undefined ? null : Number(v));
const buck = (raw) => (raw === null || raw === undefined ? null : Number(raw) / RAW);
const get = (f, path) => path.reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), f);

// The observables a pair of runs is compared on, as in compare.py's WATCHED.
const WATCHED = [["control", "k"], ["control", "bvib"], ["market", "buck_usd"], ["supply", "total"]];

// What each Compare chart draws for one run's frame.
export const METRICS = [
  { id: "k", title: "K", unit: "ratio", value: (f) => num(f?.control?.k) },
  { id: "bvib", title: "BVIB", unit: "ratio", reference: 1, value: (f) => num(f?.control?.bvib) },
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
export function selection(index, vary, held = {}) {
  const axis = index.axes.find((a) => a.name === vary);
  if (!axis) return [];
  const order = axis.values.map((v) => v.label);
  return index.runs
    .filter((r) => index.axes.every((a) => a.name === vary || r.values[a.name] === (held[a.name] ?? a.reference)))
    .sort((a, b) => order.indexOf(a.values[vary]) - order.indexOf(b.values[vary]))
    .map((r) => ({ id: r.id, label: r.values[vary] }));
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
  const fa = flatten(a?.experiment), fb = flatten(b?.experiment);
  const keys = [...new Set([...Object.keys(fa), ...Object.keys(fb)])].sort();
  const out = keys.filter((k) => fa[k] !== fb[k]).map((key) => ({ key, a: fa[key] ?? "(unset)", b: fb[key] ?? "(unset)" }));
  const sa = a?.scenario?.seed, sb = b?.scenario?.seed;
  if (sa !== sb && !out.some((d) => d.key === "scenario.seed")) out.push({ key: "scenario.seed", a: String(sa), b: String(sb) });
  return out;
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

// One run's headline figures (compare.py's headline, the ones the table shows).
export function headline(frames) {
  if (!frames.length) return { days: 0 };
  const last = frames[frames.length - 1];
  const ks = frames.map((f) => num(f.control?.k)).filter((v) => v !== null);
  const bv = frames.map((f) => num(f.control?.bvib)).filter((v) => v !== null);
  const mean = (xs) => (xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : null);
  return {
    days: frames.length, k_end: ks.at(-1) ?? null, k_mean: mean(ks),
    bvib_gap_mean: mean(bv.map((x) => Math.abs(x - 1))), buck_usd_end: num(last.market?.buck_usd),
    supply_end: buck(last.supply?.total), lien_end: buck(last.supply?.basket_lien),
    arrived: Object.values(last.population?.roles ?? {}).reduce((s, r) => s + (r.arrived ?? 0), 0),
    refused_days: frames.reduce((s, f) => s + (num(f.health?.errors) ?? 0), 0),
  };
}

export const COLUMNS = [
  { key: "k_end", label: "K at end", unit: "ratio" }, { key: "k_mean", label: "Mean K", unit: "ratio" },
  { key: "bvib_gap_mean", label: "Mean |BVIB - 1|", unit: "ratio" }, { key: "buck_usd_end", label: "BUCK/USD at end", unit: "ratio" },
  { key: "supply_end", label: "Supply at end", unit: "buck" }, { key: "lien_end", label: "Basket lien at end", unit: "buck" },
  { key: "arrived", label: "Households arrived", unit: "count" }, { key: "refused_days", label: "Refusals", unit: "count" },
];

// The table: one row per run, its figures and their differences from the first run's, and the day it
// first departs from the first run.
export function rows(runs) {
  const ref = runs[0];
  const refFigures = ref ? headline(ref.frames) : null;
  return runs.map((r, i) => {
    const h = headline(r.frames);
    const diff = {};
    for (const c of COLUMNS) {
      const a = h[c.key], b = refFigures?.[c.key];
      diff[c.key] = i === 0 || a === null || b === null || a === undefined || b === undefined ? null : a - b;
    }
    return { label: r.label, id: r.id, figures: h, diff, diverges: i === 0 ? null : divergence(r.frames, ref.frames) };
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

export function specs(runs) {
  return METRICS.map((m) => ({
    id: m.id, title: m.title, unit: m.unit, kind: "line", reference: m.reference,
    series: runs.map((r) => ({ key: r.label, label: r.label, value: (f) => m.value(f.runs[r.label]) })),
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

export class CompareTab {
  // source: { comparisons(), comparison(name), runs(), manifest(id), frames(id) }
  constructor(root, bus, source) {
    this.root = root;
    this.bus = bus;
    this.source = source;
    this.state = { mode: "comparison", name: null, vary: null, held: {}, a: null, b: null };
    this.controls = html("div", { class: "compare-controls" });
    this.question = html("p", { class: "note" });
    this.differs = html("div", { class: "compare-differs" });
    this.table = html("div", { class: "table-wrap" });
    this.grid = html("div", { class: "charts" });
    this.problem = html("p", { class: "problem", role: "alert", hidden: "" });
    root.replaceChildren(this.controls, this.question, this.problem, this.differs, this.table, this.grid);
    this.cache = new Map();
  }

  async show() {
    try {
      this.list = await this.source.comparisons();
      this.runList = await this.source.runs();
    } catch (e) {
      return this.fail(`Could not list comparisons: ${e.message}`);
    }
    if (!this.state.name && this.list.length) this.state.name = this.list[0].name;
    if (!this.list.length) this.state.mode = "pair";
    this.state.a ??= this.runList[1]?.id ?? this.runList[0]?.id ?? null;
    this.state.b ??= this.runList[0]?.id ?? null;
    await this.draw();
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

  async draw() {
    this.problem.hidden = true;
    const st = this.state;
    const modes = [["comparison", "A comparison"], ["pair", "Any two runs"]];
    const controls = [this.select("Compare", modes, st.mode, (v) => { st.mode = v; this.draw(); })];
    let runs = [], index = null;
    if (st.mode === "comparison") {
      if (!this.list.length) return this.fail("No comparisons yet: make nix-venv-compare COMPARE=<name> (alberta_buck/sim/comparisons/).");
      controls.push(this.select("Comparison", this.list.map((c) => [c.name, c.name]), st.name, (v) => {
        Object.assign(st, { name: v, vary: null, held: {} }); this.draw();
      }));
      index = this.list.find((c) => c.name === st.name) ?? this.list[0];
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
      const opts = this.runList.map((r) => [r.id, r.id]);
      controls.push(this.select("Run", opts, st.a, (v) => { st.a = v; this.draw(); }));
      controls.push(this.select("against", opts, st.b, (v) => { st.b = v; this.draw(); }));
      runs = [{ id: st.b, label: "reference" }, { id: st.a, label: "run" }].filter((r) => r.id);
      this.question.textContent = "Any two runs: the settings that differ between them, and what differs in their days.";
    }
    this.controls.replaceChildren(...controls);
    try {
      for (const r of runs) r.frames = await this.frames(r.id);
    } catch (e) {
      return this.fail(`Could not read the runs: ${e.message}`);
    }
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
        list.append(html("li", {}, `${r.label}${r.label === axis.reference ? " (reference)" : ""}: ${sets || "the experiment's own settings"}`));
      }
    } else if (runs.length === 2) {
      const [ma, mb] = await Promise.all(runs.map((r) => this.source.manifest(r.id)));
      const diff = settingsDiff(mb, ma);
      if (!diff.length) list.append(html("li", {}, "No setting differs: the runs should be the same."));
      for (const d of diff) list.append(html("li", {}, `${d.key}: ${d.a} (run) against ${d.b} (reference)`));
    }
    box.append(list);
  }

  drawTable(runs) {
    const t = html("table");
    const head = html("tr");
    for (const text of ["Run", "First differs", ...COLUMNS.map((c) => c.label)]) head.append(html("th", { scope: "col" }, text));
    t.append(html("thead"));
    t.firstChild.append(head);
    const body = html("tbody");
    for (const r of rows(runs)) {
      const tr = html("tr");
      tr.append(html("th", { scope: "row", title: r.id }, r.label));
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
