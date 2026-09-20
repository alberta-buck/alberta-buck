"""Emit canonical cross-artifact vectors for the identity-targeted unilateral-A2
deposit coupling, so test/UnilateralA2.t.sol can pin the on-chain
IdentityRegistry.verifyDepositCoupling to the Python reference
(alberta_buck.wallet.unilateral_a2) byte-for-byte.

An A2 note is KEYED to the recipient's registered receiving key, so this vector
carries `pk_recv` and the receipt's verifiable decryption is under it.

WHAT THE `deposit_coupling` SECTION PINS, AND WHAT IT NO LONGER IS.  It pins the
deployed sigma as a PRIMITIVE -- "some scalar binds the deposit account and
decrypts this ciphertext to the point committed in P_I" -- over `eSigma`, an
identity-keyed ciphertext where that relation is meaningful.  The sigma itself is
unchanged and still sound for that statement.

It is NOT the A2 spend gate any more.  With the note keyed to pk_recv, reading it
and being the Identity are facts about two different secrets, and the sigma's
shared Fiat-Shamir nonce no longer ties them: a payload thief would satisfy both
halves with its own Identity and the stolen key.  The spend gate is the folded
circuit of doc/review/notes-receiving-key.org section 3.3a, whose public inputs
this vector carries in the `fold` section.

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
from alberta_buck.registry.tree import IdentityMerkleTree
from alberta_buck.wallet.deposit_fold import deposit_fold_check, deposit_fold_witness
from alberta_buck.wallet.recvkey import receiving_key
from alberta_buck.wallet.salt import derive_salt

KYC = "kyc:ca-ab-2026"

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

    # --- the recipient's mailbox: a receiving key from its own wallet seed ---
    seed_rec = rng()
    k_recv, pk_recv = receiving_key(seed_rec)
    salt_rec = derive_salt(seed_rec, KYC)
    priv = IdentityMerkleTree(depth=10, private=True)
    priv.insert_receiving(M_rec, pk_recv, salt_rec)

    # --- mint: keyed to the recipient's mailbox ---
    rho = rng()
    minted = mint_unilateral_a2(sk_iss, E_reg_iss, pk_recv, v=1000, rho=rho,
                                issuer=ISSUER_ADDR, chainid=CHAINID, rng=rng)
    assert issuer_reenc_verify(pk_iss, E_reg_iss, minted.eIss, minted.binding,
                               ISSUER_ADDR, CHAINID), "mint binding must verify"

    # --- the sigma PRIMITIVE, over an identity-keyed ciphertext ---
    eSigma = elgamal_encrypt(M_iss, M_rec, rng())
    proof = deposit_couple_prove(m_rec, sk_dep, E_dep, eSigma,
                                 DEPOSIT_ADDR, CHAINID, rng=rng)
    assert deposit_couple_verify(pk_dep, E_dep, eSigma, proof,
                                 DEPOSIT_ADDR, CHAINID), "python self-check must pass"

    # --- the SPEND GATE: one witness, four relations ---
    fold = deposit_fold_witness(m_rec=m_rec, k=k_recv, sk_dep=sk_dep,
                                salt=salt_rec, E_dep=E_dep,
                                note_ct=minted.eIss, tree=priv, rng=rng)
    assert deposit_fold_check(fold, pk_dep=pk_dep, E_dep=E_dep,
                              note_ct=minted.eIss, root=priv.root()), \
        "the folded gate must accept the honest spender"

    # --- receipt (recipient, unilateral) ---
    receipt = make_receipt(k_recv, M_rec, minted, ISSUER_ADDR, CHAINID, tree, rng=rng)
    res = verify_receipt(receipt, pk_iss, E_reg_iss, tree.root(), tree)
    assert res.valid, f"receipt must be VALID, got: {res.reason}"

    M_I = elgamal_decrypt(minted.eIss, k_recv)
    assert elgamal_decrypt(minted.eIss, m_rec) != M_I

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
        "recipient": {
            "M":       _g1(M_rec),
            "pk_recv": _g1(pk_recv),
        },
        # The note's real ciphertext: the issuer Identity, keyed to the mailbox.
        "eIss": _ct(minted.eIss),
        # The ciphertext the deployed SIGMA is pinned over; see the docstring.
        "eSigma": _ct(eSigma),
        # The folded spend gate's public inputs.
        "fold": {
            "P":    _g1(fold.P),
            "root": scalar_to_hex(fold.root),
            "leaf": scalar_to_hex(fold.leaf),
        },
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
