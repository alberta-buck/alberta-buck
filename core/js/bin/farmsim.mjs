#!/usr/bin/env node
// The FARMER equilibrium: a 3-commodity BUCK basket, a minimal cast of
// large stabilizers, and ONE real farming operation -- $3M land+grain
// behind BuckCredit, a $750k mortgage, a $250k revolving operating
// line, lumpy harvest revenue, surplus into the BuckBasket.
//
//   node bin/farmsim.mjs --days 365 --plot farm.svg
//   node bin/farmsim.mjs --days 365 --cast clock,whales,arb,pid   # minimal?
//
// The cast question this runner exists to answer: which agents are
// LOAD-BEARING for equilibrium (bvib ~ 1) around the farmer's flows?
//   clock    simulated time (mandatory: demurrage, PID dT, aging)
//   whales   one PinWhale per token: TOKEN/USDC pinned to references
//   arb      ParityArb: ties TOKEN/BUCK to the implied triangle
//   pid      PidKeeper: the controller defends the observable
//   saver    BasketSaver: term-deposit basket demand (color, not load?)

import { parseArgs } from "node:util";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

import { anvilSession, tevmSession } from "../src/backends.js";
import { loadArtifact, repoRoot, fileJournalWriter } from "../src/nodefs.js";
import { buildEquilibriumWorld, DayClock, PidKeeper }
  from "../src/scenarios/eqworld.js";
import { PinWhale } from "../src/agents/whale.js";
import { ParityArb } from "../src/agents/arb.js";
import { BasketSaver } from "../src/agents/saver.js";
import { Farmer } from "../prototypes/farmer.js";
import { runDays } from "../src/world.js";
import { lineChart, svgDoc } from "../src/chart.js";
import id from "../src/identity.js";

const { values: a } = parseArgs({ options: {
  backend: { type: "string", default: "tevm" },   // tevm | anvil (long runs)
  rpc:     { type: "string", default: "http://127.0.0.1:8545" },
  days:    { type: "string", default: "365" },
  step:    { type: "string", default: "80" },     // reference walk, bp/day
  seed:    { type: "string", default: "61445" },
  target:  { type: "string", default: "10000000" }, // pool depth, $/pool
  cast:    { type: "string", default: "clock,whales,arb,pid,saver" },
  arbIters: { type: "string", default: "3" },
  aggr:    { type: "string", default: "550" },    // theta*apr tolerance, bp
  plot:    { type: "string", default: "" },
  journal: { type: "string", default: "" },
  out:     { type: "string", default: "" },       // summary JSON path
} });
const days = Number(a.days);
const cast = new Set(a.cast.split(",").map((s) => s.trim()));

// A farmer's basket: grain, fuel, gold -- the commodities the operation
// actually earns, burns, and stores value in.
const TOKENS = [
  { sym: "GRAN", name: "Grain",  dec: 18, p0: 6_500_000n },
  { sym: "NRGY", name: "Energy", dec: 18, p0: 75_000_000n },
  { sym: "XAU",  name: "Gold",   dec: 18, p0: 2_650_000_000n },
];

const jopt = a.journal ? { journal: fileJournalWriter(a.journal) } : {};
const session = a.backend === "anvil"
  ? await anvilSession(a.rpc, jopt)
  : await tevmSession(jopt);
const urArtifact = JSON.parse(readFileSync(
  join(repoRoot(), "alberta_buck", "sim", "artifacts", "UniversalRouter.json"),
  "utf8"));
const world = await buildEquilibriumWorld(session, loadArtifact, {
  identity: id, urArtifact, tokens: TOKENS,
  targetBuck: BigInt(a.target) * 10n ** 6n,
  feedSeed: Number(a.seed), stepBp: Number(a.step) });

const farmer = new Farmer({
  account: privateKeyToAccount(generatePrivateKey()),
  aggrBp: BigInt(a.aggr) });

const agents = [];
if (cast.has("clock")) agents.push(new DayClock());
if (cast.has("whales")) {
  agents.push(...world.tokens.map((t, i) => new PinWhale({
    simlp: world.simlp, pool: t.poolUsdc, token: t.erc20.address,
    tokenDec: t.dec, quote: world.usdc.address,
    refPrice: (d) => world.feeds[i][d % world.feeds[i].length] })));
}
if (cast.has("arb")) agents.push(new ParityArb({
  maxIters: Number(a.arbIters) }));
