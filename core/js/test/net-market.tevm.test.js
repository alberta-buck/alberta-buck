// Real V3 pool and real identity approvals with the cash-only net world.
// This does not model the multi-asset basket or a five-year economy.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { getContractAddress } from "viem";
import { devAccount, tevmSession } from "../src/backends.js";
import { buildBuckWorld, onboard, insureAsset, advanceTime, DAY } from "../src/buckworld.js";
import { netCreditOps } from "../src/net-world.js";
import { buildMarket, marketRecord, attachMarket, attachNetMarket, openTrading, fiatIn, buyBuck, buyBuckExact, sellBuck }
  from "../src/market.js";

let identity;
try { identity = await import("../src/identity.js"); } catch { /* node kernel not built */ }
const bundle = new URL("../artifacts/sandbox.mjs", import.meta.url);
const skip = !identity ? "identity kernel not built (make nix-core-build-wasm)"
  : !existsSync(bundle) ? "sandbox artifacts not built (make nix-venv-net-demurrage-smoke)" : false;
const fields = (name) => ({ given_name: name, family_name: "Market", jurisdiction: "Alberta, Canada",
  id_type: "Alberta Identity", id_number: `NET-MARKET-${name}`, date_of_birth: "1990-01-01",
  issued_at: "2026-01-01T00:00:00Z", epoch: 42 });

