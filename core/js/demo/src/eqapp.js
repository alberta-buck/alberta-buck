// The eqworld demo CONTROLLER -- the minimal two-agent equilibrium,
// interactive: boot the world on the in-browser EVM, tick simulated
// days, ADD savers and debtors mid-run, and feed the page's tabbed
// instrument panels -- overview, pools, controller (K + PID), basket,
// per-saver and per-debtor detail -- from world.series, live chain
// reads, and the agents' own ledgers.
//
// All the logic, none of the DOM: eqmain.js (the page) constructs it
// with browser-loaded dependencies; the node gate (eqapp.tevm.test.js)
// drives the SAME class with node-loaded ones.

import { advanceTime, DAY, DEPLOY_DEFAULTS } from "../../src/buckworld.js";
import { buildEquilibriumWorld, MonthlyIncome, PidKeeper }
  from "../../src/scenarios/eqworld.js";
import { PinWhale } from "../../src/agents/whale.js";
import { ParityArb } from "../../src/agents/arb.js";
import { BasketSaver } from "../../src/agents/saver.js";
import { MortgageRetiree } from "../../prototypes/mortgage-retiree.js";
import { lineChart } from "../../src/chart.js";

export const SAVER_NAMES = ["Sana", "Liam", "Aiko", "Ravi", "Zoë"];
const f6 = (v) => Number(v) / 1e6;
const f18 = (v) => Number(v) / 1e18;

export class EqWorldApp {
  /**
   * @param deps.session    a Session (deployer account)
   * @param deps.identity   the buck-identity kernel API
   * @param deps.artifacts  (name) => {abi, bytecode}
   * @param deps.urArtifact the UniversalRouter artifact
   * @param deps.makeAccount () => viem account for new agents
   * @param deps.rng        optional scalar drawer (tests inject)
   */
  constructor({ session, identity, artifacts, urArtifact, makeAccount, rng }) {
    Object.assign(this, { session, identity, artifacts, urArtifact,
                          makeAccount, rng });
    this.world = null;
    this.day = 0;
    this.core = [];      // whale(s) + arb + pid, fixed at boot
    this.savers = [];    // {name, agent}
    this.debtors = [];   // {name, agent, income, house}
  }

  async boot() {
    this.world = await buildEquilibriumWorld(this.session, this.artifacts, {
      identity: this.identity, urArtifact: this.urArtifact, rng: this.rng });
    const w = this.world;
    this.core = [
      ...w.tokens.map((t, i) => new PinWhale({
        simlp: w.simlp, pool: t.poolUsdc, token: t.erc20.address,
        tokenDec: t.dec, quote: w.usdc.address,
        refPrice: (d) => w.feeds[i][d % w.feeds[i].length] })),
      new ParityArb(),
      new PidKeeper(),
    ];
    for (const a of this.core) {
      if (a.setup) await a.setup(w);
    }
    return w;
  }

  /** Register + endow a new saver; it starts acting on the next tick. */
  async addSaver({ budget = 25_000n * 10n ** 6n, holdDays = 60 } = {}) {
    const n = this.savers.length + 1;
    const given = SAVER_NAMES[(n - 1) % SAVER_NAMES.length];
    const agent = new BasketSaver({
      account: this.makeAccount(), budget, holdDays,
      fields: {
        given_name: given, family_name: `Saver-${n}`,
        id_type: "Alberta Identity Card", id_number: `AIC-2026-SAV-${n}`,
        jurisdiction: "Alberta, Canada", date_of_birth: "1991-05-05",
        issued_at: "2026-07-04T00:00:00Z", epoch: 42,
      } });
    await agent.setup(this.world);
    const entry = { name: `${given} Saver-${n}`, agent };
    this.savers.push(entry);
    return entry.name;
  }

  /** Pledge a new debtor (insured assets -> BuckCredit headroom) with a
   *  monthly income feed; it starts acting on the next month boundary. */
  async addDebtor({ house = 400_000n * 10n ** 6n, mortgageBp = 550n,
                    premiumBp = 50n, payment = 3_000n * 10n ** 6n } = {}) {
    const n = this.debtors.length + 1;
    const account = this.makeAccount();
    const agent = new MortgageRetiree(
      { house, mortgageBp, premiumBp, payment, account });
    const income = new MonthlyIncome({ account, amount: payment });
    await agent.setup(this.world);      // pledges via the world's proxy
    const entry = { name: `Debtor-${n}`, agent, income, house };
    this.debtors.push(entry);
    return entry.name;
  }

  /** One simulated day: clock, core agents, savers, incomes + debtors,
   *  then the series sample the charts read. */
  async tick() {
    const w = this.world, d = this.day;
    await advanceTime(w, DAY);
    for (const a of this.core) await a.act(w, d, 0);
    for (const s of this.savers) await s.agent.act(w, d, 0);
    for (const dd of this.debtors) {
      await dd.income.act(w, d, 0);
      await dd.agent.act(w, d, 0);
    }
    await w.record(d);
    this.day += 1;
    return d;
  }

