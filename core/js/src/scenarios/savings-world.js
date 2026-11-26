// The savings sandbox's world, run in the visitor's tab (doc/review/bucknet-economy-implementation.org,
// "The savings world in the tab"): eqworld's basket machine with economy.toml's cast -- the whales
// pinning the TOKEN/USDC pools to the reference walks, the parity arbitrageur, the wheel and PID
// keepers, a shock agent and the household population -- run a day at a time. Each day's frame has
// the fields the savings page reads from the Python server's lite frames (alberta_buck/sim/server.py,
// snapshot.py; core/js/sandbox/src/savings/model.js row()), so the page draws either world alike.
//
// One class for Node and the browser: the caller supplies the session, the identity kernel, the
// artifacts and the Universal Router's artifact. The session must be webevm with senderMode
// "trusted": the world enrolls a visitor as the Python server does, acting as the visitor's address
// (evm_sendRawTransactionAs) to register its identity and approve the basket; the visitor's own
// transactions arrive signed (eth_sendRawTransaction) and are recovered as usual.

import { encodeFunctionData } from "viem";
import { generatePrivateKey, privateKeyToAccount, toAccount } from "viem/accounts";

import { DAY, fundAccount, onboard } from "../buckworld.js";
import { buildEquilibriumWorld, DayClock, PidKeeper, WheelKeeper } from "./eqworld.js";
import { PinWhale } from "../agents/whale.js";
import { ParityArb } from "../agents/arb.js";
import { HouseholdPopulationAgent } from "../agents/credit-household.js";
import { SAVINGS_BORROWERS } from "./savings-presets.js";
import { savingsPriceTape } from "./savings-prices.js";
import { rebalanceFills } from "./savings-rebalances.js";

const E6 = 10n ** 6n;
const E18 = 10n ** 18n;

/** The savings world's node (webevmSession's nodeOptions): the world acts as a visitor it enrolls
 *  (trusted sends), and gas is free, as on the Python server's pyrevm chain: the page's wallet signs
 *  legacy transactions at gas price 0 and holds no ETH. */
export const SAVINGS_NODE_OPTIONS = { senderMode: "trusted", baseFeePerGas: 0n, gasPrice: 0n,
                                      maxPriorityFeePerGas: 0n };

// economy.toml's population ([agents.HouseholdPopulationAgent]), scaled down by default: a tab
// carries tens of households a day at an interactive pace (plan step S5 measures it). The page's
// own settings (agents.HouseholdPopulationAgent.<key>) override these.
export const POPULATION = {
  initial_debtors: 2, initial_savers: 2, initial_investors: 6,
  debtor_rate: 2.0, saver_rate: 2.0, investor_rate: 6.0,
  growth: "steady", gain: 200.0, gain_cap: 8.0, k_ref: 0.75, signal_halflife_days: 30.0,
};
export const HOUSEHOLD_SETTINGS = {
  surplus_policy: "adaptive", divest_rate: 0.1, money_months: 1.0, adopt_start: 0.1, adopt_years: 1.0,
  carry_utilization: 0.3,
};
const POPULATION_KEYS = new Set([...Object.keys(POPULATION), "max_members", "settle_days"]);
/** Ticks a simulated day (scenario.ticks_per_day): the whales re-pin each TOKEN/USDC pool to its
 *  reference price at every tick and the arbitrageur and the wheel act every tick, so the pools
 *  track their commodities through the day (economy.toml runs 2; a tab's default is 4). */
export const TICKS_PER_DAY = 4;

/** A reference walk's price at day d and tick k of `ticks`: geometric between its days. */
function priceAt(feed, d, k, ticks) {
  const index = d % feed.length;
  const p0 = feed[index];
  if (!k) return p0;
  const p1 = feed[Math.min(index + 1, feed.length - 1)];
  return BigInt(Math.round(Number(p0) * (Number(p1) / Number(p0)) ** (k / ticks)));
}

