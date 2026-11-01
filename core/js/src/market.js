// buildMarket: a BUCK/USDC market on a BUCK world, on production identity
// contracts and the UNMODIFIED Uniswap periphery.
//
//   * USDC (a MockERC20: the world's fiat bridge mints it),
//   * a UniswapV3Factory, and the BUCK/USDC pool created AND identity-bound
//     in one step by the production UniswapV3BindingAdapter -- public and
//     carrying, under the market operator's registered identity,
//   * SimLP, the public BUCK-backed liquidity provider: it authorizes its
//     own binding (production IdentityRegistry), opts into the insurer,
//     pledges a zero-premium credit, mints BUCK and LPs full range,
//   * Permit2 and the Universal Router, deployed exactly as Uniswap ships
//     them and NEVER identity-bound: BUCK reaches a pool only by Permit2
//     (the pool pulls from the holder) and leaves it straight to the holder,
//     so the router never holds BUCK (see router.js).
//
// A holder trades once it has opened trading (openTrading): its identity
// handshake with the pool -- the pool's operator can then decrypt who it
// traded with, as the transfer gate requires of every private<->public
// transfer -- and its Permit2 approvals.

import { encodeFunctionData, keccak256, maxUint160, maxUint256, maxUint48, parseEventLogs } from "viem";

import { MINT_MAX, identityApprove, onboard } from "./buckworld.js";
import { netTransferValue } from "./net-world.js";
import { TransactionReceiptPending, TransactionReverted } from "./session.js";
import { deployUniversalRouter, encodePath, urSwapArgs, ZERO_ADDR } from "./router.js";
import { Q96, fullRangeTicks, sqrtPriceX96, spotFromSqrtPriceX96 } from "./v3.js";

export const POOL_FEE = 500;                   // 0.05 %, 10-tick spacing
const SPACING = 10;
const E6 = 10n ** 6n;
const E18 = 10n ** 18n;

// BN254.sol struct components are UPPERCASE X/Y at the ABI.
const g = (p) => ({ X: p.x, Y: p.y });
const ct = (E) => ({ R: g(E.R), C: g(E.C) });
const acctOf = (who) => who.account ?? who;

export const OPERATOR_FIELDS = {
  given_name: "Market", family_name: "Operator",
  jurisdiction: "Alberta, Canada", id_type: "Operator",
  id_number: "OP-0000001", date_of_birth: "1990-01-01",
  issued_at: "2026-01-01T00:00:00Z", epoch: 42,
};

/**
 * @param world     from buildBuckWorld (its session account deploys, is
 *                  governance, and owns the V3 factory)
 * @param artifacts (name) => {abi, bytecode}, including the vendored
 *                  "UniversalRouter" and "Permit2"
 * @param opts.operator a registered handle for the deployer (default: the
 *                  deployer onboards with OPERATOR_FIELDS)
 * @param opts.depth    full-range depth, BUCK and USDC each, 6 dp
 *                      (default 1,000,000)
 * @param opts.rng      scalar drawer
 * @returns market {usdc, v3f, adapter, pool, simlp, weth, permit2, router,
 *                  fee, operator}
 */
