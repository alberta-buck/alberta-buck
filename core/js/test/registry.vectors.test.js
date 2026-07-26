// Registry-kernel conformance, JS side: replay
// core/vectors/registry-kernel-vectors.json (emitted by the Python
// reference via alberta_buck.registry.kernel_vectors) through the
// buck-registry wasm kernel -- the identity Merkle tree and central
// aggregator as stateful wasm CLASSES, the certificate family as its
// wire bytes.  The Rust suite asserts the same file (the class-shaped
// tree/aggregator are deliberately not Python-bound: the Python
// reference keeps its own vector-locked implementation).
//
// Build the kernel first:  make nix-core-build-wasm

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

let w = null;
try {
  w = await import("../src/wallet.js");
} catch {
  // wasm not built; tests below skip
}
const skip = w ? false : "kernel not built (make nix-core-build-wasm)";

const RV = JSON.parse(readFileSync(
  fileURLToPath(new URL("../../vectors/registry-kernel-vectors.json", import.meta.url)),
  "utf8"));

const hexBytes = (h) =>
  Uint8Array.from(
    (h.startsWith("0x") ? h.slice(2) : h).match(/.{2}/g).map((b) => parseInt(b, 16)));

test("identity Merkle tree (class)", { skip }, () => {
  const t = RV.tree;
  const tree = new w.registry.MerkleTree(t.depth);
  assert.equal(tree.root(), t.empty_root);
  t.leaves.forEach((leaf, i) => {
    tree.insert_leaf(leaf);
    assert.equal(tree.root(), t.roots_after_insert[i]);
  });
  for (const [idx, want] of Object.entries(t.paths)) {
    const got = JSON.parse(tree.path(Number(idx)));
    assert.deepEqual(got, want, `path ${idx}`);
  }
  // identity_leaf agreement: insert by POINT matches the recorded leaf.
  const t2 = new w.registry.MerkleTree(t.depth);
  for (const [i, p] of t.points.entries()) {
    t2.insert_identity(p.x, p.y);
    assert.equal(t2.leaves()[i], t.leaves[i]);
  }
  // Leaf replacement (the aggregator's update path).
  tree.set_leaf(t.replace.index, t.replace.leaf);
  assert.equal(tree.root(), t.replace.root);
  assert.deepEqual(JSON.parse(tree.path(t.replace.index)), t.replace.path);
  // Event-log reconstruction.
  const rebuilt = w.registry.MerkleTree.from_leaves(tree.leaves(), t.depth);
  assert.equal(rebuilt.root(), tree.root());
});

test("registry Schnorr", { skip }, () => {
  const s = RV.registry_schnorr;
  const chainid = "0x" + s.chainid.toString(16);
  const [e, sg, rx, ry] = w.registry.schnorrSign(
    s.sk, s.msg_hash, s.registry_id, chainid, s.k);
  assert.equal(e, s.proof.e);
  assert.equal(sg, s.proof.s);
  assert.equal(BigInt(rx), BigInt(s.proof.R.x));
  assert.equal(BigInt(ry), BigInt(s.proof.R.y));
  assert.ok(w.registry.schnorrVerify(
    s.pk, e, sg, { x: rx, y: ry }, s.msg_hash, s.registry_id, chainid));
  assert.ok(!w.registry.schnorrVerify(
    s.pk, e, sg, { x: rx, y: ry }, s.msg_hash, "other-registry", chainid));
});

test("certificate wire + sealing", { skip }, () => {
  const c = RV.certificate;
  const chainid = "0x" + c.chainid.toString(16);
  const wire = w.registry.signCertificate(
    c.registry_sk, c.registry_id, c.canonical_identity,
    c.serial, c.issued_at, c.expires_at, chainid, c.k);
  assert.deepEqual(wire, hexBytes(c.signed_wire));
  assert.ok(w.registry.verifyCertificate(wire, chainid));
  assert.equal(w.registry.verifyCertificate(wire, "0x2"), c.wrong_chainid_verifies);

  const env = w.registry.sealCertificate(wire, c.client_pk, c.r_seal);
  assert.deepEqual(env, hexBytes(c.sealed_envelope));
  assert.deepEqual(w.registry.unsealCertificate(env, c.client_sk), wire);
  assert.throws(() => w.registry.unsealCertificate(env, "0x1234"));
});

test("central aggregator scenario (class)", { skip }, () => {
  const a = RV.aggregator;
  const ts = (i) => 1770000100.0 + i;

  const regA = w.registry.MerkleTree.from_leaves(a.reg_a.leaves.slice(0, 3), 12);
  const regB = w.registry.MerkleTree.from_leaves(a.reg_b.leaves, 12);
  const feat = new w.registry.MerkleTree(10);

  const svc = new w.registry.Aggregator(a.depth);
  svc.enroll("ca-ab-2026", "kyc", regA.root(), ts(0));
  svc.enroll("ca-bc-2026", "kyc", regB.root(), ts(1));
  svc.enroll("feature:age-over-18", "feature", feat.root(), ts(2));
  assert.equal(svc.identity_root(), a.root_after_enroll);

  feat.insert_identity(a.identity.x, a.identity.y);
  assert.equal(
    svc.update_sub_root("feature:age-over-18", feat.root(), ts(3)),
    a.root_after_attest);

  regA.insert_leaf(a.reg_a.leaves[3]);
  assert.equal(
    svc.update_sub_root("ca-ab-2026", regA.root(), ts(4)),
    a.root_final);

  const subProof = JSON.parse(regA.path(0));
  assert.deepEqual(subProof, a.sub_proof);
  const aggProof = JSON.parse(svc.aggregator_proof("ca-ab-2026"));
  assert.ok(w.registry.verifyFullProof(subProof, aggProof));

  // AND composition: the same identity in the feature tree, both halves
  // against the one identityRoot.
  const featProof = JSON.parse(feat.path(0));
  assert.deepEqual(featProof, a.feature_proof);
  const aggFeat = JSON.parse(svc.aggregator_proof("feature:age-over-18"));
  assert.ok(w.registry.verifyFullProof(featProof, aggFeat));
  assert.equal(aggProof.aggregator_root, aggFeat.aggregator_root);

  const aggB = JSON.parse(svc.aggregator_proof("ca-bc-2026"));
  assert.equal(aggB.sub_root, a.agg_proof_b.sub_root);
  assert.equal(aggB.aggregator_root, a.agg_proof_b.aggregator_root);
  assert.equal(aggB.aggregator_leaf_index, a.agg_proof_b.leaf_index);
});
