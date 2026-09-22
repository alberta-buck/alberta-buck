// Identity-kernel conformance, JS side: replay every prove call of
// core/vectors/identity-kernel-vectors.json (emitted by the py_ecc
// REFERENCE via alberta_buck.wallet.kernel_vectors, nonces included)
// through the buck-identity WASM kernel, and re-verify the committed
// test/vectors/identity.json fixture.  The Rust and Python suites assert
// the same files.
//
// Build the kernel first:  make nix-core-build-wasm

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

let id = null;
try {
  id = await import("../src/identity.js");
} catch {
  // wasm not built; tests below skip
}
const skip = id ? false : "kernel not built (make nix-core-build-wasm)";

const KV = JSON.parse(readFileSync(
  fileURLToPath(new URL("../../vectors/identity-kernel-vectors.json", import.meta.url)),
  "utf8"));
const IV = JSON.parse(readFileSync(
  fileURLToPath(new URL("../../../test/vectors/identity.json", import.meta.url)),
  "utf8"));

const B = BigInt;
const pt = (j) => ({ x: B(j.x), y: B(j.y) });
const ct = (j) => ({ R: pt(j.R), C: pt(j.C) });
const g2 = (j) => ({ x: [B(j.x[0]), B(j.x[1])], y: [B(j.y[0]), B(j.y[1])] });

test("curve ops + pairing", { skip }, () => {
  for (const r of KV.g1_ops) {
    assert.deepEqual(id.g1Add(pt(r.A), pt(r.B)), pt(r.add));
    assert.deepEqual(id.g1Mul(pt(r.A), B(r.k)), pt(r.mul));
    assert.deepEqual(id.g1Neg(pt(r.A)), pt(r.neg));
  }
  for (const r of KV.g2_ops) {
    assert.deepEqual(id.g2Mul(id.G2, B(r.k)), g2(r.mul));
  }
  for (const r of KV.pairing) {
    const pairs = r.pairs.map((p) => [pt(p.g1), g2(p.g2)]);
    assert.equal(id.pairingCheck(pairs), r.ok);
  }
});

test("keccak + identity scalar + canonical JSON", { skip }, () => {
  for (const r of KV.keccak_scalar) {
    assert.equal(id.keccakScalar(r.words.map(B)), B(r.scalar));
  }
  for (const r of KV.identity_scalar) {
    assert.equal(id.identityScalar(r.canonical), B(r.m));
    // canonical JSON is a fixpoint: parse -> canonicalIdentity -> same bytes
    assert.equal(id.canonicalIdentity(JSON.parse(r.canonical)), r.canonical);
  }
});

test("poseidon arities 1..16", { skip }, () => {
  let n = 0;
  for (const r of KV.poseidon) {
    assert.equal(id.poseidon(r.inputs.map(B)), B(r.hash), `arity ${r.inputs.length}`);
    n += 1;
  }
  assert.equal(n, 16);
});

test("elgamal", { skip }, () => {
  for (const r of KV.elgamal) {
    assert.deepEqual(id.elgamalEncrypt(pt(r.M), pt(r.pk), B(r.r)), ct(r.E));
    assert.deepEqual(id.elgamalDecrypt(ct(r.E), B(r.sk)), pt(r.M));
  }
});

