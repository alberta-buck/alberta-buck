// A cash-only BuckNet world with the actual credential registry, compiled
// identity kernel and the same generated artifacts the browser receives.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { parseEventLogs } from "viem";
import { tevmSession, devAccount, snapshotTevm, restoreTevm } from "../src/backends.js";
import { buildBuckWorld, attachBuckWorld, attachNetBuckWorld, worldRecord, onboard, insureAsset, identityApprove, advanceTime, DAY }
  from "../src/buckworld.js";
import { netCreditOps } from "../src/net-world.js";
import { encodeJSON, decodeJSON } from "../src/codec.js";

let identity;
try { identity = await import("../src/identity.js"); } catch { /* node kernel not built */ }
const bundle = new URL("../artifacts/sandbox.mjs", import.meta.url);
const ZERO = "0x0000000000000000000000000000000000000000";
const skip = !identity ? "identity kernel not built (make nix-core-build-wasm)"
  : !existsSync(bundle) ? "sandbox artifacts not built (make nix-venv-net-demurrage-smoke)" : false;

test("BuckNet preserves private identity approval and transfer receipts", { skip }, async () => {
  const { artifact } = await import(bundle);
  let seed = 0x253fa91n;
  const rng = () => {
    seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 256n) - 1n);
    return seed % (identity.ORDER - 1n) + 1n;
  };
  const s = await tevmSession();
  const world = await buildBuckWorld(s, artifact, { identity, rng, accounting: "net" });
  const fields = (name) => ({
    given_name: name, family_name: "Compatibility", jurisdiction: "Alberta, Canada",
    id_type: "Alberta Identity", id_number: `NET-IDENTITY-${name}`, date_of_birth: "1990-01-01",
    issued_at: "2026-01-01T00:00:00Z", epoch: 42,
  });
  const alice = await onboard(world, s.account, fields("Alice"), { rng });
  const bob = await onboard(world, devAccount(1), fields("Bob"), { rng });
  const id = await insureAsset(world, world.operator.account, alice, { face: 100_000_000n, premiumRate: 0 });
  const ops = netCreditOps(s, world.net);
  await ops.issue(alice.account, id, 100_000_000n, 60_000_000n, 0n, ZERO);

  await s.send(world.buck, "transfer", [devAccount(2).address, 1n],
    { account: alice.account, expect: "revert" });
  await s.send(world.buck, "transfer", [bob.account.address, 1n],
    { account: alice.account, expect: "revert" });

  // An ERC-20 allowance alone cannot authorize a confidential transfer.
  const allowed = 12_000_000n;
  const emptyFragment = `0x${"00".repeat(32)}`;
  await s.send(world.buck, "approve", [bob.account.address, allowed], { account: alice.account });
  await s.send(world.buck, "transferFrom", [alice.account.address, bob.account.address, 4_000_000n],
    { account: bob.account, expect: "revert" });
  assert.equal(await s.call(world.buck, "allowance", [alice.account.address, bob.account.address]), allowed);
  assert.equal(await s.call(world.buck, "receiptFragment", [alice.account.address, bob.account.address]), emptyFragment);

  // A malformed real Chaum-Pedersen proof cannot replace the allowance,
  // create a receipt fragment, or freeze the counterparty's carrying mode.
  const rPrime = rng();
  const eForBob = identity.elgamalEncrypt(alice.M, bob.kp.pk, rPrime);
  const proof = identity.chaumPedersenProve(alice.E, eForBob, alice.kp.pk, bob.kp.pk, alice.kp.sk, rPrime,
    BigInt(alice.account.address), BigInt(bob.account.address), BigInt(await s.client.getChainId()),
    BigInt(world.reg.address), rng(), rng());
  const pt = (p) => ({ X: p.x, Y: p.y });
  await s.send(world.buck, "approve", [bob.account.address, 1n,
    { R: pt(eForBob.R), C: pt(eForBob.C) },
    { e: (proof.e + 1n) % identity.ORDER, s1: proof.s1, s2: proof.s2,
      T1: pt(proof.T1), T2: pt(proof.T2), T3: pt(proof.T3) }],
  { account: alice.account, expect: "revert", gas: 1_000_000n });
  assert.equal(await s.call(world.buck, "allowance", [alice.account.address, bob.account.address]), allowed);
  assert.equal(await s.call(world.buck, "receiptFragment", [alice.account.address, bob.account.address]), emptyFragment);
  assert.equal(await s.call(world.reg, "carryingFrozen", [bob.account.address]), false);

  // Each registered party supplies its own valid proof. One direction alone
  // remains insufficient; a failed transfer rolls its allowance back.
  const aliceDisclosure = await identityApprove(world, alice, bob, { rng, allowance: allowed });
  assert.deepEqual(identity.elgamalDecrypt(aliceDisclosure, bob.kp.sk), alice.M);
  assert.equal(await s.call(world.reg, "carryingFrozen", [bob.account.address]), true);
  await s.send(world.buck, "transferFrom", [alice.account.address, bob.account.address, 4_000_000n],
    { account: bob.account, expect: "revert" });
  assert.equal(await s.call(world.buck, "allowance", [alice.account.address, bob.account.address]), allowed);
  const bobDisclosure = await identityApprove(world, bob, alice, { rng });
  assert.deepEqual(identity.elgamalDecrypt(bobDisclosure, alice.kp.sk), bob.M);

  const fromHash = await s.call(world.buck, "receiptFragment", [bob.account.address, alice.account.address]);
  const toHash = await s.call(world.buck, "receiptFragment", [alice.account.address, bob.account.address]);
  const receipt = await s.send(world.buck, "transferFrom", [alice.account.address, bob.account.address, 4_000_000n],
    { account: bob.account });
  const [identityReceipt] = parseEventLogs({ abi: world.buck.abi, logs: receipt.logs, eventName: "BuckTransferReceipt" });
  const [netReceipt] = parseEventLogs({ abi: world.buck.abi, logs: receipt.logs, eventName: "NetValueTransfer" });
  assert.equal(identityReceipt.args.fromCipherHash, fromHash);
  assert.equal(identityReceipt.args.toCipherHash, toHash);
  assert.equal(identityReceipt.args.amount, 4_000_000n);
  assert.equal(netReceipt.args.netScaled, 4_000_000n * 10n ** 36n);
  assert.equal(await s.call(world.buck, "allowance", [alice.account.address, bob.account.address]), 8_000_000n);
  assert.equal(await s.call(world.buck, "balanceOf", [bob.account.address]), 4_000_000n);
  const returned = await s.send(world.buck, "transfer", [alice.account.address, 1_000_000n], { account: bob.account });
  const [returnValue] = parseEventLogs({ abi: world.buck.abi, logs: returned.logs, eventName: "NetValueTransfer" });
  assert.equal(returnValue.args.netScaled, 1_000_000n * 10n ** 36n);
  const remaining = await s.call(world.buck, "balanceOf", [bob.account.address]);
  // A later block may floor a fraction of a raw unit of accrued demurrage.
  assert.ok(remaining >= 2_999_999n && remaining <= 3_000_000n);
  assert.equal((await ops.facility(id)).issued, 60_000_000n);
  assert.deepEqual(s.mismatches, []);
});

