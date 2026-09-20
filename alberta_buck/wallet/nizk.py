"""Registration NIZK (A'): bind a hiding PS presentation to an ElGamal ciphertext.

The holder publishes the presentation (A, B) = (a*sigma_1, a*sigma_2 + b*Y1)
(see ps.py) and proves -- without revealing m, b, r or sk -- that:

  (a') (A, B) presents a valid issuer credential on m:
          e(B, g_2) == e(A, X) * e(m*A + b*G, Y)
  (b)  E = (R, C) = (r*G, m*G + r*pk) encrypts the *same* m
  (k)  pk = sk*G  (the registrant holds the account key)

It is a Schnorr-family sigma protocol with four commitments:

  C1    = m_tilde * A + b_tilde * G     -- ONE commitment for both credential
                                            exponents (the pair (m, b) pairs
                                            with the single G2 base Y)
  T_C   = m_tilde * G + r_tilde * pk    -- ElGamal C commitment, same m_tilde
  T_R   = r_tilde * G                   -- ElGamal R commitment
  T_key = sk_tilde * G                  -- account-key commitment

Fiat-Shamir challenge e binds (A, B, E, pk, C1, T_C, T_R, T_key), the
registrant's Ethereum address, chainid, IdentityRegistry address, and domain
`AlbertaBuck/FiatShamir/IdentityRegistry/Register/v3`.

Responses (nonce draw order m_tilde, b_tilde, r_tilde, sk_tilde):
  s_m = m_tilde + e*m,  s_b = b_tilde + e*b,  s_r = r_tilde + e*r,
  s_sk = sk_tilde + e*sk   (mod ORDER).

Verifier checks:

  (fs)  e was honestly derived
  (b)   s_m*G + s_r*pk        == e*C  + T_C
  (c)   s_r*G                 == e*R  + T_R
  (k)   s_sk*G                == T_key + e*pk
  (a')  e(s_m*A + s_b*G - C1, Y) * e(e*A, X) * e(-e*B, g_2) == 1
  (g)   A != O, B != O, pk != O, R != O; all points on curve; scalars canonical

Why the old commitment leaked: the previous proof published A_ps = m_tilde*A
with response s_m, which reveals m*A = (s_m*A - A_ps)/e, a candidate test on
the identity (review finding R2).  Here m_tilde never stands alone on a public
base: the only derivable point is P = (s_m*A + s_b*G - C1)/e = m*A + b*G,
which is uniform and identical for every candidate m.  A = O is rejected as
security critical: with A = O the credential term vanishes and (a') holds for
every m.

This module contains both the prover and the verifier.  The Solidity verifier
mirrors the same checks; this Python verifier exists for unit testing the
wallet against itself and is the executable specification for the Rust and
JavaScript kernels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from py_ecc.bn128 import is_on_curve, b as curve_b

from alberta_buck.wallet.bn254 import (
    G1, G2, ORDER,
    add, mul, neg, eq, is_inf, pairing, FQ12_one,
    rand_scalar, point_to_words,
)
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.ps import PSPresentation
from alberta_buck.wallet.transcript import keccak_raw, keccak_scalar


# Domain separator, a full keccak word (not reduced mod ORDER), hashed into
# the Fiat-Shamir transcript so a registration proof cannot be replayed under
# a different protocol version.  Mirrors IdentityRegistry.REGISTER_DOMAIN.
REGISTER_DOMAIN = int.from_bytes(
    keccak_raw(b"AlbertaBuck/FiatShamir/IdentityRegistry/Register/v3"), "big"
)


@dataclass(frozen=True)
class RegistrationProof:
    e:     int
    s_m:   int
    s_b:   int
    s_r:   int
    s_sk:  int
    C1:    Tuple   # G1 point: m_tilde * A + b_tilde * G
    T_C:   Tuple   # G1 point: m_tilde * G + r_tilde * pk
    T_R:   Tuple   # G1 point: r_tilde * G
    T_key: Tuple   # G1 point: sk_tilde * G


def _canonical(s: int) -> bool:
    return isinstance(s, int) and 0 <= s < ORDER


def _on_curve(P) -> bool:
    if P is None or is_inf(P):
        return False
    return is_on_curve(P, curve_b)


def _on_curve_or_inf(P) -> bool:
    if P is None or is_inf(P):
        return True
    return is_on_curve(P, curve_b)


def _registration_transcript(
    pres: PSPresentation,
    E: ElGamalCiphertext,
    pk,
    C1,
    T_C,
    T_R,
    T_key,
    registrant: int,
    chainid: int,
    registry: int,
) -> int:
    Ax, Ay = point_to_words(pres.A)
    Bx, By = point_to_words(pres.B)
    Rx, Ry = point_to_words(E.R)
    Cx, Cy = point_to_words(E.C)
    pkx, pky = point_to_words(pk)
    C1x, C1y = point_to_words(C1)
    Tcx, Tcy = point_to_words(T_C)
    Trx, Try_ = point_to_words(T_R)
    Tkx, Tky = point_to_words(T_key)
    return keccak_scalar(
        Ax, Ay, Bx, By,
        Rx, Ry, Cx, Cy,
        pkx, pky,
        C1x, C1y, Tcx, Tcy, Trx, Try_,
        Tkx, Tky,
        registrant, chainid, registry, REGISTER_DOMAIN,
    )


def registration_prove(
    pres: PSPresentation,
    b: int,
    m: int,
    r: int,
    pk,
    E: ElGamalCiphertext,
    registrant: int,
    sk: int,
    chainid: int = 1,
    rng=None,
    *,
    registry: int = 0,
) -> RegistrationProof:
    """Build the registration NIZK proof for the presentation `pres`.

    `b` is the presentation blinding returned by ps_present.  `registrant` is
    the Ethereum address the proof is bound to, encoded as a uint256
    (left-padded uint160).  For register() this is msg.sender; for
    bindContract() it is the target contract (see bind_contract_prove).
    Binding it, `chainid`, `registry`, and REGISTER_DOMAIN into the
    Fiat-Shamir transcript prevents proof replay across addresses, chains,
    registry deployments, and protocol versions.  `sk` is the account secret
    with pk = sk*G.  Nonce draw order: m_tilde, b_tilde, r_tilde, sk_tilde.
    """
    m_tilde = rand_scalar(rng)
    b_tilde = rand_scalar(rng)
    r_tilde = rand_scalar(rng)
    sk_tilde = rand_scalar(rng)
    C1 = add(mul(pres.A, m_tilde), mul(G1, b_tilde))
    T_C = add(mul(G1, m_tilde), mul(pk, r_tilde))
    T_R = mul(G1, r_tilde)
    T_key = mul(G1, sk_tilde)

    e = _registration_transcript(
        pres, E, pk, C1, T_C, T_R, T_key, registrant, chainid, registry,
    )
    s_m = (m_tilde + e * (m % ORDER)) % ORDER
    s_b = (b_tilde + e * (b % ORDER)) % ORDER
    s_r = (r_tilde + e * (r % ORDER)) % ORDER
    s_sk = (sk_tilde + e * (sk % ORDER)) % ORDER

    return RegistrationProof(
        e=e, s_m=s_m, s_b=s_b, s_r=s_r, s_sk=s_sk,
        C1=C1, T_C=T_C, T_R=T_R, T_key=T_key,
    )


def bind_contract_prove(
    pres: PSPresentation,
    b: int,
    m: int,
    r: int,
    pk,
    E: ElGamalCiphertext,
    target: int,
    sk: int,
    chainid: int = 1,
    rng=None,
    *,
    registry: int = 0,
) -> RegistrationProof:
    """Registration NIZK for IdentityRegistry.bindContract.

    Fiat-Shamir registrant is uint160(target), not the binder.  A proof
    valid for an EOA cannot be replayed onto a contract, and vice versa.
    """
    return registration_prove(
        pres, b, m, r, pk, E, target, sk, chainid, rng, registry=registry,
    )


def registration_verify(
    pres: PSPresentation,
    E: ElGamalCiphertext,
    pk,
    issuer_X,
    issuer_Y,
    proof: RegistrationProof,
    registrant: int,
    chainid: int = 1,
    registry: int = 0,
) -> bool:
    """Mirror of the Solidity verifier; returns True iff all checks pass.

    Dispatches wholesale to the compiled kernel once it implements the A'
    relation (it exports `registration_verify_v3`; the pre-A' kernel does
    not, and the pure-Python path below is used instead).  The py_ecc
    computation remains the executable spec.
    """
    from alberta_buck.wallet._kernel import kernel as _kernel
    k = _kernel()
    if k is not None and hasattr(k, "registration_verify_v3"):
        from alberta_buck.wallet.bn254 import g2_to_words
        return k.registration_verify_v3(
            point_to_words(pres.A), point_to_words(pres.B),
            (point_to_words(E.R), point_to_words(E.C)),
            point_to_words(pk),
            g2_to_words(issuer_X), g2_to_words(issuer_Y),
            (proof.e, proof.s_m, proof.s_b, proof.s_r, proof.s_sk,
             point_to_words(proof.C1), point_to_words(proof.T_C),
             point_to_words(proof.T_R), point_to_words(proof.T_key)),
            registrant, chainid, registry,
        )
    # (g) guards
    if not all(_canonical(s) for s in (proof.e, proof.s_m, proof.s_b, proof.s_r, proof.s_sk)):
        return False
    if not (_on_curve(pres.A) and _on_curve(pres.B) and _on_curve(pk) and _on_curve(E.R)):
        return False
    if not all(_on_curve_or_inf(p) for p in (E.C, proof.C1, proof.T_C, proof.T_R, proof.T_key)):
        return False

    # (fs) Fiat-Shamir
    e_check = _registration_transcript(
        pres, E, pk, proof.C1, proof.T_C, proof.T_R, proof.T_key,
        registrant, chainid, registry,
    )
    if e_check != proof.e:
        return False

    e, s_m, s_b, s_r, s_sk = proof.e, proof.s_m, proof.s_b, proof.s_r, proof.s_sk

    # (b) ElGamal C consistency: s_m*G + s_r*pk == e*C + T_C
    if not eq(add(mul(G1, s_m), mul(pk, s_r)), add(mul(E.C, e), proof.T_C)):
        return False

    # (c) ElGamal R consistency: s_r*G == e*R + T_R
    if not eq(mul(G1, s_r), add(mul(E.R, e), proof.T_R)):
        return False

    # (k) Account-key ownership: s_sk*G == T_key + e*pk
    if not eq(mul(G1, s_sk), add(proof.T_key, mul(pk, e))):
        return False

    # (a') Presentation pairing product (three pairs):
    #   e(s_m*A + s_b*G - C1, Y) * e(e*A, X) * e(-e*B, g_2) == 1
    lhs = add(add(mul(pres.A, s_m), mul(G1, s_b)), neg(proof.C1))
    check = (
          pairing(issuer_Y, lhs)
        * pairing(issuer_X, mul(pres.A, e))
        * pairing(G2, neg(mul(pres.B, e)))
    )
    return check == FQ12_one()


def presentation_point(pres: PSPresentation, proof: RegistrationProof):
    """P = (s_m*A + s_b*G - C1) / e = m*A + b*G, the only point a verifier or
    observer can derive from the proof.  Review helper: it is uniform and the
    same for every candidate m, which is why no candidate test exists."""
    inv_e = pow(proof.e, -1, ORDER)
    return mul(add(add(mul(pres.A, proof.s_m), mul(G1, proof.s_b)), neg(proof.C1)), inv_e)


__all__ = [
    "REGISTER_DOMAIN", "RegistrationProof",
    "registration_prove", "bind_contract_prove", "registration_verify",
    "presentation_point",
]