test("ps sign / verify / rerandomize", { skip }, () => {
  const p = KV.ps;
  assert.deepEqual(id.g2Mul(id.G2, B(p.sk_x)), g2(p.pk_X));
  assert.deepEqual(id.g2Mul(id.G2, B(p.sk_y)), g2(p.pk_Y));
  for (const r of p.signs) {
    const sig = id.psSign(B(p.sk_x), B(p.sk_y), B(r.m), B(r.t));
    assert.deepEqual(sig.sigma_1, pt(r.sigma_1));
    assert.deepEqual(sig.sigma_2, pt(r.sigma_2));
    assert.ok(id.psVerify(g2(p.pk_X), g2(p.pk_Y), sig, B(r.m)));
    assert.ok(!id.psVerify(g2(p.pk_X), g2(p.pk_Y), sig, B(r.m) + 1n));
    const rr = id.psRerandomize(sig, B(r.rerand_t));
    assert.deepEqual(rr.sigma_1, pt(r.rerand_sigma_1));
    assert.deepEqual(rr.sigma_2, pt(r.rerand_sigma_2));
    const pres = id.psPresent(sig, pt(p.pk_Y1), B(r.present_a), B(r.present_b));
    assert.deepEqual(pres.A, pt(r.present_A));
    assert.deepEqual(pres.B, pt(r.present_B));
    // the presentation is NOT a signature on m
    assert.ok(!id.psVerify(g2(p.pk_X), g2(p.pk_Y), { sigma_1: pres.A, sigma_2: pres.B }, B(r.m)));
  }
  assert.ok(id.psKeyConsistent(g2(p.pk_Y), pt(p.pk_Y1)));
});

test("schnorr batch binding", { skip }, () => {
  const s = KV.schnorr;
  const hBatch = id.batchCommitment(s.cms.map(B));
  assert.equal(hBatch, B(s.h_batch_raw), "raw unreduced keccak word");
  const sig = id.issuerSchnorrSign(B(s.sk_iss), hBatch, B(s.issuer), B(s.chainid), B(s.k));
  assert.equal(sig.e, B(s.proof.e));
  assert.equal(sig.s, B(s.proof.s));
  assert.deepEqual(sig.R, pt(s.proof.R));
  assert.ok(id.issuerSchnorrVerify(pt(s.pk_iss), sig, hBatch, B(s.issuer), B(s.chainid)));
  assert.ok(!id.issuerSchnorrVerify(pt(s.pk_iss), sig, hBatch + 1n, B(s.issuer), B(s.chainid)));
});

test("registration NIZK", { skip }, () => {
  const r = KV.registration;
  const sig = { sigma_1: pt(r.sigma_1), sigma_2: pt(r.sigma_2) };
  const pres = id.psPresent(sig, pt(r.Y1), B(r.a), B(r.b));
  assert.deepEqual(pres, { A: pt(r.A), B: pt(r.B) });
  const proof = id.registrationProve(
    pres, B(r.b), B(r.m), B(r.r), pt(r.pk), ct(r.E), B(r.registrant), B(r.sk), B(r.chainid),
    B(r.registry), B(r.m_tilde), B(r.b_tilde), B(r.r_tilde), B(r.sk_tilde));
  assert.equal(proof.e, B(r.proof.e));
  assert.equal(proof.s_m, B(r.proof.s_m));
  assert.equal(proof.s_b, B(r.proof.s_b));
  assert.equal(proof.s_r, B(r.proof.s_r));
  assert.equal(proof.s_sk, B(r.proof.s_sk));
  assert.deepEqual(proof.C1, pt(r.proof.C1));
  assert.deepEqual(proof.T_C, pt(r.proof.T_C));
  assert.deepEqual(proof.T_R, pt(r.proof.T_R));
  assert.deepEqual(proof.T_key, pt(r.proof.T_key));
  assert.ok(id.registrationVerify(
    pres, ct(r.E), pt(r.pk), g2(KV.ps.pk_X), g2(KV.ps.pk_Y), proof,
    B(r.registrant), B(r.chainid), B(r.registry)));
  assert.ok(!id.registrationVerify(
    pres, ct(r.E), pt(r.pk), g2(KV.ps.pk_X), g2(KV.ps.pk_Y), proof,
    B(r.registrant) + 1n, B(r.chainid), B(r.registry)));
});