export async function buildMarket(world, artifacts, opts = {}) {
  const s = world.session;
  const me = s.account;
  const gas = 15_000_000n;
  const depth = opts.depth ?? 1_000_000n * E6;
  const { reg, credit, buck } = world;
  const operator = opts.operator
    ?? await onboard(world, me, OPERATOR_FIELDS, { rng: opts.rng });

  const usdc = await s.deploy(artifacts("MockERC20"), ["USD Coin", "USDC", 6],
    { name: "USDC", gas });
  const v3f = await s.deploy(artifacts("UniswapV3Factory"), [],
    { name: "UniswapV3Factory", gas });

  // The pool: created and bound in one step by the production adapter, to
  // the registered identity the factory names as owner.
  const adapter = await s.deploy(artifacts("UniswapV3BindingAdapter"),
    [reg.address, v3f.address], { name: "UniswapV3BindingAdapter", gas });
  await s.send(reg, "setBindingAdapter", [adapter.address, true],
    { tag: "market:setBindingAdapter" });
  await s.send(adapter, "createPoolAndBind", [buck.address, usdc.address, POOL_FEE],
    { tag: "market:createPoolAndBind" });
  const pool = s.contractAt(artifacts("UniswapV3Pool").abi,
    await s.call(v3f, "getPool", [buck.address, usdc.address, POOL_FEE]));
  const sqrt = sqrtPriceX96(buck.address, E6, usdc.address, E6);
  await s.send(pool, "initialize", [sqrt], { tag: "market:init" });

  // SimLP: it authorizes its own binding, which the operator then makes
  // (public: the operator discloses it; non-carrying: it draws credit).
  const simlp = await s.deploy(artifacts("SimLP"), [], { name: "SimLP", gas });
  const exec = (target, abi, functionName, args, tag, extra = {}) =>
    s.send(simlp, "exec", [target.address,
      encodeCall(abi, functionName, args)], { tag, ...extra });
  await exec(reg, reg.abi, "authorizeContractBinding",
    [operator.account.address, g(operator.kp.pk), ct(operator.E), true, false],
    "market:simlp:authorize");
  await s.send(reg, "bindContract",
    [simlp.address, g(operator.kp.pk), ct(operator.E), true, false],
    { tag: "market:simlp:bind" });

  // Its capital: a zero-premium credit, activated whole (Buck.mint(max):
  // K x face of headroom, depth + 20% at the resting K), then LP'd with USDC.
  const k = await s.call(world.kctrl, "buckK");
  const face = ((depth * E18) / k) * 12n / 10n;
  const now = (await s.client.getBlock()).timestamp;
  await exec(credit, credit.abi, "setCreditIssuer", [me.address, true],
    "market:simlp:acceptInsurer");
  const creditReceipt = await s.send(credit, "createCredit",
    [simlp.address, 0, face, 0n, 0, 0, now, 0], { tag: "market:simlp:credit" });
  let providerCreditId = null;
  if (world.accounting === "net") {
    const [created] = parseEventLogs({ abi: credit.abi, logs: creditReceipt.logs, eventName: "CreditCreated" });
    if (!created) throw new Error("market provider credit event missing");
    providerCreditId = created.args.tokenId;
    const [, , , maxSpendable] = await s.call(world.net.quoter, "quoteIssue", [providerCreditId, face]);
    if (maxSpendable < depth) throw new Error("market provider cannot issue enough BUCK to seed liquidity");
    await exec(buck, buck.abi, "issue", [providerCreditId, face, maxSpendable, 0n, "0x0000000000000000000000000000000000000000"], "market:simlp:issue",
      { gas: 3_000_000n });
  } else {
    await exec(buck, buck.abi, "mint", [MINT_MAX], "market:simlp:activate", { gas: 3_000_000n });
  }
  await s.send(usdc, "mint", [simlp.address, 2n * depth], { tag: "market:simlp:usdc" });
  const [t0, t1] = buck.address.toLowerCase() < usdc.address.toLowerCase()
    ? [buck.address, usdc.address] : [usdc.address, buck.address];
  const L = t0 === usdc.address ? (depth * sqrt) / Q96 : (depth * Q96) / sqrt;
  const [lo, hi] = fullRangeTicks(SPACING);
  await s.send(simlp, "mint", [pool.address, lo, hi, L, t0, t1],
    { tag: "market:simlp:liquidity" });

  // The periphery, as Uniswap ships it: Permit2 and the Universal Router.
  const weth = await s.deploy(artifacts("WETH9"), [], { name: "WETH9", gas });
  const permit2 = await s.deploy(artifacts("Permit2"), [], { name: "Permit2", gas });
  const router = await deployUniversalRouter(s, artifacts("UniversalRouter"), {
    permit2: permit2.address, weth: weth.address, v3Factory: v3f.address,
    poolInitCode: artifacts("UniswapV3Pool").bytecode,
  });

  const guard = world.accounting === "net"
    ? await s.deploy(artifacts("BuckNetSwapGuard"), [buck.address, usdc.address, v3f.address],
      { name: "BuckNetSwapGuard", gas }) : null;
  const codeHash = async (contract) => {
    const code = await s.client.getCode({ address: contract.address });
    if (!code || code === "0x") throw new Error("net market executable code missing");
    return keccak256(code);
  };
  const [guardCodeHash, routerCodeHash, permit2CodeHash] = guard
    ? await Promise.all([guard, router, permit2].map(codeHash)) : [null, null, null];

  return { usdc, v3f, adapter, pool, simlp, weth, permit2, router, guard, guardCodeHash, routerCodeHash, permit2CodeHash,
    fee: POOL_FEE, operator, providerCreditId };
}

// ABI-encode one call for SimLP.exec.
function encodeCall(abi, functionName, args) {
  return encodeFunctionData({ abi, functionName, args });
}