// The household aggregate's money fields: the Python snapshot's (_households) names, from each
// member's latest ledger row (credit-household.js record()).
const MONEY = {
  nw: "nw", hypo: "hypo", mortgage: "mortgageOwing", hypo_mortgage: "hypoOwing", cash: "banked",
  hypo_cash: "hypoBanked", drawn: "drawn", basket: "basket", basket_mark: "basketMark",
  basket_at_risk: "basketAtRisk", pending_usd: "pending", invested_usd: "investedUsd",
  issued_invest: "issuedInvest", unwound: "unwound", external_interest: "externalInterest",
  hypo_interest: "hypoInterest", external_paid: "externalPaid", hypo_paid: "hypoPaid",
  arrears: "arrears", hypo_arrears: "hypoArrears",
};
const big = (v) => (v === null || v === undefined ? 0n : BigInt(v));

/** A shock: `usd` of buying or selling pressure on the BUCK, over `days`, from the next day. */
export class ShockAgent {
  constructor() {
    this.account = privateKeyToAccount(generatePrivateKey());
    this.shocks = [];
    this.boughtUsd = 0;
    this.soldUsd = 0;
  }
  async setup(world) { await fundAccount(world, this.account.address); }
  arm(side, usd, days = 1) {
    const n = Math.max(1, Math.floor(Number(days) || 1));
    this.shocks.push({ side: side === "buy" ? "buy" : "sell", perDay: Number(usd) / n, daysLeft: n });
  }
  get active() { return this.shocks.filter((s) => s.daysLeft > 0).length; }
  async act(world, day, tick) {
    if (tick !== 0) return;
    for (const s of this.shocks) {
      if (s.daysLeft <= 0) continue;
      s.daysLeft -= 1;
      const amount = BigInt(Math.round(s.perDay * 1e6));
      try {
        const holder = await world.proxyFor(this.account);
        if (s.side === "buy") {
          await world.fiatIn(this.account, amount, { tag: "shock:usdc" });
          const received = await world.buyBuck(amount, this.account, { tag: `shock:buy:d${day}` });
          if (received > 0n) this.boughtUsd += Number(amount) / 1e6;
        } else {
          const held = await world.session.call(world.buck, "balanceOf", [holder.address]);
          if (held < amount) {
            const spendable = (amount - held) * 105n / 100n + 1n;
            const k = await world.K();
            // K scales the credit face. Allow headroom for a refreshed K at mint time.
            const face = spendable * 2n * E18 / k;
            await world.pledge(this.account, face, { spendable, tag: `shock:pledge:d${day}` });
          }
          const received = await world.sellBuck(amount, this.account, { tag: `shock:sell:d${day}` });
          if (received > 0n) this.soldUsd += Number(received) / 1e6;
        }
      } catch { /* a refused leg records no volume for this day */ }
    }
  }
}

/**
 * Accounts the world acts as without their keys: a raw transaction signed for one of them (with a
 * throwaway key) is sent with evm_sendRawTransactionAs, as that address. Installed on the session's
 * node once; every other request passes through.
 */
export function installActAs(session) {
  const node = session.webevm;
  if (!node) throw new Error("the savings world needs a webevm session");
  if (node.actAs) return node.actAs;
  const request = node.request.bind(node);
  const pending = new Map();                 // raw tx -> the address it is sent as
  node.request = (args) => {
    if (args?.method === "eth_sendRawTransaction" && pending.has(args.params?.[0])) {
      const from = pending.get(args.params[0]);
      pending.delete(args.params[0]);
      return request({ method: "evm_sendRawTransactionAs", params: [args.params[0], from] });
    }
    return request(args);
  };
  const signer = privateKeyToAccount(generatePrivateKey());
  node.actAs = (address) => toAccount({
    address,
    async signTransaction(tx, opts) {
      const raw = await signer.signTransaction(tx, opts);
      pending.set(raw, address);
      return raw;
    },
    async signMessage() { throw new Error("the world signs no messages as a visitor"); },
    async signTypedData() { throw new Error("the world signs no typed data as a visitor"); },
  });
  return node.actAs;
}

