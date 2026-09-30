// The BUCK/USDC market on production identity contracts and the unmodified
// Uniswap periphery (src/market.js):
//
//   * the pool bound public+carrying by the production UniswapV3BindingAdapter,
//     SimLP by its own authorization; the Universal Router and Permit2 NEVER
//     bound -- and BUCK never rests on the router,
//   * a registered wallet cannot trade before its identity handshake with
//     the pool, then buys (exact in and exact out) and SELLS through Permit2,
//   * real premiums: an insured credit's activation is refused by the
//     funding gate until the holder holds the premium's principal, which it
//     buys in the pool; the principal lands in the insurance pool.
//
// Skips cleanly when the kernels, the Foundry build or the vendored
// periphery (alberta_buck/sim/artifacts) are missing.

import { describe, it, before } from "node:test";
import assert from "node:assert/strict";
import { privateKeyToAccount } from "viem/accounts";

import { tevmSession } from "../src/backends.js";
import { loadAnyArtifact } from "../src/nodefs.js";

let id = null;
try {
  id = await import("../src/identity.js");
} catch {
  // kernels not built
}
let built = true;
try {
  for (const n of ["UniswapV3Factory", "UniswapV3BindingAdapter", "WETH9", "Permit2",
                   "UniversalRouter", "SimLP", "MockERC20"]) loadAnyArtifact(n);
} catch {
  built = false;
}
const skip = !id ? "kernels not built (make nix-core-build-wasm)"
  : !built ? "Foundry build or vendored periphery missing (make nix-build)" : false;

const E6 = 10n ** 6n;

