import { test } from "node:test";
import assert from "node:assert/strict";
import { attachNetMarket, buyBuck, netSaleFromReceipt, sellBuck } from "../src/market.js";
import { TransactionReceiptPending, TransactionReverted } from "../src/session.js";

test("net market restore refuses missing allowance targets or code evidence", async () => {
  const record = { accounting: "net", addresses: { guard: "0x1", router: "0x2", permit2: "0x3" },
    guardCodeHash: "0xaa", routerCodeHash: "0xbb", permit2CodeHash: "0xcc" };
  for (const name of ["router", "permit2"]) {
    const missingAddress = structuredClone(record);
    delete missingAddress.addresses[name];
    await assert.rejects(attachNetMarket(null, null, missingAddress, { accounting: "net" }), /incomplete net market record/);
    const missingHash = structuredClone(record);
    delete missingHash[`${name}CodeHash`];
    await assert.rejects(attachNetMarket(null, null, missingHash, { accounting: "net" }), /incomplete net market record/);
  }
});

test("net purchase requires a transaction hash to establish success or revert", async () => {
  const addr = (byte) => `0x${byte.repeat(40)}`;
  const account = { address: addr("1") };
  const market = { pool: { address: addr("2") }, guard: { address: addr("3") }, usdc: { address: addr("4") } };
  const world = { accounting: "net", buck: { address: addr("5") }, session: {} };
  for (const send of [
    async () => { throw new Error("dispatch failed"); },
    async () => { throw new TransactionReverted("buy", "unknown", { status: "reverted" }); },
    async () => ({ status: "reverted" }),
    async () => ({ status: "success", logs: [] }),
    async () => null,
  ]) {
    world.session.send = send;
    await assert.rejects(buyBuck(world, market, account, 100n, { minOut: 1n }),
      (err) => err instanceof TransactionReceiptPending && !err.transactionHash);
  }
  const hash = `0x${"11".repeat(32)}`;
  for (const send of [
    async () => { throw new TransactionReverted("buy", "known", { status: "reverted", transactionHash: hash }); },
    async () => ({ status: "reverted", transactionHash: hash }),
  ]) {
    world.session.send = send;
    await assert.rejects(buyBuck(world, market, account, 100n, { minOut: 1n }),
      (err) => err instanceof TransactionReverted && err.transactionHash === hash);
  }
});

test("net sale distinguishes missing dispatch evidence from a mined revert", async () => {
  const addr = (byte) => `0x${byte.repeat(40)}`;
  const account = { address: addr("1") };
  const market = { pool: { address: addr("2") }, router: { address: addr("3") }, v3f: {},
    usdc: { address: addr("4") }, fee: 500 };
  let calls = 0;
  const world = { accounting: "net", buck: { address: addr("5") }, reg: {}, session: {
    call: async (_contract, name) => name === "getPool" ? market.pool.address : true,
    send: async () => { calls++; throw new Error("dispatch outcome unavailable"); },
  } };
  const receipt = { status: "success", transactionHash: `0x${"11".repeat(32)}`, logs: [] };
  assert.throws(() => netSaleFromReceipt(world, market, account, receipt), /hash mismatch/);
  assert.throws(() => netSaleFromReceipt(world, market, account, receipt,
    { expectedHash: receipt.transactionHash, expectedInput: 100n, minimumOut: 0n }), /invalid net sale bounds/);
  await assert.rejects(sellBuck(world, market, account, 100n), /minimum output/);
  assert.equal(calls, 0);
  await assert.rejects(sellBuck(world, market, { address: market.pool.address }, 100n, { minOut: 1n }), /recipient/);
  assert.equal(calls, 0);
  world.session.call = async (_contract, name) => name === "getPool" ? addr("6") : true;
  await assert.rejects(sellBuck(world, market, account, 100n, { minOut: 1n }), /not canonical/);
  world.session.call = async (_contract, name) => name === "getPool" ? market.pool.address : name === "isVerified";
  await assert.rejects(sellBuck(world, market, account, 100n, { minOut: 1n }), /verified and Carrying/);
  assert.equal(calls, 0);
  world.session.call = async (_contract, name) => name === "getPool" ? market.pool.address : true;
  await assert.rejects(sellBuck(world, market, account, 100n, { minOut: 1n }),
    (err) => err instanceof TransactionReceiptPending && !err.transactionHash);
  assert.equal(calls, 1);
  world.session.send = async () => { throw new TransactionReverted("execute", "missing hash", { status: "reverted" }); };
  await assert.rejects(sellBuck(world, market, account, 100n, { minOut: 1n }),
    (err) => err instanceof TransactionReceiptPending && !err.transactionHash);
  world.session.send = async () => ({ status: "reverted", transactionHash: `0x${"11".repeat(32)}` });
  await assert.rejects(sellBuck(world, market, account, 100n, { minOut: 1n }),
    (err) => err instanceof TransactionReverted && Boolean(err.transactionHash));
});