test("real identities can use and safely restore a cash-only BuckNet world", { skip }, async () => {
  const { artifact } = await import(bundle);
  let seed = 0x14a2b3c4n;
  const rng = () => {
    seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 256n) - 1n);
    return seed % (identity.ORDER - 1n) + 1n;
  };
  const s = await tevmSession();
  const world = await buildBuckWorld(s, artifact, { identity, rng, accounting: "net" });
  assert.equal(await s.call(world.reg, "isVerified", [world.net.fund.address]), true);
  assert.equal(await s.call(world.reg, "isCarrying", [world.net.fund.address]), true);
  assert.equal(await s.call(world.net.basket, "netAccounting"), true);
  assert.equal((await s.call(world.buck, "insurerPool", [world.operator.account.address])).toLowerCase(),
    world.pool.address.toLowerCase());
  const holder = await onboard(world, s.account, {
    given_name: "Net", family_name: "Household", jurisdiction: "Alberta, Canada",
    id_type: "Alberta Identity", id_number: "NET-1001", date_of_birth: "1990-01-01",
    issued_at: "2026-01-01T00:00:00Z", epoch: 42,
  }, { rng });
  const face = 100_000_000n;
  const id = await insureAsset(world, world.operator.account, holder, { face, premiumRate: 0 });
  const ops = netCreditOps(s, world.net);
  assert.equal((await ops.quoteIssue(id, face)).maxSpendable, 75_000_000n);
  await ops.issue(holder.account, id, face, 60_000_000n, 0n, ZERO);
  assert.equal((await ops.facility(id)).issued, 60_000_000n);
  // The real registry does not silently bind the basket: a deposit is not
  // usable until a production-authorized basket binding is implemented.
  assert.equal(await s.call(world.reg, "isVerified", [world.net.basket.address]), false);
  await s.send(world.buck, "approve", [world.net.basket.address, 1_000_000n], { account: holder.account });
  await s.send(world.net.basket, "deposit", [world.buck.address, 1_000_000n, 0n],
    { account: holder.account, expect: "revert" });
  assert.equal((await ops.facility(id)).issued, 60_000_000n);
  await advanceTime(world, 365 * DAY + 6 * 3600);
  const [relief, , exceeds] = await ops.quoteReduction(id, 1_000_000n);
  assert.ok(relief > 0n && !exceeds);
  const deadline = (await s.client.getBlock()).timestamp + 3600n;
  await ops.authorizeReduction(holder.account, id, 1_000_000n, 0n, deadline);
  await ops.reduceAuthorized(holder.account, id, 1_000_000n);
  assert.equal((await ops.facility(id)).issued, 59_000_000n);

  const saved = decodeJSON(encodeJSON({ chain: await snapshotTevm(s), world: worldRecord(world) }));
  assert.equal(saved.world.net.issueAbiVersion, 4);
  const restoredSession = await tevmSession();
  await restoreTevm(restoredSession, saved.chain);
  assert.throws(() => attachBuckWorld(restoredSession, artifact, saved.world, { identity }), /attachNetBuckWorld/);
  const oldRecord = structuredClone(saved.world);
  delete oldRecord.net.issueAbiVersion;
  await assert.rejects(attachNetBuckWorld(restoredSession, artifact, oldRecord, { identity }), /issuance ABI version/);
  const wrongVersion = structuredClone(saved.world);
  wrongVersion.net.issueAbiVersion = 3;
  await assert.rejects(attachNetBuckWorld(restoredSession, artifact, wrongVersion, { identity }), /issuance ABI version/);
  const oldArtifact = (name) => name === "BuckNet"
    ? { ...artifact(name), abi: artifact(name).abi.filter((entry) => entry.name !== "issue") } : artifact(name);
  await assert.rejects(attachNetBuckWorld(restoredSession, oldArtifact, saved.world, { identity }), /issuance ABI/);
  const noMintArtifact = (name) => name === "BuckNet"
    ? { ...artifact(name), abi: artifact(name).abi.filter((entry) => entry.name !== "mint") } : artifact(name);
  await assert.rejects(attachNetBuckWorld(restoredSession, noMintArtifact, saved.world, { identity }), /mint ABI/);
  const ambiguousArtifact = (name) => {
    const value = artifact(name);
    if (name !== "BuckNet") return value;
    const issue = value.abi.find((entry) => entry.type === "function" && entry.name === "issue");
    return { ...value, abi: [...value.abi, { ...issue, inputs: issue.inputs.slice(0, 4) }] };
  };
  await assert.rejects(attachNetBuckWorld(restoredSession, ambiguousArtifact, saved.world, { identity }), /issuance ABI/);
  const beforeOldBuild = (await s.client.getBlock()).number;
  await assert.rejects(buildBuckWorld(s, oldArtifact, { identity, rng, accounting: "net" }), /issuance ABI/);
  assert.equal((await s.client.getBlock()).number, beforeOldBuild);
  const restored = await attachNetBuckWorld(restoredSession, artifact, saved.world, { identity });
  const restoredOps = netCreditOps(restoredSession, restored.net);
  assert.equal((await restoredOps.facility(id)).issued, 59_000_000n);
  assert.equal(restored.operator.account.address.toLowerCase(), world.operator.account.address.toLowerCase());
  const retryDeadline = (await restoredSession.client.getBlock()).timestamp + 3600n;
  await restoredOps.authorizeReduction(holder.account, id, 1_000_000n, 0n, retryDeadline);
  await restoredOps.reduceAuthorized(holder.account, id, 1_000_000n);
  assert.equal((await restoredOps.facility(id)).issued, 58_000_000n);
  const wrongFund = structuredClone(saved.world);
  wrongFund.net.fund = wrongFund.net.maturityLibrary;
  await assert.rejects(attachNetBuckWorld(restoredSession, artifact, wrongFund, { identity }), /mismatch/);
  const wrongChain = structuredClone(saved.world);
  wrongChain.net.chainId = Number(wrongChain.net.chainId) + 1;
  await assert.rejects(attachNetBuckWorld(restoredSession, artifact, wrongChain, { identity }), /chain mismatch/);
  const wrongHash = structuredClone(saved.world);
  wrongHash.net.codeHashes.fund = `0x${"00".repeat(32)}`;
  await assert.rejects(attachNetBuckWorld(restoredSession, artifact, wrongHash, { identity }), /code hash mismatch/);
  const wrongModel = structuredClone(saved.world);
  wrongModel.accounting = "legacy";
  assert.throws(() => attachBuckWorld(restoredSession, artifact, wrongModel, { identity }), /accounting model/);
  assert.deepEqual(s.mismatches, []);
  assert.deepEqual(restoredSession.mismatches, []);
});
