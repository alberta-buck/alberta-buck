"""Generate witness for identity_membership_g1tie.circom (circom-lib version).

Public:  identityRoot, PI_x[4], PI_y[4]  (4-limb F_q)
Private: Mx[4], My[4], Mx_mod, My_mod, Tx[4], Ty[4], Merkle path

The circuit verifies: M in identityRoot AND P_I = M + T (via EllipticCurveAddOptimised).
The deposit coupling sigma separately proved T = b*H.

Preserved review evidence (finding 5), not a protocol path: H is the retired
known-log generator and the leaf is the untagged Poseidon(M.x, M.y), both from
alberta_buck.review.known_log, so the committed fixture reproduces unchanged.
"""

import json, random, sys

sys.path.insert(0, '/Users/perry/src/alberta-buck')
from alberta_buck.wallet.bn254 import G1, ORDER, add, mul, point_to_words, rand_scalar
from alberta_buck.review.known_log import H_KNOWN as H_POINT, untagged_identity_leaf
from alberta_buck.wallet.poseidon import F_R
from alberta_buck.registry.tree import IdentityMerkleTree


def to_limbs(val, n=4, bits=64):
    mask = (1 << bits) - 1
    return [(val >> (i * bits)) & mask for i in range(n)]


def main():
    seed = 0xB0CA
    rng = random.Random(seed)

    # Test identity
    M_scalar = rand_scalar(lambda: rng.getrandbits(256))
    M = mul(G1, M_scalar)
    Mx, My = point_to_words(M)

    # Blinding scalar and T = b*H
    b = rand_scalar(lambda: rng.getrandbits(256)) % ORDER
    T = mul(H_POINT, b)
    Tx, Ty = point_to_words(T)

    # P_I = M + T
    P_I = add(M, T)
    PI_x, PI_y = point_to_words(P_I)

    # Merkle tree
    tree = IdentityMerkleTree(depth=10)
    tree.insert_leaf(untagged_identity_leaf(M))
    identity_root = tree.root()
    proof = tree.path(0)

    # Off-chain verification
    assert P_I == add(M, T), "P_I != M + T"
    assert T == mul(H_POINT, b), "T != b*H"
    assert proof.verify(), "Merkle proof failed"
    assert untagged_identity_leaf(M) == proof.leaf, "leaf mismatch"

    witness = {
        "identityRoot": str(identity_root),
        "PI_x": [str(v) for v in to_limbs(PI_x)],
        "PI_y": [str(v) for v in to_limbs(PI_y)],

        "Mx": [str(v) for v in to_limbs(Mx)],
        "My": [str(v) for v in to_limbs(My)],
        "Mx_mod": str(Mx % F_R),
        "My_mod": str(My % F_R),

        "Tx": [str(v) for v in to_limbs(Tx)],
        "Ty": [str(v) for v in to_limbs(Ty)],

        "pathElements": [str(s) for s in proof.siblings],
        "pathIndices": [str(b) for b in proof.index_bits],
    }

    sys.stderr.write(f"M = {hex(Mx)}, {hex(My)}\n")
    sys.stderr.write(f"T = b*H = {hex(Tx)}, {hex(Ty)}\n")
    sys.stderr.write(f"P_I = M + T = {hex(PI_x)}, {hex(PI_y)}\n")
    sys.stderr.write(f"identityRoot = {hex(identity_root)}\n")

    print(json.dumps(witness, indent=2))


if __name__ == "__main__":
    main()
