// The Savings tab's contract surface: only what a saver calls or reads, in
// viem's human-readable form -- no artifact bundle, since the world's
// contracts live on the server.  The basket is the ops shell; its venue
// facet's entry points (poolBuckValues) are reached through the shell's
// fallback at the same address.  test/savings.model.test.js checks every
// signature here against the compiled artifacts when out/ is built.

import { parseAbi } from "viem";

export const ERC20 = parseAbi([
  "function balanceOf(address) view returns (uint256)",
  "function approve(address spender, uint256 amount) returns (bool)",
  "function mint(address to, uint256 amount)",
  "event Transfer(address indexed from, address indexed to, uint256 value)",
]);

export const BASKET = parseAbi([
  "function depositToken(address token, uint256 tokenAmount, uint256 maxDeviationBp) returns (uint256 receiptId)",
  "function redeem(uint256 receiptId, uint256 redeemBp)",
  "function deposits(uint256 id) view returns (uint256 buckPrincipal, uint256 tokenPrincipal, address token, uint64 depositTime)",
  "function twapWindow() view returns (uint32)",
  "function consultTickExternal(address pool, uint32 secondsAgo) view returns (int24)",
  "event Deposited(address indexed who, uint256 indexed receiptId, address token, uint256 tokenAmount, uint256 buckMinted, uint128 liquidity)",
  "event Redeemed(address indexed who, uint256 indexed receiptId, uint256 burned, uint256 depositorBuck, uint256 treasuryBuck, uint256 remainingBp)",
  "error Amount0()",
  "error NotInBasket()",
  "error NotOwner()",
  "error EmptyDeposit()",
  "error NoOutstanding()",
  "error NoLPWithdrawn()",
  "error NoValue()",
  "error Slippage()",
  "error Underwater()",
  "error ConversionLoss()",
  "error TokenTooThin()",
  "error RedeemZero()",
  "error Bp10000()",
]);

export const POOL = parseAbi([
  "function slot0() view returns (uint160 sqrtPriceX96, int24 tick, uint16 observationIndex, uint16 observationCardinality, uint16 observationCardinalityNext, uint8 feeProtocol, bool unlocked)",
]);

export const RECEIPT = parseAbi([
  "function ownerOf(uint256 id) view returns (address)",
]);

export const DIRECTOR = parseAbi([
  "function depositHint() view returns (uint256)",
]);

// What a refusal means to a saver, by the basket's error name.
export const REASONS = {
  Slippage: "that pool is too far from its recent average price (a guard against being priced by a manipulated pool); choose another, or wait a day",
  Underwater: "the pools cannot cover the burn right now",
  ConversionLoss: "redeeming now would lose more than 1% converting TOKEN to BUCK",
  NotOwner: "that receipt is not yours",
  EmptyDeposit: "that receipt was already redeemed",
  NotInBasket: "that token is not in the basket",
  NoValue: "the basket holds no depositor liquidity yet",
};
