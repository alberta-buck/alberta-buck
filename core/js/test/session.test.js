import { test } from "node:test";
import assert from "node:assert/strict";
import { Session, TransactionReceiptPending } from "../src/session.js";

test("a receipt timeout preserves the submitted transaction hash", async () => {
  const hash = `0x${"ab".repeat(32)}`;
  const client = {
    writeContract: async () => hash,
    waitForTransactionReceipt: async () => { throw new Error("simulated receipt timeout"); },
  };
  const session = new Session(client, { account: { address: `0x${"12".repeat(20)}` } });
  await assert.rejects(session.send({ address: `0x${"34".repeat(20)}`, abi: [] }, "work"), (err) => {
    assert.ok(err instanceof TransactionReceiptPending);
    assert.equal(err.transactionHash, hash);
    assert.match(err.cause.message, /timeout/);
    return true;
  });
  assert.deepEqual(session.mismatches, []);
});
