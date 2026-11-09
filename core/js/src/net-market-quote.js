// Same-block integer V3 estimates; Python: buck_core/net_market_quote.py.
// Constant active liquidity can change at crossed ticks. These are decision
// inputs, not fair-price or execution promises. Actual buys need the net
// guard and actual sales need a positive on-chain settlement minimum.
const MAX = (1n << 256n) - 1n, PPM = 1_000_000n, Q96 = 1n << 96n;
const ceil = (n, d) => (n + d - 1n) / d;
const amount = (v) => {
  if (typeof v !== "bigint" || v < 0n || v > MAX) throw new Error("invalid net market amount");
};
const validate = (q) => {
  const keys = ["block_number", "reserve_usdc", "reserve_buck", "gross_scaled", "net_scaled", "fee_ppm", "price_num", "price_den"];
  if (!q || keys.some((k) => typeof q[k] !== "bigint") || q.block_number < 0n
      || [q.reserve_usdc, q.reserve_buck, q.gross_scaled, q.price_num, q.price_den].some((v) => v <= 0n)
      || q.reserve_usdc > MAX || q.reserve_buck > MAX
      || q.net_scaled < 0n || q.net_scaled > q.gross_scaled || q.gross_scaled > MAX || q.fee_ppm < 0n || q.fee_ppm >= PPM) {
    throw new Error("invalid net market quote");
  }
};
export function netSaleExpected(q, buck) {
  validate(q); amount(buck);
  const effective = buck * (PPM - q.fee_ppm) / PPM;
  return q.reserve_usdc * effective / (q.reserve_buck + effective);
}
export function netPurchaseExpected(q, usdc) {
  validate(q); amount(usdc);
  const effective = usdc * (PPM - q.fee_ppm) / PPM;
  const nominal = q.reserve_buck * effective / (q.reserve_usdc + effective);
  return nominal * q.net_scaled / q.gross_scaled;
}
// Smallest nominal BUCK input whose local sale estimate reaches usdc.
// This shares the forward estimate's constant-active-liquidity limits.
// Ordinary households spend full-value units; Carrying callers budget their
// own net cost separately from the nominal quantity passed to the venue.
export function netSaleInput(q, usdc) {
  validate(q); amount(usdc);
  if (!usdc) return 0n;
  if (usdc >= q.reserve_usdc) return null;
  const effective = ceil(usdc * q.reserve_buck, q.reserve_usdc - usdc);
  const needed = ceil(effective * PPM, PPM - q.fee_ppm);
  return needed <= MAX ? needed : null;
}
export function netPurchaseCost(q, netBuck) {
  validate(q); amount(netBuck);
  if (!netBuck) return 0n;
  if (!q.net_scaled) return null;
  const nominal = ceil(netBuck * q.gross_scaled, q.net_scaled);
  if (nominal >= q.reserve_buck) return null;
  const effective = ceil(q.reserve_usdc * nominal, q.reserve_buck - nominal) + 1n;
  const cost = ceil(effective * PPM, PPM - q.fee_ppm);
  return cost <= MAX ? cost : null;
}
export async function observeNetMarket(session, buck, settlement, pool, feePpm, { blockNumber = null } = {}) {
  if (!Number.isSafeInteger(feePpm) || feePpm < 0 || feePpm >= Number(PPM)) throw new Error("invalid net market fee");
  const block = blockNumber ?? (await session.client.getBlock()).number;
  if (typeof block !== "bigint" || block < 0n) throw new Error("invalid net market block");
  const read = (c, functionName, args = []) => session.client.readContract({ address: c.address, abi: c.abi,
    functionName, args, blockNumber: block });
  const t0 = (await read(pool, "token0")).toLowerCase(), t1 = (await read(pool, "token1")).toLowerCase();
  const asset = buck.address.toLowerCase(), usd = settlement.address.toLowerCase();
  if (!(t0 === asset && t1 === usd || t1 === asset && t0 === usd)) {
    throw new Error("net market quote requires the configured BUCK/settlement pair");
  }
  if (BigInt(await read(pool, "fee")) !== BigInt(feePpm)) throw new Error("net market quote fee mismatch");
  const flags = ["isVerified", "isCarrying"].map((name) => ({ type: "function", name, stateMutability: "view",
    inputs: [{ name: "account", type: "address" }], outputs: [{ name: "", type: "bool" }] }));
  const registry = session.contractAt(flags, await read(buck, "identity"));
  if (!await read(registry, "isVerified", [pool.address]) || !await read(registry, "isCarrying", [pool.address])) {
    throw new Error("net market quote needs a verified Carrying pool");
  }
  const [sqrt] = await read(pool, "slot0"), liquidity = await read(pool, "liquidity");
  if (sqrt <= 0n || liquidity <= 0n) throw new Error("net market quote needs initialized liquidity");
  const r0 = liquidity * Q96 / sqrt, r1 = liquidity * sqrt / Q96;
  const [gross, net] = await read(buck, "accountBalancesScaled", [pool.address]);
  const q = { block_number: block, reserve_usdc: t0 === asset ? r1 : r0, reserve_buck: t0 === asset ? r0 : r1,
    gross_scaled: gross, net_scaled: net, fee_ppm: BigInt(feePpm),
    price_num: t0 === asset ? sqrt * sqrt : Q96 * Q96, price_den: t0 === asset ? Q96 * Q96 : sqrt * sqrt };
  validate(q);
  return q;
}
