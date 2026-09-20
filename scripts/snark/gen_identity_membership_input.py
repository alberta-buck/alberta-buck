"""Emit circom witness inputs for circuits/identity_membership.circom from the
Python reference IdentityTree, so the circuit's Poseidon-Merkle membership is
pinned to alberta_buck.wallet.unilateral_a2 byte-for-byte.

Usage:  python gen_identity_membership_input.py <out_dir>
Writes: <out_dir>/input.json       -- a genuine member (circuit must accept)
        <out_dir>/input_bad.json   -- a wrong root      (circuit must reject)
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from alberta_buck.wallet.bn254 import G1, ORDER, mul, point_to_words
from alberta_buck.wallet.poseidon import F_R
from alberta_buck.registry.tree import AGGREGATOR_DEPTH, identity_leaf_salted
from alberta_buck.wallet.salt import derive_salt
from alberta_buck.wallet.unilateral_a2 import IdentityTree, identity_leaf

DEPTH = AGGREGATOR_DEPTH
TREE_ID = "kyc:ca-ab-2026"


def _rng(seed=0x1de):
    s = {"x": seed}

    def f():
        x = s["x"]
        x ^= (x << 13) & ((1 << 256) - 1)
        x ^= (x >> 7)
        x ^= (x << 17) & ((1 << 256) - 1)
        s["x"] = x
        return x % ORDER

    return f


def build(out_dir):
    rng = _rng()
    # A PRIVATE sub-tree: leaves are hiding commitments, so the salt is a
    # witness and an unsalted leaf is refused (accumulator specification, s4).
    tree = IdentityTree(depth=DEPTH, private=True)

    # A handful of registered identities; the 4th is our target.  Each holder
    # derives its own salt from a wallet secret it never discloses, so a
    # registry that knows every identity still cannot recompute another
    # holder's leaf.
    members = [mul(G1, rng()) for _ in range(7)]
    secrets = [rng() for _ in members]
    salts = [derive_salt(sec, TREE_ID) for sec in secrets]
    for M, salt in zip(members, salts):
        tree.insert_identity_salted(M, salt)
    target, salt = members[3], salts[3]
    leaf = identity_leaf_salted(target, salt)
    idx = tree.index_of_leaf(leaf)
    proof = tree.path(idx)
    siblings, bits, root = proof.siblings, proof.index_bits, proof.root

    # Self-check against the reference verifier before emitting.
    assert proof.verify(), "reference path must verify"
    # The unsalted leaf of the same identity is NOT in the tree: that is the
    # scan the salt exists to defeat, asserted here so a regression is loud.
    assert not tree.contains(target), \
        "the unsalted leaf of a member must not appear in a private sub-tree"

    Mx, My = point_to_words(target)
    good = {
        "identityRoot": str(root),
        "Mx": str(Mx % F_R),
        "My": str(My % F_R),
        "salt": str(salt),
        "pathElements": [str(s) for s in siblings],
        "pathIndices":  [str(b) for b in bits],
    }
    bad = dict(good, identityRoot=str((root + 1) % F_R))   # wrong root -> reject
    # A second rejection case the salt makes possible: the right identity and
    # path, the wrong salt.  The leaf changes, so the fold misses the root.
    bad_salt = dict(good, salt=str(derive_salt(secrets[3], TREE_ID, 1)))

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "input.json"), "w") as f:
        json.dump(good, f, indent=2)
    with open(os.path.join(out_dir, "input_bad.json"), "w") as f:
        json.dump(bad, f, indent=2)
    with open(os.path.join(out_dir, "input_bad_salt.json"), "w") as f:
        json.dump(bad_salt, f, indent=2)
    print("wrote input.json (member), input_bad.json (wrong root) and "
          f"input_bad_salt.json (wrong salt) to {out_dir}")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "build/snark/identity_membership")
