// One owner's canonical Buck basket receipt marks at a single block.
import { isAddress, maxUint256, zeroAddress, parseEventLogs, encodeFunctionData, decodeFunctionResult } from "viem";
import { TransactionReceiptPending, TransactionReverted } from "./session.js";

const receiptAbi = [{ type: "function", name: "ownerOf", stateMutability: "view",
  inputs: [{ name: "tokenId", type: "uint256" }], outputs: [{ name: "owner", type: "address" }] }];

const same = (a, b) => a.toLowerCase() === b.toLowerCase();
const positive = (...values) => values.every((v) => typeof v === "bigint" && v > 0n && v <= maxUint256);

/** Ordinary BUCK deposits/top-ups and explicit selected-receipt exits.
 * PRE: serialized owner, prepared allowances/identity and a trusted chain
 * reader; retain the quoted plan across uncertain submission. POST: exact
 * contract/owner/asset/quantity/hash and dated holding transitions are checked
 * before returning. No household ledger changes here. A missing reply or
 * required post-read keeps its hash for recovery without another send.
 * Quotes are advisory; existing minShares/minBuck and K/price guards govern.
 *
 * `simulate(owner, contract, functionName, args, { blockNumber })` returns the
 * raw return data of that call made AS the owner, with every write discarded.
 * The default is a direct eth_call from the owner. A contract-held account
 * needs its own read-only dispatch where the node refuses a sender with code
 * (EIP-3607); it must run the unmodified target and add no authority.
 */
