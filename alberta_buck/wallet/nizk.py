"""Registration NIZK: bind a rerandomized PS signature to an ElGamal ciphertext.

The proof shows -- without revealing m, r or sk -- that:

  (a) sigma' is a valid PS signature on m
  (b) E = (R, C) = (r*G, m*G + r*pk) encrypts the *same* m
  (k) pk = sk*G  (the registrant holds the account key)

It is a Schnorr-family sigma protocol with four commitments:

  A_ps  = m_tilde * sigma'_1             -- PS-side commitment
  T_C   = m_tilde * G + r_tilde * pk     -- ElGamal C commitment
  T_R   = r_tilde * G                    -- ElGamal R commitment
  T_key = sk_tilde * G                   -- account-key commitment

Fiat-Shamir challenge e binds (sigma', E, pk, A_ps, T_C, T_R, T_key), the
registrant's Ethereum address, chainid, IdentityRegistry address, and domain
`AlbertaBuck/FiatShamir/IdentityRegistry/Register/v2`.

Responses: s_m = m_tilde + e*m,  s_r = r_tilde + e*r,  s_sk = sk_tilde + e*sk
(mod ORDER).

Verifier checks:

  (b) s_m*G + s_r*pk        == e*C  + T_C
  (c) s_r*G                 == e*R  + T_R
  (k) s_sk*G                == T_key + e*pk
  (a) e(s_m*sigma'_1, Y) * e(-A_ps, Y) * e(e*sigma'_1, X) * e(-e*sigma'_2, g_2) == 1
  (d) Fiat-Shamir e was honestly derived
  (e) sigma'_1 != O, pk != O, R != O; scalars canonical (0 <= s < ORDER)

This module contains both the prover and the verifier.  The Solidity verifier
mirrors the same checks; this Python verifier exists for unit testing the
wallet against itself.
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
from alberta_buck.wallet.ps import PSSignature
from alberta_buck.wallet.transcript import keccak_raw, keccak_scalar


# Domain separator, a full keccak word (not reduced mod ORDER), hashed into
# the Fiat-Shamir transcript so a registration proof cannot be replayed under
# a different protocol version.  Mirrors IdentityRegistry.REGISTER_DOMAIN.
REGISTER_DOMAIN = int.from_bytes(
    keccak_raw(b"AlbertaBuck/FiatShamir/IdentityRegistry/Register/v2"), "big"
)


@dataclass(frozen=True)
class RegistrationProof:
    e:     int
    s_m:   int
    s_r:   int
    s_sk:  int
    A_ps:  Tuple   # G1 point: PS-side commitment m_tilde * sigma'_1
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
    sigma_p: PSSignature,
    E: ElGamalCiphertext,
    pk,
    A_ps,
    T_C,
    T_R,
    T_key,
    registrant: int,
    chainid: int,
    registry: int,
) -> int:
    s1x, s1y = point_to_words(sigma_p.sigma_1)
    s2x, s2y = point_to_words(sigma_p.sigma_2)
    Rx,  Ry  = point_to_words(E.R)
    Cx,  Cy  = point_to_words(E.C)
    pkx, pky = point_to_words(pk)
    Apx, Apy = point_to_words(A_ps)
    Tcx, Tcy = point_to_words(T_C)
    Trx, Try_ = point_to_words(T_R)
    Tkx, Tky = point_to_words(T_key)
    return keccak_scalar(
        s1x, s1y, s2x, s2y,
        Rx, Ry, Cx, Cy,
        pkx, pky,
        Apx, Apy, Tcx, Tcy, Trx, Try_,
        Tkx, Tky,
        registrant, chainid, registry, REGISTER_DOMAIN,
    )


def registration_prove(
    sigma_p: PSSignature,
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
    """Build the registration NIZK proof.

    `registrant` is the Ethereum address the proof is bound to, encoded as a
    uint256 (left-padded uint160).  For register() this is msg.sender; for
    bindContract() it is the target contract (see bind_contract_prove).
    Binding it, `chainid`, `registry`, and REGISTER_DOMAIN into the
    Fiat-Shamir transcript prevents proof replay across addresses, chains,
    registry deployments, and protocol versions.  `sk` is the account secret
    with pk = sk*G.
    """
    m_tilde = rand_scalar(rng)
    r_tilde = rand_scalar(rng)
    sk_tilde = rand_scalar(rng)
    A_ps = mul(sigma_p.sigma_1, m_tilde)
    T_C  = add(mul(G1, m_tilde), mul(pk, r_tilde))
    T_R  = mul(G1, r_tilde)
    T_key = mul(G1, sk_tilde)

    e = _registration_transcript(
        sigma_p, E, pk, A_ps, T_C, T_R, T_key, registrant, chainid, registry,
    )
    s_m = (m_tilde + e * (m % ORDER)) % ORDER
    s_r = (r_tilde + e * (r % ORDER)) % ORDER
    s_sk = (sk_tilde + e * (sk % ORDER)) % ORDER

    return RegistrationProof(
        e=e, s_m=s_m, s_r=s_r, s_sk=s_sk,
        A_ps=A_ps, T_C=T_C, T_R=T_R, T_key=T_key,
    )


def bind_contract_prove(
    sigma_p: PSSignature,
    m: int,
    r: int,
    pk,
    E: ElGamalCiphertext,
    target: int,
    sk: int,
    chainid: int = 1,
    rng=None,
) -> RegistrationProof:
    """Registration NIZK for IdentityRegistry.bindContract.

    Fiat-Shamir registrant is uint160(target), not the binder.  A proof
    valid for an EOA cannot be replayed onto a contract, and vice versa.
    """
    return registration_prove(sigma_p, m, r, pk, E, target, sk, chainid, rng)


def registration_verify(
    sigma_p: PSSignature,
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

    Dispatches wholesale to the compiled kernel when built (the pairing
    product costs seconds under py_ecc); the py_ecc computation below
    remains the executable spec.
    """
    from alberta_buck.wallet._kernel import kernel as _kernel
    k = _kernel()
    if k is not None:
        from alberta_buck.wallet.bn254 import g2_to_words
        return k.registration_verify(
            point_to_words(sigma_p.sigma_1), point_to_words(sigma_p.sigma_2),
            (point_to_words(E.R), point_to_words(E.C)),
            point_to_words(pk),
            g2_to_words(issuer_X), g2_to_words(issuer_Y),
            (proof.e, proof.s_m, proof.s_r, proof.s_sk,
             point_to_words(proof.A_ps), point_to_words(proof.T_C),
             point_to_words(proof.T_R), point_to_words(proof.T_key)),
            registrant, chainid, registry,
        )
    if not all(_canonical(s) for s in (proof.e, proof.s_m, proof.s_r, proof.s_sk)):
        return False
    if is_inf(sigma_p.sigma_1) or not _on_curve(pk) or not _on_curve(E.R):
        return False
    if not all(_on_curve_or_inf(p) for p in (E.C, proof.A_ps, proof.T_C, proof.T_R, proof.T_key)):
        return False

    e_check = _registration_transcript(
        sigma_p, E, pk, proof.A_ps, proof.T_C, proof.T_R, proof.T_key,
        registrant, chainid, registry,
    )
    if e_check != proof.e:
        return False

    e = proof.e
    s_m = proof.s_m
    s_r = proof.s_r
    s_sk = proof.s_sk

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

    # (k) Account-key ownership: s_sk*G == T_key + e*pk
    if not eq(mul(G1, s_sk), add(proof.T_key, mul(pk, e))):
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
