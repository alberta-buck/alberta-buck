// BigInt-native wrapper over the buck-identity wasm kernel.
//
// The raw wasm ABI (wasm/buck_identity.js, built by `make
// nix-core-build-wasm`) speaks 0x-hex strings and flat arrays; this module
// wraps it in the structured shapes the Python wallet and the JSON
// fixtures use:
//
//   point       {x, y}                       (BigInt; {0n, 0n} = infinity)
//   G2 point    {x: [c0, c1], y: [c0, c1]}
//   ciphertext  {R: point, C: point}
//   PS sig      {sigma_1: point, sigma_2: point}
//   proofs      objects with the Python dataclass field names
//
// Deterministic: every nonce is an argument.  `randScalar()` draws from
// WebCrypto for live use; tests replay recorded nonces from
// core/vectors/identity-kernel-vectors.json.

import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const wasm = require("../wasm/buck_identity.js");

// ---------------------------------------------------------------------------
// BigInt <-> hex
// ---------------------------------------------------------------------------

export const hex = (v) => "0x" + BigInt(v).toString(16).padStart(64, "0");
export const big = (s) => BigInt(s);

const P = (arr, i = 0) => ({ x: big(arr[i]), y: big(arr[i + 1]) });
const CT = (arr, i = 0) => ({ R: P(arr, i), C: P(arr, i + 2) });
const flatP = (p) => [hex(p.x), hex(p.y)];
const flatCT = (ct) => [...flatP(ct.R), ...flatP(ct.C)];
const flatG2 = (g) => [hex(g.x[0]), hex(g.x[1]), hex(g.y[0]), hex(g.y[1])];

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const ORDER = big(wasm.order());
export const F_R = ORDER;
export const FIELD_MODULUS = big(wasm.field_modulus());
export const G1 = P(wasm.g1_generator());
export const G2 = (() => {
  const g = wasm.g2_generator();
  return { x: [big(g[0]), big(g[1])], y: [big(g[2]), big(g[3])] };
})();
export const H_POINT = P(wasm.h_point());
export const FLAVOR_A1 = 1;
export const FLAVOR_A2 = 2;
export const FLAVOR_B1 = 3;

// ---------------------------------------------------------------------------
// Randomness (live use; tests inject recorded nonces instead)
// ---------------------------------------------------------------------------

/** Random non-zero scalar mod ORDER via WebCrypto. */
export function randScalar() {
  for (;;) {
    const bytes = new Uint8Array(32);
    globalThis.crypto.getRandomValues(bytes);
    let v = 0n;
    for (const b of bytes) v = (v << 8n) | BigInt(b);
    v %= ORDER;
    if (v !== 0n) return v;
  }
}

// ---------------------------------------------------------------------------
// Canonical identity JSON (Python json.dumps sort_keys/compact equivalent)
// ---------------------------------------------------------------------------

/** Canonical identity-data JSON: sorted keys, compact separators, and
 *  ASCII-only output (non-ASCII as lowercase \uXXXX escapes, Python's
 *  json.dumps default) -- byte-identical to canonical_identity_data().
 *  NOTE: the AB-RCPT/1 envelope canonicalization differs (raw UTF-8). */
export function canonicalIdentity(fields) {
  const sort = (v) => {
    if (Array.isArray(v)) return v.map(sort);
    if (v && typeof v === "object") {
      return Object.fromEntries(
        Object.keys(v).sort().map((k) => [k, sort(v[k])]),
      );
    }
    return v;
  };
  // Python escapes every char outside 0x20..0x7E; JSON.stringify already
  // handles < 0x20, so escape 0x7F..0xFFFF code units (surrogate halves
  // escape individually, matching Python's astral-pair behavior).
  return JSON.stringify(sort(fields)).replace(
    /[\u007f-\uffff]/g,
    (ch) => "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0"),
  );
}

// ---------------------------------------------------------------------------
// Curve ops / hashes
// ---------------------------------------------------------------------------