if (cast.has("pid")) agents.push(new PidKeeper());
if (cast.has("saver")) agents.push(new BasketSaver({
  account: privateKeyToAccount(generatePrivateKey()),
  budget: 100_000n * 10n ** 6n, holdDays: 90, tokenIdx: 2 }));
agents.push(farmer);

const t0 = performance.now();
await runDays(world, agents, { days, onDay: (d) => world.record(d) });
const ms = Math.round(performance.now() - t0);

const S = world.series;
const f6 = (v) => Number(v) / 1e6;
const f18 = (v) => Number(v) / 1e18;
const bv = S.map((r) => f18(r.bvib));
const ub = S.map((r) => f6(r.spotUB));
const last = farmer.ledger[farmer.ledger.length - 1] ?? {};
const summary = {
  days, ms, cast: [...cast], mismatches: session.mismatches.length,
  equilibrium: {
    bvib: { min: Math.min(...bv), max: Math.max(...bv),
            mean: bv.reduce((s, v) => s + v, 0) / bv.length,
            final: bv[bv.length - 1] },
    buckUsd: { min: Math.min(...ub), max: Math.max(...ub),
               final: ub[ub.length - 1] },
    K: { min: Math.min(...S.map((r) => f18(r.K))),
         max: Math.max(...S.map((r) => f18(r.K))),
         final: f18(S[S.length - 1].K) },
  },
  farmer: {
    months: farmer.ledger.length,
    mortgageOwing: f6(last.mortgageOwing ?? farmer.mortgageOwing),
    opDrawn: f6(last.opDrawn ?? 0n),
    drawn: f6(last.drawn ?? 0n),
    jub: f6(last.jub ?? 0n),
    banked: f6(last.banked ?? 0n),
    basketMark: f6(last.basketMark ?? 0n),
    premiumPaid: f6(farmer.premiumPaid),
    tradeLoss: f6(farmer.tradeLoss),
    throttled: farmer.throttled,
    nw: f6(last.nw ?? 0n),
    hypoNw: f6(last.hypoNw ?? 0n),
    advantage: f6((last.nw ?? 0n) - (last.hypoNw ?? 0n)),
  },
};
console.log(JSON.stringify(summary, null, 2));
if (a.out) writeFileSync(a.out, JSON.stringify(summary, null, 2));

if (a.plot) {
  const L = farmer.ledger;
  const charts = [
    lineChart({ title: `basketValueInBuck  (${days}d, cast: ${[...cast].join("+")})`,
      series: [{ label: "bvib", points: S.map((r) => [r.day, f18(r.bvib)]) }],
      refY: 1.0 }),
    lineChart({ title: "BUCK price, floating BUCK/USDC pool (USDC)",
      series: [{ label: "BUCK/USDC", points: S.map((r) => [r.day, f6(r.spotUB)]) }],
      refY: 1.0 }),
    lineChart({ title: "the farm's debts ($): mortgage + operating line + BUCK claim",
      series: [
        { label: "mortgage", points: L.map((r) => [r.day, f6(r.mortgageOwing)]) },
        { label: "op line", points: L.map((r) => [r.day, f6(r.opDrawn)]) },
        { label: "drawn - jubilee", points: L.map((r) => [r.day, f6(r.drawn - r.jub)]) },
        { label: "hypo mortgage", points: L.map((r) => [r.day, f6(r.hypoMortgage)]) },
      ] }),
    lineChart({ title: "farm net worth vs the no-BUCK counterfactual ($)",
      series: [
        { label: "BUCK operation", points: L.map((r) => [r.day, f6(r.nw)]) },
        { label: "counterfactual", points: L.map((r) => [r.day, f6(r.hypoNw)]) },
      ] }),
  ];
  writeFileSync(a.plot, svgDoc(charts));
  console.error(`chart -> ${a.plot}`);
}
process.exit(session.mismatches.length ? 1 : 0);
