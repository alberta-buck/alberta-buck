"""Witness for circuits/identity_membership_b1.circom.

Builds a depositor with a registered, salted identity leaf, runs the shipped
B1 depositor-binding sigma to obtain the public P_dep, and emits the witness
that proves -- against the same P_dep -- that the point it commits is a member.

With --bad, the attack the old generator admitted: a depositor shifting the
blind so the membership half speaks about SOMEONE ELSE's registered Identity
while the sigma speaks about its own.  That attack needs log_G(H); against
H_PEDERSEN there is no such log to shift by, so the shifted blind simply fails
to reproduce P_dep.

Run:  PYTHONPATH=.:core/python python scripts/snark/gen_b1_membership_input.py
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alberta_buck.registry.tree import IdentityMerkleTree, identity_leaf_salted
from alberta_buck.wallet.b1_binding import b1_bind_prove, b1_bind_verify
from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, eq, mul, neg, point_to_words, rand_scalar,
)
from alberta_buck.wallet.elgamal import elgamal_encrypt
from alberta_buck.wallet.nums import H_PEDERSEN
from alberta_buck.wallet.salt import derive_salt

KYC = "kyc:ca-ab-2026"
DEPTH = 10
DEPOSIT, CHAINID = 0xB0B, 1


def to_limbs(val, n=4, bits=64):
    mask = (1 << bits) - 1
    return [(val >> (i * bits)) & mask for i in range(n)]


def build(shift: bool = False):
    rng_state = random.Random(0xB1F01D)
    rng = lambda: rng_state.getrandbits(256)

    # ---- the depositor, and a second registered identity it happens to know --
    m_dep = rand_scalar(rng)
    M_dep = mul(G1, m_dep)
    sk_dep = rand_scalar(rng)
    E_dep = elgamal_encrypt(M_dep, mul(G1, sk_dep), rand_scalar(rng))
    seed_dep = rand_scalar(rng)
    salt_dep = derive_salt(seed_dep, KYC)

    m_other = rand_scalar(rng)                 # a counterparty's disclosed scalar
    M_other = mul(G1, m_other)
    salt_other = derive_salt(rand_scalar(rng), KYC)

    sk_iss = rand_scalar(rng)
    pk_iss = mul(G1, sk_iss)

    tree = IdentityMerkleTree(depth=DEPTH, private=True)
    tree.insert_leaf(identity_leaf_salted(M_dep, salt_dep))
    tree.insert_leaf(identity_leaf_salted(M_other, salt_other))

    b = rand_scalar(rng)
    proof, eDepForIss = b1_bind_prove(m_dep, sk_dep, E_dep, pk_iss,
                                      DEPOSIT, CHAINID, b=b, rng=rng)
    assert b1_bind_verify(mul(G1, sk_dep), E_dep, pk_iss, eDepForIss, proof,
                          DEPOSIT, CHAINID), "the shipped sigma must verify"

    # The circuit proves P_dep = M + b*H_PEDERSEN for a member M.
    M, blind, salt = M_dep, b, salt_dep
    if shift:
        # The old attack: claim the OTHER registered identity, absorbing the
        # difference into the blind.  Against a known-log H the shift is
        # (m_dep - m_other)/h; here no such h exists, so the best a prover can
        # do is guess -- and P_dep no longer reproduces.
        M, salt = M_other, salt_other
        blind = (b + 1) % ORDER

    T = mul(H_PEDERSEN, blind)
    if not eq(add(M, T), proof.P_dep):
        raise AssertionError(
            "P_dep != M + b*H_PEDERSEN -- the blind cannot be shifted onto "
            "another Identity without knowing log_G(H_PEDERSEN), and nobody does")

    leaf = identity_leaf_salted(M, salt)
    path = tree.path(tree.leaves.index(leaf))
    assert path.verify() and path.root == tree.root()

    Mx, My = point_to_words(M)
    PIx, PIy = point_to_words(proof.P_dep)
    return {
        "identityRoot": str(tree.root()),
        "PI_x": [str(v) for v in to_limbs(PIx)],
        "PI_y": [str(v) for v in to_limbs(PIy)],
        "Mx": [str(v) for v in to_limbs(Mx)],
        "My": [str(v) for v in to_limbs(My)],
        "b": [str(v) for v in to_limbs(blind)],
        "salt": str(salt),
        "pathElements": [str(x) for x in path.siblings],
        "pathIndices": [str(x) for x in path.index_bits],
    }


def main():
    shift = "--bad" in sys.argv
    try:
        witness = build(shift=shift)
    except AssertionError as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return 0 if shift else 1
    if shift:
        sys.stderr.write("BUILT -- must not happen!\n")
        return 1
    sys.stderr.write(f"root = {witness['identityRoot']}\n")
    print(json.dumps(witness, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