export function netBasketOps(session, basket, buck, { execute, simulate = null } = {}) {
  if (typeof execute !== "function") throw new Error("basket owner dispatch required");
  simulate ??= async (owner, c, functionName, args, { blockNumber }) => (await session.client.call({ account: owner,
    to: c.address, data: encodeFunctionData({ abi: c.abi, functionName, args }), blockNumber })).data;
  const read = (c, functionName, args, blockNumber) => session.client.readContract({
    address: c.address, abi: c.abi, functionName, args, blockNumber });
  const snapshot = async (given, tokenId) => {
    const owner = given?.address ?? given;
    if (typeof owner !== "string" || !isAddress(owner) || same(owner, zeroAddress)
        || typeof tokenId !== "bigint" || tokenId < 0n || tokenId > maxUint256) throw new Error("invalid basket owner/receipt");
    const blockNumber = (await session.client.getBlock()).number;
    if (!await read(basket, "netAccounting", [], blockNumber) || !same(await read(basket, "buck", [], blockNumber), buck.address)) {
      throw new Error("basket is not configured for this net BUCK");
    }
    const flags = ["isVerified", "isCarrying"].map((name) => ({ type: "function", name, stateMutability: "view",
      inputs: [{ name: "account", type: "address" }], outputs: [{ type: "bool" }] }));
    const reg = session.contractAt(flags, await read(buck, "identity", [], blockNumber));
    if (!await read(reg, "isVerified", [owner], blockNumber) || await read(reg, "isCarrying", [owner], blockNumber)) {
      throw new Error("basket household must be verified and Ordinary");
    }
    let priorShares = 0n, priorBasis = 0n;
    if (tokenId) {
      const nft = session.contractAt(receiptAbi, await read(basket, "receipt", [], blockNumber));
      if (!same(await read(nft, "ownerOf", [tokenId], blockNumber), owner)) throw new Error("foreign basket receipt");
      if (!same((await read(basket, "deposits", [tokenId], blockNumber))[2], buck.address)) throw new Error("household position must be a BUCK receipt");
      [priorShares, priorBasis] = await read(basket, "holdings", [tokenId], blockNumber);
    }
    if ((await session.client.getBlock()).number !== blockNumber) throw new Error("basket observation changed");
    return { blockNumber, owner, tokenId, priorShares, priorBasis };
  };
  const events = (c, eventName, receipt) => parseEventLogs({ abi: c.abi, logs: receipt.logs, eventName })
    .filter((e) => same(e.address, c.address)).map((e) => e.args);
  const evidence = (receipt, expectedHash) => {
    if (receipt?.status !== "success" || !receipt.transactionHash || typeof expectedHash !== "string" || !expectedHash
        || !same(receipt.transactionHash, expectedHash)) throw new Error("basket receipt success/hash mismatch");
  };
  const depositFromReceipt = async (p, receipt, expectedHash) => {
    evidence(receipt, expectedHash);
    const deposits = events(basket, "Deposited", receipt);
    if (deposits.length !== 1) throw new Error("basket deposit evidence missing/ambiguous");
    const e = deposits[0], id = e.receiptId;
    if (!same(e.who, p.owner) || !same(e.token, buck.address) || e.tokenAmount !== p.amount
        || !id || p.tokenId && id !== p.tokenId) throw new Error("basket deposit evidence differs from plan");
    const flows = events(buck, "NetValueTransfer", receipt).filter((e) => same(e.from, p.owner) && same(e.to, basket.address));
    if (flows.length !== 1 || flows[0].nominal !== p.amount || flows[0].netScaled !== p.amount * 10n ** 36n) {
      throw new Error("basket deposit net cash evidence mismatch");
    }
    const block = receipt.blockNumber, nft = session.contractAt(receiptAbi, await read(basket, "receipt", [], block));
    const [shares, basis] = await read(basket, "holdings", [id], block);
    if (!same(await read(nft, "ownerOf", [id], block), p.owner) || basis !== p.priorBasis + p.amount
        || shares < p.priorShares + p.minimumShares) throw new Error("basket deposit holding transition mismatch");
    const inc = events(basket, "PositionIncreased", receipt);
    if (p.tokenId) {
      if (inc.length !== 1 || inc[0].receiptId !== id || !same(inc[0].who, p.owner) || !same(inc[0].asset, buck.address)
          || inc[0].nominalAmount !== p.amount || inc[0].received !== p.amount || inc[0].sharesAdded !== shares - p.priorShares) {
        throw new Error("basket top-up evidence mismatch");
      }
    } else if (inc.length) throw new Error("new basket receipt unexpectedly topped up a position");
    return { tokenId: id, received: p.amount, sharesAdded: shares - p.priorShares, shares, basis, receipt };
  };
  const redemptionFromReceipt = async (p, receipt, expectedHash) => {
    evidence(receipt, expectedHash);
    const reductions = events(basket, "PositionReduced", receipt);
    if (reductions.length !== 1) throw new Error("basket redemption evidence missing/ambiguous");
    const e = reductions[0];
    if (!same(e.who, p.owner) || e.receiptId !== p.tokenId || e.requestedShares !== p.shares || e.sharesRetired <= 0n
        || e.sharesRetired > p.shares || e.sharesRemaining !== p.priorShares - e.sharesRetired || e.paid < p.minimumBuck) {
      throw new Error("basket redemption evidence differs from plan");
    }
    const net = events(buck, "NetValueTransfer", receipt).filter((e) => same(e.from, basket.address) && same(e.to, p.owner))
      .reduce((sum, e) => sum + e.netScaled, 0n);
    if (net / 10n ** 36n !== e.paid) throw new Error("basket redemption net payout mismatch");
    const [shares, basis] = await read(basket, "holdings", [p.tokenId], receipt.blockNumber);
    const expectedBasis = p.priorBasis - (p.priorBasis * p.shares / p.priorShares) * e.sharesRetired / p.shares;
    if (shares !== e.sharesRemaining || basis !== expectedBasis) throw new Error("basket redemption holding transition mismatch");
    const inKind = events(basket, "PaidInKind", receipt).filter((e) => same(e.to, p.owner)).map((e) => [e.token, e.amount]);
    return { tokenId: p.tokenId, paid: e.paid, sharesRetired: e.sharesRetired,
      sharesRemaining: shares, basisRemaining: basis, inKind, receipt };
  };
  const send = async (p, name, args, reader) => {
    let receipt;
    try {
      receipt = await execute(p.owner, basket, name, args, { expect: "either" });
      if (receipt?.status === "reverted" && receipt.transactionHash) throw new TransactionReverted(name, "basket operation reverted", receipt);
      return await reader(p, receipt, receipt?.transactionHash);
    } catch (err) {
      if (err instanceof TransactionReceiptPending || err instanceof TransactionReverted) throw err;
      throw new TransactionReceiptPending(err.transactionHash ?? receipt?.transactionHash ?? null, err);
    }
  };
  return {
    prepareDeposit: async (owner, amount, minimumShares, { tokenId = 0n } = {}) => {
      if (!positive(amount, minimumShares)) throw new Error("positive basket deposit/share bound required");
      return { ...await snapshot(owner, tokenId), amount, minimumShares };
    },
    prepareRedemption: async (owner, tokenId, shares, minimumBuck) => {
      if (!positive(tokenId, shares, minimumBuck)) throw new Error("positive basket redemption/cash bound required");
      const p = await snapshot(owner, tokenId);
      if (shares > p.priorShares) throw new Error("basket redemption exceeds owned shares");
      return { ...p, shares, minimumBuck };
    },
    previewRedemption: async (owner, tokenId, shares) => {
      // eth_call executes the real exit/K/fee logic and discards its writes.
      // A refused quote cannot justify bypassing that same on-chain guard.
      // POST: neither the holding nor the chain height has changed.
      if (!positive(tokenId, shares)) throw new Error("positive basket receipt/shares required");
      const p = await snapshot(owner, tokenId);
      if (shares > p.priorShares) throw new Error("basket redemption exceeds owned shares");
      const data = await simulate(p.owner, basket, "redeemShares", [tokenId, shares, 0n], { blockNumber: p.blockNumber });
      const r = decodeFunctionResult({ abi: basket.abi, functionName: "redeemShares", data });
      const [nowShares, nowBasis] = await read(basket, "holdings", [tokenId], p.blockNumber);
      if ((await session.client.getBlock()).number !== p.blockNumber || nowShares !== p.priorShares || nowBasis !== p.priorBasis) {
        throw new Error("basket observation changed");
      }
      return { blockNumber: p.blockNumber, positionId: r[0], paid: r[1], sharesRetired: r[2], sharesRemaining: r[3] };
    },
    depositFromReceipt, redemptionFromReceipt,
    deposit: (p) => send(p, p.tokenId ? "increasePosition" : "deposit", p.tokenId
      ? [p.tokenId, buck.address, p.amount, p.amount, p.minimumShares] : [buck.address, p.amount, p.minimumShares], depositFromReceipt),
    redeem: (p) => send(p, "redeemShares", [p.tokenId, p.shares, p.minimumBuck], redemptionFromReceipt),
  };
}

