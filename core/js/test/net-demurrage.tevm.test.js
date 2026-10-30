// A linked, cash-only BuckNet smoke run in the browser-compatible EVM.
// Test identities and static K deliberately do not model a real economy.
import { test } from "node:test";
import assert from "node:assert/strict";
import { encodeFunctionData } from "viem";
import { tevmSession } from "../src/backends.js";
import { loadArtifact } from "../src/nodefs.js";
import { linkArtifact } from "../src/artifacts.js";
import { advanceTime } from "../src/buckworld.js";

const RAW = 1_000_000n;
const YEAR = 365 * 86400 + 6 * 3600;
const required = ["BuckNet", "BuckNetMaturity", "BuckNetTransfers", "JubileeFund", "IdentityRegistryHarness",
  "BuckCredit", "BuckKControllerStatic", "UniswapV3Factory", "BuckBasketEquity", "SimLP"];
let skip = false;
try {
  for (const name of required) loadArtifact(name);
  if (!loadArtifact("BuckNet").linkReferences) skip = "BuckNet link metadata unavailable; build the checkout";
} catch {
  skip = "net/simulation artifacts unavailable; run make nix-sim-build";
}

test("linked BuckNet issues, accrues and retires cash-only credit", { skip }, async () => {
  const s = await tevmSession();
  const gov = s.account.address;
  const deploy = (name, args = []) => s.deploy(loadArtifact(name), args, { name });
  const registry = await deploy("IdentityRegistryHarness", [gov]);
  const credit = await deploy("BuckCredit");
  const controller = await deploy("BuckKControllerStatic", [10n ** 18n, gov]);
  const fund = await deploy("JubileeFund", [gov]);
  const maturity = await deploy("BuckNetMaturity");
  const transfers = await deploy("BuckNetTransfers");
  const linked = linkArtifact(loadArtifact("BuckNet"), {
    "src/lib/BuckNetMaturity.sol:BuckNetMaturity": maturity.address,
    "src/lib/BuckNetTransfers.sol:BuckNetTransfers": transfers.address,
  });
  const buck = await s.deploy(linked, [credit.address, controller.address, registry.address, fund.address],
    { name: "BuckNet" });
  const factory = await deploy("UniswapV3Factory");
  const basket = await deploy("BuckBasketEquity",
    [buck.address, controller.address, factory.address, gov, 3000, 600, 64, 500n, 1000n]);
  const actor = await deploy("SimLP"); // Public local fixture; not an economic household.
  const g = { X: 1n, Y: 2n };
  const E = { R: g, C: g };
  for (const [target, carrying] of [[fund.address, true], [basket.address, false], [actor.address, false]]) {
    await s.send(registry, "bindContract", [target, g, E, true, carrying]);
  }
  await s.send(credit, "setBuck", [buck.address]);
  await s.send(registry, "setBuck", [buck.address]);
  await s.send(basket, "enableNetAccounting");
  await s.send(fund, "configure", [buck.address, basket.address]);
  const exec = (target, functionName, args) => s.send(actor, "exec", [target.address,
    encodeFunctionData({ abi: target.abi, functionName, args })]);
  await exec(credit, "setCreditIssuer", [gov, true]);
  await s.send(credit, "createCredit", [actor.address, 0, 100n * RAW, 100n * RAW, 0, 0, 0, 0]);
  await exec(buck, "issue", [0n, 100n * RAW, 100n * RAW]);
  assert.equal(await s.call(buck, "balanceOf", [actor.address]), 100n * RAW);
  assert.equal(await s.call(buck, "totalIssued"), 100n * RAW);
  await advanceTime({ session: s }, YEAR);
  const [, cash] = await s.call(buck, "fundBalancesScaled");
  assert.equal(await s.call(buck, "balanceOf", [actor.address]), 98_019_867n);
  const [relief, quotedCash, exceeds] = await s.call(fund, "previewRelief", [0n, RAW]);
  assert.equal(quotedCash, cash);
  assert.equal(relief, cash / 100n);
  assert.equal(exceeds, false);
  const deadline = (await s.client.getBlock()).timestamp + 3600n;
  await exec(buck, "authorizeReduction", [0n, RAW, 0n, deadline]);
  await exec(fund, "reduce", [0n, RAW]);
  assert.equal(await s.call(buck, "totalIssued"), 99n * RAW);
  assert.equal((await s.call(credit, "issuanceOf", [0n]))[0], 99n * RAW);
  assert.ok((await s.call(buck, "fundBalancesScaled"))[1] < cash);
  assert.deepEqual(s.mismatches, []);
});