  // ==== the status bar ==================================================

  async status() {
    const w = this.world;
    const [K, bvib, spotUB] = await Promise.all([w.K(), w.bvib(), w.spotUB()]);
    return { day: this.day, K: f18(K), bvib: f18(bvib),
             price: f6(spotUB), mismatches: this.session.mismatches.length };
  }

  // ==== overview ========================================================

  /** Agent summary rows for the compact roster. */
  roster() {
    const rows = [];
    for (const { name, agent: a } of this.savers) {
      const vals = a.ledger.filter((r) => r.event !== "deposit");
      rows.push({
        kind: "saver", name,
        state: a.position
          ? `holding receipt #${a.position.receiptId} (day ${a.position.day})`
          : a.ledger.length ? "redeemed" : "waiting to deploy",
        cost: a.position ? f6(a.position.costUsd) : null,
        mark: vals.length ? f6(vals[vals.length - 1].usd) : null,
        profit: f6(a.profitUsd),
      });
    }
    for (const { name, agent: a, house } of this.debtors) {
      const last = a.ledger[a.ledger.length - 1];
      rows.push({
        kind: "debtor", name,
        mortgage: f6(a.mortgageOwing), hypo: f6(a.hypoOwing),
        drawn: last ? f6(last.drawn) : 0, banked: f6(a.usdc),
        net: last ? f6(house + last.banked - last.mortgageOwing - last.drawn)
                  : f6(house - a.mortgageOwing),
        hypoNet: f6(house - a.hypoOwing),
      });
    }
    return rows;
  }

  /** The overview panels (null before the first tick). */
  charts({ width = 840, height = 190 } = {}) {
    const S = this.world?.series ?? [];
    if (!S.length) return null;
    return {
      k: lineChart({ title: "BUCK K", width, height, refY: 0.75,
        series: [{ label: "buckK",
                   points: S.map((r) => [r.day, f18(r.K)]) }] }),
      bvib: lineChart({ title: "basketValueInBuck (the observable the PID defends)",
        width, height, refY: 1.0,
        series: [{ label: "bvib",
                   points: S.map((r) => [r.day, f18(r.bvib)]) }] }),
      price: lineChart({ title: "BUCK price, floating BUCK/USDC pool ($)",
        width, height, refY: 1.0,
        series: [{ label: "BUCK/USDC",
                   points: S.map((r) => [r.day, f6(r.spotUB)]) }] }),
    };
  }

  // ==== pools ===========================================================

  /** Live per-pool state for the pool cards. */
  async poolsSnapshot() {
    const w = this.world, s = w.session;
    const bal = (t, holder) => s.call(t, "balanceOf", [holder]);
    const spotUB = await w.spotUB();
    const out = { tokens: [], ub: null };
    for (let i = 0; i < w.tokens.length; i++) {
      const t = w.tokens[i];
      const [spotUsd, spotBuck] = await Promise.all(
        [w.spotUsd(i), w.spotBuck(i)]);
      const implied = (spotUsd * 1_000_000n) / spotUB;
      out.tokens.push({
        sym: t.sym, dec: t.dec,
        usdcPool: {
          address: t.poolUsdc.address, spot: f6(spotUsd),
          ref: f6(w.feeds[i][Math.max(0, this.day - 1) % w.feeds[i].length]),
          tok: Number(await bal(t.erc20, t.poolUsdc.address)) / 10 ** t.dec,
          usdc: f6(await bal(w.usdc, t.poolUsdc.address)),
        },
        buckPool: {
          address: t.poolBuck.address, spot: f6(spotBuck),
          implied: f6(implied),
          divBp: Number(((spotBuck - implied) * 10_000n) / implied),
          tok: Number(await bal(t.erc20, t.poolBuck.address)) / 10 ** t.dec,
          buck: f6(await bal(w.buck, t.poolBuck.address)),
        },
      });
    }
    out.ub = {
      address: w.poolUB.address, spot: f6(spotUB),
      buck: f6(await bal(w.buck, w.poolUB.address)),
      usdc: f6(await bal(w.usdc, w.poolUB.address)),
    };
    return out;
  }

