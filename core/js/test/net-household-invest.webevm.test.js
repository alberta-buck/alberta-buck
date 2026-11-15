// A mortgage-free household's credit investment on the existing canonical builder: it issues against
// its insured asset, keeps the BUCK and deposits them in the basket (closing fee first, reserve
// purchase, issue, deposit). The same fixture and fault vectors run in Python
// (alberta_buck/test/test_net_household_invest.py); cases marked "js": false run there only.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { nodeWebevmSession } from "../src/nodefs.js";
import { buildEquilibriumWorld } from "../src/scenarios/eqworld.js";
import { CreditSaverAgent, CreditHouseholdAgent } from "../src/agents/credit-household.js";
import { TransactionReceiptPending } from "../src/session.js";
import { netCreditOps } from "../src/net-world.js";
import { settlementOps } from "../src/settlement.js";
import { advanceTime, DAY } from "../src/buckworld.js";
import { Q96 } from "../src/v3.js";
import { artifact } from "../artifacts/bundle.mjs";
import * as identity from "../src/identity.js";
// Pinned to vectors and fixtures computed at one second a block (the clock before 2026-11-15 night):
// these sessions keep it until the vectors are regenerated at Ethereum's 12 s (BLOCK_SECONDS).
const PINNED_CLOCK = { blockSeconds: 1 };

const RAW = 1_000_000n;
const MIN_SQRT = 4295128740n, MAX_SQRT = 1461446703485210103287273052203988822378723970341n;
const fixture = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-invest.json", import.meta.url)));
const life = fixture.lifecycle, flow = fixture.flow, forecasts = fixture.forecasts;

