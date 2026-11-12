// The household's BUCK working balance on the existing canonical builder: real
// dollars buy net BUCK into the wallet's own Ordinary account after a payday,
// and an excess is sold from that custody for dollars delivered home. The
// same chronology and fault vectors run in Python (test_net_household_setup.py).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { encodeFunctionData } from "viem";
import { tevmSession } from "../src/backends.js";
import { buildEquilibriumWorld } from "../src/scenarios/eqworld.js";
import { CreditSaverAgent } from "../src/agents/credit-household.js";
import { moneyTarget, netWalletTarget } from "../src/agents/household-policy.js";
import { TransactionReceiptPending } from "../src/session.js";
import { observeNetMarket } from "../src/net-market-quote.js";
import { advanceTime, DAY } from "../src/buckworld.js";
import { artifact } from "../artifacts/bundle.mjs";
import * as identity from "../src/identity.js";

const RAW = 1_000_000n, Q = 10n ** 36n;
const fixture = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-wallet.json", import.meta.url)));
const life = fixture.lifecycle, flow = fixture.flow;

test("households keep a BUCK working balance in its own custody and tend it after each payday", async (t) => {
  const s = await tevmSession();
  const world = await buildEquilibriumWorld(s, artifact, { identity, wheelArb: false, targetBuck: 1_000_000n * RAW,
    urArtifact: JSON.parse(readFileSync(new URL("../../../alberta_buck/sim/artifacts/UniversalRouter.json", import.meta.url))),
  });
  const { buck, usdc } = world, execute = world.netMarket.execute;
  const owner = (a) => world.holderAddress(a.account);
  let serial = 0;
  const saver = async (name) => {
    const a = new CreditSaverAgent({ account: { address: `0x${(0xabf300n + BigInt(serial++)).toString(16).padStart(40, "0")}` },
      member: name, settings: life.settings });
    await a.setup(world); await world.households.openTrading(a.account);
    return a;
  };
  const w = await saver("wallet-life");
  const cases = [];
  for (const [i, c] of flow.cases.entries()) cases.push({ c, a: await saver(`wallet-flow-${i}`), ctr: {}, sends: [], fault: true });
  world.householdEpoch = (await s.client.getBlock()).timestamp;
  const at = async (day) => {
    await advanceTime(world, world.householdEpoch + BigInt(day * DAY) - (await s.client.getBlock()).timestamp);
    await s.send(world.kctrl, "compute");
  };
  for (const a of [w, ...cases.map((x) => x.a)]) await a.advanceNetCalendar(world, 0);
  const cashOf = (a) => s.call(usdc, "balanceOf", [owner(a)]);
  const homeScaled = (a) => s.call(buck, "netBalanceScaled", [owner(a)]);
  const walletOf = async (a) => a.wallet ? s.call(buck, "balanceOf", [a.wallet.address]) : 0n;
  const supplyOf = () => s.call(buck, "totalIssued");
  const events = (a, status, operation = null) => a.refinanceMachine.events.filter((e) => e.kind === "wallet" && e.status === status
    && (operation === null || e.operation === operation));
  const gift = (a) => s.send(world.simlp, "exec", [buck.address, encodeFunctionData({ abi: buck.abi, functionName: "transfer",
    args: [a.wallet.address, BigInt(life.gift_buck)] })]);
  // The assessment's own recorded cost/fair ratio sizes the target.
  const wanted = (a, day) => netWalletTarget(BigInt(events(a, "planned").at(-1).context.marginal_ratio_wad), day,
    Number(a.incomeAnnual) * a.p.living_frac / 1e6, a.p);
  const supply = await supplyOf(), m = w.refinanceMachine, ctr = {};

  const arm = (x) => {
    const { c, a } = x, machine = a.refinanceMachine;
    return async (who, target, name, args, opts) => {
      const f = machine.flow, phase = f?.kind === "wallet" && f.status !== "complete" ? f.phase : "other";
      if (["buy", "sell"].includes(phase)) x.sends.push([phase, name]);
      const hit = x.fault && [c.lost, c.unknown, c.refused].includes(phase);
      if (hit && c.unknown) { x.fault = false; throw new Error("submission outcome unknown"); }
      if (hit && c.refused) { x.fault = false; args = [...args]; args[flow.refuse_argument[name]] = 2n ** 200n; }
      const receipt = await execute(who, target, name, args, opts);
      if (hit && c.lost) { x.fault = false; throw new TransactionReceiptPending(receipt.transactionHash); }
      return receipt;
    };
  };
  const guarded = async (x, run) => {
    world.netMarket.execute = arm(x);
    try { return await run(); } finally { world.netMarket.execute = execute; }
  };
  const [lost, refused, unknown] = ["lost", "refused", "unknown"].map((key) => cases.find((x) => x.c[key]));

  await t.test("nothing is tended before a payday", async () => {
    assert.equal(await w.advanceNetWallet(world, 0, ctr), false); assert.equal(w.wallet ?? null, null);
    assert.equal(m.status().next_wallet_day, 30);
  });

  await at(30);
  let target, received, cashBefore, firstSpend;
  await t.test("the first payday buys the working balance into the wallet's own account", async () => {
    assert.equal(await w.advanceNetWallet(world, 30, ctr), true); assert.equal(w.lastAction, "wallet-buy", w.lastError);
    assert.equal(w.incomeTotal, w.incomeAnnual / 12n); assert.ok(w.livingPaid > 0n, "the payday came first");
    const plan = m.flow.plan; target = wanted(w, 30);
    assert.ok(target >= BigInt(Math.trunc(moneyTarget(30, Number(w.incomeAnnual) * w.p.living_frac / 1e6, 0, w.p) * 1e6)) && target > 0n);
    assert.equal(plan.action, "buy"); assert.equal(plan.target, target); assert.equal(plan.held, 0n); assert.equal(plan.cash_buffer, w.buffer);
    assert.ok(await s.call(world.reg, "isVerified", [w.wallet.address]) && !await s.call(world.reg, "isCarrying", [w.wallet.address]));
    assert.notEqual(w.wallet.address.toLowerCase(), owner(w).toLowerCase()); assert.equal(plan.wallet, w.wallet.address);
    const bought = events(w, "confirmed");
    assert.deepEqual(bought.map((e) => e.operation), ["buy"]); assert.equal(m.events.at(-1).outcome, "wallet-buy");
    received = BigInt(bought[0].amounts.net_buck_scaled) / Q;
    assert.ok(received >= plan.minimum_net && received <= target + RAW); assert.equal(received, await walletOf(w));
    assert.equal(w.walletBought, received); assert.ok(received * 100n >= target * 99n, "the shortfall was bought, less only impact and fees");
    const planned = events(w, "planned")[0];
    cashBefore = BigInt(planned.accounts.usdc); firstSpend = BigInt(bought[0].amounts.usdc);
    assert.equal(cashBefore - await cashOf(w), firstSpend); assert.ok(firstSpend <= plan.spend_usdc);
    assert.ok(await cashOf(w) >= w.buffer); assert.equal(String(await homeScaled(w)), planned.accounts.buck_scaled, "the main BUCK is untouched");
    assert.equal(bought[0].accounts.custody.wallet.buck_scaled, bought[0].amounts.net_buck_scaled);
    assert.equal(await supplyOf(), supply);
    assert.equal(m.status().kind, "wallet"); assert.equal(m.status().next_wallet_day, 60);
    const block = (await s.client.getBlock()).number;
    assert.equal(await w.advanceNetWallet(world, 30, ctr), false); assert.equal((await s.client.getBlock()).number, block, "once a pay period");
  });
  await t.test(lost.c.name, async () => {
    const { a } = lost, machine = a.refinanceMachine;
    await guarded(lost, async () => {
      assert.equal(await a.advanceNetWallet(world, 30, lost.ctr), true);
      assert.ok(machine.active); assert.equal(machine.flow.status, "pending"); assert.ok(machine.flow.hash); assert.equal(a.lastAction, "wallet-pending");
      assert.equal(a.walletBought, 0n, "nothing is booked from an unauthenticated reply");
      const hash = machine.flow.hash;
      assert.equal(await a.advanceNetWallet(world, 30, lost.ctr), true);
      assert.equal(events(a, "confirmed").at(-1).transaction_hash, hash); assert.equal(lost.sends.length, 1);
    });
    assert.equal(machine.flow.status, "complete"); assert.equal(a.lastAction, "wallet-buy");
    assert.equal(a.walletBought, await walletOf(a)); assert.ok(a.walletBought > 0n);
    assert.equal(a.walletBought, BigInt(events(a, "confirmed").at(-1).amounts.net_buck_scaled) / Q);
  });
  await t.test(`${refused.c.name} (attempt)`, async () => {
    const { a } = refused, machine = a.refinanceMachine, before = await supplyOf();
    await guarded(refused, async () => {
      assert.equal(await a.advanceNetWallet(world, 30, refused.ctr), true);
      const refusal = machine.events.filter((e) => e.status === "refused").at(-1);
      assert.equal(a.lastAction, "wallet-refused"); assert.equal(machine.active, false);
      assert.equal(machine.surplusDue, false); assert.equal(machine.settlementDue, false);
      assert.equal((await s.client.getTransactionReceipt({ hash: refusal.transaction_hash })).status, "reverted");
      assert.equal(await walletOf(a), 0n); assert.equal(a.walletBought, 0n); assert.ok(a.wallet, "the empty account stays");
      assert.equal(await a.advanceNetWallet(world, 30, refused.ctr), false); assert.equal(machine.status().next_wallet_day, 60);
    });
    assert.equal(await supplyOf(), before, "a proved refusal moved no money");
  });
  await t.test("a household builds the balance its later excess will leave", async () => {
    const { a } = unknown;
    assert.equal(await a.advanceNetWallet(world, 30, unknown.ctr), true); assert.equal(a.lastAction, "wallet-buy", a.lastError);
    await gift(a);
  });

  await at(60);
  await t.test("the second payday buys only the ramp and a month's decay", async () => {
    const held = await walletOf(w), address = w.wallet.address;
    assert.ok(held < received, "wallet money carries demurrage like any other");
    assert.equal(await w.advanceNetWallet(world, 60, ctr), true); assert.equal(w.lastAction, "wallet-buy", w.lastError);
    const plan = m.flow.plan, spending = Number(w.incomeAnnual) * w.p.living_frac / 1e6;
    // Tevm's clock moves with each payday transaction, so the assessed balance is a few units older.
    assert.equal(plan.target, wanted(w, 60)); assert.ok(plan.held <= held && held - plan.held < RAW);
    assert.ok(moneyTarget(60, spending, 0, w.p) > moneyTarget(30, spending, 0, w.p), "adoption ramps");
    assert.equal(w.wallet.address, address);
    const added = await walletOf(w) - held;
    assert.ok(added > 0n && added <= plan.target - held + RAW);
    assert.ok(plan.spend_usdc * 5n < firstSpend, "a top-up, not the balance again");
    await gift(w);
  });
  await t.test(`${refused.c.name} (next payday)`, async () => {
    const { a } = refused;
    // A known refusal blocks nothing: the next payday tends the balance afresh.
    await guarded(refused, async () => { assert.equal(await a.advanceNetWallet(world, 60, refused.ctr), true); });
    assert.equal(a.lastAction, "wallet-buy", a.lastError); assert.equal(refused.sends.length, 2);
    assert.equal(a.walletBought, await walletOf(a)); assert.ok(a.walletBought > 0n);
    assert.equal(a.incomeTotal, 2n * (a.incomeAnnual / 12n));
  });
  let unknownBooks;
  await t.test(`${unknown.c.name} (attempt)`, async () => {
    const { a } = unknown, machine = a.refinanceMachine;
    await a.calendarMachine.prepare(world, 60);
    unknownBooks = [a.walletBought, a.walletSold, await walletOf(a)];
    await guarded(unknown, async () => {
      assert.equal(await a.advanceNetWallet(world, 60, unknown.ctr), true);
      assert.equal(machine.flow.kind, "wallet"); assert.equal(machine.flow.phase, "sell");
      assert.ok(machine.active); assert.equal(machine.flow.status, "pending"); assert.equal(machine.flow.hash, null);
      assert.equal(a.lastAction, "wallet-pending");
      assert.equal(await a.advanceNetWallet(world, 60, unknown.ctr), true); assert.match(a.lastError, /wallet hash unavailable/);
    });
    assert.equal(unknown.sends.length, 1);
    assert.deepEqual([a.walletBought, a.walletSold], unknownBooks.slice(0, 2)); assert.ok(await walletOf(a) <= unknownBooks[2]);
  });

  await at(61);
  await t.test(`${unknown.c.name} (next day)`, async () => {
    const { a } = unknown;
    await guarded(unknown, async () => { assert.equal(await a.advanceNetWallet(world, 61, unknown.ctr), true); });
    assert.match(a.lastError, /wallet hash unavailable/); assert.equal(unknown.sends.length, 1);
    assert.deepEqual([a.walletBought, a.walletSold], unknownBooks.slice(0, 2));
  });

  await at(90);
  await t.test("an excess is sold from the wallet's own custody for dollars delivered home", async () => {
    await w.calendarMachine.prepare(world, 90);
    const held = await walletOf(w), soldBefore = w.walletSold, cash = await cashOf(w), home = await homeScaled(w);
    const q = await observeNetMarket(s, buck, usdc, world.poolUB, world.fees.ub, { blockNumber: (await s.client.getBlock()).number });
    assert.equal(await w.advanceNetWallet(world, 90, ctr), true); assert.equal(w.lastAction, "wallet-sell", w.lastError);
    const plan = m.flow.plan, cap = q.reserve_buck * 5000n / 1_000_000n, excess = held - plan.target;
    assert.equal(plan.action, "sell"); assert.equal(plan.sell_buck, excess < cap ? excess : cap);
    const sold = events(w, "confirmed", "sell");
    assert.equal(sold.length, 1); assert.equal(BigInt(sold[0].amounts.buck), plan.sell_buck);
    assert.equal(w.walletSold - soldBefore, plan.sell_buck);
    const left = held - await walletOf(w);
    assert.ok(left >= plan.sell_buck && left <= plan.sell_buck + RAW);
    const dollars = BigInt(sold[0].amounts.usdc);
    assert.ok(dollars >= plan.minimum_usdc);
    assert.ok(BigInt(sold[0].accounts.buck_scaled) <= home, "selling the wallet's BUCK never touches the main account's");
    assert.ok(BigInt(sold[0].accounts.usdc) >= cash + dollars - 1n, "the dollars came home (wages and bills aside)");
    assert.equal(await supplyOf(), supply);
    assert.deepEqual(m.events.map((e) => e.seq), m.events.map((_, i) => i + 1));
  });
});
