// Shared refusal/resumption vectors against the existing canonical builder.
// Each child owns a real household; there is one EVM and no shadow token model.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { encodeFunctionData } from "viem";
import { nodeWebevmSession } from "../src/nodefs.js";
import { buildEquilibriumWorld } from "../src/scenarios/eqworld.js";
import { CreditHouseholdAgent } from "../src/agents/credit-household.js";
import { TransactionReceiptPending } from "../src/session.js";
import { settlementOps } from "../src/settlement.js";
import { advanceTime, DAY } from "../src/buckworld.js";
import { artifact } from "../artifacts/bundle.mjs";
import * as identity from "../src/identity.js";

const RAW = 1_000_000n, MAX = (1n << 256n) - 1n;
const BANK = "0x000000000000000000000000000000000000dEaD";
const f = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-refinance.json", import.meta.url)));
const b = Object.fromEntries(Object.entries(f.base).map(([k, v]) => [k, BigInt(v)]));

test("household resumes only known cash remainders and keeps original credit", async (t) => {
  const s = await nodeWebevmSession();
  const world = await buildEquilibriumWorld(s, artifact, { identity, wheelArb: false, targetBuck: 1_000_000n * RAW,
    urArtifact: JSON.parse(readFileSync(new URL("../../../alberta_buck/sim/artifacts/UniversalRouter.json", import.meta.url))),
  });
  const { buck, credit, usdc, basket } = world;
  const execute = world.netMarket.execute;
  world.netSettlementOps = settlementOps(s, usdc, { execute: (who, c, name, args) =>
    execute(who, c, name, args, { expect: "either" }) });
  const pay = world.netSettlementOps.payExact;
  const proxyExec = (proxy, c, functionName, args) => s.send(proxy, "exec", [c.address,
    encodeFunctionData({ abi: c.abi, functionName, args })]);
  for (const [index, c] of f.resumption.cases.entries()) await t.test(c.name, async () => {
    const account = { address: `0x${(0xabf100n + BigInt(index)).toString(16).padStart(40, "0")}` };
    const a = new CreditHouseholdAgent({ account, member: `resume-${index}`, settings: f.resumption.settings });
    await a.setup(world); await world.households.openTrading(account); await a.advanceNetCalendar(world, 0);
    const owner = world.holderAddress(account), proxy = await world.proxyFor(account), [id] = world._credits.get(account.address);
    await proxyExec(world.simlp, buck, "transfer", [owner, b.seed_buck]);
    await s.send(world.kctrl, "compute");
    const candidate = await world.netCreditOps.assessIssueCandidate(owner, id, b.coverage, b.new_spendable, b.premium);
    const initial = { candidate, ...Object.fromEntries(["export_buck", "minimum_usdc", "mortgage_limit", "cash_buffer",
      "reserve_floor", "switch_cost", "penalty_numerator", "penalty_denominator"].map((k) => [k, b[k]])) };
    if (c.phase === "export") initial.minimum_usdc = 5000n * RAW;
    initial.switch_cost = BigInt(c.switch_cost ?? b.switch_cost);
    try {
      if (c.phase === "mortgage") world.netSettlementOps.payExact = (who, to) => pay(who, to, MAX);
      await a.startNetRefinance(world, initial, 0);
    } finally { world.netSettlementOps.payExact = pay; }
    const m = a.refinanceMachine, ctr = {};
    assert.ok(m.settlementDue); assert.equal(m.flow.phase, c.phase);
    assert.ok(a.joined && !a.refinanceClosed); assert.equal(a.refis, 0);
    const principal = (await s.call(credit, "issuanceOf", [id]))[0];
    assert.equal(principal, b.new_spendable + b.premium);
    if (c.outside_principal) {
      const amount = BigInt(c.outside_principal), paid = await pay(owner, BANK, amount);
      await a.calendarMachine.recordMortgage(world, paid.receipt, amount);
    }
    if (c.held_buck) {
      const held = await s.call(buck, "balanceOf", [owner]);
      await proxyExec(proxy, buck, "transfer", [world.simlp.address, held - BigInt(c.held_buck)]);
    }
    if (c.wait === "cash" || Object.hasOwn(c, "leave_cash")) {
      const penalty = (initial.mortgage_limit * initial.penalty_numerator + initial.penalty_denominator - 1n) / initial.penalty_denominator;
      const leave = BigInt(c.leave_cash ?? initial.cash_buffer + initial.mortgage_limit + initial.switch_cost + penalty - 1n);
      const cash = await s.call(usdc, "balanceOf", [owner]);
      await proxyExec(proxy, usdc, "transfer", [world.simlp.address, cash - leave]);
    }
    const originals = Object.fromEntries(["issue", "issueCandidate", "assessIssueCandidate", "issueCoverageMenu", "quoteGlobalReliefRate"]
      .map((key) => [key, world.netCreditOps[key]]));
    const read = s.client.readContract;
    const unavailable = () => { throw new Error("resumption must not quote or issue new credit"); };
    for (const key of Object.keys(originals)) world.netCreditOps[key] = unavailable;
    if (c.phase === "mortgage") s.client.readContract = (request) => {
      if (request.address.toLowerCase() === world.poolUB.address.toLowerCase()
          || request.address.toLowerCase() === basket.address.toLowerCase() && request.functionName === "basketValueInBuck") {
        throw new Error("BUCK market unavailable during bank-only completion");
      }
      return read(request);
    };
    try {
      const options = c.wait === "economics" ? { expected_wallet_growth: .5 } : {};
      const before = { block: (await s.client.getBlock()).number, events: m.events.length, plan: m.flow.plan, debt: a.mortgage };
      let quote = await a.assessNetRefinanceRemainder(world, 0, options);
      assert.equal((await s.client.getBlock()).number, before.block); assert.equal(m.events.length, before.events);
      assert.equal(m.flow.plan, before.plan); assert.equal(a.mortgage, before.debt); assert.equal(m.status().revision, 0);
      assert.equal(quote.context.new_credit, false); assert.equal(m.flow.origin.export_buck, initial.export_buck);
      if (c.wait) {
        assert.equal(quote.plan, null);
        await a.resumeNetRefinance(world, 0, ctr, options);
        assert.ok(m.settlementDue && a.fundingPending); assert.equal(a.lastAction, "refinance-wait");
        assert.equal((await s.client.getBlock()).number, before.block); assert.equal(m.status().revision, 0);
      }
      const day = c.days ?? 0;
      if (day) {
        await advanceTime(world, a.calendarMachine.epoch + BigInt(day * DAY) - (await s.client.getBlock()).timestamp);
        await world.turnWheel();
        assert.equal(await a.advanceNetCalendar(world, day), false, a.lastError);
        assert.ok(a.incomeTotal > 0n && a.hypoPaid > 0n, "known waiting intentions cannot freeze normal wages/bills");
      }
      quote = await a.assessNetRefinanceRemainder(world, day);
      const p = quote.plan;
      assert.ok(p, JSON.stringify(quote.context, (_, v) => typeof v === "bigint" ? String(v) : v));
      assert.equal(p.candidate, null); assert.equal(p.reserve_buy_net, 0n); assert.equal(p.reserve_buy_max_usdc, 0n);
      assert.ok(p.mortgage_limit <= initial.mortgage_limit && p.mortgage_limit <= a.mortgage);
      assert.equal(p.switch_cost, initial.switch_cost); assert.ok(p.cash_buffer >= initial.cash_buffer);
      assert.equal(p.reserve_floor, initial.reserve_floor);
      if (c.phase === "export" && p.mortgage_limit) {
        assert.ok(p.export_buck > 0n && p.export_buck < initial.export_buck);
        assert.equal(quote.context.score.parts.relief, 0); assert.equal(quote.context.score.parts.premium, 0);
      }
      if (c.held_buck) assert.ok(p.export_buck < BigInt(c.held_buck) - initial.reserve_floor);
      const supply = await s.call(buck, "totalIssued"), bankBefore = await s.call(usdc, "balanceOf", [BANK]);
      const debtBefore = a.mortgage, paidBefore = a.externalPaid, eventsBefore = m.events.length, sends = [];
      world.netMarket.execute = async (...args) => {
        const phase = m.flow.phase; sends.push(phase);
        if (c.unknown === phase) throw new Error("submission outcome unknown");
        const receipt = await execute(...args);
        if (c.lost === phase) throw new TransactionReceiptPending(receipt.transactionHash);
        return receipt;
      };
      world.netSettlementOps.payExact = async (...args) => {
        const phase = m.flow.phase; sends.push(phase);
        const result = await pay(...args);
        if (c.lost === phase) throw new TransactionReceiptPending(result.receipt.transactionHash);
        return result;
      };
      if (c.concurrent) {
        const outcomes = await Promise.allSettled([a.resumeNetRefinance(world, day, ctr), a.resumeNetRefinance(world, day, ctr)]);
        assert.equal(outcomes.filter((r) => r.status === "fulfilled").length, 1);
        assert.equal(outcomes.filter((r) => r.status === "rejected").length, 1);
      } else await a.resumeNetRefinance(world, day, ctr);
      if (c.lost || c.unknown) {
        assert.ok(m.active); assert.equal(m.flow.phase, c.lost ?? c.unknown);
        assert.equal(a.calendarMachine.status().mortgage_provisional, Boolean(m.flow.phase === "mortgage" && p.mortgage_limit));
        await assert.rejects(a.resumeNetRefinance(world, day, ctr), /refused settlement/);
        const sent = [...sends];
        await a.act(world, day, 0);
        if (c.unknown) {
          assert.ok(m.active); assert.equal(m.flow.hash, null); assert.deepEqual(sends, sent);
          assert.match(a.lastError, /hash unavailable/); assert.equal(a.mortgage, debtBefore); assert.equal(a.externalPaid, paidBefore);
          assert.equal(a.switchPaid, 0n); assert.equal(await s.call(buck, "totalIssued"), supply); return;
        }
      }
      assert.equal(m.status().status, "complete", a.lastError); assert.equal(m.settlementDue, false);
      assert.equal(m.status().revision, 1); assert.equal(m.flow.origin.export_buck, initial.export_buck);
      assert.ok(sends.filter((x) => x === "export").length <= 1 && sends.filter((x) => x === "mortgage").length <= 1);
      assert.equal(await s.call(buck, "totalIssued"), supply); assert.equal((await s.call(credit, "issuanceOf", [id]))[0], principal);
      const penalty = (p.mortgage_limit * p.penalty_numerator + p.penalty_denominator - 1n) / p.penalty_denominator;
      assert.equal(await s.call(usdc, "balanceOf", [BANK]) - bankBefore, p.mortgage_limit + p.switch_cost + penalty);
      assert.equal(a.mortgage, debtBefore - p.mortgage_limit); assert.equal(a.externalPaid, paidBefore + p.mortgage_limit);
      assert.equal(a.switchPaid, p.switch_cost); assert.equal(a.penaltyPaid, penalty);
      assert.equal(a.refis, p.mortgage_limit ? 1 : 0); assert.equal(a.refinanceClosed, Boolean(p.mortgage_limit || p.switch_cost));
      assert.deepEqual(m.events.slice(eventsBefore).filter((e) => e.status === "confirmed").map((e) => e.operation), sends);
      if (!p.mortgage_limit && !p.switch_cost) { assert.deepEqual(sends, []); assert.equal(a.lastAction, "refinance-satisfied"); }
      await assert.rejects(a.resumeNetRefinance(world, day, ctr), /refused settlement/);
    } finally {
      Object.assign(world.netCreditOps, originals); world.netMarket.execute = execute;
      world.netSettlementOps.payExact = pay; s.client.readContract = read;
    }
  });
});