/** Read actual receipt basis and TWAP share marks in raw BUCK. The ROI is not
 * annualized and excludes exit costs. Missing/foreign receipts and unavailable
 * prices fail; the result is not a promise of exit liquidity. */
export async function markNetBasketPositions(session, basket, buck, owner, tokenIds, { blockNumber = null } = {}) {
  const address = owner?.address ?? owner;
  if (typeof address !== "string" || !isAddress(address) || address.toLowerCase() === zeroAddress
      || !Array.isArray(tokenIds) || tokenIds.some((id) => typeof id !== "bigint" || id <= 0n || id > maxUint256)
      || new Set(tokenIds.map(String)).size !== tokenIds.length
      || (blockNumber !== null && (typeof blockNumber !== "bigint" || blockNumber < 0n))) {
    throw new Error("invalid basket mark inputs");
  }
  const block = blockNumber ?? (await session.client.getBlock()).number;
  const read = (contract, functionName, args = []) => session.client.readContract({
    address: contract.address, abi: contract.abi, functionName, args, blockNumber: block,
  });
  const [net, actualBuck, receiptAddress, sharePrice] = await Promise.all([
    read(basket, "netAccounting"), read(basket, "buck"), read(basket, "receipt"), read(basket, "sharePrice"),
  ]);
  if (!net || actualBuck.toLowerCase() !== buck.address.toLowerCase()) throw new Error("basket is not configured for this net BUCK");
  const receipt = { address: receiptAddress, abi: receiptAbi };
  const positions = await Promise.all(tokenIds.map(async (tokenId) => {
    const [actualOwner, [shares, basis]] = await Promise.all([
      read(receipt, "ownerOf", [tokenId]), read(basket, "holdings", [tokenId]),
    ]);
    if (actualOwner.toLowerCase() !== address.toLowerCase()) throw new Error("foreign basket receipt");
    // Read the single-rounding receipt value, not a second rounding of the
    // displayed share price. Contract arithmetic/price failures propagate.
    return { tokenId, shares, basis, mark: await read(basket, "valueOf", [tokenId]) };
  }));
  const basis = positions.reduce((sum, p) => sum + p.basis, 0n);
  const mark = positions.reduce((sum, p) => sum + p.mark, 0n);
  return { blockNumber: block, sharePrice, positions, basis, mark,
    roi: basis ? Number(mark - basis) / Number(basis) : null };
}