test("chaum-pedersen approve", { skip }, () => {
  const c = KV.chaum_pedersen;
  const proof = id.chaumPedersenProve(
    ct(c.E_a), ct(c.E_b), pt(c.pk_a), pt(c.pk_b), B(c.sk_a), B(c.r_prime),
    B(c.sender), B(c.spender), B(c.chainid), B(c.registry),
    B(c.k1), B(c.k2));
  assert.equal(proof.e, B(c.proof.e));
  assert.equal(proof.s1, B(c.proof.s1));
  assert.equal(proof.s2, B(c.proof.s2));
  assert.deepEqual(proof.T1, pt(c.proof.T1));
  assert.deepEqual(proof.T2, pt(c.proof.T2));
  assert.deepEqual(proof.T3, pt(c.proof.T3));
  assert.ok(id.chaumPedersenVerify(
    ct(c.E_a), ct(c.E_b), pt(c.pk_a), pt(c.pk_b), proof,
    B(c.sender), B(c.spender), B(c.chainid), B(c.registry)));
});

test("verifiable decryption", { skip }, () => {
  const r = KV.verifiable_decrypt;
  const proof = id.verifiableDecryptProve(
    ct(r.E), B(r.sk), pt(r.M), B(r.account), B(r.chainid), B(r.t));
  assert.equal(proof.e, B(r.proof.e));
  assert.equal(proof.s, B(r.proof.s));
  assert.deepEqual(proof.T1, pt(r.proof.T1));
  assert.deepEqual(proof.T2, pt(r.proof.T2));
  assert.ok(id.verifiableDecryptVerify(
    ct(r.E), pt(r.pk), pt(r.M), proof, B(r.account), B(r.chainid)));
});

test("issuer re-encryption binding", { skip }, () => {
  const r = KV.issuer_reenc;
  const proof = id.issuerReencProve(
    B(r.sk_iss), B(r.r_prime), pt(r.pk_rec), ct(r.E_reg), ct(r.E_iss),
    B(r.issuer), B(r.chainid),
    B(r.beta), B(r.gamma), B(r.k_r), B(r.k_b), B(r.k_s), B(r.k_g));
  for (const f of ["e", "s_r", "s_b", "s_s", "s_g"]) {
    assert.equal(proof[f], B(r.proof[f]), f);
  }
  for (const f of ["A1", "A2", "A3", "A4", "A5", "Q", "U", "T"]) {
    assert.deepEqual(proof[f], pt(r.proof[f]), f);
  }
  assert.ok(id.issuerReencVerify(
    pt(r.pk_iss), ct(r.E_reg), ct(r.E_iss), proof, B(r.issuer), B(r.chainid)));
});

test("b1 depositor binding", { skip }, () => {
  const r = KV.b1_bind;
  const { proof, eDepForIss } = id.b1BindProve(
    B(r.m_dep), B(r.sk_dep), ct(r.E_dep), pt(r.pk_iss),
    B(r.account), B(r.chainid),
    B(r.r), B(r.b), B(r.k_m), B(r.k_s), B(r.k_r), B(r.k_b));
  assert.deepEqual(eDepForIss, ct(r.eDepForIss));
  for (const f of ["e", "s_m", "s_s", "s_r", "s_b"]) assert.equal(proof[f], B(r.proof[f]), f);
  for (const f of ["A2", "A4", "B1", "B2", "A_p", "P_dep"]) {
    assert.deepEqual(proof[f], pt(r.proof[f]), f);
  }
  assert.ok(id.b1BindVerify(
    pt(r.pk_dep), ct(r.E_dep), pt(r.pk_iss), eDepForIss, proof,
    B(r.account), B(r.chainid)));
});

