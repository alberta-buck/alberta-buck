"""Generate test inputs for the identity_membership_g1tie circuit.

Produces a complete witness showing P_I = M + b*H where M is a known
identity point in a test Merkle tree.  Uses the same F_q 4-limb
decomposition the circuit expects, and computes all prover hints
(intermediate F_q operation quotients, reduce flags, λ).

Usage:
    python scripts/snark/gen_g1tie_input.py > /tmp/g1tie_input.json

See: circuits/identity_membership_g1tie.circom
"""

import json
import random
import sys
from typing import List, Tuple

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, mul, neg, eq, point_to_words, rand_scalar,
)
from alberta_buck.wallet.issuer_reenc import H_POINT
from alberta_buck.registry.tree import IdentityMerkleTree, identity_leaf
from alberta_buck.wallet.poseidon import F_R, poseidon

# BN254 base field modulus q
Q = 21888242871839275222246405745257275088696311157297823662689037894645226208583

def to_limbs(val: int, n: int = 4, limb_bits: int = 64) -> List[int]:
    """Decompose val into n limbs of limb_bits each."""
    limbs = []
    mask = (1 << limb_bits) - 1
    for i in range(n):
        limbs.append(val & mask)
        val >>= limb_bits
    return limbs

def from_limbs(limbs: List[int], limb_bits: int = 64) -> int:
    """Reconstruct a value from limbs."""
    val = 0
    for i, limb in enumerate(limbs):
        val += limb << (i * limb_bits)
    return val

def fq_sub_hints(a_val: int, b_val: int) -> Tuple[List[int], int]:
    """Compute (a - b) % Q and need_q flag for Fq4Sub."""
    need_q = 0 if a_val >= b_val else 1
    return to_limbs((a_val - b_val) % Q), need_q

def fq_mul_hints(a_val: int, b_val: int) -> Tuple[List[int], List[int]]:
    """Compute (a*b) % Q and quotient k = floor(a*b/Q) for Fq4Mul."""
    prod = a_val * b_val
    k = prod // Q
    r = prod % Q
    return to_limbs(r), to_limbs(k)

def g1_add_hints(P, Q_pt):
    """Compute λ and F_q hints for G1 point addition R = P + Q."""
    px, py = point_to_words(P)
    qx, qy = point_to_words(Q_pt)
    rx, ry = point_to_words(add(P, Q_pt))

    # λ = (qy - py) / (qx - px) mod Q
    num = (qy - py) % Q
    den = (qx - px) % Q
    lam = (num * pow(den, -1, Q)) % Q

    # Decompose input points
    px_limbs = to_limbs(px)
    py_limbs = to_limbs(py)
    qx_limbs = to_limbs(qx)
    qy_limbs = to_limbs(qy)
    rx_limbs = to_limbs(rx)
    ry_limbs = to_limbs(ry)
    lam_limbs = to_limbs(lam)

    # Fq4Sub hints: need_q = 1 if first < second (need to add Q before subtract)
    dx_limbs, dx_need = fq_sub_hints(qx, px)
    dy_limbs, dy_need = fq_sub_hints(qy, py)
    lam_sq_val = (lam * lam) % Q
    t1_limbs, t1_need = fq_sub_hints(lam_sq_val, px)
    t1_val = (lam_sq_val - px) % Q
    t2_limbs, t2_need = fq_sub_hints(t1_val, qx)
    mxdiff_limbs, mxdiff_need = fq_sub_hints(px, rx)
    mxdiff_val = (px - rx) % Q
    lam_mxdiff_val = (lam * mxdiff_val) % Q
    ycalc_limbs, ycalc_need = fq_sub_hints(lam_mxdiff_val, py)

    # Fq4Mul hints: quotient k = floor(a*b / Q); result = (a*b) % Q
    dx_val = (qx - px) % Q
    lamdx_limbs, lamdx_k = fq_mul_hints(lam, dx_val)
    lamsq_limbs, lamsq_k = fq_mul_hints(lam, lam)
    lamdiff_limbs, lamdiff_k = fq_mul_hints(lam, mxdiff_val)

    return {
        "lam": lam_limbs,
        "dx_out": dx_limbs, "dy_out": dy_limbs,
        "lam_dx_out": lamdx_limbs, "lam_sq_out": lamsq_limbs,
        "t1_out": t1_limbs, "t2_out": t2_limbs,
        "mx_diff_out": mxdiff_limbs, "lam_diff_out": lamdiff_limbs,
        "y_calc_out": ycalc_limbs,
        "sub_need_q_dx": dx_need,
        "sub_need_q_dy": dy_need,
        "sub_need_q_t1": t1_need,
        "sub_need_q_t2": t2_need,
        "sub_need_q_mxdiff": mxdiff_need,
        "sub_need_q_ycalc": ycalc_need,
        "mul_k_lamdx": lamdx_k,
        "mul_k_lamsq": lamsq_k,
        "mul_k_lamdiff": lamdiff_k,
    }


