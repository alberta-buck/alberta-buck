// The Savings tab's arithmetic, apart from the page so node can test it:
// where the world is, what a frame says, and what a receipt is worth.
//
// Units, as the server sends them (alberta_buck/sim/snapshot.py): prices in
// micro-USD (refUsd, spotUsdc, buck_usd) or micro-BUCK (spotBuck) per whole
// TOKEN; basketVal and buckK 1e18 fixed point (strings past 2^53); BUCK
// amounts 6-decimal base units; the savings block sv (server.py _savings)
// O / S / P / B / T in BUCK base units, D a plain ratio.

/** The world's base URL: ?sim= wins, then the one this browser was told to
 *  use (World settings), then the page's own configuration (sim-server.json:
 *  "same-origin" when the sim server serves the page, or a server's URL),
 *  then a local server. */
export function serverBase({ param, configured, saved, location }) {
  const fromLoc = () => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
  const pick = param || saved || configured || "ws://127.0.0.1:8797";
  return pick === "same-origin" ? fromLoc() : pick.replace(/\/+$/, "");
}

/** The three channels of one world.  `sets` are applied only when the world
 *  is built (the first connection to a new session id). */
export function channels(base, sid, sets = {}) {
  const q = Object.entries(sets).filter(([, v]) => v !== undefined && v !== "")
    .map(([k, v]) => `set=${encodeURIComponent(`${k}=${v}`)}`);
  const at = `${base}/s/${encodeURIComponent(sid)}`;
  return {
    frames: `${at}/frames?${["lite=1", "replay=1", ...q].join("&")}`,
    control: `${at}/control`,
    rpc: `${at}/rpc`,
  };
}