test("aged carrying V3 output is guarded and reported in net BUCK in both token orders", { skip }, async () => {
  const { artifact } = await import(bundle);
  let seed = 0x109ab42n;
  const rng = () => {
    seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 256n) - 1n);
    return seed % (identity.ORDER - 1n) + 1n;
  };
  const s = await tevmSession();
  const world = await buildBuckWorld(s, artifact, { identity, rng, accounting: "net" });
  const operator = await onboard(world, s.account, fields("Operator"), { rng });
  let lastMarket, lastBuyer;
  for (const [index, buckIs0] of [true, false].entries()) {
    // buildMarket's first deployment is USDC. Adjust the deployer's nonce so
    // its deterministic address exercises each canonical V3 ordering.
    let aligned = false;
    for (let i = 0; i < 32; i++) {
      const nonce = await s.client.getTransactionCount({ address: s.account.address });
      const next = getContractAddress({ from: s.account.address, nonce: BigInt(nonce) });
      if ((BigInt(world.buck.address) < BigInt(next)) === buckIs0) { aligned = true; break; }
      await s.deploy(artifact("MockERC20"), ["Nonce filler", "FILL", 6], { name: "NonceFiller" });
    }
    assert.ok(aligned, "could not construct requested token ordering");
    const market = await buildMarket(world, artifact, { operator, depth: 1_000_000_000n });
    const recorded = marketRecord(market);
    assert.throws(() => attachMarket(s, artifact, recorded), /attachNetMarket/);
    const attached = await attachNetMarket(s, artifact, recorded, world);
    assert.equal(attached.guard.address.toLowerCase(), market.guard.address.toLowerCase());
    const forgedGuard = structuredClone(recorded);
    forgedGuard.addresses.guard = market.pool.address;
    await assert.rejects(attachNetMarket(s, artifact, forgedGuard, world), /guard code mismatch/);
    assert.equal((await s.call(market.pool, "token0")).toLowerCase() === world.buck.address.toLowerCase(), buckIs0);
    assert.ok(market.providerCreditId !== null);
    const provider = await s.call(world.credit, "issuanceOf", [market.providerCreditId]);
    assert.ok(provider[0] > 1_000_000_000n);
    assert.equal(await s.call(world.reg, "isCarrying", [market.pool.address]), true);
    const buyer = await onboard(world, devAccount(index + 1), fields(`Buyer${index}`), { rng });
    lastMarket = market;
    lastBuyer = buyer;
    await fiatIn(world, market, buyer.account.address, 500_000_000n);
    await openTrading(world, market, buyer, { rng });
    const buyerId = await insureAsset(world, world.operator.account, buyer, { face: 200_000_000n, premiumRate: 0 });
    const creditOps = netCreditOps(s, world.net);
    await creditOps.issue(buyer.account, buyerId, 200_000_000n, 100_000_000n);
    const sale = await sellBuck(world, market, buyer, 99_000_000n);
    assert.ok(sale.received > 0n);
    assert.equal((await creditOps.facility(buyerId)).issued, 100_000_000n);
    await advanceTime(world, 365 * DAY + 6 * 3600);
    const poolNominal = await s.call(world.buck, "balanceOf", [market.pool.address]);
    const poolNet = await s.call(world.buck, "netBalanceOf", [market.pool.address]);
    assert.ok(poolNet < poolNominal);
    assert.ok(await s.call(world.buck, "balanceOf", [buyer.account.address]) < 2_000_000n);
    const result = await buyBuck(world, market, buyer, 100_000_000n, { minOut: 80_000_000n });
    assert.equal(result.paid, 100_000_000n);
    assert.ok(result.received > 0n && result.received < result.nominalReceived);
    assert.ok(await s.call(world.buck, "balanceOf", [buyer.account.address]) >= result.received);
    assert.equal((await s.call(world.credit, "issuanceOf", [market.providerCreditId]))[0], provider[0]);
    assert.equal((await creditOps.facility(buyerId)).issued, 100_000_000n);
    const deadline = (await s.client.getBlock()).timestamp + 3600n;
    await creditOps.authorizeReduction(buyer.account, buyerId, 10_000_000n, 0n, deadline);
    await creditOps.reduceAuthorized(buyer.account, buyerId, 10_000_000n);
    assert.equal((await creditOps.facility(buyerId)).issued, 90_000_000n);
    const beforeExact = await s.call(market.usdc, "balanceOf", [buyer.account.address]);
    const exact = await buyBuckExact(world, market, buyer, 20_000_000n, 40_000_000n);
    assert.ok(exact.received >= 20_000_000n);
    assert.ok(exact.nominalReceived > exact.received && exact.paid < 40_000_000n);
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), beforeExact - exact.paid);
    const usdBefore = await s.call(market.usdc, "balanceOf", [buyer.account.address]);
    const refused = await s.send(market.guard, "buyExactInput",
      [market.pool.address, 100_000_000n, 200_000_000n, buyer.account.address, 0n],
      { account: buyer.account, expect: "revert" });
    assert.equal(refused.status, "reverted");
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), usdBefore);
    const tooExpensive = await s.send(market.guard, "buyExactOutput",
      [market.pool.address, 20_000_000n, 1_000_000n, buyer.account.address, 0n],
      { account: buyer.account, expect: "revert" });
    assert.equal(tooExpensive.status, "reverted");
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), usdBefore);
    const callback = await s.send(market.guard, "uniswapV3SwapCallback", [1n, -1n, "0x"], { expect: "revert" });
    assert.equal(callback.status, "reverted");
    const wrongRecipient = await s.send(market.guard, "buyExactInput",
      [market.pool.address, 1_000_000n, 0n, market.pool.address, 0n],
      { account: buyer.account, expect: "revert" });
    assert.equal(wrongRecipient.status, "reverted");
  }
  // A nominal pool can remain nonzero when its full-value balance is zero.
  // Exact net output must refuse without consuming the buyer's settlement.
  await advanceTime(world, 9001 * (365 * DAY + 6 * 3600));
  assert.equal(await s.call(world.buck, "netBalanceOf", [lastMarket.pool.address]), 0n);
  assert.ok(await s.call(world.buck, "balanceOf", [lastMarket.pool.address]) > 0n);
  const cashBefore = await s.call(lastMarket.usdc, "balanceOf", [lastBuyer.account.address]);
  const empty = await s.send(lastMarket.guard, "buyExactOutput",
    [lastMarket.pool.address, 1_000_000n, 10_000_000n, lastBuyer.account.address, 0n],
    { account: lastBuyer.account, expect: "revert" });
  assert.equal(empty.status, "reverted");
  assert.equal(await s.call(lastMarket.usdc, "balanceOf", [lastBuyer.account.address]), cashBefore);
  assert.deepEqual(s.mismatches, []);
});
