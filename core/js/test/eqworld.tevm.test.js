// Risk-kill gate: the MINIMAL EQUILIBRIUM WORLD fits and runs on Tevm.
//
// The stage-6 question (platform doc): can deploy.py's basket world --
// the full BUCK stack + BuckBasketProRata/venue + TOKEN/USDC +
// TOKEN/BUCK + floating BUCK/USDC pools + Universal Router, every
// BUCK-touching contract identity-bound -- deploy inside tevm's block
// gas budget, and do its heaviest NEW ops (a basket deposit that mints
// BUCK and LPs a real pool; the permissionless PID compute()) execute?
//
// This transcribes deploy.py's op order with one basket TOKEN, then:
//   1. seeds the basket via depositToken (the bootstrap-DM-agent op),
//   2. jumps a day and advances the PID (the PidKeeper op),
// and reports every deploy's gasUsed.  Green here = stage 6's
// buildEquilibriumWorld is a port, not a research project.
//
// Skips cleanly when the wasm kernels or forge artifacts are missing.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

import { encodeFunctionData } from "viem";

import { tevmSession } from "../src/backends.js";
import { loadArtifact, artifactsAvailable, repoRoot } from "../src/nodefs.js";
import { buildBuckWorld, advanceTime, DAY } from "../src/buckworld.js";
import { deployUniversalRouter } from "../src/router.js";
import { Q96, fullRangeTicks, sqrtPriceX96 } from "../src/v3.js";
import { JournalWriter } from "../src/journal.js";

let id = null;
try {
  id = await import("../src/identity.js");
} catch {
  // kernel not built
}
const urPath = (() => {
  try {
    return join(repoRoot(), "alberta_buck", "sim", "artifacts",
                "UniversalRouter.json");
  } catch { return null; }
})();
const skip = !id ? "identity kernel not built (make nix-core-build-wasm)"
  : !artifactsAvailable() ? "forge artifacts not built (make nix-build)"
  : !(urPath && existsSync(urPath)) ? "vendored UR artifact missing"
  : false;

// deploy.py's constants, one-token minimal world.
const FEE_USDC = 3000, FEE_BUCK = 3000, FEE_UB = 500;
const SPACING = { 3000: 60, 500: 10 };
const TARGET_BUCK = 10n ** 13n;          // $10M @ 6-dec, per pool
const P0 = 2_500_000n;                   // $2.50/TOK == 2.5 BUCK/TOK at parity
const E18 = 10n ** 18n;

// The public bind identity (sim/identity.py BIND_PK/BIND_E: the G1
// generator).  BN254.sol ABI components are UPPERCASE.
const G = { X: 1n, Y: 2n };
const BIND_E = { R: G, C: G };