test("notes family + merkle", { skip }, () => {
  const n = KV.notes;
  assert.equal(id.idHashB1(B(n.m_issuer), pt(n.sigma_R), B(n.sigma_s)), B(n.id_hash_b1));
  assert.equal(
    id.idHashA1(ct(n.eNote), B(n.m_issuer), pt(n.sigma_R), B(n.sigma_s)),
    B(n.id_hash_a1));
  assert.equal(id.idHashA2(ct(n.eNote), ct(n.eIss)), B(n.id_hash_a2));
  const op = n.opening;
  assert.equal(
    id.noteCommitment(B(op.flavor), B(op.v), B(op.rho), B(op.idHash), B(op.predicate)),
    B(n.cm));
  assert.equal(id.nullifierB(B(op.rho), B(op.idHash)), B(n.nullifier_b));
  assert.equal(id.nullifierA(B(op.rho), B(op.idHash)), B(n.nullifier_a));
  assert.equal(id.identityLeaf(pt(n.identity_leaf_M)), B(n.identity_leaf));

  // merkle: fold the recorded leaves to the recorded root, verify the path
  const mk = KV.merkle;
  const depth = mk.depth;
  const zeros = [0n];
  for (let d = 1; d <= depth; d++) zeros.push(id.poseidon([zeros[d - 1], zeros[d - 1]]));
  let nodes = mk.leaves.map(B);
  for (let d = 0; d < depth; d++) {
    const next = [];
    for (let i = 0; i < nodes.length; i += 2) {
      next.push(id.poseidon([nodes[i], nodes[i + 1] ?? zeros[d]]));
    }
    nodes = next.length ? next : [zeros[d + 1]];
  }
  assert.equal(nodes[0], B(mk.root), "merkle root");
  let cur = B(mk.leaves[mk.path_index]);
  mk.siblings.forEach((sib, d) => {
    cur = mk.index_bits[d] === 0
      ? id.poseidon([cur, B(sib)])
      : id.poseidon([B(sib), cur]);
  });
  assert.equal(cur, B(mk.root), "merkle path");
});

// ---------------------------------------------------------------------------
// The committed forge fixture: verify pass + deterministic recomputation
// ---------------------------------------------------------------------------