export class SavingsWorld {
  /**
   * @param deps.session    a webevm Session (senderMode "trusted")
   * @param deps.identity   the buck-identity kernel API
   * @param deps.artifacts  (name) => {abi, bytecode}
   * @param deps.urArtifact the UniversalRouter artifact
   * @param deps.sets       the page's settings: agents.HouseholdPopulationAgent.<key>,
   *                        scenario.seed, scenario.ticks_per_day, scenario.prices, scenario.borrower
   * @param deps.days      population scheduling horizon, 3650 for an interactive world
   * @param deps.bootstrapDeposits ordered TOKEN endowments supplied by a paired replay
   * @param deps.prices     the economy's TOKENs and price paths (alberta_buck.sim.savings_prices:
   *                        {tokens, series: {history, revert}, start}); without them, eqworld's
   *                        own TOKENs on seeded walks
   */
  constructor({ session, identity, artifacts, urArtifact, sets = {}, rng, prices = null, days = 3650, bootstrapDeposits }) {
    Object.assign(this, { session, identity, artifacts, urArtifact, rng, prices, days, bootstrapDeposits });
    this.priceMode = sets["scenario.prices"] ?? (prices ? "history" : "walk");
    if ((prices && this.priceMode !== "sine" && !Object.hasOwn(prices.series, this.priceMode))
        || (!prices && this.priceMode !== "walk")) {
      throw new Error(`Price tape unavailable: ${this.priceMode}`);
    }
    const borrower = sets["scenario.borrower"] ?? "";
    if (borrower && !Object.hasOwn(SAVINGS_BORROWERS, borrower)) throw new Error(`Unknown borrower preset: ${borrower}`);
    this.borrower = borrower;
    this.day = 0;
    this.seed = Number(sets["scenario.seed"] ?? 41404) || 41404;
    this.priceTape = prices ? savingsPriceTape(prices, this.priceMode, sets, this.seed) : null;
    this.priceDays = this.priceTape?.[0].length ?? null;
    this.startDate = ["three-phase", "sine"].includes(this.priceMode) ? null : prices?.start ?? null;
    this.priceChangeDay = null;
    const ticks = Math.floor(Number(sets["scenario.ticks_per_day"] ?? TICKS_PER_DAY));
    this.ticksPerDay = Number.isSafeInteger(ticks) && ticks >= 1 && ticks <= 48 ? ticks : TICKS_PER_DAY;
    this.population = { ...POPULATION };
    this.settings = { ...HOUSEHOLD_SETTINGS };
    for (const [key, value] of Object.entries(sets)) {
      const m = key.match(/^agents\.HouseholdPopulationAgent\.(\w+)$/);
      if (!m || value === undefined || value === "") continue;
      const v = Number.isFinite(Number(value)) ? Number(value) : String(value);
      if (POPULATION_KEYS.has(m[1])) this.population[m[1]] = v;
      else this.settings[m[1]] = v;
    }
    this.enrolled = new Map();
  }

  async boot({ beforeFirstDeposit, deployment, firstTickTimestamp } = {}) {
    this.actAs = installActAs(this.session);
    let given = {};
    if (this.prices) {
      const feeds = this.priceTape.map((row) => row.map((p) => BigInt(p)));
      given = { feeds, tokens: this.prices.tokens.map((t, i) => ({ ...t, p0: feeds[i][0] })) };
    }
    const w = this.world = await buildEquilibriumWorld(this.session, this.artifacts, {
      identity: this.identity, urArtifact: this.urArtifact, rng: this.rng, basket: "machine",
      feedSeed: this.seed, bootstrapDeposits: this.bootstrapDeposits, beforeFirstDeposit,
      turnSeconds: Math.floor(DAY / this.ticksPerDay), withDesk: true, ...deployment, ...given });
    if (firstTickTimestamp !== undefined) w.householdEpoch = firstTickTimestamp;
    const T = this.ticksPerDay;
    const arb = new ParityArb({ everyTick: true });
    this.shock = new ShockAgent();
    this.pop = new HouseholdPopulationAgent({ makeAccount: () => privateKeyToAccount(generatePrivateKey()),
      seed: this.seed, days: this.days, settings: this.settings, ...this.population,
      openingDebtor: this.borrower ? { ...SAVINGS_BORROWERS[this.borrower].settings, pay_day: 1, divest_rate: 0 } : null });
    this.cast = [
      new DayClock({ ticksPerDay: T }),
      ...w.tokens.map((t, i) => new PinWhale({ simlp: w.simlp, pool: t.poolUsdc, token: t.erc20.address,
        tokenDec: t.dec, quote: w.usdc.address, everyTick: true,
        refPrice: (d, k = 0) => priceAt(w.feeds[i], d, k, T) })),
      arb,
      // The wheel turned to completion every tick, after the whales' forcing: the arbitrage it opened
      // and the basket's responses are taken before the tick ends.
      this.keeper = new WheelKeeper({ arb, complete: true }),
      new PidKeeper(),
      this.shock,
      this.pop,
    ];
    for (const a of this.cast) if (a.setup) await a.setup(w);
    this.frames = [];
    this.holdBasis = null;
    return this.cast.length;
  }