export const g1Add = (a, b) => P(wasm.g1_add(...flatP(a), ...flatP(b)));
export const g1Mul = (p, k) => P(wasm.g1_mul(...flatP(p), hex(k)));
export const g1Neg = (p) => P(wasm.g1_neg(...flatP(p)));
export const g2Mul = (g, k) => {
  const r = wasm.g2_mul(...flatG2(g), hex(k));
  return { x: [big(r[0]), big(r[1])], y: [big(r[2]), big(r[3])] };
};

/** EVM ecPairing semantics: pairs = [[g1Point, g2Point], ...]. */
export const pairingCheck = (pairs) =>
  wasm.pairing_check(pairs.flatMap(([p, q]) => [...flatP(p), ...flatG2(q)]));

export const keccakScalar = (words) => big(wasm.keccak_scalar(words.map(hex)));
export const identityScalar = (canonical) => big(wasm.identity_scalar(canonical));
export const reduceModOrder = (v) => big(wasm.reduce_mod_order(hex(v)));
export const poseidon = (inputs) => big(wasm.poseidon(inputs.map(hex)));

// ---------------------------------------------------------------------------
// ElGamal / PS
// ---------------------------------------------------------------------------

export const elgamalEncrypt = (M, pk, r) =>
  CT(wasm.elgamal_encrypt(...flatP(M), ...flatP(pk), hex(r)));
export const elgamalDecrypt = (E, sk) =>
  P(wasm.elgamal_decrypt(...flatCT(E), hex(sk)));

export function psSign(skX, skY, m, t) {
  const r = wasm.ps_sign(hex(skX), hex(skY), hex(m), hex(t));
  return { sigma_1: P(r, 0), sigma_2: P(r, 2) };
}

export const psVerify = (pkX, pkY, sig, m) =>
  wasm.ps_verify(flatG2(pkX), flatG2(pkY), ...flatP(sig.sigma_1), ...flatP(sig.sigma_2), hex(m));

export function psRerandomize(sig, t) {
  const r = wasm.ps_rerandomize(...flatP(sig.sigma_1), ...flatP(sig.sigma_2), hex(t));
  return { sigma_1: P(r, 0), sigma_2: P(r, 2) };
}

// ---------------------------------------------------------------------------
// Schnorr batch binding
// ---------------------------------------------------------------------------

/** Raw UNREDUCED keccak word (what the Schnorr transcript signs). */
export const batchCommitment = (cms) => big(wasm.batch_commitment(cms.map(hex)));

export function issuerSchnorrSign(skIss, hBatch, issuer, chainid, k) {
  const r = wasm.issuer_schnorr_sign(hex(skIss), hex(hBatch), hex(issuer), hex(chainid), hex(k));
  return { e: big(r[0]), s: big(r[1]), R: P(r, 2) };
}

export const issuerSchnorrVerify = (pkIss, proof, hBatch, issuer, chainid) =>
  wasm.issuer_schnorr_verify(
    ...flatP(pkIss),
    [hex(proof.e), hex(proof.s), ...flatP(proof.R)],
    hex(hBatch),
    hex(issuer),
    hex(chainid),
  );

// ---------------------------------------------------------------------------
// Registration NIZK
// ---------------------------------------------------------------------------

export function registrationProve(sig, m, r, pk, E, registrant, mTilde, rTilde) {
  const o = wasm.registration_prove(
    [...flatP(sig.sigma_1), ...flatP(sig.sigma_2)],
    hex(m), hex(r), ...flatP(pk), flatCT(E), hex(registrant), hex(mTilde), hex(rTilde),
  );
  return { e: big(o[0]), s_m: big(o[1]), s_r: big(o[2]), A_ps: P(o, 3), T_C: P(o, 5), T_R: P(o, 7) };
}

