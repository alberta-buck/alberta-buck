// Standard simulated bank payments, including real receipt recovery on Tevm.
import { test } from "node:test";
import assert from "node:assert/strict";
import { encodeFunctionData } from "viem";
import { devAccount, tevmSession } from "../src/backends.js";
import { artifactsAvailable, loadArtifact } from "../src/nodefs.js";
import { settlementOps } from "../src/settlement.js";
import { TransactionReceiptPending, TransactionReverted } from "../src/session.js";

test("settlement payment requires exact receipt evidence", { skip: !artifactsAvailable() }, async () => {
  const s = await tevmSession(), payer = s.account, bank = devAccount(1), stranger = devAccount(2);
  const token = await s.deploy(loadArtifact("MockERC20"), ["USD Coin", "USDC", 6]);
  await s.send(token, "mint", [payer.address, 1000n]);
  const ops = settlementOps(s, token);
  for (const [recipient, amount] of [[bank.address, 0n], [payer.address, 1n],
    ["0x0000000000000000000000000000000000000000", 1n]]) {
    await assert.rejects(ops.payExact(payer, recipient, amount), /terms/);
  }
  await assert.rejects(ops.payExact(payer, bank.address, 1001n), TransactionReverted);
  assert.equal(await s.call(token, "balanceOf", [bank.address]), 0n);
  assert.equal(s.mismatches.length, 0);
  const unknown = settlementOps(s, token, { execute: async () => { throw new Error("transport interrupted"); } });
  await assert.rejects(unknown.payExact(payer, bank.address, 300n),
    (err) => err instanceof TransactionReceiptPending && !err.transactionHash);
  const lost = settlementOps(s, token, { execute: async (owner, contract, method, args) => ({
    ...(await s.send(contract, method, args, { account: owner, expect: "either" })), logs: [],
  }) });
  let pending;
  await assert.rejects(lost.payExact(payer, bank.address, 300n), (err) => {
    pending = err; return err instanceof TransactionReceiptPending && Boolean(err.transactionHash);
  });
  assert.equal(await s.call(token, "balanceOf", [bank.address]), 300n);
  const receipt = await s.client.getTransactionReceipt({ hash: pending.transactionHash });
  assert.equal(ops.paymentFromReceipt(payer, bank.address, 300n, receipt, pending.transactionHash).amount, 300n);
  assert.throws(() => ops.paymentFromReceipt(payer, bank.address, 300n, receipt, `0x${"11".repeat(32)}`), /hash mismatch/);
  assert.throws(() => ops.paymentFromReceipt(payer, stranger.address, 300n, receipt, pending.transactionHash), /does not match/);
  assert.throws(() => ops.paymentFromReceipt(payer, bank.address, 301n, receipt, pending.transactionHash), /does not match/);
  const duplicate = { ...receipt, logs: [...receipt.logs, ...receipt.logs] };
  assert.throws(() => ops.paymentFromReceipt(payer, bank.address, 300n, duplicate, pending.transactionHash), /ambiguous/);
  assert.equal((await ops.payExact(payer, bank.address, 200n)).amount, 200n);
  assert.equal(await s.call(token, "balanceOf", [bank.address]), 500n);
  assert.equal(await s.call(token, "balanceOf", [payer.address]), 500n);

  // The same payment can be dispatched by a simulation proxy; the payer in
  // the settlement evidence is the proxy, not the transaction's outer EOA.
  const proxy = await s.deploy(loadArtifact("SimLP"), []);
  await s.send(token, "mint", [proxy.address, 100n]);
  const proxyOps = settlementOps(s, token, { execute: (_owner, contract, method, args) => s.send(proxy, "exec",
    [contract.address, encodeFunctionData({ abi: contract.abi, functionName: method, args })], { expect: "either" }) });
  assert.equal((await proxyOps.payExact(proxy.address, bank.address, 60n)).amount, 60n);
  assert.equal(await s.call(token, "balanceOf", [proxy.address]), 40n);
  assert.equal(await s.call(token, "balanceOf", [bank.address]), 560n);
});
