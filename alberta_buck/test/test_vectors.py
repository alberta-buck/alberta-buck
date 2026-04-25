"""Round-trip the emitted JSON test vectors back through the wallet verifiers.

Catches any encoding/decoding mistake in vectors.py before the Solidity tests
inherit it.  Also pins the file is reproducible from the seed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alberta_buck.wallet.bn254 import (
    G1, ORDER, mul, words_to_point,
)
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.elgamal import ElGamalCiphertext, elgamal_decrypt, IdentityKeyPair
from alberta_buck.wallet.ps import PSSignature, ps_verify
from alberta_buck.wallet.nizk import RegistrationProof, registration_verify
from alberta_buck.wallet.chaum_pedersen import CPProof, chaum_pedersen_verify
from alberta_buck.wallet.spend_cp import SpendCPProof, spend_cp_verify
from alberta_buck.wallet.vectors import build_vectors


def _h(s: str) -> int:
    return int(s, 16)


def _pt(d):
    return words_to_point(_h(d["x"]), _h(d["y"]))


def _g2_from(d):
    from py_ecc.bn128.bn128_curve import FQ2
    x = FQ2([_h(d["x"][0]), _h(d["x"][1])])
    y = FQ2([_h(d["y"][0]), _h(d["y"][1])])
    return (x, y)


def _decode(j):
    issuer_X = _g2_from(j["issuer"]["pk_X"])
    issuer_Y = _g2_from(j["issuer"]["pk_Y"])

    def party(p):
        return {
            "m":  _h(p["m"]),
            "M":  _pt(p["M"]),
            "kp": IdentityKeyPair(sk=_h(p["elgamal_kp"]["sk"]), pk=_pt(p["elgamal_kp"]["pk"])),
            "E":  ElGamalCiphertext(R=_pt(p["ciphertext"]["R"]), C=_pt(p["ciphertext"]["C"])),
            "sigma_p": PSSignature(
                sigma_1=_pt(p["ps_sig_rerand"]["sigma_1"]),
                sigma_2=_pt(p["ps_sig_rerand"]["sigma_2"]),
            ),
            "registrant": _h(p["registrant"]),
            "proof": RegistrationProof(
                e=_h(p["registration_proof"]["e"]),
                s_m=_h(p["registration_proof"]["s_m"]),
                s_r=_h(p["registration_proof"]["s_r"]),
                A_ps=_pt(p["registration_proof"]["A_ps"]),
                T_C=_pt(p["registration_proof"]["T_C"]),
                T_R=_pt(p["registration_proof"]["T_R"]),
            ),
        }

    a = party(j["alice"])
    b = party(j["bob"])
    ap = j["approve"]
    cp = ap["cp_proof"]
    approve = {
        "sender":  _h(ap["sender"]),
        "spender": _h(ap["spender"]),
        "chainid": _h(ap["chainid"]),
        "E_alice":   ElGamalCiphertext(R=_pt(ap["E_alice"]["R"]),   C=_pt(ap["E_alice"]["C"])),
        "E_for_bob": ElGamalCiphertext(R=_pt(ap["E_for_bob"]["R"]), C=_pt(ap["E_for_bob"]["C"])),
        "proof": CPProof(
            e=_h(cp["e"]), s1=_h(cp["s1"]), s2=_h(cp["s2"]),
            T1=_pt(cp["T1"]), T2=_pt(cp["T2"]), T3=_pt(cp["T3"]),
        ),
    }
    return issuer_X, issuer_Y, a, b, approve


@pytest.fixture(scope="module")
def vectors():
    """Use the in-memory build, not the on-disk file: keeps tests independent."""
    return build_vectors()


def test_canonical_identity_data_matches_emitted(vectors):
    canon = vectors["alice"]["canonical_identity_data"]
    assert canon == canonical_identity_data(vectors["alice"]["fields"])
    assert _h(vectors["alice"]["m"]) == identity_scalar(canon)


def test_ORDER_field_matches_constant(vectors):
    assert _h(vectors["ORDER"]) == ORDER


def test_alice_ps_signature_verifies(vectors):
    issuer_X, issuer_Y, a, b, _ = _decode(vectors)
    assert ps_verify(issuer_X, issuer_Y, a["sigma_p"], a["m"])


def test_bob_ps_signature_verifies(vectors):
    issuer_X, issuer_Y, a, b, _ = _decode(vectors)
    assert ps_verify(issuer_X, issuer_Y, b["sigma_p"], b["m"])


def test_alice_registration_proof_verifies(vectors):
    issuer_X, issuer_Y, a, _, _ = _decode(vectors)
    assert registration_verify(
        a["sigma_p"], a["E"], a["kp"].pk, issuer_X, issuer_Y, a["proof"], a["registrant"],
    )


def test_bob_registration_proof_verifies(vectors):
    issuer_X, issuer_Y, _, b, _ = _decode(vectors)
    assert registration_verify(
        b["sigma_p"], b["E"], b["kp"].pk, issuer_X, issuer_Y, b["proof"], b["registrant"],
    )


def test_alice_ciphertext_decrypts_to_M(vectors):
    _, _, a, _, _ = _decode(vectors)
    assert elgamal_decrypt(a["E"], a["kp"].sk) == a["M"]
    assert mul(G1, a["m"]) == a["M"]


def test_chaum_pedersen_proof_verifies(vectors):
    _, _, a, b, ap = _decode(vectors)
    assert chaum_pedersen_verify(
        ap["E_alice"], ap["E_for_bob"],
        a["kp"].pk, b["kp"].pk,
        ap["proof"],
        ap["sender"], ap["spender"], ap["chainid"],
    )


def test_bob_can_decrypt_re_encrypted_M(vectors):
    _, _, a, b, ap = _decode(vectors)
    assert elgamal_decrypt(ap["E_for_bob"], b["kp"].sk) == a["M"]


def test_spend_cp_proof_verifies(vectors):
    """Round-trip the V2 A-spend CP-DLEQ vector through the Python verifier."""
    _, _, a, _, _ = _decode(vectors)
    sc = vectors["spend_cp"]
    E_n = ElGamalCiphertext(R=_pt(sc["E_n"]["R"]), C=_pt(sc["E_n"]["C"]))
    pi  = SpendCPProof(
        e=_h(sc["proof"]["e"]), s=_h(sc["proof"]["s"]),
        T1=_pt(sc["proof"]["T1"]), T2=_pt(sc["proof"]["T2"]),
    )
    assert spend_cp_verify(
        E_n, a["E"], a["kp"].pk, pi,
        recipient=_h(sc["recipient"]),
        chainid=_h(sc["chainid"]),
    )


def test_vectors_are_deterministic_for_same_seed():
    a = build_vectors(seed=0xdeadbeef)
    b = build_vectors(seed=0xdeadbeef)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_vectors_differ_for_different_seed():
    a = build_vectors(seed=0x1)
    b = build_vectors(seed=0x2)
    assert a["alice"]["registration_proof"]["e"] != b["alice"]["registration_proof"]["e"]
