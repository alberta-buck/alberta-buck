"""BN254 (alt_bn128) primitives, mirroring Ethereum's ecAdd/ecMul/ecPairing precompiles.

Thin wrappers around py_ecc.bn128 plus encoding helpers that produce the exact
byte layout the BN254.sol Solidity helper consumes.
"""

from __future__ import annotations

import secrets
from typing import Tuple

from py_ecc.bn128 import bn128_curve as _bc
from py_ecc.bn128 import bn128_pairing as _bp
from py_ecc.bn128.bn128_curve import FQ as _FQ

# Re-exports — keep the same names the identity-example.org Python uses.
G1 = _bc.G1
G2 = _bc.G2
Z1 = _bc.Z1
Z2 = _bc.Z2
ORDER = _bc.curve_order

add = _bc.add
mul = _bc.multiply
neg = _bc.neg
eq = _bc.eq
is_inf = _bc.is_inf
pairing = _bp.pairing
FQ12_one = _bp.FQ12.one


def rand_scalar(rng=None) -> int:
    """Random non-zero scalar mod ORDER.

    rng: optional callable taking no args returning an int in [0, 2**256).
         When None, uses secrets.randbelow (cryptographic).  Tests pass a
         seeded RNG to make vector emission reproducible.
    """
    if rng is None:
        return secrets.randbelow(ORDER - 1) + 1
    while True:
        v = rng() % ORDER
        if v != 0:
            return v


# --- Encoding: the canonical bridge to Solidity ---
#
# G1 points are passed to/read from Solidity as a pair (uint256 X, uint256 Y).
# Scalars are uint256.  Both are big-endian 32-byte words.

def _fq_to_int(x) -> int:
    if isinstance(x, _FQ):
        return int(x)
    return int(x)


def point_to_words(P) -> Tuple[int, int]:
    """G1 point -> (X, Y) as two uint256s.  Point at infinity -> (0, 0)."""
    if is_inf(P):
        return (0, 0)
    return (_fq_to_int(P[0]), _fq_to_int(P[1]))


def words_to_point(X: int, Y: int):
    """(X, Y) uint256 pair -> G1 point.  (0, 0) -> point at infinity."""
    if X == 0 and Y == 0:
        return Z1
    return (_FQ(X), _FQ(Y))


def scalar_to_word(s: int) -> int:
    return s % ORDER


def word_to_scalar(w: int) -> int:
    return w % ORDER


def point_to_hex(P) -> Tuple[str, str]:
    """G1 point -> ('0x...', '0x...') 32-byte hex pair, for JSON test vectors."""
    X, Y = point_to_words(P)
    return (f"0x{X:064x}", f"0x{Y:064x}")


def scalar_to_hex(s: int) -> str:
    return f"0x{s % ORDER:064x}"


def hex_to_int(h: str) -> int:
    return int(h, 16)


def hex_to_point(hx: str, hy: str):
    return words_to_point(hex_to_int(hx), hex_to_int(hy))
