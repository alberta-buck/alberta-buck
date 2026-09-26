// The ceremonies in the halves their parties perform -- the sandbox's core
// (doc/review/sandbox-plan.org, S1):
//
//   issuer   issueCredential: a card, off-chain; the chain learns nothing
//   holder   registerWallet: from the card alone, checked against the key
//            the registry publishes before any transaction
//   insurer  insureAsset: a separate account, behind the holder's opt-in
//   holder   activateCredit: Buck.mint against the insured credit
//   anyone   observe: every transaction decoded, identity material opaque
//
// and the world as data: snapshotTevm -> encodeJSON -> restoreTevm +
// attachBuckWorld gives back the state, the credits and the clock.
//
// Skips cleanly when the wasm kernels or the contracts are not available.

import { describe, it, before } from "node:test";
import assert from "node:assert/strict";
import { privateKeyToAccount } from "viem/accounts";

import { tevmSession, snapshotTevm, restoreTevm } from "../src/backends.js";
import { loadArtifact } from "../src/nodefs.js";
import { encodeJSON, decodeJSON } from "../src/codec.js";

let id = null;
try {
  id = await import("../src/identity.js");
} catch {
  // kernels not built
}
let contracts = true;
try {
  loadArtifact("IdentityRegistry");
} catch {
  contracts = false;
}
const skip = !id
  ? "kernels not built (make nix-core-build-wasm)"
  : !contracts ? "no contracts (make nix-build, or npm ci for alberta-buck-contracts)" : false;

const YEAR = 365n * 86_400n + 6n * 3_600n;     // BuckCredit.SECONDS_PER_YEAR
const BP = 10_000n;
const BUCK = 1_000_000n;                       // 6 dp

const CHLOE = {
  given_name: "Chloé", family_name: "Bélanger-李",
  jurisdiction: "Alberta, Canada", id_type: "Alberta Identity Card",
  id_number: "AIC-2026-0007744", date_of_birth: "1994-11-02",
  issued_at: "2026-07-03T00:00:00Z", epoch: 42,
};
const BOB = {
  given_name: "Bob", family_name: "Smith",
  jurisdiction: "Alberta, Canada", id_type: "Corporate Registration",
  id_number: "AB-CORP-2026-00182", date_of_birth: "1985-07-22",
  issued_at: "2026-07-03T00:00:00Z", epoch: 42,
};

