"""Cross-language kernel vectors.

Emits ``core/vectors/identity-kernel-vectors.json`` from the pure-Python
py_ecc REFERENCE path -- the executable spec -- so the Rust (`cargo`),
Python (`pytest`), and JS (`node --test`) suites can replay every prove
call bit-identically.  Unlike ``test/vectors/identity.json`` (whose prove
nonces are internal rng draws), every row here records ALL inputs
INCLUDING nonces, in the exact order the reference draws them -- which is
also the argument order of the corresponding ``buck_identity`` kernel
function.

Regenerate: ``make nix-venv-core-identity-vectors`` (any change to this
file's draws is an ABI-break-level event for the three suites).
"""

from __future__ import annotations

import json
import os
import random
from typing import Any, Callable, Dict, List

from alberta_buck.wallet.bn254 import (
    G1, G2, ORDER, add, mul, neg, pairing, FQ12_one,
    point_to_words, rand_scalar, scalar_to_hex,
)
from alberta_buck.wallet.poseidon import F_R, poseidon
from alberta_buck.wallet.transcript import keccak_scalar
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.elgamal import elgamal_encrypt, elgamal_decrypt
from alberta_buck.wallet.ps import ps_keygen, ps_sign, ps_verify, ps_rerandomize, ps_present
from alberta_buck.wallet.schnorr import batch_commitment, issuer_schnorr_sign
from alberta_buck.wallet.chaum_pedersen import chaum_pedersen_prove
from alberta_buck.wallet.nizk import registration_prove
from alberta_buck.wallet.verifiable_decrypt import verifiable_decrypt_prove
from alberta_buck.wallet.issuer_reenc import issuer_reenc_prove
from alberta_buck.wallet.b1_binding import b1_bind_prove
from alberta_buck.wallet.notes import (
    FLAVOR_B1, NoteOpening, note_commitment, nullifier_a, nullifier_b,
    id_hash_a1, id_hash_a2, id_hash_b1,
)
from alberta_buck.wallet.unilateral_a2 import IdentityTree
from alberta_buck.registry.tree import identity_leaf


def _seeded_rng(seed: int) -> Callable[[], int]:
    rnd = random.Random(seed)
    return lambda: rnd.getrandbits(256)


from alberta_buck.wallet.ps import PSSignature as PSSignatureLike  # noqa: E402


def _replay(vals: List[int]) -> Callable[[], int]:
    """An rng that hands back exactly `vals` -- feeding pre-drawn nonces to
    a reference prover so they can be recorded alongside its output."""
    it = iter(vals)
    return lambda: next(it)


def _hx(v: int) -> str:
    """Full-width uint256 hex, NO reduction (use for raw keccak words)."""
    return f"0x{v:064x}"


def _g1(P) -> Dict[str, str]:
    x, y = point_to_words(P)
    return {"x": _hx(x), "y": _hx(y)}


def _g2(P) -> Dict[str, Any]:
    return {
        "x": [_hx(int(P[0].coeffs[0])), _hx(int(P[0].coeffs[1]))],
        "y": [_hx(int(P[1].coeffs[0])), _hx(int(P[1].coeffs[1]))],
    }


def _ct(E) -> Dict[str, Any]:
    return {"R": _g1(E.R), "C": _g1(E.C)}


def build_kernel_vectors(seed: int = 0x1DE47B0CA) -> Dict[str, Any]:
    # The reference path MUST emit these vectors: force the pure-Python
    # backend for the duration of the build (the kernel shims read this
    # env at call time), restoring the caller's setting afterwards.
    prev = os.environ.get("BUCK_IDENTITY_BACKEND")
    os.environ["BUCK_IDENTITY_BACKEND"] = "py"
    try:
        return _build_kernel_vectors(seed)
    finally:
        if prev is None:
            os.environ.pop("BUCK_IDENTITY_BACKEND", None)
        else:
            os.environ["BUCK_IDENTITY_BACKEND"] = prev


