// Cash-only net basket marks with test-only identity binding, not a full world.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { parseEventLogs } from "viem";
import { tevmSession, snapshotTevm, restoreTevm } from "../src/backends.js";
import { buildBuckWorld, attachNetBuckWorld, worldRecord, onboard, insureAsset, identityApprove, advanceTime, DAY } from "../src/buckworld.js";
import { netCreditOps } from "../src/net-world.js";
import { markNetBasketPositions } from "../src/basket.js";
import { loadArtifact } from "../src/nodefs.js";

let identity;
try { identity = await import("../src/identity.js"); } catch { /* node kernel not built */ }
const bundle = new URL("../artifacts/sandbox.mjs", import.meta.url);
const ZERO = "0x0000000000000000000000000000000000000000";
const skip = !identity ? "identity kernel not built" : !existsSync(bundle) ? "sandbox artifacts not built" : false;

test("cash-only net basket receipts expose open basis and mark", { skip }, async () => {
  const { artifact: bundled } = await import(bundle);
  const artifact = (name) => name === "IdentityRegistryHarness" ? loadArtifact(name) : bundled(name);
  let seed = 0x14a2b3c4n;
  const rng = () => {
    seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 256n) - 1n);
    return seed % (identity.ORDER - 1n) + 1n;
  };
  const s = await tevmSession();
  const world = await buildBuckWorld(s, artifact, { identity, rng, accounting: "net",
    registryArtifact: "IdentityRegistryHarness" });
  const basket = world.net.basket;
  const point = { X: 1n, Y: 2n };
  // This test-only registry bypasses contract binding authorization. The
  // production registry still needs a basket-controlled binding mechanism.
  await s.send(world.reg, "bindContract", [basket.address, point, { R: point, C: point }, true, false]);
  const holder = await onboard(world, s.account, {
    given_name: "Net", family_name: "Basket", jurisdiction: "Alberta, Canada",
    id_type: "Alberta Identity", id_number: "NET-BASKET-1", date_of_birth: "1990-01-01",
    issued_at: "2026-01-01T00:00:00Z", epoch: 42,
  }, { rng });
  const id = await insureAsset(world, world.operator.account, holder, { face: 100_000_000n, premiumRate: 0 });
  await netCreditOps(s, world.net).issue(holder.account, id, 100_000_000n, 60_000_000n, 0n, ZERO);
  await identityApprove(world, holder, { account: { address: basket.address },
    kp: { pk: { x: point.X, y: point.Y } }, fields: { given_name: "Basket" } }, { rng });
  await s.send(world.buck, "approve", [basket.address, 30_000_000n], { account: holder.account });
  const ids = [];
  for (const amount of [20_000_000n, 10_000_000n]) {
    const tx = await s.send(basket, "deposit", [world.buck.address, amount, 0n], { account: holder.account });
    ids.push(parseEventLogs({ abi: basket.abi, logs: tx.logs, eventName: "Deposited" })[0].args.receiptId);
  }
  const opening = await markNetBasketPositions(s, basket, world.buck, holder.account, ids);
  assert.equal(opening.basis, (await s.call(basket, "holdings", [ids[0]]))[1] + (await s.call(basket, "holdings", [ids[1]]))[1]);
  assert.equal(opening.mark, await s.call(basket, "valueOf", [ids[0]]) + await s.call(basket, "valueOf", [ids[1]]));
  assert.equal(opening.roi, Number(opening.mark - opening.basis) / Number(opening.basis));
  assert.equal((await markNetBasketPositions(s, basket, world.buck, holder.account, [])).roi, null);
  await assert.rejects(markNetBasketPositions(s, basket, world.buck, holder.account, [ids[0], ids[0]]), /invalid/);
  await assert.rejects(markNetBasketPositions(s, basket, world.buck, ZERO, ids), /invalid/);
  await assert.rejects(markNetBasketPositions(s, basket, { address: world.credit.address }, holder.account, ids), /configured/);
  await assert.rejects(markNetBasketPositions(s, basket, world.buck, world.operator.account, ids), /foreign/);
  await advanceTime(world, 365 * DAY + 6 * 3600);
  const aged = await markNetBasketPositions(s, basket, world.buck, holder.account, ids);
  assert.equal(aged.basis, opening.basis);
  assert.ok(aged.mark < opening.mark && aged.roi < 0);
  await s.send(basket, "redeemShares", [ids[0], aged.positions[0].shares / 2n, 0n], { account: holder.account });
  const reduced = await markNetBasketPositions(s, basket, world.buck, holder.account, ids);
  assert.ok(reduced.positions[0].shares < aged.positions[0].shares);
  assert.ok(reduced.positions[0].basis < aged.positions[0].basis);
  assert.equal(reduced.mark, await s.call(basket, "valueOf", [ids[0]]) + await s.call(basket, "valueOf", [ids[1]]));
  const historical = await markNetBasketPositions(s, basket, world.buck, holder.account, ids, { blockNumber: aged.blockNumber });
  assert.equal(historical.basis, aged.basis);
  assert.equal(historical.mark, aged.mark);
  const saved = { chain: await snapshotTevm(s), world: worldRecord(world) };
  const restoredSession = await tevmSession();
  await restoreTevm(restoredSession, saved.chain);
  const restored = await attachNetBuckWorld(restoredSession, artifact, saved.world, { identity });
  const restoredMark = await markNetBasketPositions(restoredSession, restored.net.basket, restored.buck, holder.account, ids);
  assert.equal(restoredMark.basis, reduced.basis);
  assert.equal(restoredMark.mark, reduced.mark);
  assert.deepEqual(s.mismatches, []);
});