  /** One simulated day: its ticks, every agent in cast order at each (the households decide and
   *  record their marks at the day's first), then the frame. */
  async step(onTick) {
    const w = this.world, d = this.day;
    const fills = [];
    for (let k = 0; k < this.ticksPerDay; k++) {
      for (const a of this.cast) await a.act(w, d, k);
      fills.push(...rebalanceFills(w, d, k));
      onTick?.(d + k / this.ticksPerDay);
    }
    const frame = await this.frame(d);
    frame.rebalance_fills = fills;
    this.frames.push(frame);
    this.day += 1;
    return frame;
  }

  /** Replace future returns at a day boundary. The first unwritten close was
   *  already the endpoint of the previous day's interpolation, so keep it. */
  changePriceRegime(sets) {
    const anchor = this.day;
    if (!this.priceTape || anchor + 1 >= this.priceDays) throw new Error("There are no remaining prices to replace");
    const mode = sets?.["scenario.prices"] ?? "history";
    const candidate = savingsPriceTape(this.prices, mode, sets ?? {}, this.seed, anchor + 1);
    if (candidate[0].length !== this.priceDays) throw new Error("The new regime must cover the remaining price horizon");
    const tape = this.priceTape.map((feed, i) => {
      const next = candidate[i];
      const scale = feed[anchor] / next[anchor];
      return feed.slice(0, anchor + 1).concat(next.slice(anchor + 1, this.priceDays)
        .map((price) => Math.max(1, Math.round(price * scale))));
    });
    // The world's market helpers retain the feed array from deployment.
    this.world.feeds.splice(0, tape.length, ...tape.map((feed) => feed.map((price) => BigInt(price))));
    this.priceTape = tape;
    this.priceMode = mode;
    this.priceChangeDay = anchor + 1;
  }

  /** The day's frame, in the snapshot's units (alberta_buck/sim/snapshot.py). */
  async frame(day) {
    const w = this.world, s = this.session, b = w.basket;
    const n = w.tokens.length;
    const [K, bvib, spotUB, supply] = await Promise.all([w.K(), w.bvib(), w.spotUB(),
      s.call(w.buck, "totalSupply")]);
    const spotUsdc = await Promise.all(w.tokens.map((_, i) => w.spotUsd(i)));
    const spotBuck = await Promise.all(w.tokens.map((_, i) => w.spotBuck(i)));
    const refUsd = w.tokens.map((_, i) => Number(w.feeds[i][day % w.feeds[i].length]));
    const frame = {
      day, buckK: String(K), basketVal: String(bvib), buck_usd: Number(spotUB),
      supply: String(supply), refUsd, spotUsdc: spotUsdc.map(Number), spotBuck: spotBuck.map(Number),
      shk_bought_usd: this.shock.boughtUsd, shk_sold_usd: this.shock.soldUsd, shk_active: this.shock.active,
      wh_turns: this.keeper.turns, wh_work: Number(this.keeper.work), wh_settled: this.keeper.settled,
    };
    try { frame.eq_signal_raw = String(await s.call(w.machine.parity, "rawValue")); } catch { /* no sample yet */ }
    try {
      frame.eq_w = (await s.call(b, "responseWeightsBp")).map(Number);
      frame.eq_lean = await this.targetsBp();
    } catch { /* the weights read before the first pairing */ }
    const marks = await Promise.all(w.tokens.map(async (_, i) => {
      const liquidity = await s.call(b, "liquidityOf", [BigInt(i)]);
      return s.call(b, "marks", [BigInt(i), liquidity]);
    }));
    const prices = marks.map((mark) => Number(mark.pLow));
    frame.eq_hold = null;
    if (prices.length && prices.every((price) => price > 0)) {
      if (!this.holdBasis) {
        const constituents = await Promise.all(w.tokens.map((_, i) => s.call(b, "constituents", [BigInt(i)])));
        const weights = constituents.map((c) => Number(c[9]));
        this.holdBasis = { prices, weights, total: weights.reduce((sum, w) => sum + w, 0) };
      }
      const basis = this.holdBasis;
      frame.eq_hold = prices.reduce((sum, price, i) => sum + basis.weights[i] * price / basis.prices[i], 0)
        / (basis.total || 1);
    }
    if (await s.call(b, "portfolioLimits")) {
      const [gross, lien, equity, relief, cash] = await Promise.all([
        s.call(b, "grossAt", [2]), s.call(b, "lien"), s.call(b, "equity"),
        s.call(b, "reliefAccrued"), s.call(w.buck, "netBalanceOf", [b.address]),
      ]);
      const held = marks.reduce((sum, mark) => sum + mark.posLow / 2n, 0n);
      const exposure = gross - held;
      const effectiveK = (K > E18 ? E18 : K) * 9n / 10n;
      frame.eq_token_exposure = String(exposure);
      frame.eq_net_issued = String(lien - held - cash);
      frame.eq_hard_limit = String(exposure * K / E18);
      frame.eq_ceiling = String((equity > relief ? equity - relief : 0n) * effectiveK / (E18 - effectiveK));
    }
    frame.sv = await this.savings();
    for (const [key, value] of Object.entries(this.keeper.responseCounters)) {
      frame[key] = typeof value === "bigint" ? String(value) : value;
    }
    Object.assign(frame, this.households());
    const featured = this.pop.featured;
    const ledger = featured?.ledger.at(-1);
    if (ledger) frame.borrower = {
      // The bank book can confirm a calendar payment on a day when the
      // dispatcher skips its full on-chain household mark.
      mortgage: String(featured.mortgage), baseline: String(featured.hypoMortgage),
      buck_principal: String(ledger.drawn), mortgage_paid: String(featured.externalPaid),
      error: featured.lastError, stale: ledger.day !== day,
    };
    return frame;
  }

