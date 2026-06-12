"""Client Agent -- identity holder that decrypts and verifies certificates.

A ClientAgent represents an individual who has been issued one or more identity
certificates by registries.  It holds the BN254 identity secret m (derived from
the KYC data) and can decrypt sealed certificates, verify registry signatures,
and produce membership proofs for spend-time identity verification.

The client also tracks which feature authorities have certified it for which
attributes, enabling AND-composed proofs at spend time ("I am registered AND
I am over 18").

Reference: alberta-buck-notes-identity-axis.org.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from alberta_buck.registry.certificate import (
    IdentityCertificate,
    SignedCertificate,
    SealedCertificate,
    RegistryKeyPair,
    registry_verify_certificate,
    unseal_certificate,
)
from alberta_buck.registry.tree import MembershipProof, identity_leaf
from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, mul, neg, eq, point_to_words,
)
from alberta_buck.wallet.identity import identity_scalar


# ---------------------------------------------------------------------------
# Verified identity
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VerifiedIdentity:
    """An identity certificate that has been decrypted and verified by the client.

    Attributes:
        registry_id: Which registry issued this certificate.
        serial: Certificate serial number.
        canonical_identity: The KYC data that was certified.
        M: The identity point.
        m: The identity scalar (m * G = M).
        leaf: identity_leaf(M).
        issued_at: POSIX timestamp.
        expires_at: POSIX timestamp.
        registry_pk: The registry's public key (verified).
    """
    registry_id: str
    serial: int
    canonical_identity: str
    M: Tuple
    m: int
    leaf: int
    issued_at: float
    expires_at: int
    registry_pk: Tuple

    @property
    def is_expired(self, now: Optional[float] = None) -> bool:
        """True if the certificate has expired."""
        import time
        if self.expires_at == 0:
            return False
        return (now if now is not None else time.time()) >= self.expires_at


# ---------------------------------------------------------------------------
# Client Agent
# ---------------------------------------------------------------------------

class ClientAgent:
    """An identity holder with knowledge of their BN254 identity secret m.

    The client receives sealed certificates from registries and feature
    authorities, decrypts them using their identity secret key, verifies the
    registry signatures, and maintains a local catalog of verified identities
    and feature memberships.

    Args:
        identity_sk: The client's BN254 identity secret key (m scalar).
        identity_pk: The client's corresponding public key (m * G).
    """

    def __init__(self, identity_sk: int, identity_pk=None) -> None:
        self._sk = identity_sk % ORDER
        self._pk = mul(G1, self._sk) if identity_pk is None else identity_pk
        if identity_pk is not None and not eq(mul(G1, self._sk), identity_pk):
            raise ValueError("identity_sk does not match identity_pk")

        # Verified certificates indexed by (registry_id, serial).
        self._identities: Dict[Tuple[str, int], VerifiedIdentity] = {}

        # Membership proofs indexed by sub_tree_id -> MembershipProof.
        self._memberships: Dict[str, List[MembershipProof]] = {}

    # -- properties ----------------------------------------------------------

    @property
    def sk(self) -> int:
        """The client's identity secret scalar."""
        return self._sk

    @property
    def pk(self) -> Tuple:
        """The client's identity public key (G1 point)."""
        return self._pk

    @property
    def M(self) -> Tuple:
        """The client's own identity point (same as pk for identity keys)."""
        return self._pk

    @property
    def identities(self) -> List[VerifiedIdentity]:
        """All verified identities."""
        return list(self._identities.values())

    @property
    def m(self) -> int:
        """The client's identity scalar."""
        return self._sk

    # -- certificate reception -----------------------------------------------

    def receive_certificate(
        self,
        sealed: SealedCertificate,
        registry_pk_expected=None,   # optional: expected registry pk for pinning
        chainid: int = 0,
    ) -> VerifiedIdentity:
        """Decrypt and verify a sealed certificate from a registry.

        Args:
            sealed: The SealedCertificate received from the registry.
            registry_pk_expected: If set, assert the certificate was signed by
                this specific registry key (TOFU pinning).
            chainid: EVM chain ID used during signing.

        Returns:
            VerifiedIdentity with all fields validated.

        Raises:
            ValueError: If the certificate fails signature verification or
                was encrypted for a different client.
            cryptography.exceptions.InvalidTag: If the sealed envelope is
                corrupted or was not encrypted under this client's key.
        """
        signed = unseal_certificate(sealed, self._sk)
        if not registry_verify_certificate(signed, chainid):
            raise ValueError("certificate signature verification failed")
        if registry_pk_expected is not None:
            if not eq(signed.registry_pk, registry_pk_expected):
                raise ValueError("registry pk does not match expected")

        cert = signed.cert
        M_calc = mul(G1, identity_scalar(cert.canonical_identity))
        if not eq(cert.M, M_calc):
            raise ValueError("M in certificate does not match canonical_identity")
        m_cert = identity_scalar(cert.canonical_identity)

        leaf = identity_leaf(cert.M)
        vi = VerifiedIdentity(
            registry_id=cert.registry_id,
            serial=cert.serial,
            canonical_identity=cert.canonical_identity,
            M=cert.M,
            m=m_cert,
            leaf=leaf,
            issued_at=cert.issued_at,
            expires_at=cert.expires_at,
            registry_pk=signed.registry_pk,
        )
        self._identities[(cert.registry_id, cert.serial)] = vi
        return vi

    # -- membership proof management -----------------------------------------

    def add_membership(self, sub_tree_id: str, proof: MembershipProof) -> None:
        """Store a membership proof from a registry or feature authority.

        Args:
            sub_tree_id: Which sub-tree this proof is in.
            proof: The MembershipProof for this client's identity.
        """
        if sub_tree_id not in self._memberships:
            self._memberships[sub_tree_id] = []
        self._memberships[sub_tree_id].append(proof)

    def get_membership(self, sub_tree_id: str) -> Optional[MembershipProof]:
        """Get the most recent membership proof for a sub-tree.

        Args:
            sub_tree_id: Which sub-tree to retrieve a proof for.

        Returns:
            The most recent MembershipProof, or None if not available.
        """
        proofs = self._memberships.get(sub_tree_id)
        if proofs:
            return proofs[-1]
        return None

    def list_memberships(self) -> Dict[str, List[MembershipProof]]:
        """All stored membership proofs, keyed by sub_tree_id."""
        return dict(self._memberships)

    # -- lookup --------------------------------------------------------------

    def get_identity(self, registry_id: str, serial: int) -> Optional[VerifiedIdentity]:
        """Look up a verified identity by registry and serial."""
        return self._identities.get((registry_id, serial))

    def __repr__(self) -> str:
        return (f"ClientAgent(identities={len(self._identities)}, "
                f"memberships={len(self._memberships)} sub-trees)")


__all__ = [
    "ClientAgent",
    "VerifiedIdentity",
]
