// A small line chart: the lines in an SVG stretched to its box (strokes kept
// at pixel width), the labels in HTML over it, so text stays legible at any
// width, a phone's included.  No library: a few hundred points per line.

import { h } from "./dom.js";

const SVG = "http://www.w3.org/2000/svg";
const W = 1000;
const H = 100;
const MAX_POINTS = 800;

const svg = (tag, attrs = {}) => {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
};

// 1, 2 or 5 times a power of ten: about `n` steps over [lo, hi].
function niceStep(lo, hi, n = 3) {
  const raw = (hi - lo) / n;
  const p = 10 ** Math.floor(Math.log10(raw));
  const m = raw / p;
  return (m >= 5 ? 10 : m >= 2 ? 5 : m >= 1 ? 2 : 1) * p;
}

/** A chart: `series` [{label, color (a CSS colour or var), dash}], `fmt`
 *  for its axis and legend values, `ref` a y worth a guide line (1 for an
 *  index).  update(xs, ys) redraws: ys[k] the k-th series' values (null
 *  where it has none). */
export function lineChart({ title, series, fmt = (v) => v.toPrecision(3), ref = null, height = 130,
                            note }) {
  const legend = h("div", { class: "legend" });
  const plot = h("div", { class: "plot", style: `height:${height}px` });
  const box = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" });
  plot.append(box);
  const el = h("figure", { class: "chart" },
    h("figcaption", {}, h("span", { class: "chart-title" }, title), legend), plot,
    note ? h("div", { class: "hint" }, note) : null);

  function update(xs, ys) {
    box.replaceChildren();
    for (const lab of plot.querySelectorAll(".ylab, .xlab, .nodata")) lab.remove();
    const vals = ys.flat().filter((v) => v !== null && Number.isFinite(v));
    if (xs.length < 2 || vals.length === 0) {
      plot.append(h("span", { class: "nodata" }, "waiting for the world…"));
      fillLegend(ys);
      return;
    }
    let lo = Math.min(...vals, ...(ref !== null ? [ref] : []));
    let hi = Math.max(...vals, ...(ref !== null ? [ref] : []));
    if (hi - lo < Math.abs(hi) * 1e-4 + 1e-12) {
      const pad = Math.abs(hi) * 0.01 || 1;
      lo -= pad;
      hi += pad;
    }
    const padY = (hi - lo) * 0.06;
    lo -= padY;
    hi += padY;
    const x0 = xs[0];
    const x1 = xs[xs.length - 1];
    const X = (x) => ((x - x0) / (x1 - x0 || 1)) * W;
    const Y = (y) => H - ((y - lo) / (hi - lo)) * H;

    const step = niceStep(lo, hi);
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
      box.append(svg("line", { x1: 0, x2: W, y1: Y(v), y2: Y(v), class: "grid",
                               "vector-effect": "non-scaling-stroke" }));
      plot.append(h("span", { class: "ylab", style: `top:${(Y(v) / H) * 100}%` }, fmt(v)));
    }
    if (ref !== null) {
      box.append(svg("line", { x1: 0, x2: W, y1: Y(ref), y2: Y(ref), class: "ref",
                               "vector-effect": "non-scaling-stroke" }));
    }
    const xstep = niceStep(x0, x1, 4) || 1;
    for (let x = Math.ceil(x0 / xstep) * xstep; x <= x1; x += xstep) {
      const at = (X(x) / W) * 100;
      if (at > 92) continue;
      plot.append(h("span", { class: "xlab", style: `left:${at}%` }, `day ${Math.round(x)}`));
    }

    const every = Math.max(1, Math.ceil(xs.length / MAX_POINTS));
    series.forEach((s, k) => {
      let d = "";
      let pen = false;
      xs.forEach((x, i) => {
        const y = ys[k]?.[i];
        if (i % every && i !== xs.length - 1) return;
        if (y === null || y === undefined || !Number.isFinite(y)) { pen = false; return; }
        d += `${pen ? "L" : "M"}${X(x).toFixed(1)} ${Y(y).toFixed(2)}`;
        pen = true;
      });
      if (d) {
        box.append(svg("path", { d, fill: "none", class: "line",
                                 style: `stroke:${s.color}${s.dash ? ";stroke-dasharray:6 4" : ""}`,
                                 "vector-effect": "non-scaling-stroke" }));
      }
    });
    fillLegend(ys);
  }

  function fillLegend(ys) {
    legend.replaceChildren(...series.map((s, k) => {
      const col = ys[k] ?? [];
      let last = null;
      for (let i = col.length - 1; i >= 0; i--) if (col[i] !== null && Number.isFinite(col[i])) { last = col[i]; break; }
      return h("span", { class: "key" },
        h("span", { class: `swatch${s.dash ? " dash" : ""}`, style: `border-color:${s.color}` }),
        s.label, last === null ? null : h("span", { class: "num" }, ` ${fmt(last)}`));
    }));
  }

  update([], series.map(() => []));
  return { el, update };
}
