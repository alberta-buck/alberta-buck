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
    note: "BVIB is the basket's value in net BUCK, the controlled variable: 1 is equilibrium. Each pool's price counts at its BUCK's net value, less the demurrage it carries; the quoted BVIB is the same at the pools' nominal prices (what K read before 2026-11-15). BUCK/USD is the BUCK's dollar price in its USDC pool.",
    series: [
      { key: "bvib", label: "BVIB", value: (f) => num(f.control?.bvib) },
      { key: "bvib_nominal", label: "BVIB quoted", value: (f) => num(f.control?.bvib_nominal) },
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
    id: "stages", title: "Households by life stage", unit: "count", kind: "line",
    note: "Active households by life stage, in a world with the household lifecycle: young families (the debtors), the middle-aged saving, and the retired drawing their savings down. Empty without it.",
    series: [["young", "Young families"], ["middle", "Middle-aged"], ["retired", "Retired"]].map(([key, label]) => ({
      key, label, value: (f) => num(f.population?.stages?.[key]),
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

// One chart's data: days, and each series' values (null where not recorded). A series' value is
// value(frame, index, frames), so a daily change can read the day before.
export function chartData(spec, frames) {
  return {
    days: frames.map((f) => f.day),
    series: spec.series.map((s) => ({ key: s.key, label: s.label, values: frames.map((f, i) => s.value(f, i, frames)) })),
  };
}

// The change since the previous recorded day of a cumulative counter (null on the first day).
export const perDay = (get) => (f, i, frames) => {
  if (i === 0) return null;
  const a = get(frames[i - 1]), b = get(f);
  return a === null || b === null || a === undefined || b === undefined ? null : Number(b) - Number(a);
};

const ratioOf = (a, b) => (a === null || a === undefined || !b ? null : Number(a) / Number(b));

// The basket's issuance limit in raw units: the contract's own (lien + headroom) when recorded.
// Its credit is marked at equity + held BUCK / K (BuckBasketEquityStorage._creditValueS, the "net
// mark"), so the limit K x mark is K x equity + the BUCK it holds in its own pool positions; older
// records are estimated that way (at TWAP rather than LOW marks, before unpaid relief).
export function basketLimit(f) {
  const b = f.basket ?? {};
  if (b.headroom !== null && b.headroom !== undefined && b.lien !== null && b.lien !== undefined) {
    return Number(b.lien) + Number(b.headroom);
  }
  const k = f.control?.k;
  if (k === null || k === undefined || b.equity === null || b.equity === undefined) return null;
  return k * Number(b.equity) + Number(b.held_buck ?? 0);
}

// What K bounds: the lien less the BUCK the basket still holds, per BUCK of its equity.
const netLienRatio = (f) => {
  const b = f.basket ?? {};
  if (b.lien === null || b.lien === undefined || !b.equity) return null;
  return (Number(b.lien) - Number(b.held_buck ?? 0)) / Number(b.equity);
};
const pct = (v) => (v === null || v === undefined || !Number.isFinite(v) ? null : v * 100);

// Flows are shares, valued at the opening share mark and divided by opening equity.
// The common mark cancels, so the exact ratio is flow shares / opening total shares.
const ownership = (f) => f.basket?.ownership;
const sharePercent = (part, total) => {
  if (part === null || part === undefined || total === null || total === undefined) return null;
  const a = Number(part), b = Number(total);
  return Number.isFinite(a) && Number.isFinite(b) && b > 0 ? 100 * a / b : null;
};
const flowPercent = (f, kind, owner) => {
  const o = ownership(f);
  if (!o || Number(o.opening?.equity_buck) <= 0 || o.opening?.equity_buck == null) return null;
  return sharePercent(o.flows?.[kind]?.[owner], o.opening?.total_shares);
};
const ownerPercent = (f, key) => {
  const o = ownership(f), c = o?.closing;
  return sharePercent(key === "treasury" ? c?.treasury_shares : c?.receipt_shares?.[key], c?.total_shares);
};
const OWNERSHIP_CHARTS = [
  {
    id: "ownership-flows", title: "Gross receipt flows / opening equity", unit: "pct", kind: "line", optional: true,
    note: "Shares minted and retired during each observation window, valued at the opening share mark and divided by opening equity. This equals gross shares divided by opening shares; it is not cash paid. Jubilee and other use authenticated basket events; unattributed is a supply change without a matching receipt event. Older runs have no observation.",
    series: [
      ...["jubilee", "other", "unattributed"].map((owner) => ({ key: `mint-${owner}`, label: `Mint · ${owner}`, value: (f) => flowPercent(f, "minted", owner) })),
      ...["jubilee", "other", "unattributed"].map((owner) => ({ key: `retire-${owner}`, label: `Retire · ${owner}`, value: (f) => flowPercent(f, "retired", owner) })),
    ],
  },
  {
    id: "receipt-ownership", title: "Receipt share ownership", unit: "pct", kind: "line", optional: true,
    note: "Closing shares as a percentage of all shares. Time/investor and household shares are tracked receipt IDs; other is the untracked residual. Treasury shares include redemption cuts and are not receipt ownership. Older runs have no observation.",
    series: [
      ...[["jubilee", "Jubilee"], ["time", "Time / investors"], ["household", "Households"], ["other_untracked", "Untracked receipts"], ["treasury", "Basket treasury"]].map(([key, label]) => ({ key, label, value: (f) => ownerPercent(f, key) })),
    ],
  },
];

// The BuckBasket tab. Leverage against K's cap is the chart behind observation F2: below the cap,
// K does not restrain the basket's issuance.
export const BASKET = (tokens = []) => [
  ...OWNERSHIP_CHARTS,
  {
    id: "limit", title: "Lien against K's limit", unit: "buck", kind: "line",
    note: "The basket may issue up to K times its credit's mark, and the mark is its equity plus the BUCK it holds in its own pool positions over K: so the limit is K x equity + held BUCK. Above it the basket is under water; the wheel trims back only when its liquidity is also short. Records made before 2026-11-14 lack the contract's headroom and are estimated by that formula.",
    series: [
      { key: "lien", label: "Lien", value: (f) => buck(f.basket?.lien) },
      { key: "limit", label: "Limit", value: (f) => buck(basketLimit(f)) },
    ],
  },
  {
    id: "leverage", title: "What K bounds, and K", unit: "ratio", kind: "line", reference: 0,
    note: "K bounds only the lien the basket no longer holds as BUCK: its lien less its held BUCK, per BUCK of equity. It binds when this reaches K. Below zero, the basket holds more BUCK than it owes.",
    series: [
      { key: "net", label: "(Lien - held) / equity", value: netLienRatio },
      { key: "le", label: "Lien / equity", value: (f) => ratioOf(f.basket?.lien, f.basket?.equity) },
      { key: "k", label: "K", value: (f) => num(f.control?.k) },
    ],
  },
  {
    id: "book", title: "Equity, lien and gross", unit: "buck", kind: "line",
    note: "Equity is the depositors' claim; the lien is the BUCK the basket has issued against its holdings; gross is their sum, the holdings' value.",
    series: [
      { key: "equity", label: "Equity", value: (f) => buck(f.basket?.equity) },
      { key: "lien", label: "Lien", value: (f) => buck(f.basket?.lien) },
      { key: "nav", label: "Gross", value: (f) => buck(f.basket?.nav) },
    ],
  },
  {
    id: "share", title: "Share price", unit: "ratio", kind: "line", reference: 1,
    note: "BUCK a share redeems for: the depositors' return from rebalancing, fees and arbitrage.",
    series: [{ key: "share", label: "Share price", value: (f) => num(f.basket?.share_price) }],
  },
  {
    id: "wheel", title: "Wheel trades a day", unit: "buck", kind: "line",
    note: "BUCK the basket's wheel spent buying TOKEN and received selling it, each day.",
    series: [
      { key: "buy", label: "Bought TOKEN for", value: perDay((f) => buck(f.basket?.wheel?.buy)) },
      { key: "sell", label: "Sold TOKEN for", value: perDay((f) => buck(f.basket?.wheel?.sell)) },
    ],
  },
  {
    id: "weights", title: "Held weight against target", unit: "pp", kind: "line", reference: 0,
    note: "Each constituent's held weight less the upkeep's target, in percentage points.",
    series: tokens.map((sym, i) => ({
      key: sym, label: sym,
      value: (f) => {
        const w = f.basket?.weights?.[i];
        return w && w.held_bp !== null && w.target_bp !== null ? (w.held_bp - w.target_bp) / 100 : null;
      },
    })),
  },
  {
    id: "relief", title: "Relief owed and fees carried", unit: "buck", kind: "line",
    note: "Jubilee relief the basket has earned on its lien and not yet been paid, and fees its held BUCK carries.",
    series: [
      { key: "unpaid", label: "Unpaid relief", value: (f) => buck(f.basket?.unpaid_relief) },
      { key: "fee", label: "Carried fee", value: (f) => buck(f.basket?.carried_fee) },
    ],
  },
];

// The Market tab: every pool, and whether the three-pool triangles agree. `split`: the run records
// each TOKEN/BUCK pool's BUCK (since 2026-11-14); older runs show the pools' total.
export const MARKET = (tokens = [], split = true) => [
  {
    id: "buck_usd", title: "BUCK in USD", unit: "ratio", kind: "line",
    note: "The BUCK's dollar price in its USDC pool; from the price book: what a full-value BUCK sold fetches (bid), what a net BUCK bought costs (ask: it arrives hollow from the Carrying pool), and the BUCK's fair value (the basket's dollar value by each TOKEN's best route, per BUCK at parity).",
    series: [
      { key: "buck_usd", label: "BUCK/USD", value: (f) => num(f.market?.buck_usd) },
      { key: "bid", label: "Bid (book)", value: (f) => num(f.market?.book?.buck_bid) },
      { key: "ask", label: "Ask (book)", value: (f) => num(f.market?.book?.buck_ask) },
      { key: "fair", label: "Fair (basket in USDC)", value: (f) => num(f.control?.bvib_usdc) },
    ],
  },
  {
    id: "routes", title: "TOKENs sold best through USDC", unit: "count", kind: "line",
    note: "The price book prices each TOKEN by its best route: into its own pool (at that pool's net / gross) or for USDC and the USDC into the dollar pool (at the dollar pool's). The count whose best route to net BUCK goes through USDC; the rest sell best directly.",
    series: [{ key: "via_usdc", label: "Through USDC",
      value: (f) => (f.market?.book?.bids ? f.market.book.bids.filter((b) => b.via_usdc).length : null) }],
  },
  {
    id: "triangle", title: "Triangle gap by commodity", unit: "pct", kind: "line", reference: 0,
    note: "A TOKEN's price through BUCK (its BUCK pool times BUCK/USD) against its USDC pool. Arbitrage keeps these near zero; a standing gap is flow the arbitrageurs cannot carry.",
    series: tokens.map((sym, i) => ({
      key: sym, label: sym,
      value: (f) => {
        const t = f.market?.tokens?.[i], u = f.market?.buck_usd;
        return t && t.buck && t.usdc && u ? pct((t.buck * u) / t.usdc - 1) : null;
      },
    })),
  },
  {
    id: "pinning", title: "USDC pools against their references", unit: "pct", kind: "line", reference: 0,
    note: "Each TOKEN/USDC pool's price against the scenario's reference: how closely the market maker holds the outside world.",
    series: tokens.map((sym, i) => ({
      key: sym, label: sym,
      value: (f) => {
        const t = f.market?.tokens?.[i];
        return t && t.usdc && t.ref_usd ? pct(t.usdc / t.ref_usd - 1) : null;
      },
    })),
  },
  {
    id: "pools", title: "BUCK in the pools", unit: "buck", kind: "stack",
    note: split ? "Nominal BUCK in the BUCK/USDC pool and in each TOKEN/BUCK pool."
      : "Nominal BUCK in the BUCK/USDC pool and in the TOKEN/BUCK pools together: this run was recorded before each pool was (2026-11-14); re-importing its vector splits them.",
    series: [
      { key: "dollar", label: "BUCK/USDC", value: (f) => buck(f.supply?.holders?.dollar_pool) },
      ...(split ? tokens.map((sym, i) => ({ key: sym, label: `${sym}/BUCK`, value: (f) => buck(f.market?.pools?.[i]?.buck) }))
        : [{ key: "tokens", label: "TOKEN/BUCK pools (together)", value: (f) => buck(f.supply?.holders?.token_pools) }]),
    ],
  },
  {
    id: "carry", title: "The pools' BUCK, net of its carried fee", unit: "ratio", kind: "line", reference: 1,
    note: "Each pool's BUCK is Carrying: its demurrage is carried, not paid, and BUCK leaving the pool delivers only this share of its nominal amount. 1 is fresh; it falls as the pool's BUCK ages and rises with fresh BUCK paid in. BVIB reads each TOKEN/BUCK pool's price at it.",
    series: [
      ...tokens.map((sym, i) => ({ key: sym, label: `${sym}/BUCK`, value: (f) => num(f.market?.pools?.[i]?.net) })),
      { key: "dollar", label: "BUCK/USDC", value: (f) => num(f.market?.dollar_pool_net) },
    ],
  },
  {
    id: "arb", title: "Arbitrage trades a day", unit: "count", kind: "line",
    note: "Trades by the arbitrageurs each day: cycles through BUCK and USDC, direct TOKEN trades, and BUCK/USDC trades.",
    series: [
      { key: "cycles", label: "Cycles", value: perDay((f) => num(f.market?.arb?.cycles)) },
      { key: "direct", label: "Direct", value: perDay((f) => num(f.market?.arb?.direct)) },
      { key: "ub", label: "BUCK/USDC", value: perDay((f) => num(f.market?.arb?.ub)) },
    ],
  },
];

// The Jubilee fund tab.
export const JUBILEE = () => [
  ...OWNERSHIP_CHARTS,
  {
    id: "fund", title: "Jubilee fund", unit: "buck", kind: "line",
    note: "The fund's nominal and net cash, and the mark of its basket position. Demurrage on every balance (2% a year) pays in; relief to issuers (2% of principal a year) pays out.",
    series: [
      { key: "cash", label: "Cash", value: (f) => buck(f.jubilee?.cash) },
      { key: "nominal", label: "Nominal", value: (f) => buck(f.jubilee?.nominal) },
      { key: "position", label: "Position mark", value: (f) => buck(f.jubilee?.position_mark) },
    ],
  },
  {
    id: "relief", title: "Relief available and owed", unit: "buck", kind: "line",
    note: "Relief the fund can pay now, and relief the basket has earned and not yet been paid.",
    series: [
      { key: "available", label: "Available", value: (f) => buck(f.jubilee?.relief_available) },
      { key: "unpaid", label: "Basket's unpaid relief", value: (f) => buck(f.basket?.unpaid_relief) },
    ],
  },
  {
    id: "principal", title: "Principal: open and deferred", unit: "buck", kind: "line",
    note: "Open principal is every BUCK in existence; deferred principal is retired against the fund and not yet settled.",
    series: [
      { key: "active", label: "Open", value: (f) => buck(f.supply?.active_principal) },
      { key: "deferred", label: "Deferred", value: (f) => buck(f.supply?.deferred_principal) },
    ],
  },
];

// Each tab's headline figures for the latest day.
export function tabTiles(id, frames) {
  const f = frames[frames.length - 1];
  if (!f) return [];
  if (id === "basket") {
    const limit = basketLimit(f);
    const recorded = f.basket?.headroom !== null && f.basket?.headroom !== undefined;
    return [
      { id: "equity", label: "Equity", value: buck(f.basket?.equity), unit: "buck", detail: "BUCK" },
      { id: "lien", label: "Lien", value: buck(f.basket?.lien), unit: "buck", detail: "BUCK" },
      { id: "le", label: "Lien / equity", value: ratioOf(f.basket?.lien, f.basket?.equity), unit: "ratio" },
      { id: "headroom", label: "Headroom", value: limit === null ? null : buck(limit - Number(f.basket.lien)), unit: "buck",
        detail: recorded ? "below 0: under water" : "estimated: K x equity + held" },
      { id: "share", label: "Share price", value: num(f.basket?.share_price), unit: "ratio" },
      { id: "idle", label: "Idle BUCK", value: buck(f.basket?.idle_buck), unit: "buck" },
    ];
  }
  if (id === "market") {
    const gaps = (f.market?.tokens ?? []).map((t) => (t.buck && t.usdc && f.market.buck_usd ? Math.abs(t.buck * f.market.buck_usd / t.usdc - 1) : null))
      .filter((v) => v !== null);
    return [
      { id: "buck_usd", label: "BUCK/USD", value: num(f.market?.buck_usd), unit: "ratio" },
      { id: "gap", label: "Widest triangle gap", value: gaps.length ? Math.max(...gaps) * 100 : null, unit: "pct" },
      { id: "dollar", label: "BUCK/USDC pool", value: buck(f.supply?.holders?.dollar_pool), unit: "buck", detail: "BUCK" },
      { id: "tokens", label: "TOKEN/BUCK pools", value: buck(f.supply?.holders?.token_pools), unit: "buck", detail: "BUCK" },
    ];
  }
  if (id === "jubilee") {
    return [
      { id: "cash", label: "Fund cash", value: buck(f.jubilee?.cash), unit: "buck", detail: "BUCK" },
      { id: "available", label: "Relief available", value: buck(f.jubilee?.relief_available), unit: "buck" },
      { id: "unpaid", label: "Basket's unpaid relief", value: buck(f.basket?.unpaid_relief), unit: "buck" },
      { id: "deferred", label: "Deferred principal", value: buck(f.supply?.deferred_principal), unit: "buck" },
    ];
  }
  return [];
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
// refreshed for staleSeconds is stale (its process probably died). A recorder refreshes it
// every 30 s or 10 days; the server reads a dead process on its own machine as "killed".
export function runStatus(manifest, now = Date.now(), staleSeconds = 600) {
  const status = manifest?.status ?? "unknown";
  if (status !== "running") return status;
  const updated = Date.parse(manifest.updated ?? manifest.started ?? "");
  return Number.isFinite(updated) && now - updated > staleSeconds * 1000 ? "stale" : "running";
}

export function format(value, unit) {
  if (value === null || value === undefined || Number.isNaN(value)) return "n/a";
  if (unit === "ratio") return value.toFixed(4);
  if (unit === "count") return Math.round(value).toLocaleString("en-US");
  if (unit === "pct") return `${value.toFixed(2)}%`;
  if (unit === "pp") return `${value >= 0 ? "+" : ""}${value.toFixed(2)} pp`;
  if (unit === "usd") return `${value < 0 ? "-" : ""}$${format(Math.abs(value), "buck")}`;
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
