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
        if (s.side === "buy") {
          await world.fiatIn(this.account, amount, { tag: "shock:usdc" });
          await world.buyBuck(amount, this.account, { tag: `shock:buy:d${day}` });
          this.boughtUsd += s.perDay;
        } else {
          // BUCK at about a dollar: a credit line a little larger than the sale, drawn once.
          const held = await world.session.call(world.buck, "balanceOf", [world.holderAddress(this.account)]);
          if (held < amount) await world.pledge(this.account, (amount - held) * 2n, { tag: `shock:pledge:d${day}` });
          await world.sellBuck(amount, this.account, { tag: `shock:sell:d${day}` });
          this.soldUsd += s.perDay;
        }
      } catch { /* a refused leg leaves the pressure for the next day's turn */ }
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
   * @param deps.sets       the page's settings: agents.HouseholdPopulationAgent.<key>, scenario.seed
   */
  constructor({ session, identity, artifacts, urArtifact, sets = {}, rng }) {
    Object.assign(this, { session, identity, artifacts, urArtifact, rng });
    this.day = 0;
    this.seed = Number(sets["scenario.seed"] ?? 41404) || 41404;
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

  async boot() {
    this.actAs = installActAs(this.session);
    const w = this.world = await buildEquilibriumWorld(this.session, this.artifacts, {
      identity: this.identity, urArtifact: this.urArtifact, rng: this.rng, basket: "machine",
      feedSeed: this.seed });
    const arb = new ParityArb();
    this.shock = new ShockAgent();
    let n = 0;
    this.pop = new HouseholdPopulationAgent({ makeAccount: () => privateKeyToAccount(generatePrivateKey()),
      seed: this.seed, days: 3650, settings: this.settings, ...this.population });
    this.cast = [
      new DayClock(),
      ...w.tokens.map((t, i) => new PinWhale({ simlp: w.simlp, pool: t.poolUsdc, token: t.erc20.address,
        tokenDec: t.dec, quote: w.usdc.address, refPrice: (d) => w.feeds[i][d % w.feeds[i].length] })),
      arb,
      new WheelKeeper({ arb }),
      new PidKeeper(),
      this.shock,
      this.pop,
    ];
    for (const a of this.cast) if (a.setup) await a.setup(w);
    n = this.cast.length;
    this.frames = [];
    return n;
  }

  /** One simulated day: every agent in cast order, then the households' marks and the frame. */
  async step() {
    const w = this.world, d = this.day;
    for (const a of this.cast) await a.act(w, d, 0);
    for (const m of this.pop.members) {
      try { await m.record(w, d); } catch { /* an unreadable mark keeps the last row */ }
    }
    const frame = await this.frame(d);
    this.frames.push(frame);
    this.day += 1;
    return frame;
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
    };
    try { frame.eq_signal_raw = String(await s.call(w.machine.parity, "rawValue")); } catch { /* no sample yet */ }
    try {
      frame.eq_w = (await s.call(b, "responseWeightsBp")).map(Number);
      frame.eq_lean = await this.targetsBp();
    } catch { /* the weights read before the first pairing */ }
    frame.sv = await this.savings();
    Object.assign(frame, this.households());
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
      basket_kind: "equity", machine: true, tokens, prices: ["walk"], start_date: null,
      households: { ...this.population, ...this.settings }, local: true,
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