test("a mortgage-free household issues, keeps and deposits its credit through one receipt cursor", async (t) => {
  const s = await nodeWebevmSession(PINNED_CLOCK);
  const world = await buildEquilibriumWorld(s, artifact, { identity, wheelArb: false, targetBuck: 1_000_000n * RAW,
    urArtifact: JSON.parse(readFileSync(new URL("../../../alberta_buck/sim/artifacts/UniversalRouter.json", import.meta.url))),
  });
  const { buck, usdc, basket } = world;
  const execute = world.netMarket.execute, simulate = world.netMarket.simulate;
  const credit = world.netCreditOps, settle = world.netSettlementOps;
  const owner = (a) => world.holderAddress(a.account);
  const ids = (a) => world._credits.get(a.account.address) ?? [];
  let serial = 0;
  const household = async (name, Cls = CreditSaverAgent, extra = {}) => {
    const a = new Cls({ account: { address: `0x${(0xabf400n + BigInt(serial++)).toString(16).padStart(40, "0")}` },
      member: name, settings: { ...fixture.settings, ...extra } });
    await a.setup(world); await world.households.openTrading(a.account);
    return a;
  };
  const books = async (a) => {
    const book = await world.netCreditOps.household(owner(a), ids(a));
    return { cash: await s.call(usdc, "balanceOf", [owner(a)]), issued: book.issued, premium: book.premiumNominal,
      spendable: book.spendable, supply: await s.call(buck, "totalIssued"), receipts: [...a.receipts], basis: a.basketPrincipal };
  };
  const confirmed = (a) => a.refinanceMachine.events.filter((e) => e.kind === "invest" && e.status === "confirmed");
  const life1 = await household("invest-life");
  const premium = await household("invest-premium", CreditSaverAgent, fixture.premium.settings);
  const debtor = await household("invest-debtor", CreditHouseholdAgent, { ltv: .4 });
  const cases = [];
  for (const [i, c] of flow.cases.entries()) {
    if (c.js === false) continue;
    cases.push({ c, a: await household(`invest-flow-${i}`), ctr: {}, sends: [], fault: true });
  }
  world.householdEpoch = (await s.client.getBlock()).timestamp;
  const at = async (day, ...agents) => {
    await advanceTime(world, world.householdEpoch + BigInt(day * DAY) - (await s.client.getBlock()).timestamp);
    await s.send(world.kctrl, "compute");
    for (const a of agents) await a.calendarMachine.prepare(world, day);
  };
  for (const a of [life1, premium, debtor, ...cases.map((x) => x.a)]) await a.advanceNetCalendar(world, 0);
  const fee = BigInt(life.switch_usdc);

  await t.test("the closing is paid once and each bounded bite of the carry line is deposited", async () => {
    const a = life1, m = a.refinanceMachine, ctr = {};
    const start = await books(a), block = (await s.client.getBlock()).number;
    const quote = await a.assessNetInvestment(world, 0, forecasts);
    assert.equal((await s.client.getBlock()).number, block, "an assessment sends nothing");
    assert.equal(quote.context.reason, "worthwhile-staged-investment"); assert.equal(quote.context.basis, "carry");
    assert.equal(quote.plan.switch_cost, fee); assert.equal(quote.context.programme.fixed_cost_paid_now, fee);
    assert.ok(quote.context.programme.fixed_cost_allocated > 0 && quote.context.programme.fixed_cost_allocated < Number(fee) / 1e7);
    assert.ok(quote.plan.reserve_buy_net === quote.plan.candidate.reserveShortfall && quote.plan.reserve_buy_net > 0n);
    assert.ok(quote.plan.deposit_buck > 0n && quote.plan.deposit_buck <= quote.plan.candidate.requestedSpendable);
    let principal = 0n, deposited = 0n;
    for (let day = 0; day < life.days; day++) {
      if (day) await at(day);
      const before = await books(a), seen = m.events.length;
      assert.equal(await a.advanceNetInvestment(world, day, ctr, { forecasts }), true);
      assert.equal(a.lastAction, "invest", a.lastError); assert.equal(m.flow.kind, "invest"); assert.equal(m.flow.status, "complete");
      const done = m.events.slice(seen).filter((e) => e.status === "confirmed");
      assert.deepEqual(done.map((e) => e.operation), day ? life.confirmed_later : life.confirmed_first);
      const issue = done.find((e) => e.operation === "issue").amounts, deposit = done.find((e) => e.operation === "deposit").amounts;
      const after = await books(a);
      assert.equal(BigInt(issue.principal), BigInt(issue.spendable) + BigInt(issue.premium));
      assert.equal(after.issued - before.issued, BigInt(issue.principal));
      assert.equal(after.supply - before.supply, BigInt(issue.principal), "new BUCK exist only as this facility's principal");
      assert.ok(BigInt(deposit.buck) > 0n && BigInt(deposit.buck) <= BigInt(issue.spendable));
      assert.equal(after.basis - before.basis, BigInt(deposit.buck));
      assert.ok(after.spendable >= a.netCreditReserve && a.netCreditReserve > 0n, "the reserve is not deposited");
      assert.equal(before.cash - after.cash >= fee, day === 0);
      principal += BigInt(issue.principal); deposited += BigInt(deposit.buck);
      assert.equal(await a.advanceNetInvestment(world, day, ctr, { forecasts }), false, "one decision a day");
    }
    assert.ok(a.refinanceClosed && a.joined); assert.equal(a.switchPaid, fee); assert.equal(a.deploys, life.days);
    assert.equal(a.issuedInvest, deposited); assert.equal(a.basketPrincipal, deposited);
    assert.equal(a.receipts.length, 1, "later bites top up the same receipt");
    const [shares, basis] = await s.call(basket, "holdings", [a.receipts[0]]);
    assert.deepEqual([basis, shares], [a.book.get(a.receipts[0]).principal, a.book.get(a.receipts[0]).shares]);
    assert.equal((await books(a)).issued - start.issued, principal);
    const final = m.events.filter((e) => e.kind === "invest" && e.status === "complete").at(-1);
    assert.equal(final.outcome, "invest"); assert.equal(final.basket.basis, String(deposited));
    assert.deepEqual(m.events.map((e) => e.seq), m.events.map((_, i) => i + 1));
  });

  const day1 = life.days, day2 = life.days + 1;
  await at(day1, debtor, ...cases.map((x) => x.a));
  await t.test("a debtor's decisions belong to refinancing until its mortgage is paid", async () => {
    const seen = debtor.refinanceMachine.events.length;
    assert.ok(debtor.mortgage > 0n);
    assert.equal(await debtor.advanceNetInvestment(world, day1, {}, { forecasts }), false);
    assert.equal(debtor.refinanceMachine.events.length, seen);
    await assert.rejects(debtor.assessNetInvestment(world, day1, forecasts), /mortgage-free/);
  });

  const arm = (x) => async (who, target, name, args, opts) => {
    const { c, a } = x, f = a.refinanceMachine.flow;
    const phase = f?.kind === "invest" && f.status !== "complete" ? f.phase : "other";
    x.sends.push([phase, name]);
    const hit = x.fault && [c.lost, c.unknown, c.refused].includes(phase);
    if (hit && c.unknown) { x.fault = false; throw new Error("submission outcome unknown"); }
    if (hit && c.refused) { x.fault = false; args = [...args]; args[flow.refuse_argument[name]] = 2n ** 200n; }
    const receipt = await execute(who, target, name, args, opts);
    if (hit && c.lost) { x.fault = false; throw new TransactionReceiptPending(receipt.transactionHash); }
    return receipt;
  };
  const guarded = async (x, run) => {
    const armed = arm(x), readContract = s.client.readContract;
    world.netMarket.execute = armed;
    world.netCreditOps = netCreditOps(s, world.net, armed, { simulate });
    world.netSettlementOps = settlementOps(s, usdc, { execute: armed });
    if (x.c.view === "issue") {
      const real = world.netCreditOps.assessIssueCandidate;
      world.netCreditOps.assessIssueCandidate = async (...args) => {
        const f = x.a.refinanceMachine.flow;
        if (f?.kind === "invest" && f.phase === "issue" && !x.viewed) { x.viewed = true; throw new Error("simulated credit read failure"); }
        return real(...args);
      };
    }
    if (x.c.view === "deposit") {
      s.client.readContract = (request) => {
        const f = x.a.refinanceMachine.flow;
        if (!x.viewed && f?.kind === "invest" && f.phase === "deposit" && request.functionName === "netAccounting") {
          x.viewed = true; throw new Error("simulated basket read failure");
        }
        return readContract(request);
      };
    }
    try { return await run(); }
    finally {
      world.netMarket.execute = execute; world.netCreditOps = credit; world.netSettlementOps = settle;
      s.client.readContract = readContract;
    }
  };
  const order = life.confirmed_first;
  const sent = (x, p) => x.sends.filter(([at_]) => at_ === p).map(([, name]) => name);
  for (const x of cases) await t.test(`${x.c.name} (attempt)`, async () => {
    const { c, a, ctr } = x, m = a.refinanceMachine, phase = c.lost ?? c.unknown ?? c.refused ?? c.view;
    x.before = await books(a);
    await guarded(x, () => a.advanceNetInvestment(world, day1, ctr, { forecasts }));
    assert.deepEqual(confirmed(a).map((e) => e.operation), order.slice(0, order.indexOf(phase)),
      "every earlier phase was real and stays applied");
    if (c.unknown) {
      assert.ok(m.active && m.flow.status === "pending" && m.flow.phase === phase && m.flow.hash === null);
      assert.equal(a.lastAction, "invest-pending");
      const count = x.sends.length;
      assert.equal(await guarded(x, () => a.advanceNetInvestment(world, day1, ctr, { forecasts })), true);
      assert.match(a.lastError, /invest hash unavailable/); assert.equal(x.sends.length, count); assert.deepEqual(a.receipts, []);
    } else if (c.lost) {
      assert.ok(m.active && m.flow.status === "pending" && m.flow.phase === phase && m.flow.hash);
      assert.equal(a.lastAction, "invest-pending"); assert.equal(sent(x, phase).length, 1);
      const lost = m.flow.hash;
      assert.equal(await guarded(x, () => a.advanceNetInvestment(world, day1, ctr, { forecasts })), true);
      const applied = confirmed(a).filter((e) => e.operation === phase);
      assert.equal(applied.length, 1); assert.equal(applied[0].transaction_hash, lost); assert.equal(sent(x, phase).length, 1);
    } else if (c.view === "issue") {
      // A failed read before the send is not a refusal: the flow waits where it is and retries the read.
      assert.ok(m.active && m.flow.status === "running" && m.flow.phase === "issue"); assert.deepEqual(sent(x, "issue"), []);
      assert.equal(m.events.at(-1).status, "waiting");
      assert.equal(await guarded(x, () => a.advanceNetInvestment(world, day1, ctr, { forecasts })), true);
    } else {
      const refusal = m.events.filter((e) => e.status === "refused").at(-1);
      assert.equal(a.lastAction, "invest-refused", a.lastError); assert.equal(m.active, false); assert.equal(m.flow.phase, phase);
      assert.equal(Boolean(refusal.transaction_hash), Boolean(c.refused), "a view failure sent nothing");
      const now = await books(a);
      assert.equal(m.surplusDue, phase === "deposit");
      assert.equal(a.refinanceClosed, phase !== "closing"); assert.equal(a.switchPaid, phase === "closing" ? 0n : fee);
      assert.equal(now.issued > x.before.issued, phase === "deposit"); assert.deepEqual(now.receipts, []);
      assert.equal(await guarded(x, () => a.advanceNetInvestment(world, day1, ctr, { forecasts })), false, "one attempt a day");
    }
  });
  await at(day2, ...cases.filter((x) => !x.c.unknown).map((x) => x.a));
  for (const x of cases.filter((y) => !y.c.unknown)) await t.test(`${x.c.name} (next day)`, async () => {
    const { c, a, ctr } = x, m = a.refinanceMachine, phase = c.lost ?? c.refused ?? c.view;
    if (c.refused || c.view === "deposit") {
      // A known refusal never blocks the next day's work or decision.
      assert.equal(await guarded(x, () => a.advanceNetInvestment(world, day2, ctr, { forecasts })), true);
      assert.equal(m.events.filter((e) => e.status === "resumed").length, phase === "deposit" ? 1 : 0);
    }
    assert.equal(m.active, false); assert.equal(m.flow.status, "complete"); assert.equal(a.lastAction, "invest", a.lastError);
    const done = confirmed(a).map((e) => e.operation);
    assert.deepEqual([...done].sort(), [...order].sort(), "each phase is confirmed exactly once in the end");
    assert.equal(new Set(confirmed(a).map((e) => e.transaction_hash)).size, done.length, "no receipt is applied twice");
    for (const step of ["closing", "fund", "issue"]) assert.equal(sent(x, step).length, c.refused === step ? 2 : 1, step);
    const after = await books(a);
    assert.equal(a.switchPaid, fee); assert.equal(a.receipts.length, 1);
    const issue = confirmed(a).find((e) => e.operation === "issue").amounts;
    assert.equal(after.issued - x.before.issued, BigInt(issue.principal));
    const [shares, basis] = await s.call(basket, "holdings", [a.receipts[0]]);
    assert.deepEqual([basis, shares], [a.book.get(a.receipts[0]).principal, a.book.get(a.receipts[0]).shares]);
    assert.equal(basis, a.issuedInvest);
    assert.ok(after.spendable >= a.netCreditReserve && after.cash >= a.buffer);
    assert.deepEqual(m.events.map((e) => e.seq), m.events.map((_, i) => i + 1));
  });

  // Last, because it moves every pool: the seeding helper buys BUCK with a share of every TOKEN pool.
  await t.test("with no carry line it holds until the BUCK is dear against the basket", async () => {
    const a = premium, m = a.refinanceMachine, day = day2 + 1;
    await at(day, a);
    assert.equal(await a.advanceNetInvestment(world, day, {}, { forecasts }), true);
    assert.equal(a.lastAction, "invest-hold"); assert.equal(m.events.at(-1).reason, "hold");
    const bvib = await s.call(basket, "basketValueInBuck");
    for (const token of world.tokens) {
      const pool = token.poolBuck, token0 = await s.call(pool, "token0"), token1 = await s.call(pool, "token1");
      const first = token0.toLowerCase() === token.erc20.address.toLowerCase();
      const [sqrt] = await s.call(pool, "slot0"), liquidity = await s.call(pool, "liquidity");
      const amount = (first ? liquidity * Q96 / sqrt : liquidity * sqrt / Q96) / BigInt(fixture.premium.token_pool_share);
      await s.send(token.erc20, "mint", [world.simlp.address, amount]);
      await s.send(world.simlp, "swap", [pool.address, world.simlp.address, first, amount, first ? MIN_SQRT : MAX_SQRT,
        token0, token1], { gas: 3_000_000n });
    }
    await at(day + 1, a);
    const dear = await s.call(basket, "basketValueInBuck");
    assert.ok(dear * 100n < bvib * BigInt(fixture.premium.bvib_below_percent), "the basket's TWAP carries the move");
    const quote = await a.assessNetInvestment(world, day + 1, forecasts);
    assert.equal(quote.context.basis, "premium"); assert.equal(quote.context.reason, "worthwhile-investment");
    assert.equal(quote.plan.switch_cost, 0n); assert.ok(quote.verdict.parts.execution > 0, "kept, its BUCK are worth more sold");
    assert.equal(await a.advanceNetInvestment(world, day + 1, {}, { forecasts }), true);
    assert.equal(a.lastAction, "invest", a.lastError);
    assert.deepEqual(confirmed(a).map((e) => e.operation), ["fund", "issue", "deposit"]);
    const after = await books(a);
    assert.ok(after.issued > 0n && after.basis > 0n && after.cash >= a.buffer); assert.equal(a.switchPaid, 0n);
  });
});
