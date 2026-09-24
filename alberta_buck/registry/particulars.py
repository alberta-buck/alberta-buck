"""Particulars certificates: an identity's CURRENT details, signed over its fixed M.

M is the hash of the core record, fixed at first certification (accumulator
specification, section 8; doc/review/privacy-paper-plan.org, decision 7).  It
must outlive every detail that changes: a legal name, a street address, a phone
number, a photo, the number on this year's licence.  Those live here instead,
in a certificate the registry signs over M:

    "As of version n, issued at t, the identity M has these particulars."

Each particular is committed on its own, under its own random salt, and the
signature covers only the commitments.  The holder keeps the values and salts,
and discloses any subset: a photo and an age band to a door, a name and a
mailing address to a lender, nothing at all to a vending machine.  A disclosed
field is checked against its commitment; an undisclosed one is a number that
says nothing.

Updating a detail is a new version.  Nothing else moves -- not M, not the
account registrations that encrypt it, not the receiving leaves that commit it,
not the Notes addressed to it, not the receipts that name it.  A superseded
version remains a true statement about its own date, which is exactly what a
receipt of that date should carry.  Whether a version is still the CURRENT one
is a question for the registry or a maximum age, as with every other standing.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, Mapping, Tuple

from alberta_buck.registry.certificate import (
    RegistryKeyPair, RegistrySchnorrProof, registry_schnorr_sign, registry_schnorr_verify,
)
from alberta_buck.wallet.bn254 import point_to_words, rand_scalar
from alberta_buck.wallet.domains import IDENTITY_PARTICULAR_FIELD, IDENTITY_PARTICULARS
from alberta_buck.wallet.transcript import keccak_raw


def _lp(b: bytes) -> bytes:
    """Length-prefixed bytes: no two field lists hash alike by shifting a boundary."""
    return struct.pack(">I", len(b)) + b


def field_digest(name: str, value: str, salt: int) -> int:
    """One particular's commitment: hiding (the salt) and binding (the hash)."""
    return int.from_bytes(keccak_raw(
        IDENTITY_PARTICULAR_FIELD + salt.to_bytes(32, "big")
        + _lp(name.encode()) + _lp(value.encode())), "big")


@dataclass(frozen=True)
class ParticularsCertificate:
    """What the registry signs.  Carries no value, only commitments."""
    registry_id: str
    M:           Tuple
    version:     int
    issued_at:   str                   # ISO-8601 UTC
    digests:     Dict[str, int]        # particular name -> field_digest
    signature:   RegistrySchnorrProof

    def message(self) -> bytes:
        return _message(self.registry_id, self.M, self.version, self.issued_at, self.digests)


def _message(registry_id, M, version, issued_at, digests) -> bytes:
    Mx, My = point_to_words(M)
    body = [IDENTITY_PARTICULARS, _lp(registry_id.encode()),
            Mx.to_bytes(32, "big"), My.to_bytes(32, "big"),
            struct.pack(">Q", version), _lp(issued_at.encode())]
    for name in sorted(digests):
        body += [_lp(name.encode()), digests[name].to_bytes(32, "big")]
    return keccak_raw(b"".join(body))


@dataclass(frozen=True)
class Particulars:
    """What the holder keeps: the certificate, the values and their salts."""
    certificate: ParticularsCertificate
    values:      Dict[str, str]
    salts:       Dict[str, int]


@dataclass(frozen=True)
class Disclosure:
    """What the holder shows: the certificate and a chosen subset of its fields."""
    certificate: ParticularsCertificate
    fields:      Dict[str, Tuple[str, int]]   # name -> (value, salt)


def issue_particulars(kp: RegistryKeyPair, registry_id: str, M, version: int,
                      issued_at: str, values: Mapping[str, str], rng=None) -> Particulars:
    """The registry's side: commit each particular, sign the commitments over M."""
    salts = {name: rand_scalar(rng) for name in sorted(values)}
    digests = {name: field_digest(name, values[name], salts[name]) for name in sorted(values)}
    sig = registry_schnorr_sign(kp.sk, _message(registry_id, M, version, issued_at, digests),
                                registry_id, rng=rng)
    cert = ParticularsCertificate(registry_id, M, version, issued_at, digests, sig)
    return Particulars(cert, dict(values), salts)


def disclose(p: Particulars, *names: str) -> Disclosure:
    """The holder's side: reveal exactly the named particulars."""
    missing = [n for n in names if n not in p.values]
    if missing:
        raise KeyError(f"no such particulars: {missing}")
    return Disclosure(p.certificate, {n: (p.values[n], p.salts[n]) for n in names})


def verify_disclosure(d: Disclosure, pk_registry, M) -> Dict[str, str]:
    """A counterparty's side: the registry signed these commitments over THIS M, and each
    shown value opens its commitment.  Returns the disclosed values; raises ValueError."""
    cert = d.certificate
    if point_to_words(cert.M) != point_to_words(M):
        raise ValueError("particulars certify a different identity")
    if not registry_schnorr_verify(pk_registry, cert.signature, cert.message(), cert.registry_id):
        raise ValueError("registry signature does not verify")
    for name, (value, salt) in d.fields.items():
        if cert.digests.get(name) != field_digest(name, value, salt):
            raise ValueError(f"particular {name!r} does not open its commitment")
    return {name: value for name, (value, _salt) in d.fields.items()}


__all__ = ["Disclosure", "Particulars", "ParticularsCertificate", "disclose", "field_digest",
           "issue_particulars", "verify_disclosure"]
