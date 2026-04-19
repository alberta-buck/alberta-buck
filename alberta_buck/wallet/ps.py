"""Pointcheval-Sanders single-message signatures over BN254.

Issuer's secret key is a pair of scalars (x, y); public key is a pair of points
(X, Y) in G2.  A signature on m is sigma = (h, (x + m*y)*h) for random h in G1.

Verification: e(sigma_1, X + m*Y) == e(sigma_2, g_2)

Rerandomization: sigma' = (t*sigma_1, t*sigma_2) for random t -- same m, no
correlation with the original.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from alberta_buck.wallet.bn254 import (
    G1, G2, ORDER, add, mul, pairing, eq, is_inf, rand_scalar,
)


@dataclass(frozen=True)
class PSKeyPair:
    sk_x: int
    sk_y: int
    pk_X: Tuple   # G2 point
    pk_Y: Tuple   # G2 point


@dataclass(frozen=True)
class PSSignature:
    sigma_1: Tuple    # G1 point (h)
    sigma_2: Tuple    # G1 point ((x + m*y) * h)


def ps_keygen(rng=None) -> PSKeyPair:
    x = rand_scalar(rng)
    y = rand_scalar(rng)
    return PSKeyPair(sk_x=x, sk_y=y, pk_X=mul(G2, x), pk_Y=mul(G2, y))


def ps_sign(kp: PSKeyPair, m: int, rng=None) -> PSSignature:
    """sigma = (h, (x + m*y)*h) for fresh random h = t*G1."""
    t = rand_scalar(rng)
    h = mul(G1, t)
    coeff = (kp.sk_x + (m % ORDER) * kp.sk_y) % ORDER
    return PSSignature(sigma_1=h, sigma_2=mul(h, coeff))


def ps_verify(pk_X, pk_Y, sigma: PSSignature, m: int) -> bool:
    """Check e(sigma_1, X + m*Y) == e(sigma_2, g_2) and sigma_1 != O."""
    if is_inf(sigma.sigma_1):
        return False
    lhs = pairing(add(pk_X, mul(pk_Y, m % ORDER)), sigma.sigma_1)
    rhs = pairing(G2, sigma.sigma_2)
    return lhs == rhs


def ps_rerandomize(sigma: PSSignature, rng=None) -> Tuple[PSSignature, int]:
    """sigma' = (t*sigma_1, t*sigma_2).  Returns (sigma', t)."""
    t = rand_scalar(rng)
    return (
        PSSignature(sigma_1=mul(sigma.sigma_1, t), sigma_2=mul(sigma.sigma_2, t)),
        t,
    )
