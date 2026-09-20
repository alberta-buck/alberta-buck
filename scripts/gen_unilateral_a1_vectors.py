"""Emit canonical cross-artifact vectors for the identity-targeted A1 deposit
coupling, so test/NotesCoupledA1.t.sol can pin the on-chain
IdentityRegistry.verifyDepositCoupling (reused by A1) to the Python reference
(alberta_buck.wallet.unilateral_a1) byte-for-byte.

A1's note NAMES the recipient Identity (eRec's plaintext) and is KEYED to that
Identity's registered receiving key (eRec = Enc(M_rec, pk_recv)), so this vector
carries `pk_recv` and the receipt's verifiable decryption is under it.

WHAT THE `coupling` SECTION PINS, AND WHAT IT NO LONGER IS.  It pins the
on-chain sigma `IdentityRegistry.verifyDepositCoupling` as a PRIMITIVE: "some
scalar binds the deposit account and decrypts this ciphertext to the point
committed in P_I".  That sigma is unchanged and still sound for that statement.

It is NOT the A1/A2 spend gate any more.  Once the note is keyed to pk_recv,
reading it and being the Identity are facts about two different secrets, and the
sigma's shared Fiat-Shamir nonce no longer ties them -- so a payload thief would
satisfy both halves with its own Identity and the stolen key.  The spend gate is
the folded circuit of doc/review/notes-receiving-key.org section 3.3a, whose
witness this vector also carries (the `fold` section) for the circuit to consume.

Run:  nix develop --command python scripts/gen_unilateral_a1_vectors.py
Out:  test/vectors/unilateral_a1.json
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alberta_buck.wallet.bn254 import G1, ORDER, mul, point_to_hex, scalar_to_hex
from alberta_buck.wallet.elgamal import elgamal_encrypt, elgamal_decrypt
from alberta_buck.registry.tree import IdentityMerkleTree
from alberta_buck.wallet.deposit_fold import deposit_fold_check, deposit_fold_witness
from alberta_buck.wallet.recvkey import receiving_key
from alberta_buck.wallet.salt import derive_salt
from alberta_buck.wallet.unilateral_a1 import (
    mint_unilateral_a1, deposit_couple_prove, deposit_couple_verify,
    make_receipt_a1, verify_receipt_a1,
)
from alberta_buck.wallet.unilateral_a2 import IdentityTree

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

    # --- the recipient's mailbox: a receiving key from its own wallet seed ---
    seed_rec = rng()
    k_recv, pk_recv = receiving_key(seed_rec)
    salt_rec = derive_salt(seed_rec, KYC)
    priv = IdentityMerkleTree(depth=10, private=True)
    priv.insert_receiving(M_rec, pk_recv, salt_rec)

    # --- mint: NAMES M_rec, KEYED to pk_recv ---
    rho = rng()
    minted = mint_unilateral_a1(M_rec, pk_recv, v=1000, rho=rho,
                               m_issuer=m_iss, sigma_R=mul(G1, rng()),
                               sigma_s=rng(), rng=rng)

    # --- the sigma PRIMITIVE, over an identity-keyed ciphertext ---
    # Kept in the setting where its relation is meaningful, so the vector pins
    # what the deployed sigma actually proves rather than a vacuous instance of
    # it.  See the module docstring.
    eSigma = elgamal_encrypt(M_iss, M_rec, rng())
    proof = deposit_couple_prove(m_rec, sk_dep, E_dep, eSigma,
                                 DEPOSIT_ADDR, CHAINID, rng=rng)
    assert deposit_couple_verify(pk_dep, E_dep, eSigma, proof,
                                 DEPOSIT_ADDR, CHAINID), "python self-check must pass"

    # --- the SPEND GATE: one witness, four relations ---
    fold = deposit_fold_witness(m_rec=m_rec, k=k_recv, sk_dep=sk_dep,
                                salt=salt_rec, E_dep=E_dep,
                                note_ct=minted.eRec, tree=priv, rng=rng)
    assert deposit_fold_check(fold, pk_dep=pk_dep, E_dep=E_dep,
                              note_ct=minted.eRec, root=priv.root()), \
        "the folded gate must accept the honest spender"

    # --- receipt (recipient, unilateral): names the public issuer + M_rec ---
    receipt = make_receipt_a1(k_recv, M_rec, minted, M_iss, ISSUER_ADDR, CHAINID,
                              tree, rng=rng)
    res = verify_receipt_a1(receipt, tree.root(), tree)
    assert res.valid, f"receipt must be VALID, got: {res.reason}"

    # eRec's plaintext is the Identity; its key is the mailbox.  The identity
    # scalar -- which every counterparty holds -- opens nothing.
    assert elgamal_decrypt(minted.eRec, k_recv) == M_rec
    assert elgamal_decrypt(minted.eRec, m_rec) != M_rec

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
        "recipient": {
            "M":       _g1(M_rec),
            "pk_recv": _g1(pk_recv),
        },
        # The note's real ciphertext: the Identity named, keyed to the mailbox.
        "eRec": _ct(minted.eRec),
        # The ciphertext the deployed SIGMA is pinned over -- identity-keyed, so
        # its relation is meaningful.  Not the note; see the module docstring.
        "eSigma": _ct(eSigma),
        # The folded spend gate's public inputs.  The witness itself is private;
        # what a verifier sees is P, the posted root, and the note ciphertext.
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