  poolCharts({ width = 840, height = 190 } = {}) {
    const S = this.world?.series ?? [];
    if (!S.length) return null;
    const w = this.world;
    const spotVsRef = lineChart({
      title: "TOKEN/USDC: pool spot vs the reference walk ($)",
      width, height,
      series: w.tokens.flatMap((t, i) => [
        { label: `${t.sym} spot`,
          points: S.map((r) => [r.day, f6(r.spots[i])]) },
        { label: `${t.sym} ref`,
          points: S.map((r) => [r.day, f6(r.refs[i])]) },
      ]) });
    const divergence = lineChart({
      title: "TOKEN/BUCK: divergence from the implied triangle price (bp)",
      width, height, refY: 0,
      series: w.tokens.map((t, i) => ({
        label: `${t.sym}/BUCK`,
        points: S.map((r) => {
          const implied = (r.spots[i] * 1_000_000n) / r.spotUB;
          return [r.day,
                  Number(((r.spotsBuck[i] - implied) * 10_000n) / implied)];
        }) })) });
    const float = lineChart({
      title: "BUCK/USDC: the floating pool ($, never controlled)",
      width, height, refY: 1.0,
      series: [{ label: "BUCK/USDC",
                 points: S.map((r) => [r.day, f6(r.spotUB)]) }] });
    return { spotVsRef, divergence, float };
  }

  // ==== controller (BUCK K + PID) =======================================

  async controllerSnapshot() {
    const w = this.world, s = w.session;
    const [K, ff, lastBasketCost] = await Promise.all([
      s.call(w.kctrl, "buckK"), s.call(w.kctrl, "fundingFactor"),
      s.call(w.kctrl, "lastBasketCost")]);
    return { K: f18(K), ff: f18(ff), lastBasketCost: f18(lastBasketCost),
             config: DEPLOY_DEFAULTS };
  }

  controllerCharts({ width = 840, height = 190 } = {}) {
    const S = this.world?.series ?? [];
    if (!S.length) return null;
    return {
      k: lineChart({ title: "BUCK K (the LTV lever the PID moves)",
        width, height, refY: 0.75,
        series: [{ label: "buckK",
                   points: S.map((r) => [r.day, f18(r.K)]) }] }),
      ff: lineChart({ title: "fundingFactor (counter-cyclical mint gate; 1.0 = neutral)",
        width, height, refY: 1.0,
        series: [{ label: "fundingFactor",
                   points: S.map((r) => [r.day, f18(r.ff)]) }] }),
    };
  }

  // ==== basket ==========================================================

  async basketSnapshot() {
    const w = this.world, s = w.session;
    const bvib = f18(await w.bvib());
    const constituents = [];
    for (const t of w.tokens) {
      constituents.push({
        sym: t.sym,
        tok: Number(await s.call(t.erc20, "balanceOf", [t.poolBuck.address]))
             / 10 ** t.dec,
        buck: f6(await s.call(w.buck, "balanceOf", [t.poolBuck.address])),
      });
    }
    return { bvib, receipts: w.receipts, constituents };
  }

  basketChart({ width = 840, height = 220 } = {}) {
    const S = this.world?.series ?? [];
    if (!S.length) return null;
    return lineChart({
      title: "basketValueInBuck (>1: BUCK below basket value -- inflation side)",
      width, height, refY: 1.0,
      series: [{ label: "bvib",
                 points: S.map((r) => [r.day, f18(r.bvib)]) }] });
  }

  // ==== per-agent detail ================================================

  saverChart(i, { width = 840, height = 200 } = {}) {
    const s = this.savers[i];
    if (!s) return null;
    const pts = s.agent.ledger.filter((r) => r.event !== "deposit")
                              .map((r) => [r.day, f6(r.usd)]);
    if (!pts.length) return null;
    const dep = s.agent.ledger.find((r) => r.event === "deposit");
    return lineChart({
      title: `${s.name}: basket position value ($; dashed = cost basis)`,
      width, height, refY: dep ? f6(-dep.usd) : null,
      series: [{ label: "position value", points: pts }] });
  }

  debtorCharts(i, { width = 840, height = 200 } = {}) {
    const d = this.debtors[i];
    if (!d || !d.agent.ledger.length) return null;
    const L = d.agent.ledger, house = d.house;
    return {
      net: lineChart({
        title: `${d.name}: net worth vs the untouched-mortgage counterfactual ($)`,
        width, height,
        series: [
          { label: "BuckCredit route", points: L.map((r) =>
              [r.day, f6(house + r.banked - r.mortgageOwing - r.drawn)]) },
          { label: "hypo (mortgage kept)", points: L.map((r) =>
              [r.day, f6(house - r.hypoOwing)]) },
        ] }),
      position: lineChart({
        title: `${d.name}: mortgage / credit drawn / banked ($)`,
        width, height,
        series: [
          { label: "mortgage owing", points: L.map((r) =>
              [r.day, f6(r.mortgageOwing)]) },
          { label: "BUCK credit drawn", points: L.map((r) =>
              [r.day, f6(r.drawn)]) },
          { label: "banked USDC", points: L.map((r) =>
              [r.day, f6(r.banked)]) },
        ] }),
    };
  }

  static DAY = DAY;
}
