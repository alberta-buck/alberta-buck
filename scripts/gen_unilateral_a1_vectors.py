"""Emit canonical cross-artifact vectors for the identity-targeted A1 deposit
coupling, so test/NotesCoupledA1.t.sol can pin the on-chain
IdentityRegistry.verifyDepositCoupling (reused by A1) to the Python reference
(alberta_buck.wallet.unilateral_a1) byte-for-byte.

A1's deposit coupling is the *same* gadget as A2's -- only the note ciphertext
differs (eRec = Enc(M_rec, M_rec) vs A2's eIss = Enc(M_I, M_rec)) -- so the vector
has the identical shape as unilateral_a2.json, over eRec.

Run:  nix develop --command python scripts/gen_unilateral_a1_vectors.py
Out:  test/vectors/unilateral_a1.json
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alberta_buck.wallet.bn254 import G1, ORDER, mul, point_to_hex, scalar_to_hex
from alberta_buck.wallet.elgamal import elgamal_encrypt, elgamal_decrypt
from alberta_buck.wallet.unilateral_a1 import (
    mint_unilateral_a1, deposit_couple_prove, deposit_couple_verify,
    make_receipt_a1, verify_receipt_a1,
)
from alberta_buck.wallet.unilateral_a2 import IdentityTree

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

    # --- issuer: a registered PUBLIC identity (named directly at mint) ---
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

    # --- identity tree (root is the on-chain anchor) ---
    tree = IdentityTree(depth=10)
    for _ in range(2):
        tree.insert(mul(G1, rng()))
    tree.insert(M_iss)
    tree.insert(M_rec)

    # --- mint (public issuer addresses M_rec) ---
    rho = rng()
    minted = mint_unilateral_a1(M_rec, v=1000, rho=rho, rng=rng)

    # --- deposit coupling (depositor) over eRec; the SAME sigma as A2 ---
    proof = deposit_couple_prove(m_rec, sk_dep, E_dep, minted.eRec,
                                 DEPOSIT_ADDR, CHAINID, rng=rng)
    assert deposit_couple_verify(pk_dep, E_dep, minted.eRec, proof,
                                 DEPOSIT_ADDR, CHAINID), "python self-check must pass"

    # --- receipt (recipient, unilateral): names the public issuer + M_rec ---
    receipt = make_receipt_a1(m_rec, minted, M_iss, ISSUER_ADDR, CHAINID, tree, rng=rng)
    res = verify_receipt_a1(receipt, tree.root(), tree)
    assert res.valid, f"receipt must be VALID, got: {res.reason}"

    # The coupling's committed point P_I commits the RECIPIENT identity M_rec.
    assert elgamal_decrypt(minted.eRec, m_rec) == M_rec

    return {
        "$schema_version": 1,
        "chainid": scalar_to_hex(CHAINID),
        "issuer": {
            "addr":  scalar_to_hex(ISSUER_ADDR),
            "pk":    _g1(pk_iss),
            "E_reg": _ct(E_reg_iss),
            "M":     _g1(M_iss),
        },
        "depositor": {
            "addr": scalar_to_hex(DEPOSIT_ADDR),
            "pk":   _g1(pk_dep),
            "E":    _ct(E_dep),
        },
        "eRec": _ct(minted.eRec),
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
            "valid":         res.valid,
            "issuer_M":      _g1(res.issuer_M),
            "recipient_M":   _g1(res.recipient_M),
            "value":         scalar_to_hex(res.value),
            "identity_root": scalar_to_hex(tree.root()),
        },
    }


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "test", "vectors", "unilateral_a1.json")
    data = build()
    with open(out, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
