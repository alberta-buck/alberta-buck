"""Registry Agent -- government KYC authority that certifies identities.

A RegistryAgent represents a single KYC authority (provincial, state, or federal).
It verifies identity documents (stubbed in the prototype), derives the BN254
identity scalar m = H(canonical_identity) % ORDER and point M = m*G, issues a
signed certificate, ElGamal-encrypts the identity point M under the client's
public key (so the wallet can store the ciphertext as-is and CP-re-encrypt it
to counterparties), and inserts M into its local identity tree.

The registry periodically pushes its updated sub-root to the CentralMerkleService,
which aggregates it into the on-chain identityRoot.  An identity is usable only
after its sub-root reaches the aggregator (the "commit-before-use" discipline
from alberta-buck-notes.org "Mutual Decryptability" / one-gadget model).

Reference: alberta-buck-notes.org ("Mutual Decryptability"); see also alberta-buck-notes-flow.org for the accumulator implementation. The Registry-Identity
Accumulator".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from alberta_buck.registry.certificate import (
    IdentityCertificate,
    SignedCertificate,
    SealedCertificate,
    RegistryKeyPair,
    registry_keygen,
    registry_sign_certificate,
    registry_verify_certificate,
    seal_certificate,
)
from alberta_buck.registry.tree import (
    IdentityMerkleTree, MembershipProof, identity_leaf, KYC_SUBTREE_DEPTH,
)
from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, mul, neg, eq, rand_scalar, point_to_words,
)
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.elgamal import (
    ElGamalCiphertext, IdentityKeyPair, identity_keygen, elgamal_encrypt,
)
from alberta_buck.wallet.ps import (
    PSKeyPair, PSSignature, ps_keygen, ps_sign, ps_rerandomize,
)
from alberta_buck.wallet.nizk import (
    RegistrationProof, registration_prove,
)


# ---------------------------------------------------------------------------
# Registration record
# ---------------------------------------------------------------------------

@dataclass
class RegistrationRecord:
    """A single identity registered by this registry.

    Attributes:
        serial: Unique certificate serial number.
        canonical_identity: Sorted-key JSON identity data.
        M: The identity point M = m*G.
        leaf: identity_leaf(M) -- the Poseidon hash in the tree.
        leaf_index: Position in the registry's identity tree.
        issued_at: POSIX timestamp.
        expires_at: POSIX timestamp (0 = no expiry).
        client_pk: The client's BN254 public key the certificate was sealed for.
        sealed: The SealedCertificate ready for delivery.
    """
    serial: int
    canonical_identity: str
    M: Tuple
    leaf: int
    leaf_index: int
    issued_at: float
    expires_at: int
    client_pk: Tuple
    sealed: SealedCertificate


# ---------------------------------------------------------------------------
# Registry state (persistable)
# ---------------------------------------------------------------------------

@dataclass
class RegistryState:
    """Serializable state of a RegistryAgent for persistence and recovery.

    Attributes:
        registry_id: Stable identifier for this registry.
        pk_registry: The registry's BN254 public key (G1 point).
        next_serial: Next certificate serial number.
        tree_leaves: All identity_leaf values in insertion order.
        tree_depth: Depth of the identity Merkle tree.
        issued_count: Total number of certificates issued.
    """
    registry_id: str
    pk_registry: Tuple
    next_serial: int
    tree_leaves: List[int]
    tree_depth: int
    issued_count: int

    def to_dict(self) -> dict:
        px, py = point_to_words(self.pk_registry)
        return {
            "registry_id": self.registry_id,
            "pk_registry_x": str(px),
            "pk_registry_y": str(py),
            "next_serial": self.next_serial,
            "tree_leaves": [str(lf) for lf in self.tree_leaves],
            "tree_depth": self.tree_depth,
            "issued_count": self.issued_count,
        }


@dataclass
class FullRegistrationRecord:
    """A complete identity registration: certificate + PS credential + NIZK.

    Contains everything needed for both off-chain verification (signed
    certificate) and on-chain IdentityRegistry registration (PS credential,
    NIZK, ElGamal ciphertext).

    Attributes:
        serial: Unique certificate serial number.
        canonical_identity: Sorted-key JSON identity data.
        m: The identity scalar.
        M: The identity point M = m*G.
        leaf: identity_leaf(M).
        leaf_index: Position in the registry's identity tree.
        issued_at: POSIX timestamp.
        expires_at: POSIX timestamp (0 = no expiry).
        client_kp: The client's ElGamal keypair.
        E_addr: ElGamal ciphertext of M under client_kp.pk.
        ps_sigma_rerand: Rerandomized PS credential for on-chain registration.
        registration_proof: NIZK binding the PS credential to E_addr.
        sealed: The signed, ElGamal-encrypted certificate for off-chain delivery.
        membership_proof: Merkle tree membership proof (populated after issuance).
    """
    serial: int
    canonical_identity: str
    m: int
    M: Tuple
    leaf: int
    leaf_index: int
    issued_at: float
    expires_at: int
    client_kp: IdentityKeyPair
    E_addr: ElGamalCiphertext
    ps_sigma_rerand: PSSignature
    registration_proof: RegistrationProof
    sealed: SealedCertificate
    membership_proof: Optional[MembershipProof] = None


# ---------------------------------------------------------------------------
# Registry Agent
# ---------------------------------------------------------------------------

class RegistryAgent:
    """A KYC registry that verifies identities and issues signed certificates.

    Each issued identity is inserted into a local Poseidon Merkle tree.  The
    registry periodically pushes its updated sub-root to the CentralMerkleService,
    which aggregates it into the on-chain identityRoot.

    Certificates are ElGamal-encrypted under the client's public key -- the
    identity point M itself is the plaintext -- so the wallet stores the
    ciphertext as-received and can decrypt it or CP-re-encrypt it to reveal
    to a counterparty using the same primitives as the IdentityRegistry.

    Args:
        registry_id: Stable identifier (e.g. "ca-bc-2026").
        signing_key: RegistryKeyPair for signing certificates (generated if None).
        tree_depth: Depth of the local identity tree (default 12, ~4K identities).
    """

    def __init__(
        self,
        registry_id: str,
        signing_key: Optional[RegistryKeyPair] = None,
        ps_keypair: Optional[PSKeyPair] = None,
        tree_depth: int = KYC_SUBTREE_DEPTH,
    ) -> None:
        self.registry_id = registry_id
        self._key = signing_key if signing_key is not None else registry_keygen()
        self._ps_keypair = ps_keypair  # optional: for PS credential issuance
        self._tree = IdentityMerkleTree(depth=tree_depth)
        self._next_serial = 1
        self._records: Dict[int, RegistrationRecord] = {}  # serial -> record

    # -- properties ----------------------------------------------------------

    @property
    def pk_registry(self) -> Tuple:
        """The registry's public key (G1 point)."""
        return self._key.pk

    @property
    def sub_root(self) -> int:
        """Current root of the registry's identity tree."""
        return self._tree.root()

    @property
    def identity_count(self) -> int:
        """Number of identities issued by this registry."""
        return self._tree.count

    @property
    def next_serial(self) -> int:
        """Next certificate serial number to assign."""
        return self._next_serial

    @property
    def ps_keypair(self) -> Optional[PSKeyPair]:
        """The registry's PS credential signing keypair, if configured."""
        return self._ps_keypair

    @property
    def can_issue_credentials(self) -> bool:
        """True if this registry can issue PS credentials for on-chain registration."""
        return self._ps_keypair is not None

    # -- identity issuance ---------------------------------------------------

    def issue_identity(
        self,
        identity_fields: dict,
        client_pk,                      # BN254 G1 point
        chainid: int = 0,
        expires_at: int = 0,
        rng=None,
    ) -> RegistrationRecord:
        """Verify KYC data and issue a signed, ElGamal-encrypted identity certificate.

        The certificate is signed by this registry and ElGamal-encrypted under
        client_pk.  The plaintext is the identity point M itself, so the wallet
        can store the ElGamalCiphertext as-received and later decrypt it or
        Chaum-Pedersen re-encrypt it to a counterparty.

        Args:
            identity_fields: Dict of KYC data (name, dob, region, etc.).
            client_pk: Client's BN254 G1 public key for ElGamal encryption.
            chainid: EVM chain ID for domain separation.
            expires_at: POSIX timestamp (0 = no expiry).
            rng: Optional callable for deterministic randomness.

        Returns:
            RegistrationRecord with the sealed certificate and tree position.
        """
        canonical = canonical_identity_data(identity_fields)
        M = mul(G1, identity_scalar(canonical))
        serial = self._next_serial
        self._next_serial += 1
        issued_at = time.time()

        # Sign the certificate.
        signed = registry_sign_certificate(
            registry_sk=self._key.sk,
            registry_id=self.registry_id,
            canonical_identity=canonical,
            serial=serial,
            issued_at=int(issued_at),
            expires_at=expires_at,
            chainid=chainid,
            rng=rng,
        )

        # ElGamal-encrypt the identity point M under the client's key.
        # The wallet stores this ciphertext and can CP-re-encrypt it to
        # counterparties using the same primitives as verifyApprove.
        sealed = seal_certificate(signed, client_pk, rng=rng)

        # Insert into the local identity tree (commit-before-use: this M is
        # usable only after the new sub_root reaches the aggregator + chain).
        leaf = identity_leaf(M)
        leaf_index = self._tree.insert_leaf(leaf)

        rec = RegistrationRecord(
            serial=serial,
            canonical_identity=canonical,
            M=M,
            leaf=leaf,
            leaf_index=leaf_index,
            issued_at=issued_at,
            expires_at=expires_at,
            client_pk=client_pk,
            sealed=sealed,
        )
        self._records[serial] = rec
        return rec

    def issue_batch(
        self,
        identities: List[Tuple[dict, Tuple]],  # List[(identity_fields, client_pk)]
        chainid: int = 0,
        expires_at: int = 0,
        rng=None,
    ) -> List[RegistrationRecord]:
        """Issue certificates for a batch of identities.

        Useful for bulk KYC processing (e.g. onboarding a cohort).  The tree
        is updated once for all identities; the new sub_root reflects the
        entire batch atomically.

        Args:
            identities: List of (identity_fields_dict, client_pk) pairs.
            chainid: EVM chain ID.
            expires_at: POSIX timestamp (0 = no expiry).
            rng: Optional callable for deterministic randomness.

        Returns:
            List of RegistrationRecord, one per issued identity.
        """
        records = []
        for fields, client_pk in identities:
            rec = self.issue_identity(fields, client_pk, chainid, expires_at, rng)
            records.append(rec)
        return records

    # -- full identity issuance (cert + PS credential + NIZK) ----------------

    def issue_full_identity(
        self,
        identity_fields: dict,
        client_kp: Optional[IdentityKeyPair] = None,
        chainid: int = 0,
        expires_at: int = 0,
        registrant_addr: int = 0,
        rng=None,
        registry_addr: int = 0,
    ) -> FullRegistrationRecord:
        """Issue a complete identity: certificate + PS credential + registration NIZK.

        Produces everything needed for both off-chain verification and on-chain
        IdentityRegistry registration.  If client_kp is None, generates a fresh
        ElGamal keypair.  Requires the registry to have a PS keypair configured.

        Args:
            identity_fields: Dict of KYC data (name, dob, region, etc.).
            client_kp: Client's ElGamal keypair (generated if None).
            chainid: EVM chain ID for domain separation.
            expires_at: POSIX timestamp (0 = no expiry).
            registrant_addr: Ethereum address for Fiat-Shamir binding in the NIZK.
            rng: Optional callable for deterministic randomness.
            registry_addr: IdentityRegistry address for domain separation.

        Returns:
            FullRegistrationRecord with certificate, PS credential, NIZK,
            ElGamal ciphertext, and tree position.

        Raises:
            RuntimeError: If the registry has no PS keypair configured.
        """
        if self._ps_keypair is None:
            raise RuntimeError(
                "RegistryAgent has no PS keypair; cannot issue on-chain credentials. "
                "Use issue_identity() for certificate-only issuance, or construct "
                "with ps_keypair= to enable full issuance."
            )

        canonical = canonical_identity_data(identity_fields)
        m = identity_scalar(canonical)
        M = mul(G1, m)
        serial = self._next_serial
        self._next_serial += 1
        issued_at = time.time()

        # Generate client keypair if not provided.
        kp = client_kp if client_kp is not None else identity_keygen(rng=rng)

        # ElGamal-encrypt M under the client's key for the on-chain E_addr record.
        r_elg = rand_scalar(rng) if rng is not None else rand_scalar()
        E_addr = elgamal_encrypt(M, kp.pk, r_elg)

        # PS credential: sign m with the registry's PS keypair, rerandomize.
        sigma_raw = ps_sign(self._ps_keypair, m, rng=rng)
        sigma_rerand, _ = ps_rerandomize(sigma_raw, rng=rng)

        # Registration NIZK: proves the PS credential and E_addr encrypt the same m.
        nizk = registration_prove(
            sigma_rerand, m, r_elg, kp.pk, E_addr, registrant_addr, kp.sk,
            chainid if chainid else 1, rng=rng, registry=registry_addr,
        )

        # Signed certificate for off-chain verification.
        signed = registry_sign_certificate(
            registry_sk=self._key.sk,
            registry_id=self.registry_id,
            canonical_identity=canonical,
            serial=serial,
            issued_at=int(issued_at),
            expires_at=expires_at,
            chainid=chainid,
            rng=rng,
        )
        sealed = seal_certificate(signed, kp.pk, rng=rng)

        # Insert into the local identity tree.
        leaf = identity_leaf(M)
        leaf_index = self._tree.insert_leaf(leaf)

        rec = FullRegistrationRecord(
            serial=serial,
            canonical_identity=canonical,
            m=m, M=M, leaf=leaf, leaf_index=leaf_index,
            issued_at=issued_at, expires_at=expires_at,
            client_kp=kp, E_addr=E_addr,
            ps_sigma_rerand=sigma_rerand,
            registration_proof=nizk,
            sealed=sealed,
        )
        return rec

    def issue_full_batch(
        self,
        identities: List[Tuple[dict, Optional[IdentityKeyPair]]],
        chainid: int = 0,
        expires_at: int = 0,
        registrant_addrs: Optional[List[int]] = None,
        rng=None,
        registry_addr: int = 0,
    ) -> List[FullRegistrationRecord]:
        """Issue full identities for a batch.

        Args:
            identities: List of (identity_fields_dict, client_kp_or_None) pairs.
            chainid: EVM chain ID.
            expires_at: POSIX timestamp (0 = no expiry).
            registrant_addrs: Parallel list of Ethereum addresses for NIZK binding.
            rng: Optional callable for deterministic randomness.
            registry_addr: IdentityRegistry address for domain separation.

        Returns:
            List of FullRegistrationRecord, one per issued identity.
        """
        addrs = registrant_addrs if registrant_addrs is not None else [0] * len(identities)
        if len(addrs) != len(identities):
            raise ValueError("registrant_addrs length must match identities length")
        records = []
        for (fields, kp), addr in zip(identities, addrs):
            rec = self.issue_full_identity(
                fields, kp, chainid, expires_at, addr, rng,
                registry_addr=registry_addr,
            )
            records.append(rec)
        return records

    # -- certificate recovery (registry-side, for audit/dispute) -------------

    def get_certificate(self, serial: int) -> Optional[SignedCertificate]:
        """Recover the plaintext certificate for a given serial.

        The registry holds the signing key and can re-derive the signed
        certificate from its records for audit or dispute resolution.

        Args:
            serial: Certificate serial number.

        Returns:
            SignedCertificate or None if not found.
        """
        rec = self._records.get(serial)
        if rec is None:
            return None
        return registry_sign_certificate(
            registry_sk=self._key.sk,
            registry_id=self.registry_id,
            canonical_identity=rec.canonical_identity,
            serial=serial,
            issued_at=int(rec.issued_at),
            expires_at=rec.expires_at,
        )

    # -- membership proofs ---------------------------------------------------

    def membership_proof(self, leaf_index: int) -> MembershipProof:
        """Generate a Merkle proof for the identity at leaf_index.

        Args:
            leaf_index: Position in the registry's identity tree.

        Returns:
            MembershipProof valid against the current sub_root.
        """
        return self._tree.path(leaf_index)

    def membership_proof_for_serial(self, serial: int) -> Optional[MembershipProof]:
        """Generate a Merkle proof for the certificate identified by serial.

        Args:
            serial: Certificate serial number.

        Returns:
            MembershipProof or None if not found.
        """
        rec = self._records.get(serial)
        if rec is None:
            return None
        return self._tree.path(rec.leaf_index)

    # -- state persistence ---------------------------------------------------

    def state(self) -> RegistryState:
        """Export the registry state for persistence.

        Returns:
            RegistryState suitable for serialization.  Note: the signing
            secret key is NOT included -- it must be stored separately.
        """
        return RegistryState(
            registry_id=self.registry_id,
            pk_registry=self._key.pk,
            next_serial=self._next_serial,
            tree_leaves=self._tree.leaves[:],
            tree_depth=self._tree.depth,
            issued_count=self.identity_count,
        )

    @classmethod
    def from_state(
        cls,
        state: RegistryState,
        signing_sk: int,
    ) -> 'RegistryAgent':
        """Restore a registry from saved state and the signing secret key.

        Args:
            state: RegistryState from a previous state() call.
            signing_sk: The registry's secret key scalar.

        Returns:
            RegistryAgent with restored tree and serial counter.
        """
        key = RegistryKeyPair(sk=signing_sk, pk=mul(G1, signing_sk % ORDER))
        agent = cls(registry_id=state.registry_id, signing_key=key,
                    tree_depth=state.tree_depth)
        agent._next_serial = state.next_serial
        if state.tree_leaves:
            agent._tree.insert_batch(state.tree_leaves)
        return agent

    def __repr__(self) -> str:
        return (f"RegistryAgent({self.registry_id!r}, "
                f"identities={self.identity_count}, "
                f"sub_root={self.sub_root:#x})")


__all__ = [
    "RegistryAgent",
    "RegistrationRecord",
    "RegistryState",
]