test("eqworld: the minimal equilibrium world deploys and runs on tevm", { skip }, async () => {
  const t0 = performance.now();
  const lines = [];
  const session = await tevmSession(
    { journal: new JournalWriter((l) => lines.push(l)) });
  const gas = 15_000_000n;
  const me = session.account.address;

  // --- the proven BUCK identity + Direct stack ------------------------
  const world = await buildBuckWorld(session, loadArtifact,
    { identity: id.default, registryArtifact: "IdentityRegistryHarness",
      creditArtifact: "BuckCreditHarness" });
  const { reg, credit, kctrl, buck } = world;
  const bind = (addr, carrying, tag) =>
    session.send(reg, "bindContract", [addr, G, BIND_E, true, carrying], { tag });

  // --- factory, basket + venue, wiring (deploy.py order) --------------
  const v3f = await session.deploy(loadArtifact("UniswapV3Factory"), [],
    { name: "UniswapV3Factory", gas });
  const basketArt = loadArtifact("BuckBasketProRata");
  const basket = await session.deploy(basketArt,
    [buck.address, kctrl.address, v3f.address, me, FEE_BUCK, 600, 64, 500, 1000],
    { name: "BuckBasketProRata", gas });
  const venue = await session.deploy(loadArtifact("BuckBasketUniswapV3"), [],
    { name: "BuckBasketUniswapV3", gas });
  await session.send(basket, "setVenue", [venue.address], { tag: "eq:setVenue" });
  // The insurance pool wires the basket; the world's pool is a contract
  // (bound Carrying) that acts through its exec().
  await session.send(world.pool, "exec", [buck.address, encodeFunctionData({
    abi: buck.abi, functionName: "setBasket", args: [basket.address] })],
    { tag: "eq:buck.setBasket" });
  await session.send(kctrl, "setBasket", [basket.address], { tag: "eq:kctrl.setBasket" });
  await bind(basket.address, true, "eq:bind:basket");
  // The union ABI: facet views (basketValueInBuck) served via the shell's
  // fallback -- exactly deploy.py's re-wrap.
  const facetAbi = loadArtifact("BuckBasketUniswapV3").abi;
  const seen = new Set(basketArt.abi.map((e) => `${e.type}:${e.name}`));
  const basketU = session.contractAt(
    basketArt.abi.concat(facetAbi.filter((e) => !seen.has(`${e.type}:${e.name}`))),
    basket.address);

  // --- tokens + SimLP --------------------------------------------------
  const usdc = await session.deploy(loadArtifact("MockERC20"),
    ["USD Coin", "USDC", 6], { name: "USDC", gas });
  const token = await session.deploy(loadArtifact("MockERC20"),
    ["Construction", "CNST", 18], { name: "CNST", gas });
  const simlp = await session.deploy(loadArtifact("SimLP"), [], { name: "SimLP", gas });
  await bind(simlp.address, false, "eq:bind:simlp");   // public, NON-carrying
  const big = 10n ** 30n;
  await session.send(usdc, "mint", [simlp.address, big], { tag: "eq:stock-usdc" });
  await session.send(token, "mint", [simlp.address, big], { tag: "eq:stock-token" });

  // --- TOKEN/USDC pool (the whale's reference market) ------------------
  const poolAbi = loadArtifact("UniswapV3Pool").abi;
  await session.send(v3f, "createPool", [token.address, usdc.address, FEE_USDC],
                     { tag: "eq:createPool:tok-usdc" });
  const pu = await session.call(v3f, "getPool", [token.address, usdc.address, FEE_USDC]);
  const poolTU = session.contractAt(poolAbi, pu);
  const sp = sqrtPriceX96(token.address, E18, usdc.address, P0);
  await session.send(poolTU, "initialize", [sp], { tag: "eq:init:tok-usdc" });
  const tu0 = await session.call(poolTU, "token0");
  const tu1 = await session.call(poolTU, "token1");
  const Lu = usdc.address.toLowerCase() === tu0.toLowerCase()
    ? (TARGET_BUCK * sp) / Q96 : (TARGET_BUCK * Q96) / sp;
  const [lo, hi] = fullRangeTicks(SPACING[FEE_USDC]);
  await session.send(simlp, "mint", [pu, lo, hi, Lu, tu0, tu1],
                     { tag: "eq:seed:tok-usdc" });

  // --- TOKEN/BUCK basket pool via addBasketToken (bound before LP) -----
  await session.send(basket, "addBasketToken",
    [token.address, 18, P0, 0, FEE_BUCK], { tag: "eq:addBasketToken" });
  const pb = await session.call(v3f, "getPool", [token.address, buck.address, FEE_BUCK]);
  await bind(pb, true, "eq:bind:pool-tok-buck");

  // --- floating BUCK/USDC pool: SimLP is the BUCK-backed LP ------------
  const k0 = await session.call(kctrl, "buckK");
  const mintAmt = ((TARGET_BUCK * E18) / k0) * 12n / 10n;
  const FACE = 2n * TARGET_BUCK > (mintAmt * 12n) / 10n
    ? 2n * TARGET_BUCK : (mintAmt * 12n) / 10n;
  const now = (await session.client.getBlock()).timestamp;
  await session.send(credit, "createCredit",
    [simlp.address, 0, FACE, 0n, 0, 0, now, 0], { tag: "eq:credit:simlp" });
  await session.send(simlp, "exec",
    [buck.address, encodeFunctionData(
      { abi: buck.abi, functionName: "mint", args: [mintAmt] })],
    { tag: "eq:simlp-mint-buck", gas: 3_000_000n });
  await session.send(v3f, "createPool", [buck.address, usdc.address, FEE_UB],
                     { tag: "eq:createPool:buck-usdc" });
  const pub = await session.call(v3f, "getPool", [buck.address, usdc.address, FEE_UB]);
  const poolUB = session.contractAt(poolAbi, pub);
  const spU = sqrtPriceX96(buck.address, 1_000_000n, usdc.address, 1_000_000n);
  await session.send(poolUB, "initialize", [spU], { tag: "eq:init:buck-usdc" });
  await bind(pub, true, "eq:bind:pool-buck-usdc");
  const ub0 = await session.call(poolUB, "token0");
  const ub1 = await session.call(poolUB, "token1");
  const Lub = usdc.address.toLowerCase() === ub0.toLowerCase()
    ? (TARGET_BUCK * spU) / Q96 : (TARGET_BUCK * Q96) / spU;
  const [loU, hiU] = fullRangeTicks(SPACING[FEE_UB]);
  await session.send(simlp, "mint", [pub, loU, hiU, Lub, ub0, ub1],
                     { tag: "eq:seed:buck-usdc" });
  assert.ok((await session.call(buck, "balanceOf", [pub])) > 0n,
    "the floating pool holds freshly issued BUCK");

  // --- the real router, identity-bound ---------------------------------
  const weth = await session.deploy(loadArtifact("WETH9"), [], { name: "WETH9", gas });
  const router = await deployUniversalRouter(
    session, JSON.parse(readFileSync(urPath, "utf8")),
    { weth: weth.address, v3Factory: v3f.address,
      poolInitCode: loadArtifact("UniswapV3Pool").bytecode });
  await bind(router.address, true, "eq:bind:router");

  // --- smoke 1: the bootstrap basket deposit (mints BUCK, LPs the pool)
  const dep = 1_000n * E18;                       // 1000 CNST ~= $2,500
  await session.send(token, "mint", [me, dep], { tag: "eq:depositor-stock" });
  await session.send(token, "approve", [basket.address, dep], { tag: "eq:approve" });
  await session.send(basket, "depositToken", [token.address, dep, 0n],
                     { tag: "eq:depositToken", gas: 3_000_000n });
  const buckInPool = await session.call(buck, "balanceOf", [pb]);
  assert.ok(buckInPool > 2_400n * 10n ** 6n,
    `deposit mints ~2500 BUCK into the basket pool (got ${buckInPool})`);
  const bvib = await session.call(basketU, "basketValueInBuck");
  assert.ok(bvib > 0n, "venue view basketValueInBuck serves via the shell");

  // --- smoke 2: a day passes; the PidKeeper advances the PID -----------
  await advanceTime(world, DAY);
  await session.send(kctrl, "compute", [], { tag: "eq:pid-compute" });
  const k1 = await session.call(kctrl, "buckK");
  assert.ok(k1 >= 0n && k1 <= 950_000_000_000_000_000n, "K stays railed");

  // --- the verdict ------------------------------------------------------
  const recs = lines.map(JSON.parse);
  const bad = recs.filter((r) => !r.matched);
  assert.equal(bad.length, 0,
    `every op must match its expectation: ${JSON.stringify(bad[0] ?? null)}`);
  const deploys = recs.filter((r) => r.op === "deploy");
  for (const d of deploys) {
    assert.ok(d.gas < 15_000_000, `${d.tag} gas ${d.gas} fits tevm`);
  }
  const total = recs.reduce((s, r) => s + r.gas, 0);
  const ms = Math.round(performance.now() - t0);
  console.log(`eqworld: ${recs.length} ops, ${total} total gas, ${ms} ms`);
  console.log(deploys.map((d) => `  ${d.tag}: ${d.gas}`).join("\n"));
  assert.ok(bvib > 0n && buckInPool > 0n);   // belt and suspenders
});
