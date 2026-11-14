// A leaving household on the existing canonical builder: the asset sale pays the whole mortgage,
// savings come home, the whole lien is retired, burn(0) releases what no principal backs, owned BUCK
// is sold one capped bite a day, and it leaves. An investor holds BUCK while BVIB is above its
// tolerance and withdraws at its horizon. The same fixture and fault vectors run in Python
// (alberta_buck/test/test_net_household_exit.py); cases marked "js": false run there only.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseEventLogs } from "viem";
import { tevmSession } from "../src/backends.js";
import { buildEquilibriumWorld } from "../src/scenarios/eqworld.js";
import { CreditSaverAgent, CreditHouseholdAgent, HouseholdInvestorAgent } from "../src/agents/credit-household.js";
import { TransactionReceiptPending } from "../src/session.js";
import { netCreditOps } from "../src/net-world.js";
import { settlementOps } from "../src/settlement.js";
import { advanceTime, DAY } from "../src/buckworld.js";
import { Q96 } from "../src/v3.js";
import { artifact } from "../artifacts/bundle.mjs";
import * as identity from "../src/identity.js";

const RAW = 1_000_000n, ZERO = "0x0000000000000000000000000000000000000000";
const MIN_SQRT = 4295128740n, MAX_SQRT = 1461446703485210103287273052203988822378723970341n;
const fixture = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-exit.json", import.meta.url)));
const forecasts = fixture.forecasts, flow = fixture.flow;
const UR = () => JSON.parse(readFileSync(new URL("../../../alberta_buck/sim/artifacts/UniversalRouter.json", import.meta.url)));

async function setting() {
  const s = await tevmSession();
  const world = await buildEquilibriumWorld(s, artifact, { identity, wheelArb: false, targetBuck: 1_000_000n * RAW, urArtifact: UR() });
  const owner = (a) => world.holderAddress(a.account), ids = (a) => world._credits.get(a.account.address) ?? [];
  let serial = 0;
  const household = async (Cls, name, settings, base) => {
    const a = new Cls({ account: { address: `0x${(base + BigInt(serial++)).toString(16).padStart(40, "0")}` }, member: name, settings });
    await a.setup(world); await world.households.openTrading(a.account);
    return a;
  };
  const at = async (day) => {
    await advanceTime(world, world.householdEpoch + BigInt(day * DAY) - (await s.client.getBlock()).timestamp);
    await s.send(world.kctrl, "compute");
  };
  const books = async (a) => {
    const book = ids(a).length ? await world.netCreditOps.household(owner(a), ids(a)) : null;
    return { cash: await s.call(world.usdc, "balanceOf", [owner(a)]), buck: await s.call(world.buck, "balanceOf", [owner(a)]),
      wallet: a.wallet ? await s.call(world.buck, "balanceOf", [a.wallet.address]) : 0n,
      issued: book?.issued ?? 0n, premium: book?.premiumNominal ?? 0n,
      activated: book ? book.facilities.reduce((n, f) => n + f.activated, 0n) : 0n,
      supply: await s.call(world.buck, "totalIssued"), receipts: [...a.receipts] };
  };
  const events = (a, kind, status = "confirmed") => a.refinanceMachine.events.filter((e) => e.kind === kind && e.status === status);
  // Nothing is left on-chain or in the books, and every dispatcher leaves an exited household alone.
  const gone = async (a, day, ctr) => {
    const end = await books(a);
    assert.ok(a.exited, a.lastError); assert.equal(a.exitStage, "complete"); assert.equal(a.lastAction, "exit");
    assert.equal(end.issued, 0n); assert.equal(end.premium, 0n); assert.equal(end.activated, 0n); assert.deepEqual(end.receipts, []);
    assert.ok(end.buck < RAW && end.wallet < RAW + RAW / 100n, "at most a BUCK of dust stays");
    assert.equal(a.book.size, 0); assert.equal(a.basketPrincipal, 0n); assert.equal(a.shares, 0n);
    assert.equal(a.netRepaymentTarget, null); assert.equal(a.netRepaymentReserve, null); assert.equal(a.refinanceMachine.active, false);
    const block = (await s.client.getBlock()).number, seen = a.refinanceMachine.events.length;
    assert.equal(await a.advanceNetExit(world, day, ctr), false); assert.equal(await a.advanceNetSurplus(world, day, ctr), false);
    assert.equal(await a.advanceNetWallet(world, day, ctr), false);
    assert.equal((await s.client.getBlock()).number, block); assert.equal(a.refinanceMachine.events.length, seen);
    return end;
  };
  const sellInto = async (pool, share = 20n) => {
    const token0 = await s.call(pool, "token0"), token1 = await s.call(pool, "token1");
    const first = token0.toLowerCase() === world.buck.address.toLowerCase();
    const [sqrt] = await s.call(pool, "slot0"), liquidity = await s.call(pool, "liquidity");
    const reserve = first ? liquidity * Q96 / sqrt : liquidity * sqrt / Q96;
    await s.send(world.simlp, "swap", [pool.address, world.simlp.address, first, reserve / share,
      first ? MIN_SQRT : MAX_SQRT, token0, token1], { gas: 3_000_000n });
  };
  const buyFrom = async (token, share) => {
    const pool = token.poolBuck, token0 = await s.call(pool, "token0"), token1 = await s.call(pool, "token1");
    const first = token0.toLowerCase() === token.erc20.address.toLowerCase();
    const [sqrt] = await s.call(pool, "slot0"), liquidity = await s.call(pool, "liquidity");
    const amount = (first ? liquidity * Q96 / sqrt : liquidity * sqrt / Q96) / share;
    await s.send(token.erc20, "mint", [world.simlp.address, amount]);
    await s.send(world.simlp, "swap", [pool.address, world.simlp.address, first, amount, first ? MIN_SQRT : MAX_SQRT,
      token0, token1], { gas: 3_000_000n });
  };
  return { s, world, owner, ids, household, at, books, events, gone, sellInto, buyFrom };
}

