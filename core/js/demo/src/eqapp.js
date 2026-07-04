// The eqworld demo CONTROLLER -- the minimal two-agent equilibrium,
// interactive: boot the world on the in-browser EVM, tick simulated
// days, ADD savers and debtors mid-run, and feed the live chart panels
// from world.series and the agents' own ledgers.
//
// All the logic, none of the DOM: eqmain.js (the page) constructs it
// with browser-loaded dependencies; the node gate (eqapp.tevm.test.js)
// drives the SAME class with node-loaded ones.

import { advanceTime, DAY } from "../../src/buckworld.js";
import { buildEquilibriumWorld, MonthlyIncome, PidKeeper }
  from "../../src/scenarios/eqworld.js";
import { PinWhale } from "../../src/agents/whale.js";
import { ParityArb } from "../../src/agents/arb.js";
import { BasketSaver } from "../../src/agents/saver.js";
import { MortgageRetiree } from "../../prototypes/mortgage-retiree.js";
import { lineChart } from "../../src/chart.js";

export const SAVER_NAMES = ["Sana", "Liam", "Aiko", "Ravi", "Zoë"];
const f6 = (v) => Number(v) / 1e6;

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

  /** Live chain reads for the status bar. */
  async status() {
    const w = this.world;
    const [K, bvib, spotUB] = await Promise.all([w.K(), w.bvib(), w.spotUB()]);
    return { day: this.day, K: Number(K) / 1e18, bvib: Number(bvib) / 1e18,
             price: f6(spotUB), mismatches: this.session.mismatches.length };
  }

  /** Agent summary rows for the roster panel. */
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

  /** SVG strings for the chart panels (null before the first tick). */
  charts({ width = 640, height = 190 } = {}) {
    const S = this.world?.series ?? [];
    if (!S.length) return null;
    const out = {
      k: lineChart({ title: "BUCK K", width, height, refY: 0.75,
        series: [{ label: "buckK",
                   points: S.map((r) => [r.day, Number(r.K) / 1e18]) }] }),
      bvib: lineChart({ title: "basketValueInBuck (the observable the PID defends)",
        width, height, refY: 1.0,
        series: [{ label: "bvib",
                   points: S.map((r) => [r.day, Number(r.bvib) / 1e18]) }] }),
      price: lineChart({ title: "BUCK price, floating BUCK/USDC pool ($)",
        width, height, refY: 1.0,
        series: [{ label: "BUCK/USDC",
                   points: S.map((r) => [r.day, f6(r.spotUB)]) }] }),
      debtors: null, savers: null,
    };
    const dSeries = this.debtors.flatMap(({ name, agent: a, house }) =>
      a.ledger.length ? [
        { label: name, points: a.ledger.map((r) =>
            [r.day, f6(house + r.banked - r.mortgageOwing - r.drawn)]) },
        { label: `${name} hypo`, points: a.ledger.map((r) =>
            [r.day, f6(house - r.hypoOwing)]) },
      ] : []);
    if (dSeries.length) {
      out.debtors = lineChart({
        title: "debtors: net worth vs untouched-mortgage counterfactual ($)",
        width, height, series: dSeries });
    }
    const sSeries = this.savers.flatMap(({ name, agent: a }) => {
      const pts = a.ledger.filter((r) => r.event !== "deposit")
                          .map((r) => [r.day, f6(r.usd)]);
      return pts.length ? [{ label: name, points: pts }] : [];
    });
    if (sSeries.length) {
      out.savers = lineChart({
        title: "savers: basket position value ($)",
        width, height, series: sSeries });
    }
    return out;
  }

  static DAY = DAY;
}
