// A time chart for run records: lines on one y-axis, or a stack whose band edges are lines.
// One axis always (two measures of different scale are two charts). Hovering any chart moves
// the crosshair on every chart of the page to the same day (the HoverBus), with one tooltip
// listing every series at that day. Keyboard: focus the plot, then the arrow keys move the day.
// A table view of the same numbers sits behind each card's "Table" button.

import { chartData, format, stackTops, ticks } from "./series.js";

const SVG = "http://www.w3.org/2000/svg";
const MARGIN = { top: 14, right: 128, bottom: 26, left: 58 };
const NARROW = 480;            // below this width: no end labels (the legend names the series)
const HEIGHT = 220;

const el = (tag, attrs = {}, ns = SVG) => {
  const node = document.createElementNS(ns, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
};
const html = (tag, attrs = {}, text = null) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== null) node.textContent = text;
  return node;
};

// Hover shared by every chart on the page: the day under the pointer (or null), and the chart
// it is over. Every chart draws its crosshair at that day; only that chart shows the tooltip.
export class HoverBus {
  constructor() { this.listeners = new Set(); this.day = null; this.owner = null; }
  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  set(day, owner = null) {
    if (day === this.day && owner === this.owner) return;
    this.day = day;
    this.owner = owner;
    for (const fn of this.listeners) fn(day, owner);
  }
}

export class TimeChart {
  constructor(card, spec, bus) {
    this.spec = spec;
    this.bus = bus;
    this.data = { days: [], series: [] };
    this.markers = [];
    this.showTable = false;

    const head = html("div", { class: "card-head" });
    head.append(html("h3", {}, spec.title));
    this.tableButton = html("button", { type: "button", class: "ghost", "aria-pressed": "false" }, "Table");
    this.tableButton.addEventListener("click", () => this.toggleTable());
    head.append(this.tableButton);
    card.append(head);
    if (spec.note) card.append(html("p", { class: "note" }, spec.note));
    if (spec.series.length > 1) {
      const legend = html("div", { class: "legend" });
      spec.series.forEach((s, i) => {
        const item = html("span", { class: "legend-item" });
        item.append(html("span", { class: `key ${spec.kind === "stack" ? "key-area" : "key-line"}`, style: `--c: var(--series-${i + 1})` }));
        item.append(document.createTextNode(s.label));
        legend.append(item);
      });
      card.append(legend);
    }
    this.plot = html("div", { class: "plot", tabindex: "0", role: "img", "aria-label": spec.title });
    this.tooltip = html("div", { class: "tooltip", hidden: "" });
    this.table = html("div", { class: "table-wrap", hidden: "" });
    card.append(this.plot, this.table);
    this.plot.append(this.tooltip);

    this.plot.addEventListener("pointermove", (e) => this.bus.set(this.dayAt(e), this));
    this.plot.addEventListener("pointerleave", () => this.bus.set(null));
    this.plot.addEventListener("keydown", (e) => this.key(e));
    this.plot.addEventListener("blur", () => this.bus.set(null));
    this.unsubscribe = bus.on((day, owner) => this.hover(day, owner));
    this.resize = new ResizeObserver(() => this.render());
    this.resize.observe(this.plot);
  }

  update(frames, markers = []) {
    this.data = chartData(this.spec, frames);
    this.markers = markers;
    this.render();
    if (this.showTable) this.renderTable();
  }

  // --- geometry ------------------------------------------------------------------------------ //
  scales() {
    const width = Math.max(280, this.plot.clientWidth || 640);
    const right = this.labelled(width) ? MARGIN.right : 12;
    const { days, series } = this.data;
    const x0 = days[0] ?? 0, x1 = days[days.length - 1] ?? 1;
    const tops = this.spec.kind === "stack" ? stackTops(this.data) : null;
    const values = (tops ?? series.map((s) => s.values)).flat().filter((v) => v !== null && Number.isFinite(v));
    if (this.spec.reference !== undefined) values.push(this.spec.reference);
    let lo = this.spec.kind === "stack" || this.spec.unit === "count" ? 0 : Math.min(...values);
    let hi = values.length ? Math.max(...values) : 1;
    if (!values.length) { lo = 0; hi = 1; }
    if (hi === lo) { hi += Math.abs(hi) * 0.05 || 1; lo -= this.spec.kind === "stack" || this.spec.unit === "count" ? 0 : Math.abs(lo) * 0.05 || 1; }
    const pad = (hi - lo) * 0.06;
    if (!(this.spec.kind === "stack" || this.spec.unit === "count") || lo < 0) lo -= pad;
    hi += pad;
    const iw = width - MARGIN.left - right, ih = HEIGHT - MARGIN.top - MARGIN.bottom;
    const x = (d) => MARGIN.left + (x1 === x0 ? iw / 2 : ((d - x0) / (x1 - x0)) * iw);
    const y = (v) => MARGIN.top + ih - ((v - lo) / (hi - lo)) * ih;
    return { width, right, x, y, lo, hi, x0, x1, iw, ih, tops };
  }

