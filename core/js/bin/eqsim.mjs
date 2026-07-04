#!/usr/bin/env node
// Run the minimal two-agent equilibrium on Tevm and (optionally) write
// the K / basketValueInBuck / BUCK-price / debtor-net-worth charts.
//
//   node bin/eqsim.mjs --days 180 --plot eqsim.svg --journal eqsim.jsonl
//
// Agents: DayClock, PinWhale (pins TOKEN/USDC to the seeded reference
// walk), ParityArb (ties TOKEN/BUCK to the implied triangle price),
// PidKeeper (permissionless compute()), BasketSaver (best-route
// acquisition -> basket deposit -> term -> redeem), MonthlyIncome +
// MortgageRetiree (the two-sided debtor, from the prototypes).

import { parseArgs } from "node:util";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

import { tevmSession } from "../src/backends.js";
import { loadArtifact, repoRoot, fileJournalWriter } from "../src/nodefs.js";
import { buildEquilibriumWorld, DayClock, PidKeeper, MonthlyIncome }
  from "../src/scenarios/eqworld.js";
import { PinWhale } from "../src/agents/whale.js";
import { ParityArb } from "../src/agents/arb.js";
import { BasketSaver } from "../src/agents/saver.js";
import { MortgageRetiree } from "../prototypes/mortgage-retiree.js";
import { runDays } from "../src/world.js";
import { lineChart, svgDoc } from "../src/chart.js";
import id from "../src/identity.js";

const { values: a } = parseArgs({ options: {
  days:    { type: "string", default: "180" },
  step:    { type: "string", default: "80" },     // walk step, bp/day
  seed:    { type: "string", default: "61445" },  // 0xF005
  hold:    { type: "string", default: "60" },     // saver term, days
  plot:    { type: "string", default: "" },
  journal: { type: "string", default: "" },
} });
const days = Number(a.days);

const session = await tevmSession(
  a.journal ? { journal: fileJournalWriter(a.journal) } : {});
const urArtifact = JSON.parse(readFileSync(
  join(repoRoot(), "alberta_buck", "sim", "artifacts", "UniversalRouter.json"),
  "utf8"));
const world = await buildEquilibriumWorld(session, loadArtifact, {
  identity: id, urArtifact,
  feedSeed: Number(a.seed), stepBp: Number(a.step) });

const HOUSE = 400_000n * 10n ** 6n;
const PAYMENT = 3_000n * 10n ** 6n;
const debtorAcct = privateKeyToAccount(generatePrivateKey());
const saver = new BasketSaver({
  account: privateKeyToAccount(generatePrivateKey()),
  holdDays: Number(a.hold) });
const debtor = new MortgageRetiree({
  house: HOUSE, mortgageBp: 550n, premiumBp: 50n, payment: PAYMENT,
  account: debtorAcct });

const agents = [
  new DayClock(),
  ...world.tokens.map((t, i) => new PinWhale({
    simlp: world.simlp, pool: t.poolUsdc, token: t.erc20.address,
    tokenDec: t.dec, quote: world.usdc.address,
    refPrice: (d) => world.feeds[i][d % world.feeds[i].length] })),
  new ParityArb(),
  new PidKeeper(),
  saver,
  new MonthlyIncome({ account: debtorAcct, amount: PAYMENT }),
  debtor,
];

const t0 = performance.now();
await runDays(world, agents, { days, onDay: (d) => world.record(d) });
const ms = Math.round(performance.now() - t0);

const S = world.series;
const last = S[S.length - 1];
const f6 = (v) => Number(v) / 1e6;
console.log(JSON.stringify({
  days, ms, mismatches: session.mismatches.length,
  K: Number(last.K) / 1e18, bvib: Number(last.bvib) / 1e18,
  buckUsd: f6(last.spotUB),
  saver: { events: saver.ledger.filter((r) => r.event !== "mark").length,
           profitUsd: f6(saver.profitUsd),
           roiAnnualPct: saver.dollarDays > 0n
             ? Number(saver.profitUsd * 365_00n / saver.dollarDays) / 100 : null },
  debtor: debtor.ledger.length ? {
    months: debtor.ledger.length,
    mortgageOwing: f6(debtor.mortgageOwing),
    hypoOwing: f6(debtor.hypoOwing),
    drawn: f6(debtor.ledger[debtor.ledger.length - 1].drawn),
    banked: f6(debtor.ledger[debtor.ledger.length - 1].banked),
  } : null,
}, null, 2));

if (a.plot) {
  const charts = [
    lineChart({ title: `BUCK K  (${days}d, walk ${a.step}bp/d, seed ${a.seed})`,
      series: [{ label: "buckK", points: S.map((r) => [r.day, Number(r.K) / 1e18]) }],
      refY: 0.75 }),
    lineChart({ title: "basketValueInBuck  (the observable the PID defends)",
      series: [{ label: "bvib", points: S.map((r) => [r.day, Number(r.bvib) / 1e18]) }],
      refY: 1.0 }),
    lineChart({ title: "BUCK price, floating BUCK/USDC pool (USDC)",
      series: [{ label: "BUCK/USDC", points: S.map((r) => [r.day, f6(r.spotUB)]) }],
      refY: 1.0 }),
    lineChart({ title: "debtor net worth vs the untouched-mortgage counterfactual ($)",
      series: [
        { label: "BuckCredit route", points: debtor.ledger.map((r) =>
            [r.day, f6(HOUSE + r.banked - r.mortgageOwing - r.drawn)]) },
        { label: "counterfactual", points: debtor.ledger.map((r) =>
            [r.day, f6(HOUSE - r.hypoOwing)]) },
      ] }),
  ];
  writeFileSync(a.plot, svgDoc(charts));
  console.error(`chart -> ${a.plot}`);
}
process.exit(session.mismatches.length ? 1 : 0);