test("leaving households sell, settle, retire, release and go, one capped market bite a day", async (t) => {
  const { s, world, owner, ids, household, at, books, events, gone } = await setting();
  const spec = fixture.saver, execute = world.netMarket.execute, simulate = world.netMarket.simulate;
  const credit = world.netCreditOps, settle = world.netSettlementOps;
  const life = await household(CreditSaverAgent, "exit-life", spec.settings, 0xabf500n);
  const release = await household(CreditSaverAgent, "exit-release", fixture.release.settings, 0xabf500n);
  const debtor = await household(CreditHouseholdAgent, "exit-debtor", fixture.debtor.settings, 0xabf500n);
  const cases = [];
  for (const [i, c] of flow.cases.entries()) {
    if (c.js === false) continue;
    cases.push({ c, a: await household(CreditSaverAgent, `exit-flow-${i}`, spec.settings, 0xabf500n), ctr: {}, sends: [], fault: true });
  }
  const savers = [life, ...cases.map((x) => x.a)];
  world.householdEpoch = (await s.client.getBlock()).timestamp;
  for (const a of [...savers, release, debtor]) await a.advanceNetCalendar(world, 0);
  const ctr = {}, start = new Map(), stages = [], trades = new Map();

  const arm = (x) => async (who, target, name, args, opts) => {
    const { c, a } = x, m = a.refinanceMachine;
    const step = m.flow?.kind === "exit" && m.active ? m.flow.plan.step : null;
    x.sends.push([step, name]);
    const hit = x.fault && step !== null && step === (c.lost ?? c.unknown ?? c.refused);
    if (hit && c.unknown) { x.fault = false; throw new Error("submission outcome unknown"); }
    if (hit && c.refused) { x.fault = false; args = [...args]; args[flow.refuse_argument[name]] = 2n ** 200n; }
    const receipt = await execute(who, target, name, args, opts);
    if (hit && c.lost) { x.fault = false; throw new TransactionReceiptPending(receipt.transactionHash); }
    return receipt;
  };
  const guarded = async (x, run) => {
    if (!x) return run();
    const armed = arm(x);
    world.netMarket.execute = armed;
    world.netCreditOps = netCreditOps(s, world.net, armed, { simulate });
    world.netSettlementOps = settlementOps(s, world.usdc, { execute: armed });
    if (x.c.preview) {
      world.netMarket.simulate = async (who, target, name, args, opts) => {
        if (name === "redeemShares" && (x.closedDay ?? x.day) === x.day) {
          x.closedDay = x.day; throw new Error("execution reverted: CreditShortfallIncreased");
        }
        return simulate(who, target, name, args, opts);
      };
    }
    try { return await run(); }
    finally {
      world.netMarket.execute = execute; world.netMarket.simulate = simulate;
      world.netCreditOps = credit; world.netSettlementOps = settle;
    }
  };
  const sent = (x, step) => x.sends.filter(([at_]) => at_ === step).map(([, name]) => name);
  // The moment each fault case is about: checked once, right after the turn that produced it.
  const moment = async (x, day) => {
    const { c, a } = x, m = a.refinanceMachine;
    if (c.preview) {
      if (!events(a, "exit", "waiting").length) return;
      x.checked = true;
      const waits = events(a, "exit", "waiting");
      assert.deepEqual(sent(x, "redeem"), []); assert.equal(a.exitRetries, 1); assert.equal(a.exitStage, "collect-savings");
      assert.equal(waits.length, 1); assert.match(waits[0].reason, /CreditShortfallIncreased/); assert.equal(a.lastAction, "exit-wait");
      assert.equal(await guarded(x, () => a.advanceNetExit(world, day, x.ctr)), false); assert.equal(events(a, "exit", "waiting").length, 1);
      return;
    }
    if (x.fault) return;
    x.checked = true;
    if (c.unknown) {
      assert.ok(m.active && m.flow.status === "pending" && m.flow.hash === null); assert.equal(a.lastAction, "exit-pending");
      x.count = x.sends.length;
    } else if (c.lost) {
      assert.ok(m.active && m.flow.status === "pending" && m.flow.hash); assert.equal(sent(x, c.lost).length, 1);
      const lost = m.flow.hash;
      assert.equal(await guarded(x, () => a.advanceNetExit(world, day, x.ctr)), true);
      const applied = events(a, "exit").filter((e) => e.operation === c.lost);
      assert.equal(applied.length, 1); assert.equal(applied[0].transaction_hash, lost); assert.equal(sent(x, c.lost).length, 1);
    } else {
      const refusal = m.events.filter((e) => e.status === "refused").at(-1);
      assert.equal(refusal.kind, "exit"); assert.equal((await s.client.getTransactionReceipt({ hash: refusal.transaction_hash })).status, "reverted");
      assert.equal(a.lastAction, "exit-refused"); assert.equal(a.exitRetries, 1); assert.equal(m.active, false);
      assert.equal(await guarded(x, () => a.advanceNetExit(world, day, x.ctr)), false, "a proved refusal is retried tomorrow, never today");
    }
  };
  const leave = async (a, day, x = null) => {
    if (x) x.day = day;
    const seen = a.refinanceMachine.events.length;
    for (let turn = 0; turn < 8; turn++) {
      const more = await guarded(x, () => a.advanceNetExit(world, day, x ? x.ctr : ctr));
      if (a === life && more) stages.push(a.exitStage);
      if (x && !x.checked) await moment(x, day);
      if (!more) break;
    }
    const bites = a.refinanceMachine.events.slice(seen).filter((e) => e.status === "confirmed" && ["sell", "buy"].includes(e.operation));
    if (a === life) trades.set(day, bites.length);
  };

  for (let day = 0; day < spec.last_day; day++) {
    if (day) await at(day);
    if (day === 0) {
      // Backing only: no cash, principal or premium. Only burn(0) can release it.
      await world.netCreditOps.issue(owner(release), ids(release)[0], BigInt(fixture.release.coverage), 0n, 0n, ZERO);
      start.set(release, await books(release)); release.divestDay = 0;
    }
    for (const a of savers) if (day < spec.exit_day) {
      await a.advanceNetInvestment(world, day, {}, { forecasts });
      assert.equal(a.lastAction, "invest", `${a.member} day ${day}: ${a.lastError}`);
      await a.advanceNetWallet(world, day, {});
    }
    if (day < fixture.debtor.exit_day) await debtor.advanceNetRefinancing(world, day, {}, { forecasts });
    if (day === spec.exit_day) for (const a of savers) { start.set(a, await books(a)); a.divestDay = spec.exit_day; }
    if (day === fixture.debtor.exit_day) {
      start.set(debtor, { ...(await books(debtor)), mortgage: debtor.mortgage, shadow: debtor.hypoMortgage, paid: debtor.externalPaid });
      debtor.divestDay = fixture.debtor.exit_day;
    }
    for (const a of [release, debtor, life]) if (a.exitDue(day) && !a.exited) await leave(a, day);
    for (const x of cases) if (x.a.exitDue(day) && !x.a.exited) await leave(x.a, day, x);
    if (day === fixture.debtor.exit_day) {
      assert.ok(debtor.sold); assert.equal(debtor.mortgage, 0n); assert.equal(debtor.hypoMortgage, 0n);
      assert.ok(debtor.externalPaid - start.get(debtor).paid >= start.get(debtor).mortgage);
    }
  }

  await t.test("a saver's exit: sale, savings home, the whole lien retired by its own engine, and out", async () => {
    const a = life, m = a.refinanceMachine, s0 = start.get(a);
    assert.ok(s0.issued > 0n && s0.premium > 0n && s0.wallet > RAW);
    assert.deepEqual(stages.filter((v, i) => i === 0 || v !== stages[i - 1]), spec.stages);
    assert.ok(Math.max(...trades.values()) === 1 && [...trades.values()].reduce((n, v) => n + v, 0) > 2, "one capped market bite a day");
    const end = await gone(a, spec.last_day - 1, ctr);
    assert.equal(a.exitStarted, spec.exit_day); assert.ok(a.sold); assert.equal(a.exitRetries, 0); assert.equal(a.exitShort, false);
    const sale = events(a, "calendar").filter((e) => e.phase.startsWith("sale") && e.operation === "income");
    assert.equal(sale.length, 1); assert.equal(BigInt(sale[0].amounts.usdc), a.face);
    const reductions = events(a, "retirement");
    assert.equal(reductions.reduce((n, e) => n + BigInt(e.amounts.principal), 0n), s0.issued); assert.equal(a.unwound, s0.issued);
    assert.equal(reductions.reduce((n, e) => n + BigInt(e.amounts.premium_refund_nominal), 0n), s0.premium);
    assert.ok(a.released); assert.equal(a.releasedCoverage, 0n);
    assert.equal(events(a, "exit").filter((e) => e.operation === "release").length, 0);
    const sold = events(a, "exit").filter((e) => e.operation === "sell").reduce((n, e) => n + BigInt(e.amounts.usdc), 0n);
    assert.equal(a.withdrawnUsd, sold); assert.ok(sold > 0n && end.cash > a.face);
    assert.ok(events(a, "wallet").some((e) => e.operation === "sell"), "the working balance came home");
    const final = m.events.at(-1);
    assert.deepEqual([final.kind, final.status, final.outcome], ["exit", "complete", "exited"]);
    assert.equal(final.amounts.retired_principal, String(s0.issued));
    assert.deepEqual(m.events.map((e) => e.seq), m.events.map((_, i) => i + 1));
  });

  await t.test("a debtor's sale pays the whole mortgage and its lien is bought back a bite a day", async () => {
    const a = debtor, s0 = start.get(a);
    assert.ok(a.refis >= 1 && s0.issued > 0n && s0.mortgage > 0n);
    await gone(a, spec.last_day - 1, ctr);
    const settle_ = events(a, "calendar").filter((e) => e.phase.startsWith("sale") && e.operation === "mortgage");
    assert.equal(settle_.length, 1); assert.ok(BigInt(settle_[0].amounts.usdc) >= s0.mortgage);
    const bought = events(a, "surplus").filter((e) => e.operation === "buy");
    assert.ok(bought.length > 0); assert.equal(new Set(bought.map((e) => e.day)).size, bought.length);
    assert.equal(events(a, "retirement").reduce((n, e) => n + BigInt(e.amounts.principal), 0n), s0.issued);
    assert.equal(a.unwound, s0.issued); assert.equal(a.penaltyPaid, 0n);
  });

  await t.test("coverage that no principal backs is released by burn(0) at the exit", async () => {
    const a = release, s0 = start.get(a), coverage = BigInt(fixture.release.coverage);
    assert.equal(s0.activated, coverage); assert.equal(s0.issued, 0n);
    await gone(a, spec.last_day - 1, ctr);
    const released = events(a, "exit").filter((e) => e.operation === "release");
    assert.equal(released.length, 1); assert.equal(released[0].amounts.coverage, String(coverage));
    const receipt = await s.client.getTransactionReceipt({ hash: released[0].transaction_hash });
    const logs = parseEventLogs({ abi: world.buck.abi, logs: receipt.logs, eventName: "FacilityReleased" });
    assert.deepEqual(logs.map((x) => [x.args.tokenId, x.args.coverage]), [[ids(a)[0], coverage]]);
    assert.equal(a.releasedCoverage, coverage); assert.equal(a.releasedRefund, 0n);
  });

  for (const x of cases) await t.test(x.c.name, async () => {
    const { c, a } = x, m = a.refinanceMachine, s0 = start.get(a);
    assert.ok(x.checked, "the case's moment came");
    if (c.unknown) {
      // No hash: nothing more is sent on later days and the household never counts as gone.
      assert.equal(x.sends.length, x.count); assert.match(a.lastError, /exit hash unavailable/);
      assert.ok(m.active && !a.exited);
      return;
    }
    await gone(a, spec.last_day - 1, x.ctr);
    assert.equal(events(a, "retirement").reduce((n, e) => n + BigInt(e.amounts.principal), 0n), s0.issued);
    const hashes = m.events.filter((e) => e.status === "confirmed").map((e) => e.transaction_hash);
    assert.equal(new Set(hashes).size, hashes.length, "no receipt is applied twice");
    assert.equal(sent(x, "redeem").length, c.refused === "redeem" ? 2 : c.preview ? 1 : 1);
    assert.deepEqual(m.events.map((e) => e.seq), m.events.map((_, i) => i + 1));
  });
});

