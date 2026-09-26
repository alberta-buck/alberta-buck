"""Alberta Buck Identity Registry -- government identity certification.

A federated identity-certification layer for the Identity-axis Notes system.
Each registry (provincial, state, federal) independently verifies KYC data and
issues signed, ElGamal-encrypted identity certificates.  Feature authorities
attest that identities possess specific attributes (age>18, has-license, region).
A central Merkle service aggregates sub-tree roots from all registries and
feature authorities; the single combined root is posted on chain and consumed
by Notes spend-path identity-membership proofs.

Reference: alberta-buck-notes.org ("Mutual Decryptability", "one gadget" / Identity-M model); see also alberta-buck-notes-flow.org "The Identity-M Spend Path".
"""

from alberta_buck.registry.certificate import (
    IdentityCertificate,
    SignedCertificate,
    SealedCertificate,
    RegistryKeyPair,
    RegistrySchnorrProof,
    registry_keygen,
    registry_schnorr_sign,
    registry_schnorr_verify,
    registry_sign_certificate,
    registry_verify_certificate,
    seal_certificate,
    unseal_certificate,
)
from alberta_buck.registry.tree import (
    IdentityMerkleTree,
    MembershipProof,
    identity_leaf,
    identity_leaf_salted,
    EMPTY_LEAF,
)
from alberta_buck.registry.merkle_service import (
    CentralMerkleService,
    SubTreeKind,
    SubTreeRecord,
    AggregatorMembershipProof,
    FullMembershipProof,
    ComposedMembershipProof,
)
from alberta_buck.registry.registry import (
    RegistryAgent,
    RegistrationRecord,
    FullRegistrationRecord,
    RegistryState,
)
from alberta_buck.registry.feature_authority import (
    FeatureAuthority,
    FeatureRecord,
)
from alberta_buck.registry.client import (
    ClientAgent,
    VerifiedIdentity,
)

__all__ = [
    # certificate
    "IdentityCertificate",
    "SignedCertificate",
    "SealedCertificate",
    "RegistryKeyPair",
    "RegistrySchnorrProof",
    "registry_keygen",
    "registry_schnorr_sign",
    "registry_schnorr_verify",
    "registry_sign_certificate",
    "registry_verify_certificate",
    "seal_certificate",
    "unseal_certificate",
    # tree
    "IdentityMerkleTree",
    "MembershipProof",
    "identity_leaf",
    "identity_leaf_salted",
    "EMPTY_LEAF",
    # merkle_service
    "CentralMerkleService",
    "SubTreeKind",
    "SubTreeRecord",
    "AggregatorMembershipProof",
    "FullMembershipProof",
    "ComposedMembershipProof",
    # registry
    "RegistryAgent",
    "RegistrationRecord",
    "FullRegistrationRecord",
    "RegistryState",
    # feature_authority
    "FeatureAuthority",
    "FeatureRecord",
    # client
    "ClientAgent",
    "VerifiedIdentity",
]