def _build_kernel_vectors(seed: int) -> Dict[str, Any]:
    rng = _seeded_rng(seed)
    draw = lambda: rand_scalar(rng)
    registry = int("1d" * 20, 16)

    out: Dict[str, Any] = {
        "$schema_version": 2,   # A': ps.pk_Y1/present_*, registration Y1/a/b/A/B/b_tilde, proof s_b/C1
        "backend": "py",
        "seed": _hx(seed),
        "ORDER": _hx(ORDER),
    }

    # ---- curve ops ---------------------------------------------------------
    curve = []
    for _ in range(4):
        a, b, k = draw(), draw(), draw()
        A, B = mul(G1, a), mul(G1, b)
        curve.append({
            "A": _g1(A), "B": _g1(B), "k": scalar_to_hex(k),
            "add": _g1(add(A, B)),
            "mul": _g1(mul(A, k)),
            "neg": _g1(neg(A)),
        })
    out["g1_ops"] = curve

    g2ops = []
    for _ in range(2):
        k = draw()
        g2ops.append({"k": scalar_to_hex(k), "mul": _g2(mul(G2, k))})
    out["g2_ops"] = g2ops

    # pairing product checks: e(aG, bG2) * e(-(ab)G, G2) == 1; plus a false one
    a, b = draw(), draw()
    ab = (a * b) % ORDER
    out["pairing"] = [
        {
            "pairs": [
                {"g1": _g1(mul(G1, a)), "g2": _g2(mul(G2, b))},
                {"g1": _g1(neg(mul(G1, ab))), "g2": _g2(G2)},
            ],
            "ok": True,
        },
        {
            "pairs": [
                {"g1": _g1(mul(G1, a)), "g2": _g2(mul(G2, b))},
                {"g1": _g1(neg(mul(G1, (ab + 1) % ORDER))), "g2": _g2(G2)},
            ],
            "ok": False,
        },
    ]
    assert pairing(mul(G2, b), mul(G1, a)) * pairing(G2, neg(mul(G1, ab))) == FQ12_one()

    # ---- keccak / identity scalar ------------------------------------------
    out["keccak_scalar"] = []
    for n in (1, 3, 7):
        words = [draw() for _ in range(n)]
        out["keccak_scalar"].append({
            "words": [_hx(w) for w in words],
            "scalar": scalar_to_hex(keccak_scalar(*words)),
        })
    idents = [
        {"given_name": "Alice", "epoch": 42},
        {"given_name": "Zoë", "café": True, "n": 7},
        # Astral-plane coverage: the maple emoji is a surrogate pair in
        # UTF-16 hosts (JS) and 4 UTF-8 bytes -- external implementations
        # must hash the raw UTF-8, not escapes.
        {"city": "Sainte-Thérèse", "note": "🍁 maple", "李": "CJK key"},
        {},
    ]
    out["identity_scalar"] = []
    for f in idents:
        canonical = canonical_identity_data(f)
        out["identity_scalar"].append({
            "canonical": canonical,
            "m": scalar_to_hex(identity_scalar(canonical)),
        })

    # ---- poseidon: every arity ----------------------------------------------
    out["poseidon"] = []
    for n in range(1, 17):
        inputs = [draw() % F_R for _ in range(n)]
        out["poseidon"].append({
            "inputs": [_hx(x) for x in inputs],
            "hash": _hx(poseidon(inputs)),
        })

    # ---- elgamal -------------------------------------------------------------
    out["elgamal"] = []
    for _ in range(2):
        m_s, sk, r = draw(), draw(), draw()
        M = mul(G1, m_s)
        pk = mul(G1, sk)
        E = elgamal_encrypt(M, pk, r)
        assert elgamal_decrypt(E, sk) == M
        out["elgamal"].append({
            "M": _g1(M), "pk": _g1(pk), "sk": scalar_to_hex(sk),
            "r": scalar_to_hex(r), "E": _ct(E),
        })

    # ---- ps ------------------------------------------------------------------
    kp = ps_keygen(rng=_replay([draw(), draw()]))
    out["ps"] = {
        "sk_x": scalar_to_hex(kp.sk_x), "sk_y": scalar_to_hex(kp.sk_y),
        "pk_X": _g2(kp.pk_X), "pk_Y": _g2(kp.pk_Y), "pk_Y1": _g1(kp.pk_Y1),
        "signs": [],
    }
    for _ in range(2):
        m, t = draw(), draw()
        sig = ps_sign(kp, m, rng=_replay([t]))
        assert ps_verify(kp.pk_X, kp.pk_Y, sig, m)
        t2 = draw()
        rr, _t = ps_rerandomize(sig, rng=_replay([t2]))
        a, b = draw(), draw()
        pres, _, _ = ps_present(sig, kp.pk_Y1, a=a, b=b)
        # The presentation is NOT a signature on m (review finding R1 closed).
        assert not ps_verify(kp.pk_X, kp.pk_Y, PSSignatureLike(pres.A, pres.B), m)
        out["ps"]["signs"].append({
            "m": scalar_to_hex(m), "t": scalar_to_hex(t),
            "sigma_1": _g1(sig.sigma_1), "sigma_2": _g1(sig.sigma_2),
            "rerand_t": scalar_to_hex(t2),
            "rerand_sigma_1": _g1(rr.sigma_1), "rerand_sigma_2": _g1(rr.sigma_2),
            "present_a": scalar_to_hex(a), "present_b": scalar_to_hex(b),
            "present_A": _g1(pres.A), "present_B": _g1(pres.B),
        })

    # ---- schnorr ---------------------------------------------------------------
    sk_iss, issuer_addr, chainid = draw(), 0xBEEF % (1 << 160), 1
    cms = [draw() % F_R for _ in range(3)]
    h_batch = batch_commitment(cms)
    k = draw()
    sig = issuer_schnorr_sign(sk_iss, h_batch, issuer_addr, chainid, rng=_replay([k]))
    out["schnorr"] = {
        "sk_iss": scalar_to_hex(sk_iss), "pk_iss": _g1(mul(G1, sk_iss)),
        "issuer": _hx(issuer_addr), "chainid": _hx(chainid),
        "cms": [_hx(c) for c in cms],
        "h_batch_raw": _hx(h_batch),   # UNREDUCED keccak word
        "k": scalar_to_hex(k),
        "proof": {"e": scalar_to_hex(sig.e), "s": scalar_to_hex(sig.s), "R": _g1(sig.R)},
    }

    # ---- registration NIZK -------------------------------------------------
    m, r, sk_e = draw(), draw(), draw()
    pk_e = mul(G1, sk_e)
    M = mul(G1, m)
    E = elgamal_encrypt(M, pk_e, r)
    t = draw()
    sigma = ps_sign(kp, m, rng=_replay([t]))
    a, b = draw(), draw()
    pres, _, _ = ps_present(sigma, kp.pk_Y1, a=a, b=b)
    registrant = 0xA11CE % (1 << 160)
    m_tilde, b_tilde, r_tilde, sk_tilde = draw(), draw(), draw(), draw()
    proof = registration_prove(
        pres, b, m, r, pk_e, E, registrant, sk_e, chainid,
        rng=_replay([m_tilde, b_tilde, r_tilde, sk_tilde]), registry=registry,
    )
    from alberta_buck.wallet.nizk import registration_verify
    assert registration_verify(pres, E, pk_e, kp.pk_X, kp.pk_Y, proof, registrant,
                               chainid, registry)
    out["registration"] = {
        "m": scalar_to_hex(m), "r": scalar_to_hex(r),
        "sk": scalar_to_hex(sk_e),
        "pk": _g1(pk_e), "E": _ct(E),
        "sigma_1": _g1(sigma.sigma_1), "sigma_2": _g1(sigma.sigma_2),
        "Y1": _g1(kp.pk_Y1),
        "a": scalar_to_hex(a), "b": scalar_to_hex(b),
        "A": _g1(pres.A), "B": _g1(pres.B),
        "registrant": _hx(registrant), "chainid": _hx(chainid),
        "registry": _hx(registry),
        "m_tilde": scalar_to_hex(m_tilde), "b_tilde": scalar_to_hex(b_tilde),
        "r_tilde": scalar_to_hex(r_tilde), "sk_tilde": scalar_to_hex(sk_tilde),
        "proof": {
            "e": scalar_to_hex(proof.e), "s_m": scalar_to_hex(proof.s_m),
            "s_b": scalar_to_hex(proof.s_b),
            "s_r": scalar_to_hex(proof.s_r), "s_sk": scalar_to_hex(proof.s_sk),
            "C1": _g1(proof.C1),
            "T_C": _g1(proof.T_C), "T_R": _g1(proof.T_R),
            "T_key": _g1(proof.T_key),
        },
    }

    # ---- chaum-pedersen approve ---------------------------------------------
    sk_b = draw()
    pk_b = mul(G1, sk_b)
    r_prime = draw()
    E_b = elgamal_encrypt(M, pk_b, r_prime)
    sender, spender = registrant, 0x0B0B % (1 << 160)
    k1, k2 = draw(), draw()
    cp = chaum_pedersen_prove(E, E_b, pk_e, pk_b, sk_e, r_prime,
                              sender, spender, chainid, rng=_replay([k1, k2]),
                              registry=registry)
    out["chaum_pedersen"] = {
        "E_a": _ct(E), "E_b": _ct(E_b), "pk_a": _g1(pk_e), "pk_b": _g1(pk_b),
        "sk_a": scalar_to_hex(sk_e), "r_prime": scalar_to_hex(r_prime),
        "sender": _hx(sender), "spender": _hx(spender), "chainid": _hx(chainid),
        "registry": _hx(registry),
        "k1": scalar_to_hex(k1), "k2": scalar_to_hex(k2),
        "proof": {
            "e": scalar_to_hex(cp.e), "s1": scalar_to_hex(cp.s1),
            "s2": scalar_to_hex(cp.s2), "T1": _g1(cp.T1),
            "T2": _g1(cp.T2), "T3": _g1(cp.T3),
        },
    }

    # ---- verifiable decryption ------------------------------------------------
    t_vd = draw()
    vd = verifiable_decrypt_prove(E_b, sk_b, M, spender, chainid, rng=_replay([t_vd]))
    out["verifiable_decrypt"] = {
        "E": _ct(E_b), "sk": scalar_to_hex(sk_b), "pk": _g1(pk_b), "M": _g1(M),
        "account": _hx(spender), "chainid": _hx(chainid),
        "t": scalar_to_hex(t_vd),
        "proof": {"e": scalar_to_hex(vd.e), "s": scalar_to_hex(vd.s),
                  "T1": _g1(vd.T1), "T2": _g1(vd.T2)},
    }

    # ---- issuer re-encryption binding -------------------------------------------
    # Issuer: registered credential E_reg encrypting M_iss under pk_i.
    sk_i, m_i, r_reg = draw(), draw(), draw()
    pk_i = mul(G1, sk_i)
    M_iss = mul(G1, m_i)
    E_reg = elgamal_encrypt(M_iss, pk_i, r_reg)
    # Recipient key (a registered pk, or an identity point for unilateral A2).
    pk_rec = mul(G1, draw())
    rp = draw()
    E_iss = elgamal_encrypt(M_iss, pk_rec, rp)
    beta, gamma, k_r, k_b, k_s, k_g = (draw() for _ in range(6))
    ir = issuer_reenc_prove(sk_i, rp, pk_rec, E_reg, E_iss, issuer_addr, chainid,
                            rng=_replay([k_r, k_b, k_s, k_g]),
                            beta=beta, gamma=gamma)
    out["issuer_reenc"] = {
        "sk_iss": scalar_to_hex(sk_i), "pk_iss": _g1(pk_i),
        "E_reg": _ct(E_reg), "E_iss": _ct(E_iss), "pk_rec": _g1(pk_rec),
        "r_prime": scalar_to_hex(rp),
        "issuer": _hx(issuer_addr), "chainid": _hx(chainid),
        "beta": scalar_to_hex(beta), "gamma": scalar_to_hex(gamma),
        "k_r": scalar_to_hex(k_r), "k_b": scalar_to_hex(k_b),
        "k_s": scalar_to_hex(k_s), "k_g": scalar_to_hex(k_g),
        "proof": {
            "e": scalar_to_hex(ir.e), "s_r": scalar_to_hex(ir.s_r),
            "s_b": scalar_to_hex(ir.s_b), "s_s": scalar_to_hex(ir.s_s),
            "s_g": scalar_to_hex(ir.s_g),
            "A1": _g1(ir.A1), "A2": _g1(ir.A2), "A3": _g1(ir.A3),
            "A4": _g1(ir.A4), "A5": _g1(ir.A5),
            "Q": _g1(ir.Q), "U": _g1(ir.U), "T": _g1(ir.T),
        },
    }

    # ---- a registered deposit account, for the B1 depositor binding -----------------
    m_rec, sk_dep, r_d = draw(), draw(), draw()
    M_rec = mul(G1, m_rec)
    pk_dep = mul(G1, sk_dep)
    E_dep = elgamal_encrypt(M_rec, pk_dep, r_d)
    account = 0xDE9051 % (1 << 160)

    # ---- b1 depositor binding ---------------------------------------------------
    r_f, b_f, k_m3, k_s3, k_r3, k_b3 = (draw() for _ in range(6))
    db, eDepForIss = b1_bind_prove(m_rec, sk_dep, E_dep, pk_i, account, chainid,
                                   rng=_replay([k_m3, k_s3, k_r3, k_b3]),
                                   r=r_f, b=b_f)
    out["b1_bind"] = {
        "m_dep": scalar_to_hex(m_rec), "sk_dep": scalar_to_hex(sk_dep),
        "pk_dep": _g1(pk_dep), "E_dep": _ct(E_dep), "pk_iss": _g1(pk_i),
        "account": _hx(account), "chainid": _hx(chainid),
        "r": scalar_to_hex(r_f), "b": scalar_to_hex(b_f),
        "k_m": scalar_to_hex(k_m3), "k_s": scalar_to_hex(k_s3),
        "k_r": scalar_to_hex(k_r3), "k_b": scalar_to_hex(k_b3),
        "eDepForIss": _ct(eDepForIss),
        "proof": {
            "e": scalar_to_hex(db.e), "s_m": scalar_to_hex(db.s_m),
            "s_s": scalar_to_hex(db.s_s), "s_r": scalar_to_hex(db.s_r),
            "s_b": scalar_to_hex(db.s_b),
            "A2": _g1(db.A2), "A4": _g1(db.A4), "B1": _g1(db.B1),
            "B2": _g1(db.B2), "A_p": _g1(db.A_p), "P_dep": _g1(db.P_dep),
        },
    }

    # ---- notes family ------------------------------------------------------------
    rho = draw()
    sigma_R = mul(G1, draw())
    sigma_s = draw()
    idh_b1 = id_hash_b1(m_i, sigma_R, sigma_s)
    # Addressed ciphertexts are keyed to a MAILBOX key, never to an identity
    # point; these rows only pin the hashes, but they keep the protocol's shape.
    pk_mailbox = mul(G1, draw())
    eNote = elgamal_encrypt(mul(G1, 250), pk_mailbox, draw())
    eIss = elgamal_encrypt(M_iss, pk_mailbox, draw())
    idh_a1 = id_hash_a1(eNote, m_i, sigma_R, sigma_s)
    idh_a2 = id_hash_a2(eNote, eIss)
    opening = NoteOpening(flavor=FLAVOR_B1, v=250, rho=rho, id_hash=idh_b1, predicate=0)
    out["notes"] = {
        "m_issuer": scalar_to_hex(m_i),
        "sigma_R": _g1(sigma_R), "sigma_s": scalar_to_hex(sigma_s),
        "eNote": _ct(eNote), "eIss": _ct(eIss),
        "id_hash_b1": _hx(idh_b1), "id_hash_a1": _hx(idh_a1), "id_hash_a2": _hx(idh_a2),
        "opening": {"flavor": _hx(FLAVOR_B1), "v": _hx(250), "rho": scalar_to_hex(rho),
                    "idHash": _hx(idh_b1), "predicate": _hx(0)},
        "cm": _hx(note_commitment(opening)),
        "nullifier_b": _hx(nullifier_b(rho, idh_b1)),
        "nullifier_a": _hx(nullifier_a(rho, idh_b1)),
        "identity_leaf_M": _g1(M_rec),
        "identity_leaf": _hx(identity_leaf(M_rec)),
    }

    # ---- merkle tree (depth 10, the on-chain depth) -------------------------------
    tree = IdentityTree()
    points = [mul(G1, draw()) for _ in range(5)]
    for P in points:
        tree.insert(P)
    pf = tree.path(2)
    out["merkle"] = {
        "depth": tree.depth,
        "leaves": [_hx(identity_leaf(P)) for P in points],
        "root": _hx(tree.root()),
        "path_index": 2,
        "siblings": [_hx(s) for s in pf.siblings],
        "index_bits": pf.index_bits,
    }

    return out


def emit_kernel_vectors(path: str, seed: int = 0x1DE47B0CA) -> Dict[str, Any]:
    data = build_kernel_vectors(seed=seed)
    with open(path, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")
    return data


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "core/vectors/identity-kernel-vectors.json"
    data = emit_kernel_vectors(out)
    sys.stderr.write(f"wrote {out} ({len(json.dumps(data))} bytes JSON)\n")
