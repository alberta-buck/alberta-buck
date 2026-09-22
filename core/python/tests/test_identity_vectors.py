"""Identity-kernel conformance, Python side.

Replays every prove call of core/vectors/identity-kernel-vectors.json
(emitted by the py_ecc REFERENCE via alberta_buck.wallet.kernel_vectors,
nonces included) through the buck_core.buck_identity binding, and
re-verifies the committed test/vectors/identity.json fixture.  The Rust
and JS suites assert the same files.

Build the kernel binding first: make nix-core-build-py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

bi = pytest.importorskip(
    "buck_core.buck_identity",
    reason="kernel binding not built (make nix-core-build-py)",
)

_REPO = Path(__file__).resolve().parents[3]


def _load(rel: str) -> dict:
    return json.loads((_REPO / rel).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def kv() -> dict:
    return _load("core/vectors/identity-kernel-vectors.json")


@pytest.fixture(scope="module")
def iv() -> dict:
    return _load("test/vectors/identity.json")


def _i(h: str) -> int:
    return int(h, 16)


def _pt(j: dict) -> tuple:
    return (_i(j["x"]), _i(j["y"]))


def _ct(j: dict) -> tuple:
    return (_pt(j["R"]), _pt(j["C"]))


def _g2(j: dict) -> tuple:
    return ((_i(j["x"][0]), _i(j["x"][1])), (_i(j["y"][0]), _i(j["y"][1])))


# ---------------------------------------------------------------------------
# Kernel vectors: full prove-path replay
# ---------------------------------------------------------------------------

def test_curve_ops_and_pairing(kv):
    for row in kv["g1_ops"]:
        a, b, k = _pt(row["A"]), _pt(row["B"]), _i(row["k"])
        assert bi.g1_add(a, b) == _pt(row["add"])
        assert bi.g1_mul(a, k) == _pt(row["mul"])
        assert bi.g1_neg(a) == _pt(row["neg"])
    for row in kv["g2_ops"]:
        assert bi.g2_mul(bi.G2, _i(row["k"])) == _g2(row["mul"])
    for row in kv["pairing"]:
        pairs = [(_pt(p["g1"]), _g2(p["g2"])) for p in row["pairs"]]
        assert bi.pairing_check(pairs) is row["ok"]


def test_keccak_and_identity_scalar(kv):
    for row in kv["keccak_scalar"]:
        assert bi.keccak_scalar([_i(w) for w in row["words"]]) == _i(row["scalar"])
    for row in kv["identity_scalar"]:
        assert bi.identity_scalar(row["canonical"]) == _i(row["m"])


def test_poseidon_all_arities(kv):
    rows = kv["poseidon"]
    assert len(rows) == 16
    for row in rows:
        assert bi.poseidon([_i(x) for x in row["inputs"]]) == _i(row["hash"])


def test_elgamal(kv):
    for row in kv["elgamal"]:
        m, pk = _pt(row["M"]), _pt(row["pk"])
        e = _ct(row["E"])
        assert bi.elgamal_encrypt(m, pk, _i(row["r"])) == e
        assert bi.elgamal_decrypt(e, _i(row["sk"])) == m


def test_ps(kv):
    p = kv["ps"]
    sk_x, sk_y = _i(p["sk_x"]), _i(p["sk_y"])
    pk_x, pk_y = _g2(p["pk_X"]), _g2(p["pk_Y"])
    assert bi.g2_mul(bi.G2, sk_x) == pk_x
    assert bi.g2_mul(bi.G2, sk_y) == pk_y
    for row in p["signs"]:
        m = _i(row["m"])
        sig = bi.ps_sign(sk_x, sk_y, m, _i(row["t"]))
        assert sig == (_pt(row["sigma_1"]), _pt(row["sigma_2"]))
        assert bi.ps_verify(pk_x, pk_y, sig[0], sig[1], m)
        assert not bi.ps_verify(pk_x, pk_y, sig[0], sig[1], m + 1)
        rr = bi.ps_rerandomize(sig[0], sig[1], _i(row["rerand_t"]))
        assert rr == (_pt(row["rerand_sigma_1"]), _pt(row["rerand_sigma_2"]))
        pres = bi.ps_present(sig[0], sig[1], _pt(p["pk_Y1"]),
                             _i(row["present_a"]), _i(row["present_b"]))
        assert pres == (_pt(row["present_A"]), _pt(row["present_B"]))
        assert not bi.ps_verify(pk_x, pk_y, pres[0], pres[1], m)
    assert bi.ps_key_consistent(pk_y, _pt(p["pk_Y1"]))


def test_schnorr(kv):
    s = kv["schnorr"]
    cms = [_i(c) for c in s["cms"]]
    h_batch = bi.batch_commitment(cms)
    assert h_batch == _i(s["h_batch_raw"]), "raw unreduced keccak word"
    issuer, chainid, k = _i(s["issuer"]), _i(s["chainid"]), _i(s["k"])
    proof = bi.issuer_schnorr_sign(_i(s["sk_iss"]), h_batch, issuer, chainid, k)
    assert proof == (_i(s["proof"]["e"]), _i(s["proof"]["s"]), _pt(s["proof"]["R"]))
    assert bi.issuer_schnorr_verify(_pt(s["pk_iss"]), proof, h_batch, issuer, chainid)
    assert not bi.issuer_schnorr_verify(_pt(s["pk_iss"]), proof, h_batch ^ 1, issuer, chainid)


def test_registration(kv):
    r = kv["registration"]
    sigma = (_pt(r["sigma_1"]), _pt(r["sigma_2"]))
    pres = bi.ps_present(sigma[0], sigma[1], _pt(r["Y1"]), _i(r["a"]), _i(r["b"]))
    assert pres == (_pt(r["A"]), _pt(r["B"]))
    e_ct = _ct(r["E"])
    proof = bi.registration_prove(
        pres[0], pres[1], _i(r["b"]), _i(r["m"]), _i(r["r"]), _pt(r["pk"]), e_ct,
        _i(r["registrant"]), _i(r["sk"]), _i(r["chainid"]),
        _i(r["registry"]),
        _i(r["m_tilde"]), _i(r["b_tilde"]), _i(r["r_tilde"]), _i(r["sk_tilde"]),
    )
    pf = r["proof"]
    assert proof == (
        _i(pf["e"]), _i(pf["s_m"]), _i(pf["s_b"]), _i(pf["s_r"]), _i(pf["s_sk"]),
        _pt(pf["C1"]), _pt(pf["T_C"]), _pt(pf["T_R"]), _pt(pf["T_key"]),
    )
    ps = kv["ps"]
    for verify in (bi.registration_verify, bi.registration_verify_v3):
        assert verify(
            pres[0], pres[1], e_ct, _pt(r["pk"]),
            _g2(ps["pk_X"]), _g2(ps["pk_Y"]), proof, _i(r["registrant"]),
            _i(r["chainid"]), _i(r["registry"]),
        )
        assert not verify(
            pres[0], pres[1], e_ct, _pt(r["pk"]),
            _g2(ps["pk_X"]), _g2(ps["pk_Y"]), proof, _i(r["registrant"]) ^ 1,
            _i(r["chainid"]), _i(r["registry"]),
        )


def test_chaum_pedersen(kv):
    c = kv["chaum_pedersen"]
    e_a, e_b = _ct(c["E_a"]), _ct(c["E_b"])
    pk_a, pk_b = _pt(c["pk_a"]), _pt(c["pk_b"])
    args = (
        _i(c["sender"]), _i(c["spender"]), _i(c["chainid"]),
        _i(c["registry"]),
    )
    proof = bi.chaum_pedersen_prove(
        e_a, e_b, pk_a, pk_b, _i(c["sk_a"]), _i(c["r_prime"]),
        *args, _i(c["k1"]), _i(c["k2"]),
    )
    pf = c["proof"]
    assert proof == (
        _i(pf["e"]), _i(pf["s1"]), _i(pf["s2"]),
        _pt(pf["T1"]), _pt(pf["T2"]), _pt(pf["T3"]),
    )
    assert bi.chaum_pedersen_verify(e_a, e_b, pk_a, pk_b, proof, *args)


def test_verifiable_decrypt(kv):
    r = kv["verifiable_decrypt"]
    e_ct = _ct(r["E"])
    proof = bi.verifiable_decrypt_prove(
        e_ct, _i(r["sk"]), _pt(r["M"]), _i(r["account"]), _i(r["chainid"]), _i(r["t"]),
    )
    pf = r["proof"]
    assert proof == (_i(pf["e"]), _i(pf["s"]), _pt(pf["T1"]), _pt(pf["T2"]))
    assert bi.verifiable_decrypt_verify(
        e_ct, _pt(r["pk"]), _pt(r["M"]), proof, _i(r["account"]), _i(r["chainid"]),
    )


def test_issuer_reenc(kv):
    r = kv["issuer_reenc"]
    e_reg, e_iss = _ct(r["E_reg"]), _ct(r["E_iss"])
    proof = bi.issuer_reenc_prove(
        _i(r["sk_iss"]), _i(r["r_prime"]), _pt(r["pk_rec"]), e_reg, e_iss,
        _i(r["issuer"]), _i(r["chainid"]),
        _i(r["beta"]), _i(r["gamma"]),
        _i(r["k_r"]), _i(r["k_b"]), _i(r["k_s"]), _i(r["k_g"]),
    )
    pf = r["proof"]
    assert proof == (
        (_i(pf["e"]), _i(pf["s_r"]), _i(pf["s_b"]), _i(pf["s_s"]), _i(pf["s_g"])),
        (_pt(pf["A1"]), _pt(pf["A2"]), _pt(pf["A3"]), _pt(pf["A4"]), _pt(pf["A5"]),
         _pt(pf["Q"]), _pt(pf["U"]), _pt(pf["T"])),
    )
    assert bi.issuer_reenc_verify(
        _pt(r["pk_iss"]), e_reg, e_iss, proof, _i(r["issuer"]), _i(r["chainid"]),
    )


def test_b1_bind(kv):
    r = kv["b1_bind"]
    e_dep = _ct(r["E_dep"])
    proof, e_dep_for_iss = bi.b1_bind_prove(
        _i(r["m_dep"]), _i(r["sk_dep"]), e_dep, _pt(r["pk_iss"]),
        _i(r["account"]), _i(r["chainid"]),
        _i(r["r"]), _i(r["b"]),
        _i(r["k_m"]), _i(r["k_s"]), _i(r["k_r"]), _i(r["k_b"]),
    )
    assert e_dep_for_iss == _ct(r["eDepForIss"])
    pf = r["proof"]
    assert proof == (
        _i(pf["e"]), _i(pf["s_m"]), _i(pf["s_s"]), _i(pf["s_r"]), _i(pf["s_b"]),
        _pt(pf["A2"]), _pt(pf["A4"]), _pt(pf["B1"]), _pt(pf["B2"]),
        _pt(pf["A_p"]), _pt(pf["P_dep"]),
    )
    assert bi.b1_bind_verify(
        _pt(r["pk_dep"]), e_dep, _pt(r["pk_iss"]), e_dep_for_iss, proof,
        _i(r["account"]), _i(r["chainid"]),
    )


def test_notes_and_merkle(kv):
    n = kv["notes"]
    e_note, e_iss = _ct(n["eNote"]), _ct(n["eIss"])
    assert bi.id_hash_b1(_i(n["m_issuer"]), _pt(n["sigma_R"]), _i(n["sigma_s"])) == _i(n["id_hash_b1"])
    assert bi.id_hash_a1(e_note, _i(n["m_issuer"]), _pt(n["sigma_R"]), _i(n["sigma_s"])) == _i(n["id_hash_a1"])
    assert bi.id_hash_a2(e_note, e_iss) == _i(n["id_hash_a2"])
    op = n["opening"]
    assert bi.note_commitment(
        _i(op["flavor"]), _i(op["v"]), _i(op["rho"]), _i(op["idHash"]), _i(op["predicate"]),
    ) == _i(n["cm"])
    assert bi.nullifier_b(_i(op["rho"]), _i(op["idHash"])) == _i(n["nullifier_b"])
    assert bi.nullifier_a(_i(op["rho"]), _i(op["idHash"])) == _i(n["nullifier_a"])
    assert bi.identity_leaf(_pt(n["identity_leaf_M"])) == _i(n["identity_leaf"])

    mk = kv["merkle"]
    depth = mk["depth"]
    zeros = [0]
    for _ in range(depth):
        zeros.append(bi.poseidon([zeros[-1], zeros[-1]]))
    nodes = [_i(x) for x in mk["leaves"]]
    for d in range(depth):
        nxt = []
        for i in range(0, len(nodes), 2):
            right = nodes[i + 1] if i + 1 < len(nodes) else zeros[d]
            nxt.append(bi.poseidon([nodes[i], right]))
        nodes = nxt or [zeros[d + 1]]
    assert nodes[0] == _i(mk["root"])
    cur = _i(mk["leaves"][mk["path_index"]])
    for sib, bit in zip(mk["siblings"], mk["index_bits"]):
        cur = bi.poseidon([cur, _i(sib)]) if bit == 0 else bi.poseidon([_i(sib), cur])
    assert cur == _i(mk["root"])


def test_prove_rejects_inconsistent_witness(kv):
    # The kernel mirrors the reference's loud-failure asserts.
    r = kv["issuer_reenc"]
    with pytest.raises(ValueError):
        bi.issuer_reenc_prove(
            _i(r["sk_iss"]) + 1,  # wrong secret key for E_reg
            _i(r["r_prime"]), _pt(r["pk_rec"]), _ct(r["E_reg"]), _ct(r["E_iss"]),
            _i(r["issuer"]), _i(r["chainid"]),
            _i(r["beta"]), _i(r["gamma"]),
            _i(r["k_r"]), _i(r["k_b"]), _i(r["k_s"]), _i(r["k_g"]),
        )


# ---------------------------------------------------------------------------
# The committed forge fixture: verify pass + deterministic recomputation
# ---------------------------------------------------------------------------

def test_identity_fixture(iv):
    chainid = _i(iv["chainid"])
    registry = _i(iv["registry"])
    iss_x, iss_y = _g2(iv["issuer"]["pk_X"]), _g2(iv["issuer"]["pk_Y"])

    for who in ("alice", "bob"):
        p = iv[who]
        m = _i(p["m"])
        assert bi.identity_scalar(p["canonical_identity_data"]) == m
        assert bi.g1_mul(bi.G1, m) == _pt(p["M"])
        assert bi.elgamal_encrypt(_pt(p["M"]), _pt(p["elgamal_kp"]["pk"]), _i(p["r"])) == _ct(p["ciphertext"])
        assert bi.ps_verify(iss_x, iss_y, _pt(p["ps_sig_raw"]["sigma_1"]), _pt(p["ps_sig_raw"]["sigma_2"]), m)
        A, B = _pt(p["ps_presentation"]["A"]), _pt(p["ps_presentation"]["B"])
        assert not bi.ps_verify(iss_x, iss_y, A, B, m), "presentation is not a signature"
        pf = p["registration_proof"]
        proof = (
            _i(pf["e"]), _i(pf["s_m"]), _i(pf["s_b"]), _i(pf["s_r"]), _i(pf["s_sk"]),
            _pt(pf["C1"]), _pt(pf["T_C"]), _pt(pf["T_R"]), _pt(pf["T_key"]),
        )
        assert bi.registration_verify_v3(
            A, B, _ct(p["ciphertext"]), _pt(p["elgamal_kp"]["pk"]),
            iss_x, iss_y, proof, _i(p["registrant"]), chainid, registry,
        )
    assert bi.ps_key_consistent(iss_y, _pt(iv["issuer"]["pk_Y1"]))

    # Unicode canonical-dialect pin: raw UTF-8 (accents + CJK) hashes to m.
    up = iv["unicode_party"]
    canonical = up["canonical_identity_data"]
    assert "Chloé" in canonical and "李" in canonical and "\\u" not in canonical
    assert bi.identity_scalar(canonical) == _i(up["m"])
    assert bi.g1_mul(bi.G1, _i(up["m"])) == _pt(up["M"])

    ap = iv["approve"]
    assert bi.elgamal_encrypt(
        _pt(iv["alice"]["M"]), _pt(iv["bob"]["elgamal_kp"]["pk"]), _i(ap["r_prime"]),
    ) == _ct(ap["E_for_bob"])
    cp = ap["cp_proof"]
    assert bi.chaum_pedersen_verify(
        _ct(ap["E_alice"]), _ct(ap["E_for_bob"]),
        _pt(iv["alice"]["elgamal_kp"]["pk"]), _pt(iv["bob"]["elgamal_kp"]["pk"]),
        (_i(cp["e"]), _i(cp["s1"]), _i(cp["s2"]), _pt(cp["T1"]), _pt(cp["T2"]), _pt(cp["T3"])),
        _i(ap["sender"]), _i(ap["spender"]), chainid,
        _i(ap["registry"]),
    )

    isch = iv["issuer_schnorr"]
    h_batch = bi.batch_commitment([_i(c) for c in isch["cms"]])
    assert h_batch == _i(isch["hBatch"])  # stored raw: what the chain computes
    assert bi.issuer_schnorr_verify(
        _pt(isch["pk"]),
        (_i(isch["proof"]["e"]), _i(isch["proof"]["s"]), _pt(isch["proof"]["R"])),
        h_batch, _i(isch["issuer"]), chainid,
    )

    rc = iv["receipt"]
    op = rc["opening"]
    cm = bi.note_commitment(
        _i(op["flavor"]), _i(op["v"]), _i(op["rho"]), _i(op["idHash"]), _i(op["predicate"]),
    )
    assert cm == _i(rc["cm"]) and cm in [_i(c) for c in rc["cms"]]
    rcpt_h = bi.batch_commitment([_i(c) for c in rc["cms"]])
    assert rcpt_h == _i(rc["hBatch"])
    assert bi.nullifier_b(_i(op["rho"]), _i(op["idHash"])) == _i(rc["nullifier"])
    assert bi.issuer_schnorr_verify(
        _pt(rc["issuer_pk"]),
        (_i(rc["issuer_sig"]["e"]), _i(rc["issuer_sig"]["s"]), _pt(rc["issuer_sig"]["R"])),
        rcpt_h, _i(rc["issuer"]), chainid,
    )

    ar = iv["approve_receipt"]
    assert bi.verifiable_decrypt_verify(
        _ct(ar["E_for_spender"]), _pt(ar["spender_pk"]), _pt(ar["M_named"]),
        (_i(ar["vd_proof"]["e"]), _i(ar["vd_proof"]["s"]),
         _pt(ar["vd_proof"]["T1"]), _pt(ar["vd_proof"]["T2"])),
        _i(ar["spender"]), chainid,
    )

    ir = iv["issuer_reenc"]
    pf = ir["proof"]
    assert bi.issuer_reenc_verify(
        _pt(ir["pk_iss"]), _ct(ir["E_reg"]), _ct(ir["E_iss"]),
        ((_i(pf["e"]), _i(pf["s_r"]), _i(pf["s_b"]), _i(pf["s_s"]), _i(pf["s_g"])),
         (_pt(pf["A1"]), _pt(pf["A2"]), _pt(pf["A3"]), _pt(pf["A4"]), _pt(pf["A5"]),
          _pt(pf["Q"]), _pt(pf["U"]), _pt(pf["T"]))),
        _i(ir["issuer"]), chainid,
    )
