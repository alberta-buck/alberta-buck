// Exact payments of a standard settlement ERC-20. A transfer to a simulated
// bank does not prove that a real lender credited any particular loan.
import { isAddress, maxUint256, parseEventLogs, zeroAddress } from "viem";
import { TransactionReceiptPending, TransactionReverted } from "./session.js";

const same = (a, b) => a.toLowerCase() === b.toLowerCase();

function terms(owner, recipient, amount) {
  const sender = owner?.address ?? owner;
  if (typeof sender !== "string" || !isAddress(sender) || typeof recipient !== "string" || !isAddress(recipient)
      || same(sender, recipient) || same(sender, zeroAddress) || same(recipient, zeroAddress)
      || typeof amount !== "bigint" || amount <= 0n || amount > maxUint256) {
    throw new Error("invalid settlement payment terms");
  }
  return sender;
}

/** Authenticate one exact transfer against previously saved payment terms. */
export function settlementPaymentFromReceipt(token, owner, recipient, amount, receipt, expectedHash) {
  const sender = terms(owner, recipient, amount);
  if (receipt?.status !== "success" || !receipt.transactionHash) throw new Error("settlement payment receipt is not successful");
  if (typeof expectedHash !== "string" || !expectedHash || !same(receipt.transactionHash, expectedHash)) {
    throw new Error("settlement payment transaction hash mismatch");
  }
  const events = parseEventLogs({ abi: token.abi, logs: receipt.logs, eventName: "Transfer" })
    .filter((e) => same(e.address, token.address));
  if (events.length !== 1) throw new Error("settlement payment evidence missing or ambiguous");
  const event = events[0].args;
  if (!same(event.from, sender) || !same(event.to, recipient) || event.value !== amount) {
    throw new Error("settlement payment evidence does not match terms");
  }
  return { amount, receipt };
}

/** Serialize payer actions and apply each authenticated receipt once. This
 * helper assumes a non-rebasing, non-fee-on-transfer token; it neither tracks
 * pending operations nor establishes finality or updates a mortgage ledger. */
export function settlementOps(session, token, { execute = null } = {}) {
  execute ??= (owner, contract, method, args) => session.send(contract, method, args,
    { account: owner, expect: "either", tag: "household:external-payment" });
  return {
    paymentFromReceipt: (owner, recipient, amount, receipt, expectedHash) =>
      settlementPaymentFromReceipt(token, owner, recipient, amount, receipt, expectedHash),
    payExact: async (owner, recipient, amount) => {
      terms(owner, recipient, amount);
      let receipt;
      try {
        receipt = await execute(owner, token, "transfer", [recipient, amount]);
        if (receipt?.status === "reverted" && receipt.transactionHash) {
          throw new TransactionReverted("transfer", "settlement payment reverted", receipt);
        }
        if (receipt?.status !== "success" || !receipt.transactionHash) {
          throw new TransactionReceiptPending(receipt?.transactionHash ?? null);
        }
        return settlementPaymentFromReceipt(token, owner, recipient, amount, receipt, receipt.transactionHash);
      } catch (err) {
        if (err instanceof TransactionReceiptPending) throw err;
        if (err instanceof TransactionReverted && err.receipt?.status === "reverted" && err.transactionHash) throw err;
        throw new TransactionReceiptPending(err?.transactionHash ?? receipt?.transactionHash ?? null, err);
      }
    },
  };
}
