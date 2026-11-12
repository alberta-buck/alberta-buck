// Reserve-safe saving, basket redemption and discounted retirement on the
// existing canonical builder. One EVM; every household owns real cash, a real
// receipt and real selected-facility principal. The shared flow vectors and
// chronology also run in Python (test_net_household_setup.py).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { encodeFunctionData, parseEventLogs } from "viem";
import { tevmSession } from "../src/backends.js";
import { buildEquilibriumWorld } from "../src/scenarios/eqworld.js";
import { CreditHouseholdAgent, CreditSaverAgent } from "../src/agents/credit-household.js";
import { TransactionReceiptPending, TransactionReverted } from "../src/session.js";
import { netBasketOps } from "../src/basket.js";
import { observeNetMarket } from "../src/net-market-quote.js";
import { advanceTime, DAY } from "../src/buckworld.js";
import { artifact } from "../artifacts/bundle.mjs";
import * as identity from "../src/identity.js";

const RAW = 1_000_000n, Q = 10n ** 36n, Q96 = 1n << 96n;
const MIN_SQRT = 4295128740n, MAX_SQRT = 1461446703485210103287273052203988822378723970341n;
const fixture = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-surplus.json", import.meta.url)));
const programme = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-refinance.json", import.meta.url))).programme;
const life = fixture.lifecycle, flow = fixture.flow;
const show = (v) => JSON.stringify(v, (_, x) => typeof x === "bigint" ? String(x) : x);