  /** The weights the upkeep facet steers to (basket_machine.targets_bp): each declared weight
   *  leaned against its TOKEN's price over its reference price, shared over the book, within the
   *  lean cap. */
  async targetsBp() {
    const s = this.session, b = this.world.basket, n = this.world.tokens.length;
    const [, , tilt, cap] = await s.call(b, "response");
    const held = await Promise.all([...Array(n).keys()].map((i) => s.call(b, "constituents", [BigInt(i)])));
    const declared = held.map((c) => Number(c[9]));
    if (!Number(tilt)) return declared;
    const leaned = [];
    for (let i = 0; i < n; i++) {
      const price = Number((await s.call(b, "marks", [BigInt(i), 0n])).pTwap);
      const anchor = Number(await s.call(b, "referencePrice", [BigInt(i)])) || Number(held[i][3]);
      leaned.push(price && anchor ? declared[i] * (anchor / price) ** (Number(tilt) / 10000) : declared[i]);
    }
    const total = leaned.reduce((a, v) => a + v, 0), sum = declared.reduce((a, v) => a + v, 0);
    const c = Number(cap) / 10000;
    return leaned.map((w) => Math.round(Math.min(w * (1 + c), Math.max(w * (1 - c), sum * w / total))));
  }

  /** The equity basket's savings block (server.py _savings_equity). */
  async savings() {
    const s = this.session, b = this.world.basket;
    try {
      const [N, NT, E, sp, L, relief, Q, H, lam, k] = await Promise.all([
        s.call(b, "totalShares"), s.call(b, "treasuryShares"), s.call(b, "equity"), s.call(b, "sharePrice"),
        s.call(b, "lien"), s.call(b, "reliefAccrued"), s.call(b, "liquidity"), s.call(b, "headroom"),
        s.call(b, "LAMBDA_BP"), s.call(this.world.kctrl, "buckK")]);
      let fee = 0n;
      for (let i = 0; i < this.world.tokens.length; i++) {
        const f = BigInt((await s.call(b, "constituents", [BigInt(i)]))[4]);
        if (f > fee) fee = f;
      }
      return { kind: "equity", N: String(N), NT: String(NT), E: String(E), sp: String(sp), L: String(L),
               relief: String(relief), Q: String(Q), H: String(H), lam: Number(lam),
               chg: String(fee * 10n ** 12n * (E18 + k) / (2n * E18)), D: Number(sp) / 1e18 };
    } catch {
      return null;
    }
  }