/** The market as plain data, for attachMarket (serialize with codec.js). */
export function marketRecord(market) {
  const a = {};
  for (const k of ["usdc", "v3f", "adapter", "pool", "simlp", "weth", "permit2", "router"]) {
    a[k] = market[k].address;
  }
  if (market.guard) a.guard = market.guard.address;
  const { account, ...operator } = market.operator;
  const record = { addresses: a, fee: market.fee, operator };
  if (market.guard) {
    record.accounting = "net";
    record.guardCodeHash = market.guardCodeHash;
    record.routerCodeHash = market.routerCodeHash;
    record.permit2CodeHash = market.permit2CodeHash;
  }
  if (market.providerCreditId !== null && market.providerCreditId !== undefined) record.providerCreditId = market.providerCreditId;
  return record;
}

/** Rebuild a market's handles from marketRecord() over a restored chain;
 *  the operator is the session's (deployer) account. */
export function attachMarket(session, artifacts, record) {
  if (record.accounting === "net" || record.addresses.guard) throw new Error("use attachNetMarket to verify a net market");
  return attachMarketHandles(session, artifacts, record);
}

function attachMarketHandles(session, artifacts, record) {
  const names = {
    usdc: "MockERC20", v3f: "UniswapV3Factory", adapter: "UniswapV3BindingAdapter",
    pool: "UniswapV3Pool", simlp: "SimLP", weth: "WETH9", permit2: "Permit2",
    router: "UniversalRouter",
  };
  const m = { fee: record.fee, operator: { ...record.operator, account: session.account },
    providerCreditId: record.providerCreditId ?? null };
  for (const [k, name] of Object.entries(names)) {
    m[k] = session.contractAt(artifacts(name).abi, record.addresses[k]);
  }
  m.guard = record.addresses.guard ? session.contractAt(artifacts("BuckNetSwapGuard").abi, record.addresses.guard) : null;
  return m;
}

/** Verify a restored net market before approving its settlement guard. Saved
 *  hashes detect inconsistent records; they are not an independent audit. */
export async function attachNetMarket(session, artifacts, record, world) {
  if (world.accounting !== "net" || record.accounting !== "net" || !record.addresses?.guard
      || !record.addresses?.router || !record.addresses?.permit2 || !record.guardCodeHash
      || !record.routerCodeHash || !record.permit2CodeHash)
    throw new Error("incomplete net market record");
  const m = attachMarketHandles(session, artifacts, record);
  for (const [name, hash] of [["guard", record.guardCodeHash], ["router", record.routerCodeHash],
    ["permit2", record.permit2CodeHash]]) {
    const code = await session.client.getCode({ address: m[name].address });
    if (!code || code === "0x" || keccak256(code) !== hash) throw new Error(`net market ${name} code mismatch`);
  }
  const matches = async (contract, name, expected, args = []) => {
    if ((await session.call(contract, name, args)).toLowerCase() !== expected.toLowerCase())
      throw new Error(`net market ${name} mismatch`);
  };
  await matches(m.guard, "buck", world.buck.address);
  await matches(m.guard, "settlement", m.usdc.address);
  await matches(m.guard, "factory", m.v3f.address);
  await matches(m.v3f, "getPool", m.pool.address, [world.buck.address, m.usdc.address, m.fee]);
  if (!await session.call(world.reg, "isVerified", [m.pool.address])
      || !await session.call(world.reg, "isCarrying", [m.pool.address])) throw new Error("net market pool mode mismatch");
  m.guardCodeHash = record.guardCodeHash;
  m.routerCodeHash = record.routerCodeHash;
  m.permit2CodeHash = record.permit2CodeHash;
  return m;
}

/** The market's contracts by address, for observe()'s `extra`. */
export function marketContracts(market, artifacts) {
  const names = {
    usdc: ["USDC", "MockERC20"], v3f: ["UniswapV3Factory", "UniswapV3Factory"],
    adapter: ["UniswapV3BindingAdapter", "UniswapV3BindingAdapter"],
    pool: ["BUCK/USDC pool", "UniswapV3Pool"], simlp: ["SimLP", "SimLP"],
    weth: ["WETH9", "WETH9"], permit2: ["Permit2", "Permit2"],
    router: ["UniversalRouter", "UniversalRouter"],
  };
  const out = {};
  for (const [k, [name, art]] of Object.entries(names)) {
    out[market[k].address] = { name, role: art, abi: artifacts(art).abi };
  }
  if (market.guard) out[market.guard.address] = { name: "Net BUCK swap guard", role: "BuckNetSwapGuard",
    abi: artifacts("BuckNetSwapGuard").abi };
  return out;
}

