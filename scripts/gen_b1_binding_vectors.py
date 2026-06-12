"""Emit canonical cross-artifact vectors for the B1 depositor binding, so
test/B1Binding.t.sol can pin IdentityRegistry.verifyDepositorBinding to the
Python reference (alberta_buck.wallet.b1_binding) byte-for-byte.

Run:  nix develop --command python scripts/gen_b1_binding_vectors.py
Out:  test/vectors/b1_binding.json
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alberta_buck.wallet.bn254 import G1, ORDER, mul, point_to_hex, scalar_to_hex
from alberta_buck.wallet.elgamal import elgamal_encrypt, elgamal_decrypt
from alberta_buck.wallet.unilateral_a2 import IdentityTree
from alberta_buck.wallet.b1_binding import (
    b1_bind_prove, b1_bind_verify, make_issuer_receipt, verify_issuer_receipt,
)

CHAINID = 1
ISSUER_ADDR = 0xC0FFEE
DEPOSIT_ADDR = 0xB0B
SEED = 0xB1


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

    # public issuer: M_iss + account (sk_iss, pk_iss, E_reg)
    m_iss = rng()
    M_iss = mul(G1, m_iss)
    sk_iss = rng()
    pk_iss = mul(G1, sk_iss)
    E_reg_iss = elgamal_encrypt(M_iss, pk_iss, rng())

    # depositor: m_dep + payout account
    m_dep = rng()
    M_dep = mul(G1, m_dep)
    sk_dep = rng()
    pk_dep = mul(G1, sk_dep)
    E_dep = elgamal_encrypt(M_dep, pk_dep, rng())

    tree = IdentityTree(depth=10)
    for _ in range(2):
        tree.insert(mul(G1, rng()))
    tree.insert(M_iss)
    tree.insert(M_dep)

    proof, eDepForIss = b1_bind_prove(m_dep, sk_dep, E_dep, pk_iss,
                                      DEPOSIT_ADDR, CHAINID, rng=rng)
    assert b1_bind_verify(pk_dep, E_dep, pk_iss, eDepForIss, proof,
                          DEPOSIT_ADDR, CHAINID), "python self-check must pass"

    receipt = make_issuer_receipt(sk_iss, M_iss, eDepForIss, value=750,
                                  issuer=ISSUER_ADDR, chainid=CHAINID, tree=tree, rng=rng)
    res = verify_issuer_receipt(receipt, tree.root(), tree)
    assert res.valid, f"issuer receipt must be VALID, got: {res.reason}"

    return {
        "$schema_version": 1,
        "chainid": scalar_to_hex(CHAINID),
        "issuer": {
            "addr": scalar_to_hex(ISSUER_ADDR),
            "pk":   _g1(pk_iss),
            "E_reg": _ct(E_reg_iss),
        },
        "depositor": {
            "addr": scalar_to_hex(DEPOSIT_ADDR),
            "pk":   _g1(pk_dep),
            "E":    _ct(E_dep),
        },
        "eDepForIss": _ct(eDepForIss),
        "depositor_binding": {
            "e":     scalar_to_hex(proof.e),
            "s_m":   scalar_to_hex(proof.s_m),
            "s_s":   scalar_to_hex(proof.s_s),
            "s_r":   scalar_to_hex(proof.s_r),
            "s_b":   scalar_to_hex(proof.s_b),
            "A2":    _g1(proof.A2),
            "A4":    _g1(proof.A4),
            "B1":    _g1(proof.B1),
            "B2":    _g1(proof.B2),
            "A_p":   _g1(proof.A_p),
            "P_dep": _g1(proof.P_dep),
        },
        "receipt": {
            "valid":       res.valid,
            "issuer_M":    _g1(res.issuer_M),
            "depositor_M": _g1(res.recipient_M),
            "value":       scalar_to_hex(res.value),
            "identity_root": scalar_to_hex(tree.root()),
        },
    }


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "test", "vectors", "b1_binding.json")
    with open(out, "w") as f:
        json.dump(build(), f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