export const registrationVerify = (sig, E, pk, issuerX, issuerY, proof, registrant) =>
  wasm.registration_verify(
    [...flatP(sig.sigma_1), ...flatP(sig.sigma_2)],
    flatCT(E),
    ...flatP(pk),
    flatG2(issuerX),
    flatG2(issuerY),
    [hex(proof.e), hex(proof.s_m), hex(proof.s_r),
     ...flatP(proof.A_ps), ...flatP(proof.T_C), ...flatP(proof.T_R)],
    hex(registrant),
  );

// ---------------------------------------------------------------------------
// Chaum-Pedersen approve
// ---------------------------------------------------------------------------

export function chaumPedersenProve(eAlice, eBob, pkA, pkB, skA, rPrime, sender, spender, chainid, k1, k2) {
  const o = wasm.chaum_pedersen_prove(
    flatCT(eAlice), flatCT(eBob), ...flatP(pkA), ...flatP(pkB),
    hex(skA), hex(rPrime), hex(sender), hex(spender), hex(chainid), hex(k1), hex(k2),
  );
  return { e: big(o[0]), s1: big(o[1]), s2: big(o[2]), T1: P(o, 3), T2: P(o, 5), T3: P(o, 7) };
}

export const chaumPedersenVerify = (eAlice, eBob, pkA, pkB, proof, sender, spender, chainid) =>
  wasm.chaum_pedersen_verify(
    flatCT(eAlice), flatCT(eBob), ...flatP(pkA), ...flatP(pkB),
    [hex(proof.e), hex(proof.s1), hex(proof.s2),
     ...flatP(proof.T1), ...flatP(proof.T2), ...flatP(proof.T3)],
    hex(sender), hex(spender), hex(chainid),
  );

// ---------------------------------------------------------------------------
// Verifiable decryption
// ---------------------------------------------------------------------------

export function verifiableDecryptProve(E, sk, M, account, chainid, t) {
  const o = wasm.verifiable_decrypt_prove(
    flatCT(E), hex(sk), ...flatP(M), hex(account), hex(chainid), hex(t),
  );
  return { e: big(o[0]), s: big(o[1]), T1: P(o, 2), T2: P(o, 4) };
}

export const verifiableDecryptVerify = (E, pk, M, proof, account, chainid) =>
  wasm.verifiable_decrypt_verify(
    flatCT(E), ...flatP(pk), ...flatP(M),
    [hex(proof.e), hex(proof.s), ...flatP(proof.T1), ...flatP(proof.T2)],
    hex(account), hex(chainid),
  );

// ---------------------------------------------------------------------------
// A2 issuer re-encryption binding
// ---------------------------------------------------------------------------

export function issuerReencProve(skIss, rPrime, pkRec, eReg, eIss, issuer, chainid,
                                 beta, gamma, kR, kB, kS, kG) {
  const o = wasm.issuer_reenc_prove(
    hex(skIss), hex(rPrime), ...flatP(pkRec), flatCT(eReg), flatCT(eIss),
    hex(issuer), hex(chainid),
    hex(beta), hex(gamma), hex(kR), hex(kB), hex(kS), hex(kG),
  );
  return {
    e: big(o[0]), s_r: big(o[1]), s_b: big(o[2]), s_s: big(o[3]), s_g: big(o[4]),
    A1: P(o, 5), A2: P(o, 7), A3: P(o, 9), A4: P(o, 11), A5: P(o, 13),
    Q: P(o, 15), U: P(o, 17), T: P(o, 19),
  };
}

export const issuerReencVerify = (pkIss, eReg, eIss, proof, issuer, chainid) =>
  wasm.issuer_reenc_verify(
    ...flatP(pkIss), flatCT(eReg), flatCT(eIss),
    [hex(proof.e), hex(proof.s_r), hex(proof.s_b), hex(proof.s_s), hex(proof.s_g),
     ...flatP(proof.A1), ...flatP(proof.A2), ...flatP(proof.A3),
     ...flatP(proof.A4), ...flatP(proof.A5),
     ...flatP(proof.Q), ...flatP(proof.U), ...flatP(proof.T)],
    hex(issuer), hex(chainid),
  );

