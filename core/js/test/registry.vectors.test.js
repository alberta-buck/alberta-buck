// Registry-kernel conformance, JS side: replay
// core/vectors/registry-kernel-vectors.json (emitted by the Python
// reference via alberta_buck.registry.kernel_vectors) through the
// buck-registry wasm kernel -- the identity Merkle tree and central
// aggregator as stateful wasm CLASSES, the certificate family as its
// wire bytes; then the accumulator services -- salts, private feature
// subtrees, the root ring, composed paths, the insurance regulator and its
// issuance gate, attribute proofs.  The Rust suite asserts the same file
// (the class-shaped services are deliberately not Python-bound: the Python
// reference keeps its own vector-locked implementation).
//
// Build the kernel first:  make nix-core-build-wasm

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

let w = null;
let id = null;
try {
  w = await import("../src/wallet.js");
  id = await import("../src/identity.js");
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

test("holder-derived salts", { skip }, () => {
  const s = RV.salt;
  for (const [t, tag] of Object.entries(s.tree_tags)) assert.equal(id.treeTag(t), BigInt(tag), t);
  for (const c of s.cases) {
    assert.equal(id.deriveSalt(BigInt(s.secret), c.tree_id, c.counter), BigInt(c.salt));
  }
  assert.throws(() => id.deriveSalt(0n, "kyc:x", 0));
  assert.throws(() => id.treeTag(""));
});

const refused = (fn) => {
  try {
    fn();
    return false;
  } catch {
    return true;
  }
};

test("private feature subtree (class)", { skip }, () => {
  const f = RV.feature_private;
  const [qa, qb] = f.points;
  const [sa, sb] = f.salts;
  const fa = new w.registry.FeatureAuthority(f.id, f.depth, true);
  assert.equal(JSON.parse(fa.attest(qa.x, qa.y, sa, 0)).leaf, f.leaf_a);
  fa.attest(qb.x, qb.y, sb, 0);
  assert.equal(fa.sub_root(), f.root_after_two);
  assert.deepEqual(JSON.parse(fa.membership_proof_for_identity(qa.x, qa.y)), f.proof_a);
  const seven = id.g1Mul(id.G1, 7n);
  assert.equal(refused(() => fa.attest(id.hex(seven.x), id.hex(seven.y), undefined, 0)),
               f.no_salt_refused);
  assert.equal(refused(() => fa.attest(qa.x, qa.y, sa, 0)), f.dup_refused);
  assert.equal(fa.revoke(qa.x, qa.y), f.revoked_index);
  assert.equal(fa.sub_root(), f.root_after_revoke);
  assert.equal(fa.has_identity(qa.x, qa.y), f.has_a);
  assert.equal(fa.has_identity(qb.x, qb.y), f.has_b);
  // A public subtree refuses a salt.
  const pub = new w.registry.FeatureAuthority("feature:x", 4, false);
  assert.throws(() => pub.attest(qa.x, qa.y, sa, 0));
});

test("root ring (class)", { skip }, () => {
  const r = RV.root_ring;
  const svc = new w.registry.Aggregator(r.depth);
  svc.enroll(r.sub_tree_id, "kyc", "0x1", r.enroll_ts);
  for (const p of r.posts) {
    svc.update_sub_root(r.sub_tree_id, "0x" + p.sub_root.toString(16), p.at);
    const rec = JSON.parse(svc.post(p.at));
    assert.equal(rec.root, p.root);
    assert.equal(rec.sequence, p.sequence);
  }
  const [r0, r1] = [r.posts[0].root, r.posts[1].root];
  assert.equal(svc.root_record(r0) !== undefined, r.r0_retained);
  assert.equal(JSON.parse(svc.root_record(r1)).posted_at, r.r1_posted_at);
  assert.equal(svc.max_retained_age(r.now), r.max_retained_age);
  for (const a of r.accepts) assert.equal(svc.accepts(a.root, a.max_age, r.now), a.want);
});

test("composed path (class)", { skip }, () => {
  const c = RV.composed;
  const svc = new w.registry.Aggregator(c.depth);
  svc.enroll("kyc:neighbour", "kyc",
             w.registry.MerkleTree.from_leaves(c.neighbour_leaves, c.sub_depth).root(), 0);
  const kyc = w.registry.MerkleTree.from_leaves(c.kyc_leaves, c.sub_depth);
  svc.enroll("kyc:ca-ab-2026", "kyc", kyc.root(), 0);
  const sub = kyc.path(1);
  assert.deepEqual(JSON.parse(sub), c.sub_proof);
  const comp = JSON.parse(svc.composed_path("kyc:ca-ab-2026", sub));
  assert.deepEqual(comp, c.composed);
  assert.equal(comp.root, svc.identity_root());
});

test("insurance regulator + issuance gate (class)", { skip }, () => {
  const r = RV.regulator;
  const reg = new w.registry.Regulator(r.jurisdiction, r.depth);
  const env = JSON.parse(reg.attest(
    r.insurer.x, r.insurer.y, JSON.stringify(r.envelope), ["asset:bicycle"], true));
  reg.attest(r.other.x, r.other.y, JSON.stringify(r.other_envelope), ["asset:car"], false);
  assert.deepEqual(env.scopes, r.scopes);
  const names = reg.predicate_names(JSON.stringify(env), ["asset:bicycle"]);
  assert.deepEqual(names, r.predicate_names);
  names.forEach((n, i) => assert.equal(w.registry.subtreeKey(n), r.subtree_keys[i]));
  assert.deepEqual(
    JSON.parse(reg.membership_proof(r.insurer.x, r.insurer.y, "insurer:face:5")), r.face_proof);
  assert.equal(reg.revoke(r.other.x, r.other.y), r.cleared_other);
  assert.deepEqual(JSON.parse(reg.sub_roots()), r.sub_roots_after_revoke);
  for (const [f, b] of r.bands) assert.equal(w.registry.bandForFace(f), b, f);
  for (const c of r.cases) {
    assert.equal(
      w.registry.checkIssuance(env, c.scope, c.face, c.dep_type, c.dep_rate, c.premium_rate, c.now),
      c.want);
  }
  assert.ok(env.scopes.includes(w.registry.scopeId(reg.scope_name("asset:bicycle"))));
});

test("attribute proofs (class)", { skip }, () => {
  const a = RV.attributes;
  const kyc = w.registry.MerkleTree.from_leaves(a.kyc_leaves, 12);
  const age = w.registry.MerkleTree.from_leaves(a.age_leaves, 10);
  const svc = new w.registry.Aggregator(a.depth);
  svc.enroll("kyc:ca-ab-2026", "kyc", kyc.root(), 0);
  svc.enroll("feature:age-over-18", "feature", age.root(), 0);
  svc.post(a.posted_at);
  const claims = [["kyc:ca-ab-2026", JSON.parse(kyc.path(0))],
                  ["feature:age-over-18", JSON.parse(age.path(0))]];
  const ap = svc.prove_attributes(JSON.stringify(claims), a.person.x, a.person.y);
  assert.equal(JSON.parse(ap).root, a.root);
  for (const c of a.verify) {
    assert.equal(svc.verify_attributes(ap, c.required, c.max_age, c.now), c.want);
  }
  assert.throws(() => svc.verify_attributes(ap, [], 1, 0));
});