/** Mint USDC to an address: the sandbox's fiat bridge. */
export async function fiatIn(world, market, address, amount, { tag = "fiat:in" } = {}) {
  await world.session.send(market.usdc, "mint", [address, amount], { tag });
}

/** BUCK's price in the pool: USDC (6 dp) per BUCK. */
export async function buckPrice(world, market) {
  const [sqrt] = await world.session.call(market.pool, "slot0");
  return spotFromSqrtPriceX96(sqrt, world.buck.address, 6, market.usdc.address);
}

/** What the pool holds: {buck, usdc}. */
export async function poolReserves(world, market) {
  const [b, u] = await Promise.all([
    world.session.call(world.buck, "balanceOf", [market.pool.address]),
    world.session.call(market.usdc, "balanceOf", [market.pool.address]),
  ]);
  return { buck: b, usdc: u };
}

// Approvals count as given only when effectively unlimited: a partial one
// (spent, or set by hand) is topped up rather than trusted.
const UNLIMITED_ERC20 = maxUint256 >> 1n;
const UNLIMITED_PERMIT2 = maxUint160 >> 1n;

/** Whether `address` has opened trading: the pool handshake and both tokens'
 *  Permit2 approvals for the router. */
export async function tradingOpen(world, market, address) {
  const s = world.session;
  if (world.accounting === "net" && !market.guard) return false;
  const frag = await s.call(world.buck, "receiptFragment", [address, market.pool.address]);
  if (/^0x0*$/.test(frag)) return false;
  for (const token of [world.buck, market.usdc]) {
    if (await s.call(token, "allowance", [address, market.permit2.address]) < UNLIMITED_ERC20) {
      return false;
    }
    const [amount] = await s.call(market.permit2, "allowance",
      [address, token.address, market.router.address]);
    if (amount < UNLIMITED_PERMIT2) return false;
  }
  if (world.accounting === "net" && await s.call(market.usdc, "allowance", [address, market.guard.address]) < UNLIMITED_ERC20)
    return false;
  return true;
}

/**
 * Open trading for a registered holder, skipping any step already done:
 * the identity handshake with the pool (its operator can then decrypt the
 * holder's identity from any BUCK transfer between them), then for BUCK and
 * USDC the plain ERC-20 approval of Permit2 and Permit2's allowance for the
 * router.
 */
export async function openTrading(world, market, holder, opts = {}) {
  const s = world.session;
  const h = acctOf(holder);
  const frag = await s.call(world.buck, "receiptFragment", [h.address, market.pool.address]);
  if (/^0x0*$/.test(frag)) {
    const pk = await s.call(world.reg, "pkOf", [market.pool.address]);
    await identityApprove(world, holder, {
      account: { address: market.pool.address },
      kp: { pk: { x: pk.X, y: pk.Y } },
      fields: { given_name: "BUCK/USDC pool" },
    }, opts);
  }
  for (const token of [world.buck, market.usdc]) {
    const sym = token === world.buck ? "BUCK" : "USDC";
    if (await s.call(token, "allowance", [h.address, market.permit2.address]) < UNLIMITED_ERC20) {
      await s.send(token, "approve", [market.permit2.address, maxUint256],
        { account: h, tag: `trade:approve-permit2:${sym}` });
    }
    const [amount] = await s.call(market.permit2, "allowance",
      [h.address, token.address, market.router.address]);
    if (amount < UNLIMITED_PERMIT2) {
      await s.send(market.permit2, "approve",
        [token.address, market.router.address, maxUint160, maxUint48],
        { account: h, tag: `trade:permit2-router:${sym}` });
    }
  }
  if (world.accounting === "net") {
    if (!market.guard) throw new Error("net market guard is missing");
    if (await s.call(market.usdc, "allowance", [h.address, market.guard.address]) < UNLIMITED_ERC20) {
      await s.send(market.usdc, "approve", [market.guard.address, maxUint256],
        { account: h, tag: "trade:approve-net-guard" });
    }
  }
}

