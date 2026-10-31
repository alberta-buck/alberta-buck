// Partial BuckNet household operations for discount buyback. The complete
// household lifecycle, savings basket and sandbox integration remain separate.
import { buyBuck, netPurchaseFromReceipt } from "../market.js";
import { netCreditOps } from "../net-world.js";
import { TransactionReceiptPending, TransactionReverted } from "../session.js";

/**
 * Prepare the existing household's cash/balance/read/pending/buy/retire operations.
 * `facilityIds(account)` must return the actual NFT IDs belonging to the
 * selected owner. Browser EOAs work directly; simulations using proxies can
 * supply `ownerFor`, `creditOps` and `buy` with their own transaction dispatch.
 * The holder must already have the market identity handshake and settlement
 * allowance; this helper does not grant either permission.
 *
 * The returned object is intentionally partial: it cannot bootstrap a world,
 * issue credit, invest, exit a basket, record wealth or restore a sandbox.
 */
export function netHouseholdRepaymentOps(world, market, { facilityIds, ownerFor = (a) => a,
  creditOps = null, buy = buyBuck } = {}) {
  if (world.accounting !== "net" || !world.net || typeof facilityIds !== "function") {
    throw new Error("net household repayment requires a BuckNet world and facility IDs");
  }
  creditOps ??= netCreditOps(world.session, world.net);
  const s = world.session;
  const holder = (account) => ownerFor(account);
  const address = (account) => {
    const h = holder(account);
    const owner = h.account ?? h;
    return typeof owner === "string" ? owner : owner.address;
  };
  const receiptFor = async (hash) => {
    const receipt = await s.client.getTransactionReceipt({ hash });
    if (!receipt?.transactionHash || receipt.transactionHash.toLowerCase() !== hash.toLowerCase()) {
      throw new Error("transaction hash mismatch");
    }
    return receipt;
  };
  const validateCancellation = async (account, id, expected) => {
    const ids = await facilityIds(account);
    await creditOps.household(address(account), ids); // reject foreign facilities
    if (!ids.some((candidate) => BigInt(candidate) === BigInt(id))) throw new Error("foreign household facility");
    const actual = await s.call(world.buck, "reductionAuthorization", [id]);
    if (!expected[0] || actual.some((value, i) => value !== expected[i])) {
      throw new Error("pending repayment changed; reconcile transaction before canceling");
    }
  };
  const cancellationEvidence = async (account, id, receipt, hash) => {
    const h = holder(account);
    creditOps.authorizationFromReceipt(h.account ?? h,
      { tokenId: id, principal: 0n, minimumReliefScaled: 0n }, 0n, receipt, hash);
    const actual = await s.call(world.buck, "reductionAuthorization", [id]);
    if (actual.some((value) => value !== 0n)) throw new Error("repayment cancellation changed; reconcile before proceeding");
  };
  return {
    cash: (account) => s.call(market.usdc, "balanceOf", [address(account)]),
    balance: (account) => s.call(world.buck, "balanceOf", [address(account)]),
    read: async (account) => {
      const state = await creditOps.household(address(account), await facilityIds(account));
      return { ...state, drawn: state.issued };
    },
    pending: async (account) => {
      for (const id of await facilityIds(account)) {
        if ((await s.call(world.buck, "reductionAuthorization", [id]))[0] > 0n) return true;
      }
      return false;
    },
    authorization: async (_account, id) => (await s.call(world.buck, "reductionAuthorization", [id]))[0],
    validateCancellation,
    cancel: async (account, id, expected) => {
      await validateCancellation(account, id, expected);
      const h = holder(account), receipt = await creditOps.cancelReduction(h.account ?? h, id);
      if (receipt?.status === "reverted" && receipt.transactionHash) {
        throw new TransactionReverted("cancelReduction", "cancellation reverted", receipt);
      }
      if (receipt?.status !== "success" || !receipt.transactionHash) throw new TransactionReceiptPending(receipt?.transactionHash ?? null);
      try { await cancellationEvidence(account, id, receipt, receipt.transactionHash); }
      catch (err) { throw new TransactionReceiptPending(receipt.transactionHash, err); }
      return receipt;
    },
    reconcileCancellation: async (account, id, hash) => {
      const receipt = await receiptFor(hash);
      if (receipt.status === "reverted") return { reverted: true };
      await cancellationEvidence(account, id, receipt, hash);
      return { reverted: false };
    },
    reconcileBuy: async (account, hash) => {
      const receipt = await receiptFor(hash);
      if (receipt.status === "reverted") return { reverted: true };
      const r = netPurchaseFromReceipt(world, market, holder(account), receipt, hash);
      return { reverted: false, spent: r.paid, got: r.received };
    },
    reconcileReduction: async (account, id, principal, hash) => {
      const receipt = await receiptFor(hash);
      if (receipt.status === "reverted") return { reverted: true };
      const h = holder(account);
      return { ...creditOps.reductionFromReceipt(h.account ?? h, id, principal, receipt, hash), reverted: false };
    },
    reconcileAuthorization: async (account, id, principal, minimumReliefScaled, deadline, hash) => {
      const receipt = await receiptFor(hash);
      const actual = await s.call(world.buck, "reductionAuthorization", [id]);
      if (receipt.status === "reverted") {
        if (actual[0]) throw new Error("authorization changed; reconcile before retrying");
        return { reverted: true };
      }
      const h = holder(account);
      creditOps.authorizationFromReceipt(h.account ?? h, { tokenId: id, principal, minimumReliefScaled }, deadline, receipt, hash);
      if (actual[0] !== principal || actual[1] !== minimumReliefScaled || actual[2] !== deadline) {
        throw new Error("authorization changed; reconcile before canceling");
      }
      return { reverted: false };
    },
    buy: async (account, amount, { minNet = 0n } = {}) => {
      const r = await buy(world, market, holder(account), amount, { minOut: minNet });
      return { spent: r.paid, got: r.received };
    },
    retire: async (account, target, acquired, reserve = null) => {
      const h = holder(account), me = address(account);
      const held = await s.call(world.buck, "balanceOf", [me]);
      // Keep the pre-purchase working balance, including the issue reserve.
      const buffer = reserve ?? ((held > acquired ? held - acquired : 0n) + 1n);
      const deadline = (await s.client.getBlock()).timestamp + 3600n;
      const result = await creditOps.retireHousehold(h.account ?? h, await facilityIds(account), target, deadline, { buffer });
      // Once settlement's outcome is uncertain, return the saved plan before
      // any further RPC read can fail and erase the information the agent needs.
      if (result.status === "authorization-pending") return { ...result, reserve: buffer };
      if (result.retired >= target) return { ...result, reserve: null };
      try {
        const remaining = await s.call(world.buck, "balanceOf", [me]);
        return { ...result, reserve: remaining <= buffer ? null : buffer };
      } catch (err) {
        // The read must not erase completed settlement receipts. Keep the
        // pre-purchase reserve until a later read can establish the balance.
        return { ...result, reserve: buffer, balanceError: String(err).slice(0, 200) };
      }
    },
  };
}
