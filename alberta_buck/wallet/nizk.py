"""Registration NIZK: bind a rerandomized PS signature to an ElGamal ciphertext.

The proof shows -- without revealing m or r -- that:

  (a) sigma' is a valid PS signature on m
  (b) E = (R, C) = (r*G, m*G + r*pk) encrypts the *same* m

It is a Schnorr-family sigma protocol with three commitments:

  A_ps = m_tilde * sigma'_1             -- PS-side commitment
  T_C  = m_tilde * G + r_tilde * pk     -- ElGamal C commitment
  T_R  = r_tilde * G                    -- ElGamal R commitment

Fiat-Shamir challenge e binds (sigma', E, pk, A_ps, T_C, T_R) and -- in the
Solidity verifier -- the registrant's Ethereum address for domain separation.

Responses: s_m = m_tilde + e*m,  s_r = r_tilde + e*r  (mod ORDER).

Verifier checks five things:

  (b) s_m*G + s_r*pk        == e*C  + T_C
  (c) s_r*G                 == e*R  + T_R
  (a) e(s_m*sigma'_1, Y) * e(-A_ps, Y) * e(e*sigma'_1, X) * e(-e*sigma'_2, g_2) == 1
  (d) Fiat-Shamir e was honestly derived
  (e) sigma'_1 != O

This module contains both the prover and the verifier.  The Solidity verifier
mirrors checks (b)/(c)/(a)/(d)/(e); this Python verifier exists for unit
testing the wallet against itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from alberta_buck.wallet.bn254 import (
    G1, G2, ORDER,
    add, mul, neg, eq, is_inf, pairing, FQ12_one,
    rand_scalar, point_to_words,
)
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.ps import PSSignature
from alberta_buck.wallet.transcript import keccak_scalar


@dataclass(frozen=True)
class RegistrationProof:
    e:    int
    s_m:  int
    s_r:  int
    A_ps: Tuple   # G1 point: PS-side commitment m_tilde * sigma'_1
    T_C:  Tuple   # G1 point: m_tilde * G + r_tilde * pk
    T_R:  Tuple   # G1 point: r_tilde * G


def _registration_transcript(
    sigma_p: PSSignature,
    E: ElGamalCiphertext,
    pk,
    A_ps,
    T_C,
    T_R,
    registrant: int,
) -> int:
    s1x, s1y = point_to_words(sigma_p.sigma_1)
    s2x, s2y = point_to_words(sigma_p.sigma_2)
    Rx,  Ry  = point_to_words(E.R)
    Cx,  Cy  = point_to_words(E.C)
    pkx, pky = point_to_words(pk)
    Apx, Apy = point_to_words(A_ps)
    Tcx, Tcy = point_to_words(T_C)
    Trx, Try_ = point_to_words(T_R)
    return keccak_scalar(
        s1x, s1y, s2x, s2y,
        Rx, Ry, Cx, Cy,
        pkx, pky,
        Apx, Apy, Tcx, Tcy, Trx, Try_,
        registrant,
    )


def registration_prove(
    sigma_p: PSSignature,
    m: int,
    r: int,
    pk,
    E: ElGamalCiphertext,
    registrant: int,
    rng=None,
) -> RegistrationProof:
    """Build the registration NIZK proof.

    `registrant` is the Ethereum address of the address that will submit the
    proof, encoded as a uint256 (left-padded uint160).  Binding it into the
    Fiat-Shamir transcript prevents proof replay across addresses.
    """
    m_tilde = rand_scalar(rng)
    r_tilde = rand_scalar(rng)
    A_ps = mul(sigma_p.sigma_1, m_tilde)
    T_C  = add(mul(G1, m_tilde), mul(pk, r_tilde))
    T_R  = mul(G1, r_tilde)

    e = _registration_transcript(sigma_p, E, pk, A_ps, T_C, T_R, registrant)
    s_m = (m_tilde + e * (m % ORDER)) % ORDER
    s_r = (r_tilde + e * (r % ORDER)) % ORDER

    return RegistrationProof(e=e, s_m=s_m, s_r=s_r, A_ps=A_ps, T_C=T_C, T_R=T_R)


def registration_verify(
    sigma_p: PSSignature,
    E: ElGamalCiphertext,
    pk,
    issuer_X,
    issuer_Y,
    proof: RegistrationProof,
    registrant: int,
) -> bool:
    """Mirror of the Solidity verifier; returns True iff all five checks pass."""
    # (e) Non-triviality
    if is_inf(sigma_p.sigma_1):
        return False

    # (d) Fiat-Shamir
    e_check = _registration_transcript(
        sigma_p, E, pk, proof.A_ps, proof.T_C, proof.T_R, registrant
    )
    if e_check != proof.e:
        return False

    e = proof.e
    s_m = proof.s_m
    s_r = proof.s_r

    # (b) ElGamal C consistency: s_m*G + s_r*pk == e*C + T_C
    lhs_C = add(mul(G1, s_m), mul(pk, s_r))
    rhs_C = add(mul(E.C, e), proof.T_C)
    if not eq(lhs_C, rhs_C):
        return False

    # (c) ElGamal R consistency: s_r*G == e*R + T_R
    lhs_R = mul(G1, s_r)
    rhs_R = add(mul(E.R, e), proof.T_R)
    if not eq(lhs_R, rhs_R):
        return False

    # (a) PS pairing product:
    #   e(s_m*sigma'_1, Y) * e(-A_ps, Y) * e(e*sigma'_1, X) * e(-e*sigma'_2, g_2) == 1
    check = (
          pairing(issuer_Y, mul(sigma_p.sigma_1, s_m))
        * pairing(issuer_Y, neg(proof.A_ps))
        * pairing(issuer_X, mul(sigma_p.sigma_1, e))
        * pairing(G2,       neg(mul(sigma_p.sigma_2, e)))
    )
    return check == FQ12_one()
