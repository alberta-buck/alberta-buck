# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded mitigation prototypes. No production protocol is changed here.

The compact approval uses the three-relation statement in the findings.
Credential and note helpers define the native-field relation for the separate
Circom prototype; they do NOT implement blind issuance, revocation or receipt
delivery. That circuit's execution and integration tests remain pending.
"""

from dataclasses import dataclass
from eth_hash.auto import keccak
from py_ecc.bn128 import is_on_curve, b as curve_b, field_modulus

from alberta_buck.wallet.bn254 import G1, ORDER, add, mul, neg, eq, point_to_words, rand_scalar
from alberta_buck.wallet.transcript import keccak_scalar
from alberta_buck.wallet.poseidon import poseidon

APPROVE_DOMAIN = int.from_bytes(keccak(b"AlbertaBuck:Review:Approve:v2"), "big")
CREDENTIAL_TAG, NOTE_TAG, CONTEXT_TAG = 90201, 90202, 90203


@dataclass(frozen=True)
class ApprovalContext:
    sender: int
    spender: int
    chainid: int
    registry: int
    nonce: int = 0


@dataclass(frozen=True)
class CompactApproval:
    e: int
    u: int
    v: int


def _challenge(Ea, Eb, pka, pkb, points, context):
    return keccak_scalar(APPROVE_DOMAIN, context.sender, context.spender,
                         context.chainid, context.registry, context.nonce,
                         *(w for p in (Ea.R, Ea.C, Eb.R, Eb.C, pka, pkb, *points)
                           for w in point_to_words(p)))


def prove_approval(Ea, Eb, pka, pkb, sk, r_prime, context, rng=None):
    """Deliberately accepts arbitrary witnesses so tests can make malicious proofs."""
    a, b = rand_scalar(rng), rand_scalar(rng)
    points = (mul(G1, a), mul(G1, b), add(mul(Ea.R, a), neg(mul(pkb, b))))
    e = _challenge(Ea, Eb, pka, pkb, points, context)
    return CompactApproval(e, (a+e*sk) % ORDER, (b+e*r_prime) % ORDER)


def verify_approval(Ea, Eb, pka, pkb, proof, context):
    # All serialized scalars must be canonical; account keys and R must be
    # nonzero. C may legitimately be infinity. Inputs here are py_ecc points;
    # raw-coordinate decoding needs its own canonical check at a wire boundary.
    if not all(0 <= s < ORDER for s in (proof.e, proof.u, proof.v)):
        return False
    for p in (Ea.R, Eb.R, pka, pkb):
        if p is None:
            return False
    if not all(is_on_curve(p, curve_b) for p in (Ea.R, Ea.C, Eb.R, Eb.C, pka, pkb)):
        return False
    e, u, v = proof.e, proof.u, proof.v
    points = (add(mul(G1, u), neg(mul(pka, e))),
              add(mul(G1, v), neg(mul(Eb.R, e))),
              add(add(mul(Ea.R, u), neg(mul(pkb, v))),
                  neg(mul(add(Ea.C, neg(Eb.C)), e))))
    return e == _challenge(Ea, Eb, pka, pkb, points, context)


def independent_generator():
    """Review-only hash-to-point example, NOT an RFC 9380 suite or PQ primitive.

    Hash to x, try successive counters, take the smaller square root on BN254.
    There is no exported scalar h with H=hG. Timing is on public data only.
    A production suite still needs specification and cryptographic review.
    """
    from alberta_buck.wallet.bn254 import words_to_point
    domain = b"AlbertaBuck:Review:IndependentGenerator:v1"
    for counter in range(1000):
        x = int.from_bytes(keccak(domain + counter.to_bytes(4, "big")), "big") % field_modulus
        y2 = (x*x*x + 3) % field_modulus
        y = pow(y2, (field_modulus+1)//4, field_modulus)
        if y*y % field_modulus == y2:
            return words_to_point(x, min(y, field_modulus-y))
    raise RuntimeError("hash-to-point search exhausted")


def credential_leaf(m, holder_secret, salt):
    return poseidon([CREDENTIAL_TAG, m, poseidon([holder_secret]), salt])


def payment_context(account, chainid, registry, nonce):
    return poseidon([CONTEXT_TAG, account, chainid, registry, nonce])


def payment_commitment(flavor, value, rho, issuer, recipient, predicate=0):
    return poseidon([NOTE_TAG, flavor, value, rho, issuer, recipient, predicate])


def prove_key_ownership(pk, sk, domain, rng=None):
    """One-relation Schnorr that registration and approve both need: pk = sk*G.

    Review prototype only.  Production registration currently omits this
    (finding 9); production approve omits it (finding 3).
    """
    a = rand_scalar(rng)
    T = mul(G1, a)
    e = keccak_scalar(domain, *point_to_words(pk), *point_to_words(T))
    return (e, (a + e * (sk % ORDER)) % ORDER, T)


def verify_key_ownership(pk, proof, domain):
    e, u, T = proof
    if not (0 <= e < ORDER and 0 <= u < ORDER):
        return False
    if pk is None or T is None:
        return False
    if not all(is_on_curve(p, curve_b) for p in (pk, T)):
        return False
    return e == keccak_scalar(domain, *point_to_words(pk), *point_to_words(T)) \
        and eq(mul(G1, u), add(T, mul(pk, e)))


def membership_proof_required(proof: bytes, verifier_set: bool) -> bool:
    """Intended fail-closed gate for Notes._verifyIdentityMembership.

    Production currently returns early on empty proof or unset verifier
    (finding 8).  A repair accepts the spend only when this is True.
    """
    return verifier_set and len(proof) > 0
