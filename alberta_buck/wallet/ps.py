"""Pointcheval-Sanders single-message signatures over BN254, plus the A'
hiding presentation used for account registration.

Issuer's secret key is a pair of scalars (x, y); public key is (X, Y) in G2
plus Y1 = y*G in G1.  A signature on m is sigma = (h, (x + m*y)*h) for random
h in G1.

Verification (raw credential, off chain): e(sigma_1, X + m*Y) == e(sigma_2, g_2)

Rerandomization: sigma' = (t*sigma_1, t*sigma_2) for random t -- same m.  It is a
wallet-internal step only: a rerandomized pair is still a verifiable
signature on m, so anyone holding a candidate m can test it (review finding
R1).  The wallet never publishes it.

Presentation (A'): for fresh nonzero a, b the wallet publishes

    A = a*sigma_1
    B = a*sigma_2 + b*Y1 = (x + m*y)*A + b*Y1

together with the registration NIZK (nizk.py).  For uniform a, b the pair
(A, B) is a uniform G1 pair independent of m: B = x*A + y*P with
P = m*A + b*G uniform.  No public object is a signature anyone can verify or
re-present without b, and stripping b*Y1 from B needs y*(b*G), a CDH instance.

Publishing Y1 puts the issuer key in the committed-message form of
Pointcheval-Sanders (Section 6 of their paper); the issuer keeps the signature
base h with a discrete log unknown to holders, which ps_sign does by sampling
it.  a and b MUST be fresh per presentation (reusing b across two accounts
gives P_1 - P_2 = m*(A_1 - A_2), a candidate test).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from alberta_buck.wallet.bn254 import (
    G1, G2, ORDER, add, mul, pairing, eq, is_inf, rand_scalar,
)


@dataclass(frozen=True)
class PSKeyPair:
    sk_x: int
    sk_y: int
    pk_X: Tuple   # G2 point: x*G2
    pk_Y: Tuple   # G2 point: y*G2
    pk_Y1: Optional[Tuple] = None   # G1 point: y*G (presentation base)

    def __post_init__(self):
        if self.pk_Y1 is None:
            object.__setattr__(self, "pk_Y1", mul(G1, self.sk_y % ORDER))


@dataclass(frozen=True)
class PSSignature:
    sigma_1: Tuple    # G1 point (h)
    sigma_2: Tuple    # G1 point ((x + m*y) * h)


@dataclass(frozen=True)
class PSPresentation:
    """The published, hiding form of a credential: (A, B) = (a*sigma_1, a*sigma_2 + b*Y1)."""
    A: Tuple          # G1 point
    B: Tuple          # G1 point


def ps_keygen(rng=None) -> PSKeyPair:
    x = rand_scalar(rng)
    y = rand_scalar(rng)
    Y1 = mul(G1, y)
    from alberta_buck.wallet._kernel import kernel as _kernel
    k = _kernel()
    if k is not None:
        from alberta_buck.wallet.bn254 import g2_to_words, words_to_g2
        g2w = g2_to_words(G2)
        return PSKeyPair(
            sk_x=x, sk_y=y,
            pk_X=words_to_g2(*k.g2_mul(g2w, x)),
            pk_Y=words_to_g2(*k.g2_mul(g2w, y)),
            pk_Y1=Y1,
        )
    return PSKeyPair(sk_x=x, sk_y=y, pk_X=mul(G2, x), pk_Y=mul(G2, y), pk_Y1=Y1)


def ps_key_consistent(pk_X, pk_Y, pk_Y1) -> bool:
    """e(Y1, g_2) == e(G, Y): the G1 key component matches the G2 one.

    The registry checks this once when it trusts an issuer key.  A wrong Y1
    cannot help a forger (the verifier never uses it); it would only make
    every honest presentation fail to verify.
    """
    if pk_Y1 is None or is_inf(pk_Y1):
        return False
    return pairing(G2, pk_Y1) == pairing(pk_Y, G1)


def ps_sign(kp: PSKeyPair, m: int, rng=None) -> PSSignature:
    """sigma = (h, (x + m*y)*h) for fresh random h = t*G1."""
    t = rand_scalar(rng)
    h = mul(G1, t)
    coeff = (kp.sk_x + (m % ORDER) * kp.sk_y) % ORDER
    return PSSignature(sigma_1=h, sigma_2=mul(h, coeff))


def ps_verify(pk_X, pk_Y, sigma: PSSignature, m: int) -> bool:
    """Check e(sigma_1, X + m*Y) == e(sigma_2, g_2) and sigma_1 != O.

    Verifies a RAW credential (holder and issuer side).  Applied to a
    published presentation (A, B) it returns False for every m; that is the
    point of the presentation.

    Dispatches the whole pairing check to the compiled kernel when built
    (py_ecc pairings cost seconds); the py_ecc computation below remains
    the executable spec.
    """
    from alberta_buck.wallet._kernel import kernel as _kernel
    k = _kernel()
    if k is not None:
        from alberta_buck.wallet.bn254 import g2_to_words, point_to_words
        return k.ps_verify(
            g2_to_words(pk_X), g2_to_words(pk_Y),
            point_to_words(sigma.sigma_1), point_to_words(sigma.sigma_2),
            m % ORDER,
        )
    if is_inf(sigma.sigma_1):
        return False
    lhs = pairing(add(pk_X, mul(pk_Y, m % ORDER)), sigma.sigma_1)
    rhs = pairing(G2, sigma.sigma_2)
    return lhs == rhs


def ps_rerandomize(sigma: PSSignature, rng=None) -> Tuple[PSSignature, int]:
    """sigma' = (t*sigma_1, t*sigma_2).  Returns (sigma', t).

    Wallet-internal only; the result is still a signature on m and must not
    be published.  Use ps_present for anything that leaves the wallet.
    """
    t = rand_scalar(rng)
    return (
        PSSignature(sigma_1=mul(sigma.sigma_1, t), sigma_2=mul(sigma.sigma_2, t)),
        t,
    )


def ps_present(sigma: PSSignature, pk_Y1, rng=None, *, a: Optional[int] = None,
               b: Optional[int] = None) -> Tuple[PSPresentation, int, int]:
    """(A, B) = (a*sigma_1, a*sigma_2 + b*Y1).  Returns (presentation, a, b).

    Draw order when rng supplies the scalars: a, then b.  Both must be fresh
    and nonzero for every presentation; the caller keeps b as a witness for
    the registration NIZK and then discards it.
    """
    a = rand_scalar(rng) if a is None else a % ORDER
    b = rand_scalar(rng) if b is None else b % ORDER
    if a == 0 or b == 0:
        raise ValueError("presentation scalars a, b must be nonzero")
    A = mul(sigma.sigma_1, a)
    B = add(mul(sigma.sigma_2, a), mul(pk_Y1, b))
    return PSPresentation(A=A, B=B), a, b


__all__ = [
    "PSKeyPair", "PSSignature", "PSPresentation",
    "ps_keygen", "ps_key_consistent", "ps_sign", "ps_verify",
    "ps_rerandomize", "ps_present",
]
