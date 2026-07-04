// Tiny dependency-free SVG line charts, BigInt-free numbers in, string
// out -- renders identically in node (write to a .svg file) and the
// browser (innerHTML), so the demo page and bin/eqsim.mjs share it.

const COLORS = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed"];

/**
 * @param opts.title  chart title
 * @param opts.series [{label, points: [[x, y], ...]}] -- y as Number
 * @param opts.refY   optional dashed reference line (e.g. 1.0 parity)
 * @param opts.yFmt   tick formatter
 * @returns a standalone <svg> fragment string (nestable via svgDoc)
 */
export function lineChart({ title, series, width = 860, height = 240,
                            refY = null, yFmt = (v) => v.toPrecision(4) }) {
  const m = { l: 64, r: 14, t: 26, b: 22 };
  const pts = series.flatMap((s) => s.points);
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  let ymin = Math.min(...ys, refY ?? Infinity);
  let ymax = Math.max(...ys, refY ?? -Infinity);
  const pad = (ymax - ymin || Math.abs(ymax) || 1) * 0.08;
  ymin -= pad; ymax += pad;
  const sx = (x) => m.l + ((x - xmin) / (xmax - xmin || 1)) * (width - m.l - m.r);
  const sy = (y) => height - m.b - ((y - ymin) / (ymax - ymin)) * (height - m.t - m.b);

  const yTicks = [0, 1, 2, 3, 4].map((i) => ymin + (i / 4) * (ymax - ymin));
  const xTicks = [0, 1, 2, 3, 4, 5].map((i) => xmin + (i / 5) * (xmax - xmin));
  const el = [];
  el.push(`<rect width="${width}" height="${height}" fill="#ffffff"/>`);
  el.push(`<text x="${m.l}" y="16" font-size="13" font-weight="bold" ` +
          `font-family="monospace">${title}</text>`);
  for (const t of yTicks) {
    el.push(`<line x1="${m.l}" y1="${sy(t)}" x2="${width - m.r}" y2="${sy(t)}" ` +
            `stroke="#e5e7eb"/>`);
    el.push(`<text x="${m.l - 6}" y="${sy(t) + 4}" font-size="10" ` +
            `font-family="monospace" text-anchor="end">${yFmt(t)}</text>`);
  }
  for (const t of xTicks) {
    el.push(`<text x="${sx(t)}" y="${height - 6}" font-size="10" ` +
            `font-family="monospace" text-anchor="middle">${Math.round(t)}</text>`);
  }
  if (refY !== null) {
    el.push(`<line x1="${m.l}" y1="${sy(refY)}" x2="${width - m.r}" ` +
            `y2="${sy(refY)}" stroke="#9ca3af" stroke-dasharray="5,4"/>`);
  }
  series.forEach((s, i) => {
    const path = s.points.map((p) => `${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`)
                         .join(" ");
    const c = s.color ?? COLORS[i % COLORS.length];
    el.push(`<polyline points="${path}" fill="none" stroke="${c}" stroke-width="1.6"/>`);
    el.push(`<text x="${width - m.r}" y="${m.t + 12 * i}" font-size="11" ` +
            `font-family="monospace" text-anchor="end" fill="${c}">${s.label}</text>`);
  });
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" ` +
         `height="${height}" viewBox="0 0 ${width} ${height}">${el.join("")}</svg>`;
}

/** Stack chart fragments vertically into one SVG document. */
export function svgDoc(charts, { width = 860 } = {}) {
  let y = 0;
  const parts = [];
  for (const c of charts) {
    const h = Number(c.match(/height="(\d+)"/)[1]);
    parts.push(`<svg y="${y}">${c.replace(/<\/?svg[^>]*>/g, "")}</svg>`);
    y += h + 8;
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${y}">` +
         parts.join("") + `</svg>`;
}