// One single-pool swap through the router, the holder paying by Permit2 and
// receiving directly. For BuckNet, the ERC-20 Transfer event is nominal;
// NetValueTransfer records the full-value amount actually delivered.
async function swap(world, market, holder, tokenIn, tokenOut, o, tag) {
  const s = world.session;
  const h = acctOf(holder);
  const netBuckOutput = world.accounting === "net" && same(tokenOut.address, world.buck.address);
  const netBuckInput = world.accounting === "net" && same(tokenIn.address, world.buck.address);
  if (netBuckInput) {
    if (o.exactOut) throw new Error("net BUCK exact-output sales are not implemented");
    if (!h?.address || same(h.address, ZERO_ADDR) || same(h.address, market.pool.address)) {
      throw new Error("invalid net sale recipient");
    }
    if (typeof o.amount !== "bigint" || o.amount <= 0n || o.amount > maxUint160
        || typeof o.limit !== "bigint" || o.limit <= 0n || o.limit > maxUint256) {
      throw new Error("net BUCK sale needs positive input and minimum output");
    }
    const [canonical, verified, carrying] = await Promise.all([
      s.call(market.v3f, "getPool", [world.buck.address, market.usdc.address, market.fee]),
      s.call(world.reg, "isVerified", [market.pool.address]), s.call(world.reg, "isCarrying", [market.pool.address]),
    ]);
    if (!same(canonical, market.pool.address)) throw new Error("net sale pool is not canonical");
    if (!verified || !carrying) throw new Error("net sale pool must be verified and Carrying");
  }
  if (netBuckOutput) {
    if (!market.guard) throw new Error("net market guard is missing");
    const name = o.exactOut ? "buyExactOutput" : "buyExactInput";
    const args = [market.pool.address, o.amount, o.limit, h.address, 0n];
    let rcpt;
    try {
      rcpt = await s.send(market.guard, name, args, { account: h, gas: 2_000_000n, tag, expect: "either" });
      if (rcpt?.status === "reverted" && rcpt.transactionHash) {
        throw new TransactionReverted(name, s.lastRevertReason || "net purchase reverted", rcpt);
      }
      if (rcpt?.status !== "success" || !rcpt.transactionHash) {
        throw new TransactionReceiptPending(rcpt?.transactionHash ?? null);
      }
      return netPurchaseFromReceipt(world, market, h, rcpt, rcpt.transactionHash);
    }
    catch (err) {
      if (err instanceof TransactionReceiptPending) throw err;
      if (err instanceof TransactionReverted && err.receipt?.status === "reverted" && err.transactionHash) throw err;
      throw new TransactionReceiptPending(err?.transactionHash ?? rcpt?.transactionHash ?? null, err);
    }
  }
  const path = o.exactOut
    ? encodePath([tokenOut.address, market.fee, tokenIn.address])
    : encodePath([tokenIn.address, market.fee, tokenOut.address]);
  const [commands, inputs] = urSwapArgs({ ...o, recipient: h.address, path, payerIsUser: true });
  let rcpt;
  try {
    rcpt = await s.send(market.router, "execute", [commands, inputs],
      { account: h, gas: 1_500_000n, tag, ...(netBuckInput ? { expect: "either" } : {}) });
  } catch (err) {
    if (!netBuckInput || err instanceof TransactionReceiptPending) throw err;
    if (err instanceof TransactionReverted && err.receipt?.status === "reverted" && err.transactionHash) throw err;
    throw new TransactionReceiptPending(err?.transactionHash ?? null, err);
  }
  if (netBuckInput) {
    if (rcpt?.status === "reverted" && rcpt.transactionHash) {
      throw new TransactionReverted("execute", s.lastRevertReason || "net sale reverted", rcpt);
    }
    try { return netSaleFromReceipt(world, market, h, rcpt,
      { expectedHash: rcpt?.transactionHash, expectedInput: o.amount, minimumOut: o.limit }); }
    catch (err) { throw new TransactionReceiptPending(rcpt?.transactionHash ?? null, err); }
  }
  const moved = (token, from, to) => parseEventLogs({ abi: token.abi, logs: rcpt.logs,
                                                      eventName: "Transfer" })
    .filter((e) => same(e.address, token.address) && same(e.args.from, from)
                   && same(e.args.to, to))
    .reduce((sum, e) => sum + e.args.value, 0n);
  const paid = moved(tokenIn, h.address, market.pool.address);
  const received = moved(tokenOut, market.pool.address, h.address);
  return { paid, received };
}

const same = (a, b) => a.toLowerCase() === b.toLowerCase();