test("households save in place, redeem and retire discounted principal through one receipt cursor", async (t) => {
  const s = await tevmSession();
  const world = await buildEquilibriumWorld(s, artifact, { identity, wheelArb: false, targetBuck: 1_000_000n * RAW,
    urArtifact: JSON.parse(readFileSync(new URL("../../../alberta_buck/sim/artifacts/UniversalRouter.json", import.meta.url))),
  });
  const { buck, usdc, basket } = world;
  const execute = world.netMarket.execute, simulate = world.netMarket.simulate;
  const proxyExec = (proxy, c, functionName, args) => s.send(proxy, "exec", [c.address,
    encodeFunctionData({ abi: c.abi, functionName, args })]);
  const owner = (a) => world.holderAddress(a.account);
  const ids = (a) => world._credits.get(a.account.address) ?? [];
  const cashOf = (a) => s.call(usdc, "balanceOf", [owner(a)]), buckOf = (a) => s.call(buck, "balanceOf", [owner(a)]);
  const principalOf = async (a) => (await world.netCreditOps.household(owner(a), ids(a))).issued;
  let serial = 0;
  const account = () => ({ address: `0x${(0xabf200n + BigInt(serial++)).toString(16).padStart(40, "0")}` });
  const saver = async (name) => {
    const a = new CreditSaverAgent({ account: account(), member: name, settings: { ...life.settings, surplus_policy: "basket" } });
    await a.setup(world); await world.households.openTrading(a.account);
    await proxyExec(world.simlp, buck, "transfer", [owner(a), BigInt(life.seed_buck)]);
    return a;
  };
  // Every household exists before the shared epoch; none has traded yet.
  const debtor = new CreditHouseholdAgent({ account: account(), member: programme.members[0],
    settings: { ...programme.settings, surplus_policy: "basket" } });
  await debtor.setup(world); await world.households.openTrading(debtor.account);
  const cases = [];
  for (const [i, c] of flow.cases.entries()) cases.push({ c, a: await saver(`surplus-flow-${i}`), ctr: {}, sends: [], fault: true });
  const arms = { cash: await saver("surplus-cash"), basket: await saver("surplus-basket") };
  const sdk = await saver("surplus-sdk"), stranger = await saver("surplus-stranger");
  world.householdEpoch = (await s.client.getBlock()).timestamp;
  const at = async (day) => {
    await advanceTime(world, world.householdEpoch + BigInt(day * DAY) - (await s.client.getBlock()).timestamp);
    await s.send(world.kctrl, "compute");
  };
  const savers = [...cases.map((x) => x.a), arms.cash, arms.basket, sdk, stranger];
  for (const a of savers) {
    await a.advanceNetCalendar(world, 0);
    assert.equal(await cashOf(a), BigInt(life.settings.cash_k) * 1000n * RAW, a.lastError);
  }
  const supplyOf = () => s.call(buck, "totalIssued");
  const holding = async (id) => { const [shares, basis] = await s.call(basket, "holdings", [id]); return { shares, basis }; };
  const confirmed = (m, operation) => m.events.filter((e) => e.status === "confirmed" && e.operation === operation);
  const issueCredit = async (a) => {
    const c = await world.netCreditOps.assessIssueCandidate(owner(a), ids(a)[0], BigInt(life.coverage),
      BigInt(life.spendable), BigInt(life.premium));
    await world.netCreditOps.issueCandidate(owner(a), c);
    return principalOf(a);
  };
  // Real sales by the seeding helper: a twentieth of one pool's active BUCK reserve.
  const sellInto = async (pool) => {
    const token0 = await s.call(pool, "token0"), token1 = await s.call(pool, "token1");
    const first = token0.toLowerCase() === buck.address.toLowerCase();
    const [sqrt] = await s.call(pool, "slot0"), liquidity = await s.call(pool, "liquidity");
    const reserve = first ? liquidity * Q96 / sqrt : liquidity * sqrt / Q96;
    assert.ok(await s.call(buck, "balanceOf", [world.simlp.address]) > reserve / 20n, "seeding helper holds the BUCK it sells");
    await s.send(world.simlp, "swap", [pool.address, world.simlp.address, first, reserve / 20n,
      first ? MIN_SQRT : MAX_SQRT, token0, token1], { gas: 3_000_000n });
  };
  const market = async () => observeNetMarket(s, buck, usdc, world.poolUB, world.fees.ub,
    { blockNumber: (await s.client.getBlock()).number });
  const retirementHistory = async (a, principal, plan) => {
    const m = a.refinanceMachine;
    const funded = m.events.filter((e) => e.kind === "surplus" && e.status === "complete" && e.outcome === "retirement-funded");
    assert.equal(funded.length, 1); assert.equal(funded[0].accounts.issued, String(principal), "funding a target retires nothing");
    assert.equal(funded[0].retirement.target, String(plan.target_principal)); assert.equal(funded[0].retirement.uncertain, false);
    const reductions = m.events.filter((e) => e.kind === "retirement" && e.status === "confirmed");
    assert.equal(reductions.reduce((sum, e) => sum + BigInt(e.amounts.principal), 0n), principal);
    for (const e of reductions) {
      assert.ok(e.seq > funded[0].seq && e.phase === "reduction" && e.operation === "reduce" && !e.recovered);
      const receipt = await s.client.getTransactionReceipt({ hash: e.transaction_hash });
      const logs = parseEventLogs({ abi: buck.abi, logs: receipt.logs, eventName: "FacilityReduced" });
      assert.deepEqual(logs.map((x) => [String(x.args.tokenId), String(x.args.principal), String(x.args.reliefScaled)]),
        [[e.amounts.token_id, e.amounts.principal, e.amounts.relief_scaled]]);
    }
    assert.equal(reductions.at(-1).accounts.issued, "0");
    assert.deepEqual([m.events.at(-1).kind, m.events.at(-1).status], ["retirement", "complete"]);
    assert.equal(m.status().retirement, null); assert.equal(m.status().kind, "surplus"); assert.equal(m.status().status, "complete");
    assert.deepEqual(m.events.map((e) => e.seq), m.events.map((_, i) => i + 1));
    return reductions;
  };
  const settle = async (a, day) => {
    for (let i = 0; i < 6; i++) {
      if (a.refinanceMachine.active || a.netRepaymentTarget !== null || a.netRepaymentInterrupted) await a.recoverPendingOperations(world, day);
    }
  };

  await t.test("saving between refinance tranches keeps the credit front and frees other cash", async () => {
    const a = debtor, m = a.refinanceMachine, ctr = {}, f = programme.forecasts;
    assert.equal(await a.advanceNetRefinancing(world, 0, ctr), false);
    assert.equal(a.netCreditReserve, 0n); assert.equal(a.refis, 0);
    await at(1);
    await a.advanceNetRefinancing(world, 1, ctr, { forecasts: f });
    const front = m.flow.plan.reserve_floor;
    assert.equal(a.refis, 1, a.lastError); assert.equal(m.flow.status, "complete");
    assert.ok(front > 0n); assert.equal(a.netCreditReserve, front);
    assert.equal(m.events.at(-1).accounts.reserve_floor, String(front)); assert.equal(m.events.at(-1).accounts.surplus_protection, null);
    // Unrelated owned BUCK arrives: cash above the issuance front, and dollars to save.
    await proxyExec(world.simlp, buck, "transfer", [owner(a), 500n * RAW]);
    await s.send(usdc, "mint", [owner(a), 20_000n * RAW]);
    await at(2); await a.calendarMachine.prepare(world, 2);
    const saving = await a.assessNetSurplus(world, 2, { forecasts: f });
    const held = await buckOf(a);
    assert.ok(saving.plan && saving.plan.action === "save", show(saving.context));
    assert.equal(saving.plan.reserve_floor, held); assert.ok(held > front + 400n * RAW);
    assert.equal(saving.plan.cash_buffer, saving.context.credit_funding.cash_floor); assert.ok(saving.plan.cash_buffer >= a.buffer);
    const cash = await cashOf(a), issued = await principalOf(a), mortgage = a.mortgage;
    await a.advanceNetSurplus(world, 2, ctr, { forecasts: f });
    assert.equal(a.lastAction, "save", a.lastError); assert.equal(m.flow.kind, "surplus"); assert.equal(m.flow.status, "complete");
    assert.equal(a.receipts.length, 1); assert.equal(a.mortgage, mortgage);
    assert.equal(await principalOf(a), issued, "saving neither issues nor retires principal");
    assert.ok(await cashOf(a) >= saving.plan.cash_buffer, "the next tranche's dollar front survives");
    assert.equal(cash - await cashOf(a), saving.plan.spend_usdc);
    const after = await buckOf(a);
    assert.ok(after >= saving.plan.reserve_floor - RAW, "one operation protected every prior wallet BUCK");
    // ...but that protection ended with the operation. The lasting front is
    // unchanged, and the other owned BUCK can still be converted to the bank.
    assert.equal(a.netCreditReserve, front); assert.ok(m.flow.plan.reserve_floor > front);
    const done = m.events.filter((e) => e.kind === "surplus" && e.status === "confirmed");
    assert.deepEqual(done.map((e) => e.operation), ["buy", "deposit"]);
    for (const e of done) {
      assert.equal(e.accounts.reserve_floor, String(front));
      assert.deepEqual(e.accounts.surplus_protection, { usdc: String(saving.plan.cash_buffer), buck: String(held) });
    }
    // With no further credit to draw, only owned cash can go to the bank.
    a.p.utilization = programme.insufficient_utilization;
    const block = (await s.client.getBlock()).number;
    const quoted = await a.assessNetRefinance(world, 2, f);
    const frozen = await a.assessNetRefinance(world, 2, { ...f, reserve_floor: m.flow.plan.reserve_floor });
    assert.equal((await s.client.getBlock()).number, block); assert.equal(quoted.context.protected_buck, front);
    // The old cursor default froze all but the save's own sizing cushion.
    assert.equal(frozen.context.protected_buck, held); assert.ok(!frozen.plan || frozen.plan.export_buck < 10n * RAW);
    const plan = quoted.plan;
    assert.ok(plan && plan.candidate === null, show(quoted.context)); assert.equal(plan.reserve_floor, front);
    assert.ok(plan.export_buck > 400n * RAW && plan.export_buck <= after - front);
    assert.equal(plan.switch_cost, 0n); assert.equal(plan.reserve_buy_net, 0n);
    await a.advanceNetRefinancing(world, 2, ctr, { forecasts: f });
    assert.equal(a.refis, 2, a.lastError); assert.notEqual(m.flow.kind, "surplus"); assert.equal(m.flow.status, "complete");
    assert.ok(a.mortgage < mortgage); assert.equal(a.netCreditReserve, front);
    const left = await buckOf(a);
    assert.ok(left >= front && left <= after - plan.export_buck);
    assert.equal(await principalOf(a), issued, "a wallet prepayment issues nothing"); assert.equal(a.receipts.length, 1);
  });

  // ---- shared flow vectors: the faulted attempt, on each household's first decision day ----
  const index = flow.refuse_argument;
  const arm = (x) => {
    const { c, a } = x, m = a.refinanceMachine;
    return async (who, target, name, args, opts) => {
      const phase = m.flow && m.flow.status !== "complete" ? m.flow.phase : "repay";
      x.sends.push([phase, name]);
      const hit = x.fault && [c.lost, c.unknown, c.refused].includes(phase);
      if (hit && c.unknown) { x.fault = false; throw new Error("submission outcome unknown"); }
      if (hit && c.refused) { x.fault = false; args = [...args]; args[index[name]] = 2n ** 200n; } // the contract refuses its own bound
      const receipt = await execute(who, target, name, args, opts);
      if (hit && c.lost) { x.fault = false; throw new TransactionReceiptPending(receipt.transactionHash); }
      return receipt;
    };
  };
  const sent = (x, phase) => x.sends.filter(([p]) => p === phase).length;
  const books = async (a, rid) => show([await cashOf(a), await principalOf(a), a.receipts, [...a.book.entries()],
    rid ? await holding(rid) : null]);
  const attempt = async (x, day) => {
    const { c, a } = x, m = a.refinanceMachine, read = s.client.readContract, plan = world.netCreditOps.planReduction;
    await a.calendarMachine.prepare(world, day);
    x.rid = a.receipts[0] ?? null; x.before = await books(a, x.rid); x.cash = await cashOf(a);
    x.principal = await principalOf(a); x.wallet = await buckOf(a); x.held = x.rid ? await holding(x.rid) : null;
    world.netMarket.execute = arm(x);
    try {
      if (c.view) {
        let failed = false;
        s.client.readContract = (request) => {
          if (!failed && m.flow?.phase === "deposit" && request.functionName === "netAccounting") {
            failed = true; throw new Error("simulated basket read failure");
          }
          return read(request);
        };
      }
      if (c.preview) {
        const previews = [];
        world.netMarket.simulate = async (_who, _target, name) => { previews.push(name); throw new Error("execution reverted: CreditShortfallIncreased"); };
        const quote = await a.assessNetSurplus(world, day);
        assert.deepEqual(previews, ["redeemShares"]);
        assert.equal(quote.context.basket_exits.length, 1); assert.equal(quote.context.basket_exits[0].token_id, x.rid);
        assert.match(quote.context.basket_exits[0].unavailable, /CreditShortfallIncreased/);
        assert.equal(quote.plan.action, "retire"); assert.equal(quote.plan.redeem_shares, 0n); assert.ok(quote.plan.spend_usdc > 0n);
        assert.equal(quote.context.reason, "purchase-to-retire");
      }
      if (c.reduction) {
        let plans = 0;
        world.netCreditOps.planReduction = async (who, tokenId, requested, options) => ++plans === 1
          ? { tokenId: BigInt(tokenId), principal: 0n, reliefScaled: 0n, minimumReliefScaled: 0n, cashScaled: 0n, reason: c.reduction }
          : plan(who, tokenId, requested, options);
      }
      await a.advanceNetSurplus(world, day, x.ctr);
      const phase = c.lost ?? c.unknown ?? c.refused ?? c.view;
      if (c.unknown) {
        // No hash: nothing is booked and nothing is sent again.
        assert.ok(m.active); assert.equal(m.flow.status, "pending"); assert.equal(m.flow.phase, phase); assert.equal(m.flow.hash, null);
        assert.equal(a.lastAction, "surplus-pending"); assert.equal(m.status().kind, "surplus");
        const count = x.sends.length;
        assert.equal(await a.advanceNetSurplus(world, day, x.ctr), true);
        assert.match(a.lastError, /surplus hash unavailable/); assert.equal(x.sends.length, count);
      } else if (c.lost) {
        // The transaction mined; its saved hash is authenticated and applied once.
        assert.ok(m.active); assert.equal(m.flow.status, "pending"); assert.equal(m.flow.phase, phase); assert.ok(m.flow.hash);
        assert.equal(a.lastAction, "surplus-pending"); assert.equal(sent(x, phase), 1);
        const lost = m.flow.hash, count = x.sends.length;
        assert.equal(await principalOf(a), x.principal); assert.deepEqual(a.receipts, x.rid ? [x.rid] : []);
        assert.equal(await a.advanceNetSurplus(world, day, x.ctr), true);
        assert.equal(confirmed(m, phase).at(-1).transaction_hash, lost); assert.equal(sent(x, phase), 1);
        assert.equal(x.sends.length, count + (phase === "buy" ? 1 : 0), "recovery re-sent nothing; a buy then deposits");
      } else if (c.refused || c.view) {
        const refusal = m.events.filter((e) => e.status === "refused").at(-1);
        assert.equal(a.lastAction, "surplus-refused"); assert.equal(m.active, false); assert.equal(m.flow.phase, phase);
        assert.equal(Boolean(refusal.transaction_hash), Boolean(c.refused), "a view failure sent nothing");
        if (c.refused) assert.equal((await s.client.getTransactionReceipt({ hash: refusal.transaction_hash })).status, "reverted");
        assert.equal(m.surplusDue, phase === "deposit"); assert.equal(a.netRepaymentTarget, null);
        if (phase === "deposit") {
          const bought = confirmed(m, "buy");
          assert.equal(bought.length, 1); assert.equal(confirmed(m, "deposit").length, 0); assert.equal(a.receipts.length, 0);
          assert.ok(await buckOf(a) >= x.wallet + BigInt(bought[0].amounts.net_buck_scaled) / Q - RAW);
          assert.equal(await a.advanceNetSurplus(world, day, x.ctr), false, "one attempt a day");
        } else assert.equal(await books(a, x.rid), x.before, "a proved refusal changed nothing");
      }
      if (!c.unknown && !c.refused && !c.view) await settle(a, day);
    } finally {
      world.netMarket.execute = execute; world.netMarket.simulate = simulate;
      s.client.readContract = read; world.netCreditOps.planReduction = plan;
    }
  };
  // The next day: a known refusal never blocks later work or decisions.
  const proceed = async (x, day) => {
    const { c, a } = x, m = a.refinanceMachine, phase = c.refused ?? c.view;
    world.netMarket.execute = arm(x);
    try {
      if (c.unknown) {
        const count = x.sends.length;
        assert.equal(await a.advanceNetSurplus(world, day, x.ctr), true);
        assert.match(a.lastError, /surplus hash unavailable/); assert.equal(x.sends.length, count);
        return;
      }
      if (!phase) return;
      assert.equal(await a.advanceNetSurplus(world, day, x.ctr), true);
      if (phase === "deposit") {
        assert.equal(m.events.filter((e) => e.status === "resumed").length, 1);
        assert.equal(sent(x, "buy"), 1, "the purchased cash is deposited; nothing is bought again");
      }
      await settle(a, day);
    } finally { world.netMarket.execute = execute; }
  };
  const finish = async (x) => {
    const { c, a } = x, m = a.refinanceMachine, retire = c.action === "retire";
    if (c.unknown) {
      const phase = c.unknown;
      assert.ok(m.active); assert.equal(m.flow.phase, phase); assert.equal(m.flow.hash, null);
      assert.equal(a.netRepaymentTarget, null); assert.equal(await principalOf(a), x.principal);
      if (phase === "buy") assert.equal(await books(a, x.rid), x.before);
      else assert.equal(confirmed(m, "deposit").length, retire ? 1 : 0);
      return;
    }
    assert.equal(m.active, false); assert.equal(m.flow.status, "complete", a.lastError); assert.equal(m.status().retirement, null);
    assert.deepEqual(m.events.map((e) => e.seq), m.events.map((_, i) => i + 1));
    const hashes = m.events.filter((e) => e.status === "confirmed").map((e) => e.transaction_hash);
    assert.equal(new Set(hashes).size, hashes.length, "no receipt is applied twice");
    if (!retire) {
      assert.equal(a.lastAction, "save"); assert.equal(a.receipts.length, 1); assert.equal(confirmed(m, "deposit").length, 1);
      assert.equal(sent(x, "buy"), c.refused === "buy" ? 2 : 1); assert.equal(sent(x, "deposit"), c.refused === "deposit" ? 2 : 1);
      const h = await holding(a.receipts[0]), booked = a.book.get(a.receipts[0]);
      assert.deepEqual([booked.principal, booked.shares], [h.basis, h.shares]);
      assert.equal(h.basis, a.basketPrincipal); assert.equal(h.basis, BigInt(confirmed(m, "deposit")[0].amounts.buck));
      assert.ok(await cashOf(a) >= a.buffer); assert.equal(await principalOf(a), 0n);
      return;
    }
    const reductions = m.events.filter((e) => e.kind === "retirement" && e.status === "confirmed");
    assert.equal(reductions.reduce((sum, e) => sum + BigInt(e.amounts.principal), 0n), x.principal);
    assert.equal(a.unwound, x.principal); assert.equal(await principalOf(a), 0n);
    assert.ok(await buckOf(a) >= x.wallet - RAW, "every prior wallet BUCK survives");
    const redeemed = confirmed(m, "redeem");
    if (c.preview) {
      assert.equal(redeemed.length, 0); assert.equal(sent(x, "redeem"), 0); assert.deepEqual(await holding(x.rid), x.held);
      assert.ok(await cashOf(a) < x.cash, "the retirement was bought, never an unguarded exit");
    } else {
      assert.equal(redeemed.length, 1); assert.equal(sent(x, "redeem"), c.refused ? 2 : 1);
      assert.equal((await holding(x.rid)).shares, x.held.shares - BigInt(redeemed[0].amounts.shares_retired));
      assert.equal(await cashOf(a), x.cash);
    }
    if (c.reduction) {
      const waits = m.events.filter((e) => e.kind === "retirement" && e.status === "waiting");
      assert.equal(waits.length, 1); assert.ok(waits[0].reason.includes(c.reduction));
      // The exit's cash stayed owned; the target and its protected reserve waited.
      assert.equal(waits[0].accounts.issued, String(x.principal)); assert.ok(waits[0].seq < reductions[0].seq);
      assert.equal(waits[0].retirement.target, String(x.principal)); assert.ok(BigInt(waits[0].retirement.reserve) > x.wallet - RAW);
    }
  };

  const saves = cases.filter((x) => x.c.action === "save"), retires = cases.filter((x) => x.c.action === "retire");
  const supply = await supplyOf();
  for (const x of saves) await t.test(`${x.c.name} (attempt)`, () => attempt(x, 2));
  await t.test("retiring households and the basket arm first save real cash in place", async () => {
    for (const a of [...retires.map((x) => x.a), arms.basket]) {
      await a.calendarMachine.prepare(world, 2);
      const quote = await a.assessNetSurplus(world, 2);
      assert.equal(quote.plan.action, "save"); assert.equal(quote.plan.cash_buffer, a.buffer);
      await a.advanceNetSurplus(world, 2, {});
      assert.equal(a.lastAction, "save", a.lastError); assert.equal(a.receipts.length, 1); assert.equal(a.savings ?? null, null);
      assert.ok(await buckOf(a) >= quote.plan.reserve_floor - RAW); assert.ok(await cashOf(a) >= a.buffer);
    }
    assert.equal(await supplyOf(), supply, "buying and depositing existing BUCK issues nothing");
  });

  await at(3);
  for (const x of saves) await t.test(`${x.c.name} (next day)`, async () => { await proceed(x, 3); await finish(x); });
  let shares, basis, rid;
  await t.test("a second saving tops up the same receipt", async () => {
    const a = arms.basket;
    await a.advanceNetSurplus(world, 3, {});
    assert.equal(a.lastAction, "save", a.lastError); assert.equal(a.receipts.length, 1);
    rid = a.receipts[0]; ({ shares, basis } = await holding(rid));
    assert.equal(shares, a.shares); assert.equal(basis, a.basketPrincipal); assert.equal(basis, a.book.get(rid).principal);
    assert.equal(confirmed(a.refinanceMachine, "deposit").length, 2);
    assert.equal(await supplyOf(), supply);
  });
  const borrowers = [...retires.map((x) => x.a), arms.cash, arms.basket], owed = new Map();
  for (const a of borrowers) owed.set(a, await issueCredit(a));
  for (const a of borrowers) a.p.surplus_policy = "adaptive";
  const beforeSale = await market();
  await sellInto(world.poolUB);

  await at(4);
  const bvib = await s.call(basket, "basketValueInBuck");
  await t.test("one dislocated route is not a discount in basket purchasing power", async () => {
    // The dollar pool alone is far cheaper, but fair value is the basket's
    // BUCK price times that same pool. Route arbitrage owns that repair.
    const spot = await market();
    assert.ok(spot.price_num * beforeSale.price_den * 100n < 92n * beforeSale.price_num * spot.price_den);
    for (const [name, a] of Object.entries(arms)) {
      await a.calendarMachine.prepare(world, 4);
      const lone = await a.assessNetSurplus(world, 4);
      assert.equal(lone.context.choice.action, "save"); assert.equal(lone.context.choice.target_principal, 0n);
      const ratio = lone.context.choice.marginal_ratio_wad - 10n ** 18n;
      assert.ok(ratio < 2n * 10n ** 15n && ratio > -2n * 10n ** 15n, show(lone.context));
      assert.equal(lone.plan === null, name === "basket" && await cashOf(a) === a.buffer);
    }
  });
  // The same holder sells the same share of each TOKEN pool: BUCK now buys
  // less of the basket itself, and its TWAP carries that into fair value.
  for (const token of world.tokens) await sellInto(token.poolBuck);

  await at(5);
  assert.ok(await s.call(basket, "basketValueInBuck") * 100n > bvib * 108n);
  for (const x of retires) await t.test(`${x.c.name} (attempt)`, () => attempt(x, 5));
  for (const [name, a] of Object.entries(arms)) await t.test(`${name} arm retires its actual discounted liability`, async () => {
    const principal = owed.get(a), m = a.refinanceMachine;
    await a.calendarMachine.prepare(world, 5);
    const block = (await s.client.getBlock()).number, quote = await a.assessNetSurplus(world, 5);
    assert.equal((await s.client.getBlock()).number, block);
    assert.ok(quote.plan && quote.plan.action === "retire", show(quote.context));
    assert.ok(quote.context.choice.marginal_ratio_wad < 93n * 10n ** 16n);
    // A receipt is redeemed first; without one the retirement is bought.
    assert.equal(Boolean(quote.plan.redeem_shares), a.receipts.length > 0);
    if (name === "basket") {
      assert.ok(quote.plan.redeem_shares < shares); assert.ok(quote.context.exit.paid >= quote.plan.minimum_buck);
      assert.equal(await holding(rid).then((h) => h.shares), shares, "the preview retired nothing");
    }
    const issuedSupply = await supplyOf();
    await a.advanceNetSurplus(world, 5, {});
    assert.notEqual(a.lastAction, "surplus-unavailable", a.lastError);
    await settle(a, 5);
    assert.equal(await principalOf(a), 0n, a.lastError); assert.equal(a.unwound, principal);
    assert.equal(await supplyOf(), issuedSupply - principal, "only the confirmed reductions extinguish supply");
    assert.ok(await buckOf(a) >= quote.plan.reserve_floor - RAW);
    assert.equal(a.mortgage, 0n); assert.equal(a.externalPaid, 0n);
    await retirementHistory(a, principal, quote.plan);
    if (name === "basket") {
      const redeemed = confirmed(m, "redeem");
      assert.equal(redeemed.length, 1); assert.equal(a.receipts.length, 1);
      assert.ok(a.basketPrincipal > 0n && a.basketPrincipal < basis && a.shares > 0n && a.shares < shares);
      const h = await holding(rid);
      assert.deepEqual([a.book.get(rid).principal, a.book.get(rid).shares], [h.basis, h.shares]);
      const paid = BigInt(redeemed[0].amounts.buck), preview = quote.context.exit.paid;
      assert.ok(paid >= quote.plan.minimum_buck && (paid > preview ? paid - preview : preview - paid) * 10_000n <= preview,
        "the executed exit matches its preview within one basis point");
    }
  });

  await at(6);
  for (const x of retires) await t.test(`${x.c.name} (next day)`, async () => { await proceed(x, 6); await finish(x); });

  await t.test("basket operations authenticate top-ups, previews and partial net redemptions", async () => {
    const a = sdk, me = owner(a);
    const ops = netBasketOps(s, basket, buck, { execute, simulate });
    await proxyExec(world.simlp, buck, "transfer", [me, 200n * RAW]);
    const before = await supplyOf();
    const minimum = async (amount) => { const m = amount * 10n ** 18n / await s.call(basket, "sharePrice") * 9900n / 10000n; return m > 1n ? m : 1n; };
    const firstPlan = await ops.prepareDeposit(me, 20n * RAW, await minimum(20n * RAW));
    const first = await ops.deposit(firstPlan);
    assert.equal(first.received, 20n * RAW); assert.equal(first.basis, 20n * RAW); assert.equal(first.sharesAdded, first.shares);
    const again = await ops.depositFromReceipt(firstPlan, first.receipt, first.receipt.transactionHash);
    assert.equal(show({ ...again, receipt: null }), show({ ...first, receipt: null }), "authenticating evidence books nothing twice");
    await assert.rejects(ops.depositFromReceipt(firstPlan, first.receipt, `0x${"00".repeat(32)}`), /hash/);
    await assert.rejects(ops.prepareDeposit(owner(stranger), RAW, 1n, { tokenId: first.tokenId }), /foreign/);
    await assert.rejects(ops.prepareDeposit(me, RAW, 0n), /positive/);
    const topup = await ops.prepareDeposit(me, 10n * RAW, await minimum(10n * RAW), { tokenId: first.tokenId });
    const lossy = netBasketOps(s, basket, buck, { simulate, execute: async (...args) => {
      const receipt = await execute(...args); lossy.sent.push(receipt); throw new TransactionReceiptPending(receipt.transactionHash);
    } });
    lossy.sent = [];
    const pending = await lossy.deposit(topup).then(() => null, (err) => err);
    assert.ok(pending instanceof TransactionReceiptPending && pending.transactionHash);
    const updated = await ops.depositFromReceipt(topup, lossy.sent[0], pending.transactionHash);
    assert.equal(updated.tokenId, first.tokenId); assert.equal(updated.basis, 30n * RAW);
    assert.equal(updated.shares, first.shares + updated.sharesAdded); assert.equal(await supplyOf(), before);
    await assert.rejects(ops.deposit({ ...topup, priorShares: updated.shares, priorBasis: updated.basis, minimumShares: 2n ** 120n }),
      (err) => err instanceof TransactionReverted);
    assert.deepEqual(await holding(first.tokenId), { shares: updated.shares, basis: updated.basis });
    const part = updated.shares / 4n, worth = await s.call(basket, "valueOf", [first.tokenId]) * part / updated.shares;
    const redemption = await ops.prepareRedemption(me, first.tokenId, part, worth * 9900n / 10000n);
    const observed = async () => show([(await s.client.getBlock()).number, await holding(first.tokenId), await s.call(basket, "totalShares"),
      await s.call(buck, "netBalanceScaled", [me]), await s.call(buck, "netBalanceScaled", [basket.address]), await supplyOf(),
      await s.call(world.kctrl, "currentBuckK")]);
    const state = await observed();
    const preview = await ops.previewRedemption(me, first.tokenId, part);
    assert.ok(preview.paid >= redemption.minimumBuck); assert.equal(preview.sharesRetired, part);
    assert.equal(preview.positionId, first.tokenId); assert.equal(preview.sharesRemaining, updated.shares - part);
    assert.equal(await observed(), state, "a simulated exit changes neither holdings, cash, K nor chain height");
    await assert.rejects(ops.previewRedemption(owner(stranger), first.tokenId, part), /foreign/);
    await assert.rejects(ops.previewRedemption(me, first.tokenId, updated.shares + 1n), /exceeds/);
    const lost = await lossy.redeem(redemption).then(() => null, (err) => err);
    assert.ok(lost instanceof TransactionReceiptPending && lost.transactionHash);
    const reduced = await ops.redemptionFromReceipt(redemption, lossy.sent.at(-1), lost.transactionHash);
    assert.ok(reduced.paid >= redemption.minimumBuck); assert.equal(reduced.sharesRetired, part);
    assert.equal(reduced.sharesRemaining, updated.shares - part);
    assert.ok(reduced.basisRemaining > 0n && reduced.basisRemaining < updated.basis); assert.deepEqual(reduced.inKind, []);
    assert.ok((reduced.paid > preview.paid ? reduced.paid - preview.paid : preview.paid - reduced.paid) * 10_000n <= preview.paid);
    const nominal = parseEventLogs({ abi: buck.abi, logs: reduced.receipt.logs, eventName: "NetValueTransfer" })
      .filter((e) => e.args.to.toLowerCase() === me.toLowerCase()).reduce((sum, e) => sum + e.args.nominal, 0n);
    assert.ok(nominal > reduced.paid, "an aged Carrying basket grosses up the actual net cash payout");
    assert.equal(await supplyOf(), before); assert.equal(await principalOf(a), 0n);
  });

  await t.test("an unknown outcome also holds the next payday's income", async () => {
    await at(30);
    for (const x of cases.filter((y) => y.c.unknown)) {
      const count = x.sends.length;
      world.netMarket.execute = arm(x);
      try { assert.equal(await x.a.advanceNetSurplus(world, 30, x.ctr), true); }
      finally { world.netMarket.execute = execute; }
      assert.match(x.a.lastError, /surplus hash unavailable/); assert.equal(x.sends.length, count);
      assert.equal(x.a.incomeTotal, 0n); assert.equal(x.a.refinanceMachine.flow.phase, x.c.unknown);
    }
  });
});
