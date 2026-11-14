// What the dashboard draws, as pure functions of run-record frames (schema buck-run/1,
// alberta_buck/sim/runrecord.py). No DOM here: node --test reads this module directly
// (core/js/test/dashboard-series.test.js), and every source -- a finished run, a growing
// one, or later a world running in the browser tab -- feeds it the same frames.

export const RAW = 1e6;

const buck = (raw) => (raw === null || raw === undefined ? null : Number(raw) / RAW);
const num = (v) => (v === null || v === undefined ? null : Number(v));

// The Overview's charts. A chart is "line" (one y-axis, any number of series) or "stack"
// (series summed bottom to top). Values are numbers or null (not recorded that day).
export const OVERVIEW = [
  {
    id: "k", title: "K", unit: "ratio", kind: "line",
    note: "The fraction of insured value that may be drawn as BUCK. The controller lowers it when the BUCK is cheap against the basket (BVIB above 1) and raises it when dear.",
    series: [{ key: "k", label: "K", value: (f) => num(f.control?.k) }],
  },
  {
    id: "price", title: "BVIB and BUCK/USD", unit: "ratio", kind: "line", reference: 1,
    note: "BVIB is the basket's value in BUCK, the controlled variable: 1 is equilibrium. BUCK/USD is the BUCK's dollar price in its USDC pool.",
    series: [
      { key: "bvib", label: "BVIB", value: (f) => num(f.control?.bvib) },
      { key: "buck_usd", label: "BUCK/USD", value: (f) => num(f.market?.buck_usd) },
    ],
  },
  {
    id: "holders", title: "Who holds the BUCK", unit: "buck", kind: "stack",
    note: "Nominal BUCK held by the Carrying accounts (the pools and the basket); everyone else is supply less those.",
    series: [
      { key: "token_pools", label: "TOKEN/BUCK pools", value: (f) => buck(f.supply?.holders?.token_pools) },
      { key: "dollar_pool", label: "BUCK/USDC pool", value: (f) => buck(f.supply?.holders?.dollar_pool) },
      { key: "basket", label: "Basket", value: (f) => buck(f.supply?.holders?.basket) },
      { key: "others", label: "Everyone else", value: (f) => buck(f.supply?.holders?.others) },
    ],
  },
  {
    id: "sources", title: "Supply by source", unit: "buck", kind: "stack",
    note: "Every BUCK is someone's principal. The basket's lien, the households' drawn principal, and all other principal (the opening lenders and keepers).",
    series: [
      { key: "basket_lien", label: "Basket lien", value: (f) => buck(f.supply?.basket_lien) },
      { key: "household_principal", label: "Household principal", value: (f) => buck(f.supply?.household_principal) },
      {
        key: "other", label: "Other principal",
        value: (f) => {
          const s = f.supply;
          if (!s || s.total === null || s.total === undefined) return null;
          return buck(Number(s.total) - Number(s.basket_lien ?? 0) - Number(s.household_principal ?? 0));
        },
      },
    ],
  },
  {
    id: "population", title: "Households by role", unit: "count", kind: "line",
    note: "Active households of each role.",
    series: ["debtor", "saver", "investor"].map((role) => ({
      key: role, label: role[0].toUpperCase() + role.slice(1) + "s",
      value: (f) => num(f.population?.roles?.[role]?.active),
    })),
  },
  {
    id: "health", title: "Refusals and faults", unit: "count", kind: "line",
    note: "Households whose last action was refused or failed that day, and dispatcher faults (a fault stops a household until it is reconciled).",
    series: [
      { key: "errors", label: "Households refused", value: (f) => num(f.health?.errors) },
      { key: "faults", label: "Faults", value: (f) => num(f.health?.faults) },
    ],
  },
];

// The frames inside a time window: "all", or the last N days of what has been recorded.
export function windowFrames(frames, range) {
  if (range === "all" || !frames.length) return frames;
  const last = frames[frames.length - 1].day;
  return frames.filter((f) => f.day > last - Number(range));
}

// One chart's data: days, and each series' values (null where not recorded).
export function chartData(spec, frames) {
  return {
    days: frames.map((f) => f.day),
    series: spec.series.map((s) => ({ key: s.key, label: s.label, values: frames.map((f) => s.value(f)) })),
  };
}

// A stack's cumulative tops, bottom series first; a missing value counts as 0 within a day
// that has any value, and leaves the day out when none has.
export function stackTops(data) {
  const tops = data.series.map(() => []);
  for (let i = 0; i < data.days.length; i++) {
    const any = data.series.some((s) => s.values[i] !== null);
    let sum = 0;
    data.series.forEach((s, j) => {
      sum += s.values[i] ?? 0;
      tops[j].push(any ? sum : null);
    });
  }
  return tops;
}

// The headline figures for the latest recorded day.
export function tiles(frames, manifest) {
  const f = frames[frames.length - 1];
  if (!f) return [];
  const roles = f.population?.roles ?? {};
  const active = Object.values(roles).reduce((n, r) => n + (r.active ?? 0), 0);
  const exited = Object.values(roles).reduce((n, r) => n + (r.exited ?? 0), 0);
  const faults = frames.reduce((n, x) => n + (x.health?.faults ?? 0), 0);
  const days = manifest?.scenario?.days;
  return [
    { id: "day", label: "Day", value: f.day, unit: "count", detail: days ? `of ${days}` : "" },
    { id: "k", label: "K", value: num(f.control?.k), unit: "ratio" },
    { id: "bvib", label: "BVIB", value: num(f.control?.bvib), unit: "ratio" },
    { id: "buck_usd", label: "BUCK/USD", value: num(f.market?.buck_usd), unit: "ratio" },
    { id: "supply", label: "Supply", value: buck(f.supply?.total), unit: "buck", detail: "BUCK" },
    { id: "households", label: "Households", value: f.population ? active : null, unit: "count",
      detail: f.population ? `${exited} exited` : "" },
    { id: "refused", label: "Refused today", value: num(f.health?.errors), unit: "count" },
    { id: "faults", label: "Faults", value: faults, unit: "count", detail: "whole run" },
  ];
}

// A run's state as the page shows it: a "running" run whose manifest has not been
// refreshed for staleSeconds is stale (its process probably died).
export function runStatus(manifest, now = Date.now(), staleSeconds = 120) {
  const status = manifest?.status ?? "unknown";
  if (status !== "running") return status;
  const updated = Date.parse(manifest.updated ?? manifest.started ?? "");
  return Number.isFinite(updated) && now - updated > staleSeconds * 1000 ? "stale" : "running";
}

export function format(value, unit) {
  if (value === null || value === undefined || Number.isNaN(value)) return "n/a";
  if (unit === "ratio") return value.toFixed(4);
  if (unit === "count") return Math.round(value).toLocaleString("en-US");
  if (unit === "buck") {
    const a = Math.abs(value);
    if (a >= 1e9) return (value / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return (value / 1e6).toFixed(2) + "M";
    if (a >= 1e3) return (value / 1e3).toFixed(1) + "k";
    return value.toFixed(2);
  }
  return String(value);
}

// Round-number axis ticks covering [lo, hi].
export function ticks(lo, hi, count = 5) {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw * (1 - 1e-9));   // (0.05/5 is 0.01 + 1e-17)
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}
