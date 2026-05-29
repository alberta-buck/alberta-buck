"""Schnorr signature over a note-batch commitment by an issuer's registered
identity key -- the public-issuer half of the BUCK Notes deferred-approve
handshake (mutual-decryptability, Phase 1).

Reference: alberta-buck-notes-decryptability.org.

A public issuer (A1 / B1) signs the batch commitment ``hBatch = keccak256(cms)``
so a depositor can later produce a cryptographically sound receipt naming the
issuer's decrypted Identity.

The statement is plain Schnorr knowledge-of-discrete-log over BN254 G1:

    Prove knowledge of sk_iss such that pk_iss === sk_iss * G,
    signing the message hBatch.

Sigma protocol:
    Prover picks k in Fr, computes R = k * G.
    Challenge:  e = H(pk_iss.x, pk_iss.y, R.x, R.y, hBatch, issuer, chainid)  mod ORDER
    Response:   s = k + e * sk_iss                                            mod ORDER

Proof: ``(e, s, R)``.  Verified on-chain by IdentityRegistry.verifyIssuerSchnorr:
    s*G == R + e*pk_iss   and   e == H(...)
(2 ecMul + 1 ecAdd + Fiat-Shamir keccak), gated on isPublicIdentity[issuer].

The transcript order MUST match IdentityRegistry._fsIssuerSchnorr byte-for-byte:
points (pk_iss, R) contribute (X, Y) word pairs, then scalars (hBatch, issuer,
chainid).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from alberta_buck.wallet.bn254 import G1, ORDER, add, mul, eq, rand_scalar, point_to_words
from alberta_buck.wallet.transcript import keccak_scalar


@dataclass(frozen=True)
class SchnorrProof:
    """3-element Schnorr proof for the public-issuer note binding."""
    e: int
    s: int
    R: Tuple    # G1: k*G


def batch_commitment(cms) -> int:
    """hBatch = keccak256(abi.encodePacked(cms)) as a uint256.

    ``cms`` is the list of per-leaf Poseidon commitments; each is packed as a
    32-byte big-endian word and concatenated, matching Notes.mint's
    ``keccak256(abi.encodePacked(cms))``.
    """
    from eth_utils import keccak
    packed = b"".join(int(c).to_bytes(32, "big") for c in cms)
    return int.from_bytes(keccak(packed), "big")


def _issuer_schnorr_transcript(pk_iss, R, h_batch: int, issuer: int, chainid: int) -> int:
    """Fiat-Shamir challenge; order matches IdentityRegistry._fsIssuerSchnorr."""
    pkx, pky = point_to_words(pk_iss)
    Rx, Ry = point_to_words(R)
    return keccak_scalar(pkx, pky, Rx, Ry, h_batch, issuer, chainid)


def issuer_schnorr_sign(
    sk_iss:  int,
    h_batch: int,
    issuer:  int,
    chainid: int,
    rng=None,
) -> SchnorrProof:
    """Sign a note-batch commitment ``h_batch`` under the issuer's key.

    ``issuer`` is the issuer's address as an int (uint160); ``chainid`` binds
    the transcript so a signature cannot be replayed against another issuer or
    chain.
    """
    pk_iss = mul(G1, sk_iss % ORDER)
    k  = rand_scalar(rng)
    R  = mul(G1, k)
    e  = _issuer_schnorr_transcript(pk_iss, R, h_batch, issuer, chainid)
    s  = (k + e * (sk_iss % ORDER)) % ORDER
    return SchnorrProof(e=e, s=s, R=R)


def issuer_schnorr_verify(
    pk_iss,
    proof:   SchnorrProof,
    h_batch: int,
    issuer:  int,
    chainid: int,
) -> bool:
    """Verify a public-issuer Schnorr proof.  Mirrors verifyIssuerSchnorr's
    algebraic + Fiat-Shamir checks (the on-chain isPublicIdentity gate is not
    modelled here)."""
    # Check 1: s*G == R + e*pk_iss
    if not eq(mul(G1, proof.s), add(proof.R, mul(pk_iss, proof.e))):
        return False
    # Check 2: Fiat-Shamir
    return proof.e == _issuer_schnorr_transcript(pk_iss, proof.R, h_batch, issuer, chainid)