  /** The households' aggregate and open positions (snapshot.py _households), from each member's
   *  latest ledger row. */
  households() {
    const members = this.pop.members;
    if (!members.length) return { households: null, household_positions: [] };
    const out = Object.fromEntries(Object.keys(MONEY).map((k) => [k, 0n]));
    let active = 0, exited = 0, errors = 0;
    const positions = [];
    for (const m of members) {
      const r = m.ledger?.at(-1);
      if (!r) continue;
      active += 1;
      if (m.exited) exited += 1;
      if (m.lastError) errors += 1;
      for (const [k, field] of Object.entries(MONEY)) out[k] += big(r[field]);
      if (big(r.basketAtRisk) || r.receipts) {
        positions.push({ member: m.member, role: m.p.role, basket_at_risk: String(big(r.basketAtRisk)),
                         basket_mark: String(big(r.basketMark)), basket_roi: r.basketRoi ?? null });
      }
    }
    const aggregate = Object.fromEntries(Object.entries(out).map(([k, v]) => [k, String(v)]));
    const roles = (role) => members.filter((m) => m.p.role === role && m.ledger?.length).length;
    Object.assign(aggregate, { active, exited, errors, scheduled: members.length,
      savers: roles("saver"), investors: roles("investor"), debtors: roles("debtor") });
    return { households: aggregate, household_positions: positions };
  }

  /** A JSON-RPC batch from the page ([{jsonrpc, id, method, params}]), in order: sim_info and
   *  sim_enroll from the world, every other method from its node. The host serializes batches
   *  against the days (server.py's chain lock), so a visitor's transaction lands between days. */
  async rpc(reqs) {
    const out = [];
    for (const r of reqs) {
      try {
        const result = r.method === "sim_info" ? await this.info()
          : r.method === "sim_enroll" ? await this.enroll(r.params?.[0])
          : r.method === "sim_price_tape" ? this.priceTape
          : await this.session.webevm.request({ method: r.method, params: r.params ?? [] });
        out.push({ jsonrpc: "2.0", id: r.id, result });
      } catch (e) {
        out.push({ jsonrpc: "2.0", id: r.id,
                   error: { code: e?.code ?? -32000, message: e?.message ?? String(e), data: e?.data } });
      }
    }
    return out;
  }

  /** sim_info: the world's addresses and constituents (server.py info()). */
  async info() {
    const w = this.world, s = this.session;
    const tokens = await Promise.all(w.tokens.map(async (t) => ({
      symbol: t.sym, address: t.erc20.address, decimals: t.dec,
      pool_usdc: t.poolUsdc.address, pool_buck: t.poolBuck.address })));
    let receipt = null;
    try { receipt = await s.call(w.basket, "receipt"); } catch { /* none */ }
    return {
      chain_id: await s.client.getChainId(), buck: w.buck.address, usdc: w.usdc.address,
      basket: w.basket.address, receipt, kctrl: w.kctrl.address, pool_ub: w.poolUB.address,
      router: w.router?.address ?? null, wheel: w.wheel?.address ?? null, director: null,
      basket_kind: "equity", machine: true, tokens, prices: [this.priceMode],
      start_date: this.startDate,
      borrower: this.borrower,
      households: { ...this.population, ...this.settings }, local: true, ticks_per_day: this.ticksPerDay,
    };
  }

  /** sim_enroll: register `address` with the world's issuer, as the world's own depositors are, and
   *  lay down its identity-bound approval of the basket (which pays exits in BUCK). Idempotent. */
  async enroll(address) {
    const key = String(address).toLowerCase();
    if (this.enrolled.has(key)) return { address, registered: true, approved: true };
    const account = this.actAs(address);
    await fundAccount(this.world, address);
    await onboard(this.world, account, {
      given_name: "Visitor", family_name: `Saver-${this.enrolled.size + 1}`,
      id_type: "Alberta Identity Card", id_number: `AIC-2026-VIS-${this.enrolled.size + 1}`,
      jurisdiction: "Alberta, Canada", date_of_birth: "1990-01-01",
      issued_at: "2026-07-04T00:00:00Z", epoch: 42,
    }, { rng: this.rng });
    await this.world.approveBasket(account);
    this.enrolled.set(key, account);
    return { address, registered: true, approved: true };
  }
}

export { DAY, E6 };
