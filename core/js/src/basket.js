// One owner's open cash-only BuckNet basket receipt marks at a single block.
import { isAddress, maxUint256, zeroAddress } from "viem";

const WAD = 10n ** 18n;
const receiptAbi = [{ type: "function", name: "ownerOf", stateMutability: "view",
  inputs: [{ name: "tokenId", type: "uint256" }], outputs: [{ name: "owner", type: "address" }] }];

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
    const product = shares * sharePrice;
    if (product > maxUint256) throw new Error("basket mark overflows contract arithmetic");
    return { tokenId, shares, basis, mark: product / WAD };
  }));
  const basis = positions.reduce((sum, p) => sum + p.basis, 0n);
  const mark = positions.reduce((sum, p) => sum + p.mark, 0n);
  return { blockNumber: block, sharePrice, positions, basis, mark,
    roi: basis ? Number(mark - basis) / Number(basis) : null };
}