// ---------------------------------------------------------------------------
// Deposit coupling / B1 depositor binding
// ---------------------------------------------------------------------------

export function depositCoupleProve(mRec, skDep, eDep, eIss, account, chainid, b, kM, kS, kB) {
  const o = wasm.deposit_couple_prove(
    hex(mRec), hex(skDep), flatCT(eDep), flatCT(eIss),
    hex(account), hex(chainid), hex(b), hex(kM), hex(kS), hex(kB),
  );
  return {
    e: big(o[0]), s_m: big(o[1]), s_s: big(o[2]), s_b: big(o[3]),
    A2: P(o, 4), A3: P(o, 6), A4: P(o, 8), P_I: P(o, 10),
  };
}

export const depositCoupleVerify = (pkDep, eDep, eIss, proof, account, chainid) =>
  wasm.deposit_couple_verify(
    ...flatP(pkDep), flatCT(eDep), flatCT(eIss),
    [hex(proof.e), hex(proof.s_m), hex(proof.s_s), hex(proof.s_b),
     ...flatP(proof.A2), ...flatP(proof.A3), ...flatP(proof.A4), ...flatP(proof.P_I)],
    hex(account), hex(chainid),
  );

export function b1BindProve(mDep, skDep, eDep, pkIss, account, chainid, r, b, kM, kS, kR, kB) {
  const o = wasm.b1_bind_prove(
    hex(mDep), hex(skDep), flatCT(eDep), ...flatP(pkIss),
    hex(account), hex(chainid),
    hex(r), hex(b), hex(kM), hex(kS), hex(kR), hex(kB),
  );
  return {
    proof: {
      e: big(o[0]), s_m: big(o[1]), s_s: big(o[2]), s_r: big(o[3]), s_b: big(o[4]),
      A2: P(o, 5), A4: P(o, 7), B1: P(o, 9), B2: P(o, 11), A_p: P(o, 13), P_dep: P(o, 15),
    },
    eDepForIss: CT(o, 17),
  };
}

export const b1BindVerify = (pkDep, eDep, pkIss, eDepForIss, proof, account, chainid) =>
  wasm.b1_bind_verify(
    ...flatP(pkDep), flatCT(eDep), ...flatP(pkIss), flatCT(eDepForIss),
    [hex(proof.e), hex(proof.s_m), hex(proof.s_s), hex(proof.s_r), hex(proof.s_b),
     ...flatP(proof.A2), ...flatP(proof.A4), ...flatP(proof.B1),
     ...flatP(proof.B2), ...flatP(proof.A_p), ...flatP(proof.P_dep)],
    hex(account), hex(chainid),
  );

// ---------------------------------------------------------------------------
// Notes family
// ---------------------------------------------------------------------------

export const noteCommitment = (flavor, v, rho, idHash, predicate) =>
  big(wasm.note_commitment(Number(flavor), hex(v), hex(rho), hex(idHash), hex(predicate)));
export const nullifierB = (rho, idHash) => big(wasm.nullifier_b(hex(rho), hex(idHash)));
export const nullifierA = (rho, idHash) => big(wasm.nullifier_a(hex(rho), hex(idHash)));
export const idHashB1 = (mIssuer, sigmaR, sigmaS) =>
  big(wasm.id_hash_b1(hex(mIssuer), ...flatP(sigmaR), hex(sigmaS)));
export const idHashA1 = (eNote, mIssuer, sigmaR, sigmaS) =>
  big(wasm.id_hash_a1(flatCT(eNote), hex(mIssuer), ...flatP(sigmaR), hex(sigmaS)));
export const idHashA2 = (eNote, eIss) => big(wasm.id_hash_a2(flatCT(eNote), flatCT(eIss)));
export const identityLeaf = (M) => big(wasm.identity_leaf(...flatP(M)));
