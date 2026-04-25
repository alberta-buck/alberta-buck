"""Simulated government / institutional credential issuer.

Mirrors the issuance ceremony described in alberta-buck-identity.org sec
"Issuance: The Issuer Signs Once": the issuer verifies identity documents
out-of-band, canonicalizes them, computes m = H(canonical_identity_data),
produces the PS signature sigma = (h, (x + m*y)*h), and hands the applicant
back (m, sigma, identity_data) for storage in their wallet's Holochain
Private entry.  All wallet-side derivation steps (rerandomization, fresh
identity key pair, ElGamal encryption under that key, registration NIZK)
happen later, in the wallet -- not here.

The on-chain trust anchor for an issuer is its Ethereum address registered
via IdentityRegistry.trustIssuer(addr, pk) (referred to as the
TrustedIssuersRegistry in the spec).  This module supplies the matching
off-chain entity: same PS keypair, the address that the registry uses as
the trust anchor, and a simulated issuance log standing in for the
"issuance event published to the issuer's Holochain source chain" called
out in the same section.

Revocation follows the epoch-based renewal model from
alberta-buck-identity.org sec "Epoch-Based Credential Renewal": the issuer
does not invalidate the cryptographic artifact (sigma is eternal), it
simply refuses to re-issue at the next epoch -- which is exactly what
Issuer.revoke() simulates.

The optional `applicant_pk` parameter to issue() is a test convenience for
modeling a confidential delivery channel; the spec assumes the (m, sigma,
identity_data) hand-off uses the secure channel of the in-person KYC
ceremony itself.  Wallet-side ElGamal re-encryption (Alice -> Bob during
approve) is handled by alberta_buck.wallet.chaum_pedersen and is not the
issuer's job either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from alberta_buck.wallet.bn254 import G1, mul, rand_scalar
from alberta_buck.wallet.elgamal import ElGamalCiphertext, elgamal_encrypt
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.ps import (
    PSKeyPair, PSSignature, ps_keygen, ps_rerandomize, ps_sign, ps_verify,
)


@dataclass(frozen=True)
class IssuedCredential:
    """What the issuer hands back to a successful applicant.

    `canonical` is the canonical-JSON the m-hash was taken over (so the
    applicant can recompute m and audit the signing target).  `sigma` is
    the PS signature on m (raw, not yet rerandomized).  `delivery`, when
    present, is the ElGamal ciphertext (R, C) = (r*G, M + r*pk_applicant)
    used to confidentially transmit M = m*G.
    """
    canonical:  str
    m:          int
    sigma:      PSSignature
    issuer_id:  str
    issuer_addr: int
    delivery:   Optional[ElGamalCiphertext] = None


@dataclass
class _LogEntry:
    applicant_addr: int
    canonical:      str
    m:              int


@dataclass
class Issuer:
    """A trusted credential issuer (e.g. ATB Financial, Service Alberta).

    `issuer_id` is the human-readable identifier that appears in identity
    documents (e.g. "atb-financial-ca") and matches the `issuer_id` field
    used when canonicalizing identity data.

    `issuer_addr` is the Ethereum address the on-chain IdentityRegistry
    uses as the trust anchor for this issuer's PS public key.

    `keypair` is the PS keypair used to sign identity scalars.

    `revoked` is the set of applicant Ethereum addresses whose credentials
    have been revoked (simulated — the protocol's actual revocation lives
    on-chain via IdentityRegistry.revokeIssuer / off-chain epoch rotation).
    """
    issuer_id:   str
    issuer_addr: int
    keypair:     PSKeyPair
    _log:        List[_LogEntry]      = field(default_factory=list)
    _revoked:    set                   = field(default_factory=set)

    @classmethod
    def setup(cls, issuer_id: str, issuer_addr: int, rng=None) -> "Issuer":
        return cls(
            issuer_id=issuer_id,
            issuer_addr=issuer_addr,
            keypair=ps_keygen(rng=rng),
        )

    @property
    def pk_X(self):
        return self.keypair.pk_X

    @property
    def pk_Y(self):
        return self.keypair.pk_Y

    def issue(
        self,
        identity_fields: Mapping,
        applicant_addr: int,
        applicant_pk=None,
        rng=None,
    ) -> IssuedCredential:
        """Run the issuance ceremony.

        - Stamps `issuer_id` into the identity record (overwrites whatever
          the applicant submitted) so signed credentials cannot lie about
          which issuer signed them.
        - Canonicalizes the resulting record and computes m.
        - Produces sigma = PS_sign(m).
        - If `applicant_pk` is given, also encrypts M = m*G under it for
          confidential delivery.

        Raises ValueError if the applicant's address is in the revocation
        set (no re-issuance to revoked subjects without explicit reset).
        """
        if applicant_addr in self._revoked:
            raise ValueError(
                f"applicant {hex(applicant_addr)} is revoked at issuer "
                f"{self.issuer_id!r}; reset() first to allow re-issuance"
            )

        record = dict(identity_fields)
        record["issuer_id"] = self.issuer_id
        canonical = canonical_identity_data(record)
        m = identity_scalar(canonical)
        sigma = ps_sign(self.keypair, m, rng=rng)

        delivery: Optional[ElGamalCiphertext] = None
        if applicant_pk is not None:
            r = rand_scalar(rng)
            M = mul(G1, m)
            delivery = elgamal_encrypt(M, applicant_pk, r)

        self._log.append(_LogEntry(applicant_addr=applicant_addr, canonical=canonical, m=m))
        return IssuedCredential(
            canonical=canonical,
            m=m,
            sigma=sigma,
            issuer_id=self.issuer_id,
            issuer_addr=self.issuer_addr,
            delivery=delivery,
        )

    def revoke(self, applicant_addr: int) -> None:
        """Mark an applicant as ineligible for re-issuance at the next epoch.

        Per identity.org sec "Epoch-Based Credential Renewal", the issuer
        does not (and cannot) invalidate already-issued PS signatures; it
        simply refuses to provide the applicant's next-epoch credential.
        Existing on-chain registrations remain mathematically valid until
        their epoch expires, at which point isVerified returns false at
        the IdentityRegistry layer.  This method records the issuer's
        intent; the on-chain effect is the absence of a fresh issuance.
        """
        self._revoked.add(applicant_addr)

    def reset(self, applicant_addr: int) -> None:
        self._revoked.discard(applicant_addr)

    def issuance_log(self) -> List[_LogEntry]:
        """Return a copy of the issuance log (test/audit use only)."""
        return list(self._log)

    def verify_credential(self, cred: IssuedCredential) -> bool:
        """Check sigma is valid for m under this issuer's PS public key.

        The wallet would do this on receipt before rerandomizing.
        """
        if cred.issuer_id != self.issuer_id:
            return False
        return ps_verify(self.pk_X, self.pk_Y, cred.sigma, cred.m)


def rerandomize_for_registration(
    cred: IssuedCredential, rng=None
) -> Tuple[PSSignature, int]:
    """Convenience: wallet-side rerandomization step.

    Returns (sigma_p, t).  The rerandomized sigma_p is what gets sent to
    IdentityRegistry.register; the unblinded sigma is never published.
    """
    return ps_rerandomize(cred.sigma, rng=rng)


__all__ = [
    "IssuedCredential",
    "Issuer",
    "rerandomize_for_registration",
]