describe("the ceremonies by role", { skip }, () => {
  let bw;                 // ../src/buckworld.js
  let rng;
  let session;
  let world;
  let card;               // Chloé's credential
  let chloe;              // her registered handle
  let bob;
  let insurer;            // "Sandbox Mutual": a fresh account, not the deployer
  let homeId;             // the credit Sandbox Mutual insures for Chloé

  const head = async (s = session) => (await s.client.getBlock()).number;

  before(async () => {
    bw = await import("../src/buckworld.js");
    let seed = 0x5a4db0en;
    rng = () => {
      seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 256n) - 1n);
      const v = seed % id.ORDER;
      return v === 0n ? 1n : v;
    };
    session = await tevmSession();
    world = await bw.buildBuckWorld(session, loadArtifact, { identity: id, rng });
  });

  it("the issuer's card verifies under the key the registry publishes; no transaction", async () => {
    const before = await head();
    card = bw.issueCredential(world, CHLOE, { rng });
    assert.equal(card.issuer, world.issuer.addr);
    assert.equal(card.fields.issuer_id, "atb-financial-ca");
    assert.equal(card.canonical, id.canonicalIdentity(card.fields));
    assert.equal(card.m, id.identityScalar(card.canonical));
    const key = await bw.issuerKey(world, card.issuer);
    assert.deepEqual(key.pkY1, world.issuer.pkY1);
    assert.ok(id.psVerify(key.pkX, key.pkY, card.sigma, card.m));
    assert.equal(await head(), before, "issuance is off-chain");
  });

  it("the holder registers a fresh account from the card alone", async (t) => {
    const acct = privateKeyToAccount("0x" + "c1".repeat(32));
    await bw.fundAccount(world, acct.address);
    const handed = decodeJSON(encodeJSON(card));        // as a file or a paste
    const t0 = performance.now();
    chloe = await bw.registerWallet(world, acct, handed, { rng });
    t.diagnostic(`registration (prove + register tx): ${(performance.now() - t0).toFixed(0)} ms`);
    assert.equal(await session.call(world.reg, "isVerified", [acct.address]), true);
    assert.equal(await session.call(world.reg, "issuerOf", [acct.address]), world.issuer.addr);
    assert.deepEqual(chloe.M, id.g1Mul(id.G1, card.m));
    assert.deepEqual(chloe.fields, card.fields);
  });

  it("a tampered or foreign card is refused before any transaction", async () => {
    const acct = privateKeyToAccount("0x" + "c2".repeat(32));
    const before = await head();
    const renamed = { ...card, fields: { ...card.fields, family_name: "Smith" } };
    await assert.rejects(bw.registerWallet(world, acct, renamed, { rng }),
      /does not match its own record/);
    const fields = renamed.fields;
    const canonical = id.canonicalIdentity(fields);
    const forged = { ...card, fields, canonical, m: id.identityScalar(canonical) };
    await assert.rejects(bw.registerWallet(world, acct, forged, { rng }),
      /does not verify under the issuer's key/);
    const foreign = { ...card, issuer: "0x" + "16".repeat(20) };
    await assert.rejects(bw.registerWallet(world, acct, foreign, { rng }),
      /is not trusted by this registry/);
    assert.equal(await head(), before);
  });

  it("onboard is still the two halves in one call", async () => {
    const acct = privateKeyToAccount("0x" + "b0".repeat(32));
    await bw.fundAccount(world, acct.address);
    bob = await bw.onboard(world, acct, BOB, { rng });
    assert.equal(await session.call(world.reg, "isVerified", [acct.address]), true);
    assert.equal(bob.issuer, world.issuer.addr);
  });

  it("an insurer the holder never accepted cannot issue", async () => {
    insurer = privateKeyToAccount("0x" + "5a".repeat(32));
    await bw.fundAccount(world, insurer.address);
    const now = (await session.client.getBlock()).timestamp;
    await session.send(world.credit, "createCredit",
      [chloe.account.address, 1, 1_000n * BUCK, 0n, 0, 0, now, 0],
      { account: insurer, expect: "revert", tag: "credit:unaccepted" });
    assert.equal(session.lastRevertReason, "BuckCredit: insurer not accepted by client");
    session.mismatches.length = 0;
  });

  it("Sandbox Mutual insures a home; the holder activates part of it", async () => {
    const terms = {
      face: 400_000n * BUCK, floor: 100_000n * BUCK, assetClass: 1,
      depType: bw.DEPRECIATION.LINEAR, depRate: 250,        // 2.5 %/yr of the depreciable part
    };
    const before = await head();
    homeId = await bw.insureAsset(world, insurer, chloe, terms);
    assert.equal(await head(), before + 2n, "opt-in, then the credit");
    assert.equal(await session.call(world.credit, "acceptsCreditFrom",
      [chloe.account.address, insurer.address]), true);

    const v = await bw.creditView(world, homeId);
    assert.equal(v.holder, chloe.account.address);
    assert.equal(v.insurer, insurer.address);
    assert.equal(v.assetClass, 1);
    assert.equal(v.face, terms.face);
    assert.equal(v.floor, terms.floor);
    assert.equal(v.depType, bw.DEPRECIATION.LINEAR);
    assert.equal(v.depRate, 250);
    assert.equal(v.activated, 0n);
    assert.equal(v.currentValue, 0n);

    // A second credit from the same insurer needs no second opt-in.
    const b2 = await head();
    const carId = await bw.insureAsset(world, insurer, chloe, {
      face: 30_000n * BUCK, assetClass: 2,
      depType: bw.DEPRECIATION.DECLINING_BALANCE, depRate: 1_500,
    });
    assert.equal(await head(), b2 + 1n);
    assert.deepEqual(await bw.creditsOf(world, chloe.account.address), [homeId, carId]);

    const minted = await bw.activateCredit(world, chloe, 50_000n * BUCK, { tokenIds: [homeId] });
    // Seconds of depreciation since insurance: 50,000 of present value
    // takes a hair more face.
    assert.ok(minted.coverage >= 50_000n * BUCK && minted.coverage < 50_001n * BUCK);
    assert.equal(minted.premium, 0n);
    const a = await bw.accountView(world, chloe.account.address);
    assert.equal(a.verified, true);
    assert.equal(a.creditLimit, minted.newLimit);
    assert.equal((await bw.creditView(world, homeId)).activated, minted.coverage);
  });

  it("the insured value depreciates on the contract's schedule as the clock moves", async () => {
    await bw.advanceTime(world, 365 * bw.DAY);
    const now = (await session.client.getBlock()).timestamp;
    const v = await bw.creditView(world, homeId);
    const loss = (v.face - v.floor) * BigInt(v.depRate) * (now - v.depStartAt) / (YEAR * BP);
    assert.equal(v.depreciatedFace, v.face - loss);
    assert.equal(v.currentValue, v.depreciatedFace * v.activated / v.face);
    assert.ok(v.currentValue < 50_000n * BUCK);
  });

  it("the observer decodes every transaction and never sees a name", async () => {
    await bw.identityApprove(world, chloe, bob, { rng });
    await bw.identityApprove(world, bob, chloe, { rng });
    await session.send(world.buck, "transfer", [bob.account.address, 1_234n * BUCK],
      { account: chloe.account, gas: 1_000_000n, tag: "pay:bob" });

    const { observe } = await import("../src/observer.js");
    const rows = await observe(world, 0n);
    const find = (fn, pred = () => true) => rows.filter((r) => r.fn === fn && pred(r));
    const arg = (r, name) => r.args.find((a) => a.name === name);

    assert.deepEqual(find("deploy").map((r) => r.contract),
      ["IdentityRegistry", "Insurance pool", "BuckCredit", "BuckKControllerDirect", "Buck"]);
    assert.ok(find("transfer ETH").length >= 3);

    const [reg] = find("register", (r) => r.from === chloe.account.address);
    assert.equal(reg.contract, "IdentityRegistry");
    assert.equal(arg(reg, "issuer").kind, "public");
    for (const name of ["pk", "E", "pres", "proof"]) {
      assert.equal(arg(reg, name).kind, "opaque", name);
      assert.ok(arg(reg, name).note, name);
    }
    assert.match(arg(reg, "E").note, /only the holder/);
    assert.deepEqual(reg.events.map((e) => e.name), ["Registered"]);

    const [appr] = find("approve", (r) => r.from === chloe.account.address);
    assert.match(arg(appr, "E_bob").note, /only the payee/);
    assert.equal(appr.events[0].args.find((a) => a.name === "receiptHash").kind, "opaque");

    const [pay] = find("transfer", (r) => r.from === chloe.account.address);
    assert.equal(arg(pay, "amount").kind, "public");
    assert.equal(arg(pay, "amount").value, 1_234n * BUCK);
    const receipt = pay.events.find((e) => e.name === "BuckTransferReceipt");
    assert.equal(receipt.args.find((a) => a.name === "amount").value, 1_234n * BUCK);
    assert.equal(receipt.args.find((a) => a.name === "fromCipherHash").kind, "opaque");

    const [unaccepted] = find("createCredit", (r) => r.status === "revert");
    assert.equal(unaccepted.from, insurer.address);

    // The privacy claim itself: no field of any row spells a name, a
    // document number or a birth date.
    const text = encodeJSON(rows);
    for (const who of [CHLOE, BOB]) {
      for (const v of [who.given_name, who.family_name, who.id_number, who.date_of_birth]) {
        assert.ok(!text.includes(v), `observer row leaks ${v}`);
      }
    }
  });

  it("snapshot -> JSON -> restore gives back the state, the credits and the clock", async (t) => {
    const snap = await snapshotTevm(session);
    const { account, ...chloeData } = chloe;
    const saved = encodeJSON({ snap, world: bw.worldRecord(world), chloe: chloeData });
    t.diagnostic(`saved world: ${(saved.length / 1024).toFixed(0)} KiB of JSON`);

    const back = decodeJSON(saved);
    const s2 = await tevmSession();
    const t0 = performance.now();
    await restoreTevm(s2, back.snap);
    t.diagnostic(`restore (${back.snap.block} blocks): ${(performance.now() - t0).toFixed(0)} ms`);
    const w2 = bw.attachBuckWorld(s2, loadArtifact, back.world, { identity: id });

    const h1 = await session.client.getBlock();
    const h2 = await s2.client.getBlock();
    assert.equal(h2.number, h1.number);
    assert.equal(h2.timestamp, h1.timestamp);
    for (const who of [chloe.account.address, bob.account.address, insurer.address]) {
      assert.deepEqual(await bw.accountView(w2, who), await bw.accountView(world, who));
    }
    assert.deepEqual(await bw.creditView(w2, homeId), await bw.creditView(world, homeId));
    assert.deepEqual(back.chloe.kp, chloe.kp);

    // The restored world carries on: time moves, a payment lands, and the
    // observer sees only what happened after the restore.
    await bw.advanceTime(w2, 30 * bw.DAY);
    await s2.send(w2.buck, "transfer", [chloe.account.address, 100n * BUCK],
      { account: bob.account, gas: 1_000_000n, tag: "pay:back" });
    assert.ok((await bw.accountView(w2, bob.account.address)).feeOwing >= 0n);
    assert.ok((await bw.creditView(w2, homeId)).currentValue
              < (await bw.creditView(world, homeId)).currentValue);
    const { observe } = await import("../src/observer.js");
    const fresh = await observe(w2, back.snap.block + 1n);
    assert.deepEqual(fresh.map((r) => r.fn), ["transfer"]);
    assert.equal(session.mismatches.length, 0);
    assert.equal(s2.mismatches.length, 0);
  });
});