  labelled(width) {
    return width >= NARROW && this.spec.series.length > 1 && this.spec.series.length <= 4;
  }

  dayAt(event) {
    const { days } = this.data;
    if (!days.length) return null;
    const box = this.plot.getBoundingClientRect();
    const { x0, x1, iw } = this.geom ?? this.scales();
    const t = Math.min(1, Math.max(0, (event.clientX - box.left - MARGIN.left) / iw));
    const target = x0 + t * (x1 - x0);
    let best = days[0];
    for (const d of days) if (Math.abs(d - target) < Math.abs(best - target)) best = d;
    return best;
  }

  key(e) {
    const { days } = this.data;
    if (!days.length || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    let i = days.indexOf(this.bus.day);
    if (i < 0) i = days.length - 1;
    if (e.key === "ArrowLeft") i = Math.max(0, i - 1);
    if (e.key === "ArrowRight") i = Math.min(days.length - 1, i + 1);
    if (e.key === "Home") i = 0;
    if (e.key === "End") i = days.length - 1;
    this.bus.set(days[i], this);
  }

  // --- drawing ------------------------------------------------------------------------------- //
  render() {
    const g = this.geom = this.scales();
    const { width, right, x, y, lo, hi, tops, iw } = g;
    const { days, series } = this.data;
    const svg = el("svg", { viewBox: `0 0 ${width} ${HEIGHT}`, width, height: HEIGHT, "aria-hidden": "true" });
    const empty = series.every((s) => s.values.every((v) => v === null || !Number.isFinite(v)));

    for (const t of empty ? [] : ticks(lo, hi, 4)) {
      svg.append(el("line", { x1: MARGIN.left, x2: width - right, y1: y(t), y2: y(t), class: "grid" }));
      const label = el("text", { x: MARGIN.left - 8, y: y(t) + 4, class: "axis-label", "text-anchor": "end" });
      label.textContent = this.spec.unit === "ratio" ? Number(t.toPrecision(6)).toString() : format(t, this.spec.unit);
      svg.append(label);
    }
    svg.append(el("line", { x1: MARGIN.left, x2: width - right, y1: HEIGHT - MARGIN.bottom, y2: HEIGHT - MARGIN.bottom, class: "baseline" }));
    if (days.length) {
      for (const t of ticks(days[0], days[days.length - 1], Math.max(2, Math.floor(iw / 90))).filter((d) => Number.isInteger(d))) {
        const label = el("text", { x: x(t), y: HEIGHT - 8, class: "axis-label", "text-anchor": "middle" });
        label.textContent = `day ${t}`;
        svg.append(label);
      }
    }
    if (this.spec.reference !== undefined && this.spec.reference >= lo && this.spec.reference <= hi) {
      svg.append(el("line", { x1: MARGIN.left, x2: width - right, y1: y(this.spec.reference), y2: y(this.spec.reference), class: "reference" }));
    }
    for (const m of this.markers) {
      if (!days.length || m.day < days[0] || m.day > days[days.length - 1]) continue;
      svg.append(el("line", { x1: x(m.day), x2: x(m.day), y1: MARGIN.top, y2: HEIGHT - MARGIN.bottom, class: "marker" }));
      const label = el("text", { x: x(m.day) + 3, y: MARGIN.top + 9, class: "marker-label" });
      label.textContent = m.label;
      svg.append(label);
    }

    const path = (vals) => {
      let d = "", open = false;
      vals.forEach((v, i) => {
        if (v === null || !Number.isFinite(v)) { open = false; return; }
        d += `${open ? "L" : "M"}${x(days[i]).toFixed(1)},${y(v).toFixed(1)}`;
        open = true;
      });
      return d;
    };
    const ends = [];
    if (tops) {
      tops.forEach((top, j) => {
        const below = j ? tops[j - 1] : days.map(() => lo < 0 ? 0 : lo);
        let d = "";
        const idx = days.map((_, i) => i).filter((i) => top[i] !== null);
        if (idx.length) {
          d = idx.map((i, k) => `${k ? "L" : "M"}${x(days[i]).toFixed(1)},${y(top[i]).toFixed(1)}`).join("")
            + idx.slice().reverse().map((i) => `L${x(days[i]).toFixed(1)},${y(below[i] ?? 0).toFixed(1)}`).join("") + "Z";
          svg.append(el("path", { d, class: "band", style: `--c: var(--series-${j + 1})` }));
        }
      });
      tops.forEach((top, j) => {
        svg.append(el("path", { d: path(top), class: "gap" }));
        svg.append(el("path", { d: path(top), class: "line", style: `--c: var(--series-${j + 1})` }));
        const last = top.map((v, i) => [v, i]).filter(([v]) => v !== null).pop();
        const below = j ? tops[j - 1] : null;
        if (last) ends.push({ j, y: y(below ? (last[0] + (below[last[1]] ?? 0)) / 2 : last[0] / 2), label: series[j].label });
      });
    } else {
      series.forEach((s, j) => {
        svg.append(el("path", { d: path(s.values), class: "line", style: `--c: var(--series-${j + 1})` }));
        const last = s.values.map((v, i) => [v, i]).filter(([v]) => v !== null && Number.isFinite(v)).pop();
        if (last) ends.push({ j, y: y(last[0]), label: s.label });
      });
    }
    // Direct labels at the right end (up to four series), pushed apart so none overlap.
    if (this.labelled(width)) {
      ends.sort((a, b) => a.y - b.y);
      for (let i = 1; i < ends.length; i++) ends[i].y = Math.max(ends[i].y, ends[i - 1].y + 13);
      for (const e of ends) {
        svg.append(el("line", { x1: width - right + 6, x2: width - right + 16, y1: e.y, y2: e.y, class: "line", style: `--c: var(--series-${e.j + 1})` }));
        const t = el("text", { x: width - right + 20, y: e.y + 4, class: "direct-label" });
        // The margin holds about 18 characters; the legend and tooltip carry the whole name.
        t.textContent = e.label.length > 18 ? `${e.label.slice(0, 17)}…` : e.label;
        svg.append(t);
      }
    }
    if (days.length && empty) {
      // A run recorded before these fields existed: say so rather than draw empty axes.
      const t = el("text", { x: MARGIN.left + iw / 2, y: MARGIN.top + (HEIGHT - MARGIN.top - MARGIN.bottom) / 2,
        class: "empty-label", "text-anchor": "middle" });
      t.textContent = "Not recorded in this run";
      svg.append(t);
    }
    this.cross = el("line", { y1: MARGIN.top, y2: HEIGHT - MARGIN.bottom, class: "crosshair", visibility: "hidden" });
    svg.append(this.cross);
    this.plot.querySelector("svg")?.remove();
    this.plot.prepend(svg);
    if (!days.length) {
      this.tooltip.hidden = true;
    }
    this.hover(this.bus.day, this.bus.owner);
  }

  hover(day, owner = null) {
    const { days, series } = this.data;
    const i = day === null ? -1 : days.indexOf(day);
    if (!this.cross) return;
    if (i < 0) {
      this.cross.setAttribute("visibility", "hidden");
      this.tooltip.hidden = true;
      return;
    }
    const { x, width } = this.geom;
    const cx = x(day);
    this.cross.setAttribute("x1", cx);
    this.cross.setAttribute("x2", cx);
    this.cross.setAttribute("visibility", "visible");
    if (owner !== this) {
      this.tooltip.hidden = true;
      return;
    }
    this.tooltip.replaceChildren(html("div", { class: "tip-head" }, `day ${day}`));
    const rows = series.map((s, j) => [s, j]);
    if (this.spec.kind === "stack") rows.reverse();
    for (const [s, j] of rows) {
      const row = html("div", { class: "tip-row" });
      row.append(html("span", { class: "key key-line", style: `--c: var(--series-${j + 1})` }));
      row.append(html("strong", {}, format(s.values[i], this.spec.unit)));
      row.append(html("span", { class: "tip-label" }, s.label));
      this.tooltip.append(row);
    }
    if (this.spec.kind === "stack") {
      const total = series.reduce((n, s) => n + (s.values[i] ?? 0), 0);
      const row = html("div", { class: "tip-row tip-total" });
      row.append(html("strong", {}, format(total, this.spec.unit)));
      row.append(html("span", { class: "tip-label" }, "Total"));
      this.tooltip.append(row);
    }
    for (const m of this.markers.filter((mk) => mk.day === day)) {
      this.tooltip.append(html("div", { class: "tip-marker" }, m.label));
    }
    this.tooltip.hidden = false;
    const tw = this.tooltip.offsetWidth || 160;
    const left = cx + 12 + tw > width ? cx - 12 - tw : cx + 12;
    this.tooltip.style.left = `${Math.max(0, left)}px`;
    this.tooltip.style.top = `${MARGIN.top}px`;
  }

  toggleTable() {
    this.showTable = !this.showTable;
    this.tableButton.setAttribute("aria-pressed", String(this.showTable));
    this.table.hidden = !this.showTable;
    if (this.showTable) this.renderTable();
  }

  renderTable() {
    const { days, series } = this.data;
    const LIMIT = 400;
    const table = html("table");
    const head = html("tr");
    head.append(html("th", { scope: "col" }, "Day"));
    for (const s of series) head.append(html("th", { scope: "col" }, s.label));
    table.append(html("thead"));
    table.tHead.append(head);
    const body = html("tbody");
    for (let i = days.length - 1; i >= Math.max(0, days.length - LIMIT); i--) {
      const tr = html("tr");
      tr.append(html("td", {}, String(days[i])));
      for (const s of series) tr.append(html("td", {}, format(s.values[i], this.spec.unit)));
      body.append(tr);
    }
    table.append(body);
    const caption = days.length > LIMIT ? `Latest ${LIMIT} of ${days.length} days, newest first.` : "Newest first.";
    this.table.replaceChildren(html("p", { class: "note" }, caption), table);
  }
}