/** Authenticate one guarded net purchase, optionally against a saved transaction hash. */
export function netPurchaseFromReceipt(world, market, holder, receipt, expectedHash = null) {
  if (world.accounting !== "net" || !market.guard || receipt?.status !== "success") {
    throw new Error("net purchase receipt is not successful");
  }
  if (expectedHash !== null && (!receipt.transactionHash || !same(receipt.transactionHash, expectedHash))) {
    throw new Error("net purchase transaction hash mismatch");
  }
  const raw = acctOf(holder), h = typeof raw === "string" ? { address: raw } : raw;
  const purchases = parseEventLogs({ abi: market.guard.abi, logs: receipt.logs, eventName: "NetBuckPurchased" })
    .filter((e) => same(e.address, market.guard.address) && same(e.args.buyer, h.address)
      && same(e.args.recipient, h.address) && same(e.args.pool, market.pool.address));
  if (purchases.length !== 1) throw new Error("net BUCK purchase event missing or ambiguous");
  const [purchase] = purchases;
  const net = netTransferValue(world.buck, receipt, market.pool.address, h.address);
  if (!net.count || net.nominal !== purchase.args.nominal || net.netScaled !== purchase.args.netScaled)
    throw new Error("net BUCK transfer evidence mismatch");
  return { paid: purchase.args.paid, received: net.netScaled / (10n ** 36n),
    receivedScaled: net.netScaled, nominalReceived: net.nominal, transactionHash: receipt.transactionHash };
}

/** Authenticate one single-pool sale; a successful sale is not mortgage repayment.
 *  The caller must apply its saved transaction hash once and handle finality. */
export function netSaleFromReceipt(world, market, holder, receipt,
  { expectedHash = null, expectedInput = null, minimumOut = null } = {}) {
  if (world.accounting !== "net" || receipt?.status !== "success" || !receipt.transactionHash) {
    throw new Error("net sale receipt is not successful or lacks a hash");
  }
  if (typeof expectedHash !== "string" || !expectedHash || !same(receipt.transactionHash, expectedHash)) {
    throw new Error("net sale transaction hash mismatch");
  }
  if (typeof expectedInput !== "bigint" || expectedInput <= 0n
      || typeof minimumOut !== "bigint" || minimumOut <= 0n) throw new Error("invalid net sale bounds");
  const raw = acctOf(holder), h = typeof raw === "string" ? { address: raw } : raw;
  const transfers = parseEventLogs({ abi: world.buck.abi, logs: receipt.logs, eventName: "Transfer" })
    .filter((e) => same(e.address, world.buck.address) && same(e.args.from, h.address) && same(e.args.to, market.pool.address));
  const payouts = parseEventLogs({ abi: market.usdc.abi, logs: receipt.logs, eventName: "Transfer" })
    .filter((e) => same(e.address, market.usdc.address) && same(e.args.from, market.pool.address) && same(e.args.to, h.address));
  if (transfers.length !== 1 || payouts.length !== 1) throw new Error("net sale transfer evidence missing or ambiguous");
  const nominalPaid = transfers[0].args.value, received = payouts[0].args.value;
  const net = netTransferValue(world.buck, receipt, h.address, market.pool.address);
  if (nominalPaid === 0n || received === 0n || net.count !== 1 || net.nominal !== nominalPaid
      || nominalPaid > expectedInput || received < minimumOut) {
    throw new Error("net sale transfer evidence does not match bounds");
  }
  return { paid: net.netScaled / (10n ** 36n), paidScaled: net.netScaled, nominalPaid, received,
    transactionHash: receipt.transactionHash };
}

/** Buy BUCK with exactly `usdcIn`; returns {paid, received}. */
export function buyBuck(world, market, holder, usdcIn, { minOut = 0n } = {}) {
  return swap(world, market, holder, market.usdc, world.buck,
    { amount: usdcIn, limit: minOut }, "trade:buy");
}

/** Buy exactly `buckOut` BUCK, paying at most `maxUsdcIn`. */
export function buyBuckExact(world, market, holder, buckOut, maxUsdcIn) {
  return swap(world, market, holder, market.usdc, world.buck,
    { exactOut: true, amount: buckOut, limit: maxUsdcIn }, "trade:buy-exact");
}

/** Sell up to `buckIn` BUCK for USDC; net mode requires a positive `minOut`. */
export function sellBuck(world, market, holder, buckIn, { minOut = 0n } = {}) {
  return swap(world, market, holder, world.buck, market.usdc,
    { amount: buckIn, limit: minOut }, "trade:sell");
}