/** A fresh, unguessable session id: whoever holds it can drive the world. */
export function newSid(rand = (n) => crypto.getRandomValues(new Uint8Array(n))) {
  return [...rand(12)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const big = (v) => (v === null || v === undefined ? 0n : BigInt(v));
const e18 = (v) => Number(big(v)) / 1e18;

/** One frame, cut down to what the tab draws (a few dozen numbers). */
export function row(f) {
  const sv = f.sv ?? {};
  return {
    day: f.day,
    ref: f.refUsd ?? [], su: f.spotUsdc ?? [], sb: f.spotBuck ?? [],
    weights: f.poolWeights ?? [],
    bu: (f.buck_usd ?? 0) / 1e6,                   // USD per BUCK, the pool
    bv: e18(f.basketVal),                          // the basket, in BUCK
    k: e18(f.buckK),
    supply: Number(big(f.supply)) / 1e6,
    D: sv.D ?? null, O: sv.O ?? null, S: sv.S ?? null, P: sv.P ?? null, B: sv.B ?? null,
    T: sv.T ?? null,
    credited: f.wh_sol_credited_usd ?? 0,          // the wheel's harvest to depositors
    callers: f.wh_sol_share_usd ?? 0,
    gasUsd: f.wh_sol_gas_usd ?? 0,
    cycles: f.wh_sol_cycles ?? 0,
    utIssued: Number(f.ut_issued_open ?? 0) / 1e6,
    utAbsorbed: Number(f.ut_absorbed_open ?? 0) / 1e6,
    shockBought: f.shk_bought_usd ?? 0,
    shockSold: f.shk_sold_usd ?? 0,
    shockActive: f.shk_active ?? 0,
    pace: f.pace ?? 0,
    paused: !!f.paused,
  };
}

/** A constituent's three prices, USD per whole TOKEN: the real commodity
 *  (the reference), the TOKEN/USDC pool, and TOKEN/BUCK times BUCK/USDC. */
export function prices(r, i) {
  return { ref: (r.ref[i] ?? 0) / 1e6, usdc: (r.su[i] ?? 0) / 1e6,
           viaBuck: ((r.sb[i] ?? 0) / 1e6) * r.bu };
}

/** Where a deposit helps most: the director's hint, else the most
 *  underweight pool (target less actual value weight). */
export function hintIndex(hint, weights) {
  const n = weights.length;
  if (hint !== undefined && hint !== null && BigInt(hint) < BigInt(n)) return Number(hint);
  let best = 0;
  let gap = -Infinity;
  weights.forEach(([actual, target], i) => {
    if (target - actual > gap) { gap = target - actual; best = i; }
  });
  return best;
}

/** How far a pool's price is from its recent average, in basis points --
 *  what the deposit guard measures (spot against the TWAP).  From ticks, so
 *  for either token order: the larger of the two readings. */
export function deviationBp(tick, twapTick) {
  if (tick === null || tick === undefined || twapTick === null || twapTick === undefined) return 0;
  const d = Number(tick) - Number(twapTick);
  return Math.max(1.0001 ** d - 1, 1 - 1.0001 ** -d, 1.0001 ** -d - 1, 1 - 1.0001 ** d) * 10_000;
}

/** Where a deposit helps most and is accepted: the director's hint when its
 *  pool is within the guard, else the most underweight pool within it, else
 *  the calmest pool. */
export function chooseIndex(hint, weights, devBp, guardBp) {
  const ok = (i) => (devBp?.[i] ?? 0) <= guardBp;
  if (hint !== undefined && hint !== null && BigInt(hint) < BigInt(weights.length) && ok(Number(hint))) {
    return Number(hint);
  }
  let best = -1;
  let gap = -Infinity;
  weights.forEach(([actual, target], i) => {
    if (ok(i) && target - actual > gap) { gap = target - actual; best = i; }
  });
  if (best >= 0) return best;
  best = 0;
  (devBp ?? []).forEach((d, i) => { if (d < devBp[best]) best = i; });
  return best;
}

/** TOKEN base units for `usd` dollars at `priceUsd` per whole TOKEN. */
export function tokenFor(usd, priceUsd, decimals) {
  if (!(priceUsd > 0)) throw new Error("no price for that token yet");
  const micro = BigInt(Math.round(usd * 1e6));
  const p = BigInt(Math.round(priceUsd * 1e6));
  return (micro * 10n ** BigInt(decimals)) / p;
}

/** A receipt's worth at a frame's book (sv), in BUCK base units.  Its
 *  burn obligation Rb is its principal R plus its slice of the credited
 *  bonus S; its claim V = Rb * 2B / O; a redemption pays the TOKEN side:
 *  the half of V when B >= O (the BUCK surplus goes to the treasury), V
 *  less the burn when B < O.  Mirrors BuckBasketProRata._redeem. */
export function receiptWorth(buckPrincipal, sv) {
  const R = big(buckPrincipal);
  const O = big(sv?.O);
  const S = big(sv?.S);
  if (sv?.B === null || sv?.B === undefined || O === 0n || R === 0n) return null;
  const B = big(sv.B);
  const slice = S === 0n ? 0n : (R * S) / (O - S);
  const Rb = R + slice;
  const V = (Rb * 2n * B) / O;
  const half = V / 2n;
  const net = V - Rb;
  return { burn: Rb, claim: V, paid: half < net ? half : net };
}

/** Days since the world began as a date, when the world says when it began. */
export function dateOf(startDate, day) {
  if (!startDate) return "";
  const t = Date.parse(`${startDate}T00:00:00Z`);
  return Number.isNaN(t) ? "" : new Date(t + day * 86_400_000).toISOString().slice(0, 10);
}

/** 12,345 / 1.23M / 0.0123: a compact number for a chart label. */
export function compact(v, digits = 3) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1e9) return `${(v / 1e9).toPrecision(digits)}B`;
  if (a >= 1e6) return `${(v / 1e6).toPrecision(digits)}M`;
  if (a >= 1e4) return `${(v / 1e3).toPrecision(digits)}k`;
  if (a >= 1) return v.toFixed(a >= 100 ? 0 : 2);
  return v.toPrecision(digits);
}

/** USD, to the cent. */
export const usd = (v) => (v === null || v === undefined ? "–"
  : `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
