import { test } from "node:test";
import assert from "node:assert/strict";
import { CreditHouseholdAgent } from "../src/agents/credit-household.js";
import { TransactionReceiptPending, TransactionReverted } from "../src/session.js";

test("unknown buyback target stops the household before a new action", async () => {
  const agent = new CreditHouseholdAgent({ account: { address: "0x" + "11".repeat(20) } });
  agent.netRepaymentReserve = 1n;
  agent.netRepaymentTarget = null;
  const world = { accounting: "net", households: { pending: async () => false, retire: async () => {
    throw new Error("must not retire without saved terms");
  } } };
  await agent.act(world, 0, 0);
  assert.equal(agent.lastAction, "repay-pending");
  assert.match(agent.lastError, /target or reserve unavailable/);
  assert.equal(agent.netRepaymentReserve, 1n);
  assert.equal(agent.started, false);
  await assert.rejects(agent.retireAcquired(world, 25n, 0n), /target unavailable/);
  agent.netRepaymentReserve = null;
  agent.netRepaymentTarget = 25n;
  await agent.act(world, 0, 0);
  assert.equal(agent.lastAction, "repay-pending");
  assert.equal(agent.netRepaymentTarget, 25n);
  assert.equal(agent.started, false);
  await assert.rejects(agent.retireAcquired(world, 25n, 0n), /reserve unavailable/);
});

test("daily turns finish only the saved buyback target", async () => {
  const agent = new CreditHouseholdAgent({ account: { address: "0x" + "11".repeat(20) } });
  agent.netRepaymentReserve = 1n;
  agent.netRepaymentTarget = 25n;
  const requested = [];
  const world = { accounting: "net", households: {
    pending: async () => false,
    read: async () => ({ drawn: 25n }),
    retire: async (_account, target) => {
      requested.push(target);
      const retired = target < 10n ? target : 10n;
      return { retired, status: "partial", reserve: retired === target ? null : 1n };
    },
  } };
  for (let i = 0; i < 3; i++) await agent.act(world, 0, 0);
  assert.deepEqual(requested, [25n, 15n, 5n]);
  assert.equal(agent.unwound, 25n);
  assert.equal(agent.netRepaymentReserve, null);
  assert.equal(agent.netRepaymentTarget, null);
  assert.equal(agent.started, false);
  agent.netRepaymentReserve = 1n; agent.netRepaymentTarget = 25n;
  world.households.read = async () => ({ drawn: 0n });
  await agent.act(world, 0, 0);
  assert.deepEqual(requested, [25n, 15n, 5n]);
  assert.equal(agent.netRepaymentReserve, null);
  assert.equal(agent.netRepaymentTarget, null);
});

test("revert without hash keeps buyback terms and stops retry", async () => {
  const agent = new CreditHouseholdAgent({ account: { address: "0x" + "11".repeat(20) } });
  const world = { accounting: "net", households: {
    balance: async () => 10n,
    buy: async () => { throw new TransactionReverted("buy", "missing hash", { status: "reverted" }); },
  } };
  await assert.rejects(agent.buyForRepayment(world, 50n, 1n, 25n),
    (err) => err instanceof TransactionReceiptPending && !err.transactionHash);
  assert.equal(agent.netTradeUncertain, true);
  assert.equal(agent.netRepaymentReserve, 11n);
  assert.equal(agent.netRepaymentTarget, 25n);

  for (const result of [null, { spent: 51n, got: 25n }, { spent: 50n, got: 0n }]) {
    agent.netRepaymentReserve = null; agent.netRepaymentTarget = null;
    world.households.buy = async () => result;
    await assert.rejects(agent.buyForRepayment(world, 50n, 1n, 25n),
      (err) => err instanceof TransactionReceiptPending && !err.transactionHash);
    assert.equal(agent.netTradeUncertain, true);
    assert.equal(agent.netRepaymentTarget, 25n);
  }
});