test("an investor buys the basket within its BVIB tolerance, holds BUCK above it, and withdraws", async (t) => {
  const { s, world, household, at, books, events, gone, sellInto, buyFrom } = await setting();
  const spec = fixture.investor, tolerance = spec.settings.bvib_tolerance, ctr = {};
  const a = await household(HouseholdInvestorAgent, "exit-investor", spec.settings, 0xabf600n);
  world.householdEpoch = (await s.client.getBlock()).timestamp;
  await a.advanceNetCalendar(world, 0);
  const opening = await books(a);
  assert.equal(a.p.bvib_tolerance, tolerance);
  let quote = await a.assessNetInvestor(world, 0);
  assert.equal(quote.context.action, "invest"); assert.equal(quote.context.reason, "buy-and-deposit");
  assert.equal(await a.advanceNetInvestor(world, 0, ctr), true); assert.equal(a.lastAction, "save", a.lastError);
  const saved = await books(a), basis = a.basketPrincipal;
  assert.equal(saved.receipts.length, 1); assert.ok(basis > RAW && saved.buck <= basis / 500n);
  assert.equal(a.investedUsd, opening.cash - saved.cash);
  assert.equal(await a.advanceNetInvestor(world, 0, ctr), false, "one decision a day");
  // A broad sell-off: BVIB rises beyond its tolerance; it buys BUCK and holds them.
  await sellInto(world.poolUB);
  for (const token of world.tokens) await sellInto(token.poolBuck);
  await at(1); await a.calendarMachine.prepare(world, 1);
  quote = await a.assessNetInvestor(world, 1);
  assert.ok(quote.context.bvib_wad > BigInt(Math.round((1 + tolerance) * 1e6)) * 10n ** 12n, String(quote.context.bvib_wad));
  assert.equal(quote.context.action, "wait");
  assert.equal(await a.advanceNetInvestor(world, 1, ctr), true); assert.equal(a.lastAction, "buy", a.lastError);
  const held = await books(a);
  assert.ok(held.buck > RAW); assert.deepEqual(held.receipts, saved.receipts); assert.equal(a.basketPrincipal, basis);
  // BVIB comes back within its tolerance: it deposits what it held, topping up the same receipt.
  for (const token of world.tokens) await buyFrom(token, BigInt(spec.token_pool_share));
  await at(2); await a.calendarMachine.prepare(world, 2);
  quote = await a.assessNetInvestor(world, 2);
  assert.equal(quote.context.action, "invest", String(quote.context.bvib_wad)); assert.equal(quote.plan.action, "convert");
  assert.equal(quote.plan.position_id, saved.receipts[0]);
  assert.equal(await a.advanceNetInvestor(world, 2, ctr), true); assert.equal(a.lastAction, "convert", a.lastError);
  const begin = await books(a);
  assert.deepEqual(begin.receipts, saved.receipts); assert.ok(begin.buck < held.buck / 100n);
  assert.equal(begin.cash, held.cash); assert.equal(begin.supply, opening.supply);
  assert.deepEqual(events(a, "surplus").map((e) => e.operation), ["buy", "deposit", "buy", "deposit"]);
  a.exitDay = spec.exit_day;
  let day = spec.exit_day;
  for (; day < spec.last_day; day++) {
    await at(day);
    for (let turn = 0; turn < 8; turn++) if (!await a.advanceNetExit(world, day, ctr)) break;
    if (a.exited) break;
  }
  const end = await gone(a, day, ctr);
  assert.equal(a.sold, false); assert.equal(end.supply, begin.supply, "an investor has no asset, lien or coverage");
  assert.equal(a.withdrawnUsd, end.cash - begin.cash); assert.ok(a.withdrawnUsd > 0n); assert.equal(ctr.invExits, 1);
});
