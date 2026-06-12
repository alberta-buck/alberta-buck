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
from alberta_buck.wallet.unilateral_a2 import IdentityTree, identity_leaf

DEPTH = 10


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
    tree = IdentityTree(depth=DEPTH)

    # A handful of registered identities; the 4th is our target.
    members = [mul(G1, rng()) for _ in range(7)]
    for M in members:
        tree.insert(M)
    target = members[3]
    idx = tree.index_of(target)
    siblings, bits = tree.path(idx)
    root = tree.root()

    # Self-check against the reference verifier before emitting.
    assert IdentityTree.verify_path(identity_leaf(target), siblings, bits, root), \
        "reference path must verify"

    Mx, My = point_to_words(target)
    good = {
        "identityRoot": str(root),
        "Mx": str(Mx % F_R),
        "My": str(My % F_R),
        "pathElements": [str(s) for s in siblings],
        "pathIndices":  [str(b) for b in bits],
    }
    bad = dict(good, identityRoot=str((root + 1) % F_R))   # wrong root -> reject

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "input.json"), "w") as f:
        json.dump(good, f, indent=2)
    with open(os.path.join(out_dir, "input_bad.json"), "w") as f:
        json.dump(bad, f, indent=2)
    print(f"wrote input.json (member) and input_bad.json (wrong root) to {out_dir}")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "build/snark/identity_membership")
