"""Emit canonical cross-artifact vectors for the identity-targeted unilateral-A2
deposit coupling, so test/UnilateralA2.t.sol can pin the on-chain
IdentityRegistry.verifyDepositCoupling to the Python reference
(alberta_buck.wallet.unilateral_a2) byte-for-byte.

Run:  nix develop --command python scripts/gen_unilateral_a2_vectors.py
Out:  test/vectors/unilateral_a2.json
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alberta_buck.wallet.bn254 import (
    G1, ORDER, mul, point_to_hex, scalar_to_hex,
)
from alberta_buck.wallet.elgamal import elgamal_encrypt, elgamal_decrypt
from alberta_buck.wallet.unilateral_a2 import (
    IdentityTree, mint_unilateral_a2,
    deposit_couple_prove, deposit_couple_verify,
    make_receipt, verify_receipt,
)
from alberta_buck.wallet.issuer_reenc import issuer_reenc_verify

CHAINID = 1
ISSUER_ADDR = 0xA11CE
DEPOSIT_ADDR = 0xB0B
SEED = 0x5EED


def _rng_factory(seed):
    state = {"x": seed}

    def rng():
        x = state["x"]
        x ^= (x << 13) & ((1 << 256) - 1)
        x ^= (x >> 7)
        x ^= (x << 17) & ((1 << 256) - 1)
        state["x"] = x
        return x % ORDER

    return rng


def _g1(P):
    x, y = point_to_hex(P)
    return {"x": x, "y": y}


def _ct(E):
    return {"R": _g1(E.R), "C": _g1(E.C)}


def build():
    rng = _rng_factory(SEED)

    # --- issuer: a registered private identity + account ---
    m_iss = rng()
    M_iss = mul(G1, m_iss)
    sk_iss = rng()
    pk_iss = mul(G1, sk_iss)
    E_reg_iss = elgamal_encrypt(M_iss, pk_iss, rng())

    # --- recipient: identity m_rec + one Fountain account used to deposit ---
    m_rec = rng()
    M_rec = mul(G1, m_rec)
    sk_dep = rng()
    pk_dep = mul(G1, sk_dep)
    E_dep = elgamal_encrypt(M_rec, pk_dep, rng())

    # --- identity tree (root is the on-chain anchor; tree lives off chain) ---
    tree = IdentityTree(depth=10)
    for _ in range(2):
        tree.insert(mul(G1, rng()))
    tree.insert(M_iss)
    tree.insert(M_rec)

    # --- mint (issuer) ---
    rho = rng()
    minted = mint_unilateral_a2(sk_iss, E_reg_iss, M_rec, v=1000, rho=rho,
                                issuer=ISSUER_ADDR, chainid=CHAINID, rng=rng)
    assert issuer_reenc_verify(pk_iss, E_reg_iss, minted.eIss, minted.binding,
                               ISSUER_ADDR, CHAINID), "mint binding must verify"

    # --- deposit coupling (depositor) ---
    proof = deposit_couple_prove(m_rec, sk_dep, E_dep, minted.eIss,
                                 DEPOSIT_ADDR, CHAINID, rng=rng)
    assert deposit_couple_verify(pk_dep, E_dep, minted.eIss, proof,
                                 DEPOSIT_ADDR, CHAINID), "python self-check must pass"

    # --- receipt (recipient, unilateral) ---
    receipt = make_receipt(m_rec, minted, ISSUER_ADDR, CHAINID, tree, rng=rng)
    res = verify_receipt(receipt, pk_iss, E_reg_iss, tree.root(), tree)
    assert res.valid, f"receipt must be VALID, got: {res.reason}"

    M_I = elgamal_decrypt(minted.eIss, m_rec)

    return {
        "$schema_version": 1,
        "chainid": scalar_to_hex(CHAINID),
        "issuer": {
            "addr":  scalar_to_hex(ISSUER_ADDR),
            "pk":    _g1(pk_iss),
            "E_reg": _ct(E_reg_iss),
        },
        "depositor": {
            "addr": scalar_to_hex(DEPOSIT_ADDR),
            "pk":   _g1(pk_dep),
            "E":    _ct(E_dep),
        },
        "eIss": _ct(minted.eIss),
        "deposit_coupling": {
            "e":   scalar_to_hex(proof.e),
            "s_m": scalar_to_hex(proof.s_m),
            "s_s": scalar_to_hex(proof.s_s),
            "s_b": scalar_to_hex(proof.s_b),
            "A2":  _g1(proof.A2),
            "A3":  _g1(proof.A3),
            "A4":  _g1(proof.A4),
            "P_I": _g1(proof.P_I),
        },
        "receipt": {
            "valid":       res.valid,
            "issuer_M":    _g1(res.issuer_M),
            "recipient_M": _g1(res.recipient_M),
            "value":       scalar_to_hex(res.value),
            "identity_root": scalar_to_hex(tree.root()),
            "M_I_decrypted": _g1(M_I),
        },
    }


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "test", "vectors", "unilateral_a2.json")
    data = build()
    with open(out, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