def main():
    seed = 0xB0CA
    rng = random.Random(seed)

    # Create a test identity.
    M_scalar = rand_scalar(lambda: rng.getrandbits(256))
    M = mul(G1, M_scalar)
    Mx, My = point_to_words(M)

    # Choose a blinding scalar b.
    b = rand_scalar(lambda: rng.getrandbits(256)) % ORDER

    # Compute T = b * H
    T = mul(H_POINT, b)
    Tx, Ty = point_to_words(T)

    # Compute P_I = M + T (the commitment point).
    P_I = add(M, T)
    PI_x, PI_y = point_to_words(P_I)

    # Build a test Merkle tree with this identity.
    tree = IdentityMerkleTree(depth=10)
    tree.insert_identity(M)
    identity_root = tree.root()
    proof = tree.path(0)

    # Decompose into 4-limb F_q representation.
    Mx_limbs = to_limbs(Mx)
    My_limbs = to_limbs(My)
    Tx_limbs = to_limbs(Tx)
    Ty_limbs = to_limbs(Ty)
    PI_x_limbs = to_limbs(PI_x)
    PI_y_limbs = to_limbs(PI_y)

    # G1 point addition hints.
    add_hints = g1_add_hints(M, T)

    # Build the full witness.
    witness = {
        "identityRoot": str(identity_root),
        "PI_x": [str(v) for v in PI_x_limbs],
        "PI_y": [str(v) for v in PI_y_limbs],

        "Mx": [str(v) for v in Mx_limbs],
        "My": [str(v) for v in My_limbs],
        "Mx_mod": str(Mx % F_R),
        "My_mod": str(My % F_R),
        "b": str(b),

        "Tx": [str(v) for v in Tx_limbs],
        "Ty": [str(v) for v in Ty_limbs],

        "lam": [str(v) for v in add_hints["lam"]],

        "dx_out": [str(v) for v in add_hints["dx_out"]],
        "dy_out": [str(v) for v in add_hints["dy_out"]],
        "lam_dx_out": [str(v) for v in add_hints["lam_dx_out"]],
        "lam_sq_out": [str(v) for v in add_hints["lam_sq_out"]],
        "t1_out": [str(v) for v in add_hints["t1_out"]],
        "t2_out": [str(v) for v in add_hints["t2_out"]],
        "mx_diff_out": [str(v) for v in add_hints["mx_diff_out"]],
        "lam_diff_out": [str(v) for v in add_hints["lam_diff_out"]],
        "y_calc_out": [str(v) for v in add_hints["y_calc_out"]],

        "sub_need_q_dx": str(add_hints["sub_need_q_dx"]),
        "sub_need_q_dy": str(add_hints["sub_need_q_dy"]),
        "mk0_lamdx": str(add_hints["mul_k_lamdx"][0]),
        "mk1_lamdx": str(add_hints["mul_k_lamdx"][1]),
        "mk2_lamdx": str(add_hints["mul_k_lamdx"][2]),
        "mk3_lamdx": str(add_hints["mul_k_lamdx"][3]),
        "mk0_lamsq": str(add_hints["mul_k_lamsq"][0]),
        "mk1_lamsq": str(add_hints["mul_k_lamsq"][1]),
        "mk2_lamsq": str(add_hints["mul_k_lamsq"][2]),
        "mk3_lamsq": str(add_hints["mul_k_lamsq"][3]),
        "sub_need_q_t1": str(add_hints["sub_need_q_t1"]),
        "sub_need_q_t2": str(add_hints["sub_need_q_t2"]),
        "sub_need_q_mxdiff": str(add_hints["sub_need_q_mxdiff"]),
        "mk0_lamdiff": str(add_hints["mul_k_lamdiff"][0]),
        "mk1_lamdiff": str(add_hints["mul_k_lamdiff"][1]),
        "mk2_lamdiff": str(add_hints["mul_k_lamdiff"][2]),
        "mk3_lamdiff": str(add_hints["mul_k_lamdiff"][3]),
        "sub_need_q_ycalc": str(add_hints["sub_need_q_ycalc"]),

        "pathElements": [str(s) for s in proof.siblings],
        "pathIndices": [str(b) for b in proof.index_bits],
    }

    # Verification (off-chain):
    assert eq(P_I, add(M, T)), "P_I != M + T"
    assert eq(T, mul(H_POINT, b)), "T != b * H"
    assert proof.verify(), "M not in tree"
    leaf = identity_leaf(M)
    assert leaf == proof.leaf, "leaf mismatch"
    assert from_limbs(Mx_limbs) == Mx
    assert from_limbs(My_limbs) == My
    assert from_limbs(PI_x_limbs) == PI_x
    assert from_limbs(PI_y_limbs) == PI_y

    import sys as _sys
    _sys.stderr.write(f"// G1-tie circuit witness (all checks pass off-chain)\n")
    _sys.stderr.write(f"// M = {hex(Mx)}, {hex(My)}\n")
    _sys.stderr.write(f"// b = {hex(b)}\n")
    _sys.stderr.write(f"// H = {hex(point_to_words(H_POINT)[0])}, {hex(point_to_words(H_POINT)[1])}\n")
    _sys.stderr.write(f"// T = b*H = {hex(Tx)}, {hex(Ty)}\n")
    _sys.stderr.write(f"// P_I = M + T = {hex(PI_x)}, {hex(PI_y)}\n")
    _sys.stderr.write(f"// identityRoot = {hex(identity_root)}\n")

    print(json.dumps(witness, indent=2))


if __name__ == "__main__":
    main()