describe("the BUCK/USDC market through an unbound router", { skip }, () => {
  let bw;
  let mk;
  let session;
  let world;
  let market;
  let rng;
  let insurancePool;
  let uma;

  before(async () => {
    bw = await import("../src/buckworld.js");
    mk = await import("../src/market.js");
    let seed = 0x3a5e7n;
    rng = () => {
      seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 256n) - 1n);
      const v = seed % id.ORDER;
      return v === 0n ? 1n : v;
    };
    session = await tevmSession();
    world = await bw.buildBuckWorld(session, loadAnyArtifact, { identity: id, rng });
    insurancePool = world.poolAcct;              // a SimLP the registry binds Carrying
    market = await mk.buildMarket(world, loadAnyArtifact, { rng });

    const acct = privateKeyToAccount("0x" + "0a".repeat(32));
    await bw.fundAccount(world, acct.address);
    uma = await bw.onboard(world, acct, { given_name: "Uma", family_name: "User", epoch: 42 },
      { rng });
    await mk.fiatIn(world, market, acct.address, 10_000n * E6);
  });

  const routerBuck = () => session.call(world.buck, "balanceOf", [market.router.address]);

  it("binds the pool and SimLP on production contracts, and leaves the periphery unbound", async () => {
    const is = (fn, a) => session.call(world.reg, fn, [a]);
    assert.equal(await is("isPublicIdentity", market.pool.address), true);
    assert.equal(await is("isCarrying", market.pool.address), true);
    assert.equal(await is("isPublicIdentity", market.simlp.address), true);
    assert.equal(await is("isCarrying", market.simlp.address), false);
    assert.equal(await is("isVerified", market.router.address), false);
    assert.equal(await is("isVerified", market.permit2.address), false);
    assert.equal(await is("isCarrying", world.poolAcct), true, "the insurance pool is Carrying");
    assert.equal(await mk.buckPrice(world, market), E6, "the pool opens at $1");
    const res = await mk.poolReserves(world, market);
    assert.ok(res.buck > 999_000n * E6 && res.usdc > 999_000n * E6);
  });

  it("refuses a wallet that has not introduced itself to the pool", async () => {
    await session.send(world.buck, "approve", [market.permit2.address, 1n], { account: uma.account });
    await assert.rejects(mk.buyBuck(world, market, uma, 100n * E6));
    session.mismatches.length = 0;
    assert.equal(await mk.tradingOpen(world, market, uma.account.address), false);
  });

  it("opens trading once, then buys and sells through Permit2", async () => {
    await mk.openTrading(world, market, uma, { rng });
    assert.equal(await mk.tradingOpen(world, market, uma.account.address), true);
    const b = await session.client.getBlock();
    await mk.openTrading(world, market, uma, { rng });           // nothing left to do
    assert.equal((await session.client.getBlock()).number, b.number);

    const buy = await mk.buyBuck(world, market, uma, 1_000n * E6);
    assert.equal(buy.paid, 1_000n * E6);
    assert.ok(buy.received > 998n * E6 && buy.received < 1_000n * E6, "0.05 % fee and a little slippage");
    assert.equal(await routerBuck(), 0n);

    const exact = await mk.buyBuckExact(world, market, uma, 500n * E6, 600n * E6);
    assert.equal(exact.received, 500n * E6);
    // The 1,000 bought above moved the price ~0.2 % in $1M of depth; plus the fee.
    assert.ok(exact.paid > 500n * E6 && exact.paid < 503n * E6, `paid ${exact.paid}`);
    assert.equal(await routerBuck(), 0n);

    // The route an unbound router could not take by pre-funding: BUCK in.
    const sell = await mk.sellBuck(world, market, uma, 300n * E6);
    assert.equal(sell.paid, 300n * E6);
    // BUCK trades a little above $1 after the buys; less the fee.
    assert.ok(sell.received > 299n * E6 && sell.received < 302n * E6, `received ${sell.received}`);
    assert.equal(await routerBuck(), 0n);
    assert.equal(session.mismatches.length, 0);
  });

  it("charges a real premium: refused without the principal, paid once it is held", async () => {
    const home = await bw.insureAsset(world, session.account, uma, {
      face: 400_000n * E6, floor: 120_000n * E6, assetClass: 1,
      depType: bw.DEPRECIATION.LINEAR, depRate: 250, premiumRate: 35,   // 0.35 %/yr
    });
    const want = 100_000n * E6;
    const q = await bw.quoteActivation(world, uma, want);
    assert.deepEqual(q.tokenIds, [home]);
    assert.equal(q.factor, 10n ** 18n, "no basket: the funding factor is 1.0");
    // principal = net * e / (K - e), e = 10r: 0.35 %/yr at the 10 % pool ROI.
    const k = await session.call(world.kctrl, "currentBuckK");
    const e = 350n * 10n ** 14n;
    const expect = (want * e) / (k - e);
    assert.ok(q.principal + 2n >= expect && q.principal <= expect + 2n, `${q.principal} vs ${expect}`);
    assert.equal(q.required, q.principal);
    assert.ok(q.shortfall > 0n && q.shortfall === q.required - q.balance);

    // Uma holds ~1,200 BUCK; 100,000 needs ~4,895 up front at K 0.75.
    await assert.rejects(bw.activateCredit(world, uma, want), /insufficient mint funding/);
    session.mismatches.length = 0;

    await mk.buyBuckExact(world, market, uma, q.shortfall + 10n * E6, q.shortfall * 2n);
    const q2 = await bw.quoteActivation(world, uma, want);
    assert.equal(q2.shortfall, 0n);
    const pool0 = await session.call(world.buck, "balanceOf", [insurancePool]);
    const minted = await bw.activateCredit(world, uma, want);
    assert.equal(minted.premium, q2.principal);
    assert.equal(await session.call(world.buck, "balanceOf", [insurancePool]) - pool0,
      minted.premium, "the premium's principal is in the insurance pool");
    // Coverage is face: seconds more depreciation take a hair more of it.
    assert.ok(minted.coverage >= q2.coverage && minted.coverage - q2.coverage < 1_000n);
  });

  it("round-trips the market as data", async () => {
    const rec = mk.marketRecord(market);
    const back = mk.attachMarket(session, loadAnyArtifact, rec);
    assert.equal(back.pool.address, market.pool.address);
    assert.equal(await mk.buckPrice(world, back), await mk.buckPrice(world, market));
    assert.deepEqual(back.operator.kp, market.operator.kp);
    const known = mk.marketContracts(market, loadAnyArtifact);
    assert.equal(known[market.pool.address].name, "BUCK/USDC pool");
  });
});