test("identity.json: parties, approve, schnorr, receipts, issuer_reenc", { skip }, () => {
  const chainid = B(IV.chainid);
  const registry = B(IV.registry);
  const issX = g2(IV.issuer.pk_X);
  const issY = g2(IV.issuer.pk_Y);

  for (const who of ["alice", "bob"]) {
    const p = IV[who];
    const m = B(p.m);
    // canonical -> m -> M, and the canonical string is a fixpoint
    assert.equal(id.identityScalar(p.canonical_identity_data), m);
    assert.equal(id.canonicalIdentity(JSON.parse(p.canonical_identity_data)),
      p.canonical_identity_data);
    assert.deepEqual(id.g1Mul(id.G1, m), pt(p.M));
    // recorded-randomness encryption replay
    assert.deepEqual(
      id.elgamalEncrypt(pt(p.M), pt(p.elgamal_kp.pk), B(p.r)), ct(p.ciphertext));
    // raw credential verifies; the published presentation does not (A')
    assert.ok(id.psVerify(issX, issY,
      { sigma_1: pt(p.ps_sig_raw.sigma_1), sigma_2: pt(p.ps_sig_raw.sigma_2) }, m));
    const pres = { A: pt(p.ps_presentation.A), B: pt(p.ps_presentation.B) };
    assert.ok(!id.psVerify(issX, issY, { sigma_1: pres.A, sigma_2: pres.B }, m));
    const pf = p.registration_proof;
    const proof = {
      e: B(pf.e), s_m: B(pf.s_m), s_b: B(pf.s_b), s_r: B(pf.s_r), s_sk: B(pf.s_sk),
      C1: pt(pf.C1), T_C: pt(pf.T_C), T_R: pt(pf.T_R), T_key: pt(pf.T_key),
    };
    assert.ok(id.registrationVerify(
      pres, ct(p.ciphertext), pt(p.elgamal_kp.pk), issX, issY, proof,
      B(p.registrant), chainid, registry));
  }

  // unicode canonical-dialect pin: raw UTF-8 (accents + CJK + sorted keys)
  const up = IV.unicode_party;
  assert.ok(up.canonical_identity_data.includes("Chloé"));
  assert.ok(up.canonical_identity_data.includes("李"));
  assert.ok(!up.canonical_identity_data.includes("\\u"));
  assert.equal(id.identityScalar(up.canonical_identity_data), B(up.m));
  assert.equal(id.canonicalIdentity(JSON.parse(up.canonical_identity_data)),
    up.canonical_identity_data);
  assert.deepEqual(id.g1Mul(id.G1, B(up.m)), pt(up.M));

  // approve
  const ap = IV.approve;
  assert.deepEqual(
    id.elgamalEncrypt(pt(IV.alice.M), pt(IV.bob.elgamal_kp.pk), B(ap.r_prime)),
    ct(ap.E_for_bob));
  const cpp = {
    e: B(ap.cp_proof.e), s1: B(ap.cp_proof.s1), s2: B(ap.cp_proof.s2),
    T1: pt(ap.cp_proof.T1), T2: pt(ap.cp_proof.T2), T3: pt(ap.cp_proof.T3),
  };
  assert.ok(id.chaumPedersenVerify(
    ct(ap.E_alice), ct(ap.E_for_bob),
    pt(IV.alice.elgamal_kp.pk), pt(IV.bob.elgamal_kp.pk),
    cpp, B(ap.sender), B(ap.spender), chainid,
    B(ap.registry)));

  // issuer schnorr (hBatch stored raw: what the chain computes and signs)
  const is = IV.issuer_schnorr;
  const hBatch = id.batchCommitment(is.cms.map(B));
  assert.equal(hBatch, B(is.hBatch));
  assert.ok(id.issuerSchnorrVerify(
    pt(is.pk),
    { e: B(is.proof.e), s: B(is.proof.s), R: pt(is.proof.R) },
    hBatch, B(is.issuer), chainid));

  // B1 receipt: commitment / nullifier / batch schnorr
  const rc = IV.receipt;
  const op = rc.opening;
  const cm = id.noteCommitment(B(op.flavor), B(op.v), B(op.rho), B(op.idHash), B(op.predicate));
  assert.equal(cm, B(rc.cm));
  assert.ok(rc.cms.map(B).includes(cm));
  const rcptHBatch = id.batchCommitment(rc.cms.map(B));
  assert.equal(rcptHBatch, B(rc.hBatch));
  assert.equal(id.nullifierB(B(op.rho), B(op.idHash)), B(rc.nullifier));
  assert.ok(id.issuerSchnorrVerify(
    pt(rc.issuer_pk),
    { e: B(rc.issuer_sig.e), s: B(rc.issuer_sig.s), R: pt(rc.issuer_sig.R) },
    rcptHBatch, B(rc.issuer), chainid));

  // approve receipt: verifiable decryption naming alice
  const ar = IV.approve_receipt;
  assert.ok(id.verifiableDecryptVerify(
    ct(ar.E_for_spender), pt(ar.spender_pk), pt(ar.M_named),
    { e: B(ar.vd_proof.e), s: B(ar.vd_proof.s), T1: pt(ar.vd_proof.T1), T2: pt(ar.vd_proof.T2) },
    B(ar.spender), chainid));

  // A2 issuer re-encryption binding (SNARK-fixture-pinned section)
  const ir = IV.issuer_reenc;
  const irp = {
    e: B(ir.proof.e), s_r: B(ir.proof.s_r), s_b: B(ir.proof.s_b),
    s_s: B(ir.proof.s_s), s_g: B(ir.proof.s_g),
    A1: pt(ir.proof.A1), A2: pt(ir.proof.A2), A3: pt(ir.proof.A3),
    A4: pt(ir.proof.A4), A5: pt(ir.proof.A5),
    Q: pt(ir.proof.Q), U: pt(ir.proof.U), T: pt(ir.proof.T),
  };
  assert.ok(id.issuerReencVerify(
    pt(ir.pk_iss), ct(ir.E_reg), ct(ir.E_iss), irp, B(ir.issuer), chainid));
});
