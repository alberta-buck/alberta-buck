"""Tests for alberta_buck.registry -- identity certification and Merkle aggregation.

Exercises the full registry prototype: certificate signing, ElGamal-encrypted
delivery, identity Merkle tree management, feature attestation, central Merkle
service aggregation, and client-side decryption+verification.

Run with:
    nix develop --command python -m pytest alberta_buck/test/test_registry.py -v
"""

import json
import os
import time
from typing import List, Tuple

import pytest

from alberta_buck.wallet.bn254 import G1, ORDER, add, mul, neg, eq, rand_scalar, point_to_words
from alberta_buck.wallet.elgamal import (
    ElGamalCiphertext,
    IdentityKeyPair,
    identity_keygen,
    elgamal_encrypt,
    elgamal_decrypt,
)
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.schnorr import SchnorrProof
from alberta_buck.registry.certificate import (
    IdentityCertificate,
    SignedCertificate,
    SealedCertificate,
    RegistryKeyPair,
    RegistrySchnorrProof,
    registry_keygen,
    registry_sign_certificate,
    registry_verify_certificate,
    registry_schnorr_sign,
    registry_schnorr_verify,
    seal_certificate,
    unseal_certificate,
)
from alberta_buck.registry.tree import (
    IdentityMerkleTree,
    MembershipProof,
    identity_leaf,
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


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def rng():
    """Deterministic RNG for reproducible test vectors."""
    import random
    r = random.Random(42)
    def _rng():
        return r.randint(1, 2**256 - 1)
    return _rng


@pytest.fixture
def registry_key(rng):
    """A registry's signing keypair."""
    return registry_keygen(rng)


@pytest.fixture
def client_keypair(rng):
    """A client's BN254 identity keypair."""
    return identity_keygen(rng)


@pytest.fixture
def client2_keypair(rng):
    """A second client's keypair."""
    return identity_keygen(rng)


@pytest.fixture
def sample_identity():
    """Sample KYC data."""
    return {
        "family_name": "Smith",
        "given_name": "Alice",
        "dob": "1990-05-15",
        "region": "BC",
        "nationality": "CA",
    }


@pytest.fixture
def sample_identity2():
    return {
        "family_name": "Jones",
        "given_name": "Bob",
        "dob": "1985-03-22",
        "region": "AB",
        "nationality": "CA",
    }


# ============================================================================
# Certificate tests
# ============================================================================

class TestCertificate:
    """Certificate creation, signing, serialization."""

    def test_create_and_sign(self, registry_key, sample_identity, rng):
        canonical = canonical_identity_data(sample_identity)
        signed = registry_sign_certificate(
            registry_sk=registry_key.sk,
            registry_id="ca-bc-2026",
            canonical_identity=canonical,
            serial=1,
            issued_at=int(time.time()),
            rng=rng,
        )
        assert signed.cert.registry_id == "ca-bc-2026"
        assert signed.cert.serial == 1
        assert signed.cert.canonical_identity == canonical

        # Verify.
        assert registry_verify_certificate(signed)

    def test_signature_verification(self, registry_key, sample_identity, rng):
        canonical = canonical_identity_data(sample_identity)
        msg_hash = IdentityCertificate(
            registry_id="ca-bc-2026", serial=1,
            canonical_identity=canonical,
            M=mul(G1, identity_scalar(canonical)),
            issued_at=1234567890, expires_at=0,
        ).to_hash_bytes()

        sig = registry_schnorr_sign(
            registry_key.sk, msg_hash, "ca-bc-2026", rng=rng,
        )
        assert registry_schnorr_verify(
            registry_key.pk, sig, msg_hash, "ca-bc-2026",
        )

    def test_wrong_message_rejected(self, registry_key, sample_identity, rng):
        canonical = canonical_identity_data(sample_identity)
        cert = IdentityCertificate(
            registry_id="ca-bc-2026", serial=1,
            canonical_identity=canonical,
            M=mul(G1, identity_scalar(canonical)),
            issued_at=1234567890, expires_at=0,
        )
        msg_hash = cert.to_hash_bytes()

        sig = registry_schnorr_sign(
            registry_key.sk, msg_hash, "ca-bc-2026", rng=rng,
        )
        # Tamper with the message.
        bad_hash = bytearray(msg_hash)
        bad_hash[0] ^= 0x01
        assert not registry_schnorr_verify(
            registry_key.pk, sig, bytes(bad_hash), "ca-bc-2026",
        )

    def test_wrong_registry_rejected(self, registry_key, sample_identity, rng):
        canonical = canonical_identity_data(sample_identity)
        cert = IdentityCertificate(
            registry_id="ca-bc-2026", serial=1,
            canonical_identity=canonical,
            M=mul(G1, identity_scalar(canonical)),
            issued_at=1234567890, expires_at=0,
        )
        msg_hash = cert.to_hash_bytes()

        sig = registry_schnorr_sign(
            registry_key.sk, msg_hash, "ca-bc-2026", rng=rng,
        )
        # Wrong registry_id in transcript.
        assert not registry_schnorr_verify(
            registry_key.pk, sig, msg_hash, "ca-ab-2026",
        )

    def test_M_consistency_enforced(self, registry_key, sample_identity, rng):
        """Certificate must have M matching the canonical identity."""
        canonical = canonical_identity_data(sample_identity)
        M_wrong = mul(G1, rand_scalar(rng))  # random point, not matching
        with pytest.raises(ValueError, match="M does not match"):
            IdentityCertificate(
                registry_id="ca-bc-2026", serial=1,
                canonical_identity=canonical,
                M=M_wrong,
                issued_at=1234567890, expires_at=0,
            )

    def test_serialize_roundtrip(self, registry_key, sample_identity, rng):
        canonical = canonical_identity_data(sample_identity)
        signed = registry_sign_certificate(
            registry_sk=registry_key.sk,
            registry_id="ca-bc-2026",
            canonical_identity=canonical,
            serial=1, issued_at=1234567890,
            rng=rng,
        )
        data = signed.serialize()
        signed2 = SignedCertificate.deserialize(data)
        assert signed2.cert.registry_id == signed.cert.registry_id
        assert signed2.cert.serial == signed.cert.serial
        assert signed2.cert.canonical_identity == signed.cert.canonical_identity
        assert eq(signed2.cert.M, signed.cert.M)
        assert signed2.verify()


# ============================================================================
# ElGamal sealed delivery tests
# ============================================================================

class TestSealedDelivery:
    """ElGamal-encrypted certificate delivery."""

    def test_seal_and_unseal(self, registry_key, client_keypair, sample_identity, rng):
        canonical = canonical_identity_data(sample_identity)
        signed = registry_sign_certificate(
            registry_sk=registry_key.sk,
            registry_id="ca-bc-2026",
            canonical_identity=canonical,
            serial=1, issued_at=int(time.time()),
            rng=rng,
        )
        sealed = seal_certificate(signed, client_keypair.pk, rng=rng)

        # Verify the ElGamal ciphertext shape.
        assert sealed.ct.R is not None
        assert sealed.ct.C is not None
        # It should decrypt to the certificate's M.
        M_dec = elgamal_decrypt(sealed.ct, client_keypair.sk)
        assert eq(M_dec, signed.cert.M)

        # Full unseal.
        unsealed = unseal_certificate(sealed, client_keypair.sk)
        assert unsealed.cert.serial == 1
        assert unsealed.verify()

    def test_wrong_client_cannot_decrypt(self, registry_key, client_keypair,
                                          client2_keypair, sample_identity, rng):
        canonical = canonical_identity_data(sample_identity)
        signed = registry_sign_certificate(
            registry_sk=registry_key.sk,
            registry_id="ca-bc-2026",
            canonical_identity=canonical,
            serial=1, issued_at=int(time.time()),
            rng=rng,
        )
        sealed = seal_certificate(signed, client_keypair.pk, rng=rng)

        # client2 tries to decrypt -- should fail because the decrypted M
        # won't match the certificate's M.
        with pytest.raises(ValueError, match="not sealed for this client"):
            unseal_certificate(sealed, client2_keypair.sk)

    def test_envelope_roundtrip(self, registry_key, client_keypair, sample_identity, rng):
        canonical = canonical_identity_data(sample_identity)
        signed = registry_sign_certificate(
            registry_sk=registry_key.sk,
            registry_id="ca-bc-2026",
            canonical_identity=canonical,
            serial=1, issued_at=int(time.time()),
            rng=rng,
        )
        sealed = seal_certificate(signed, client_keypair.pk, rng=rng)
        env = sealed.envelope
        sealed2 = SealedCertificate.from_envelope(env)
        unsealed = unseal_certificate(sealed2, client_keypair.sk)
        assert unsealed.cert.serial == 1

    def test_cp_reencrypt_to_counterparty(self, registry_key, client_keypair,
                                           client2_keypair, sample_identity, rng):
        """The sealed ElGamal ciphertext can be CP-re-encrypted to a counterparty,
        matching the pattern used by verifyApprove / verifyDepositorForIssuer."""
        from alberta_buck.wallet.chaum_pedersen import (
            chaum_pedersen_prove, chaum_pedersen_verify,
        )

        canonical = canonical_identity_data(sample_identity)
        signed = registry_sign_certificate(
            registry_sk=registry_key.sk,
            registry_id="ca-bc-2026",
            canonical_identity=canonical,
            serial=1, issued_at=int(time.time()),
            rng=rng,
        )
        sealed = seal_certificate(signed, client_keypair.pk, rng=rng)

        # Client re-encrypts the sealed ciphertext for client2.
        # This is exactly the chaum_pedersen_prove pattern: prove that
        # E_new encrypts the same plaintext as E_old under a new key.
        r_reenc = rand_scalar(rng)
        E_reenc = elgamal_encrypt(signed.cert.M, client2_keypair.pk, r_reenc)
        # Signature: (E_alice, E_bob, pk_alice, pk_bob, sk_alice, r_prime,
        #              sender, spender, chainid, rng=None)
        cp = chaum_pedersen_prove(
            sealed.ct, E_reenc, client_keypair.pk, client2_keypair.pk,
            client_keypair.sk, r_reenc,
            1234,  # sender address (dummy)
            5678,  # spender address (dummy)
            1,     # chainid
            rng=rng,
        )
        assert chaum_pedersen_verify(
            sealed.ct, E_reenc, client_keypair.pk, client2_keypair.pk,
            cp, 1234, 5678, 1,
        )
        # client2 can now decrypt.
        M_recovered = elgamal_decrypt(E_reenc, client2_keypair.sk)
        assert eq(M_recovered, signed.cert.M)


# ============================================================================
# Identity Merkle tree tests
# ============================================================================

class TestIdentityMerkleTree:
    """Poseidon Merkle tree for identity points."""

    def test_empty_tree_root(self):
        tree = IdentityMerkleTree(depth=12)
        root = tree.root()
        assert root != 0
        # Root should be stable for empty tree.
        tree2 = IdentityMerkleTree(depth=12)
        assert tree2.root() == root

    def test_insert_and_path(self, rng):
        tree = IdentityMerkleTree(depth=10)
        N = 5
        identities = [mul(G1, rand_scalar(rng)) for _ in range(N)]
        for M in identities:
            tree.insert_identity(M)
        assert tree.count == N

        for i, M in enumerate(identities):
            proof = tree.path(i)
            assert proof.verify()
            assert proof.leaf == identity_leaf(M)
            assert proof.root == tree.root()
            assert proof.leaf_index == i

    def test_batch_insert(self, rng):
        tree = IdentityMerkleTree(depth=10)
        identities = [mul(G1, rand_scalar(rng)) for _ in range(10)]
        leaves = [identity_leaf(M) for M in identities]
        first = tree.insert_batch(leaves)
        assert first == 0
        assert tree.count == 10

        for i, M in enumerate(identities):
            proof = tree.path(i)
            assert proof.verify()

    def test_membership_proof_detects_tamper(self, rng):
        tree = IdentityMerkleTree(depth=10)
        M = mul(G1, rand_scalar(rng))
        tree.insert_identity(M)
        proof = tree.path(0)
        assert proof.verify()

        # Tamper with the root.
        from dataclasses import replace
        bad_proof = MembershipProof(
            leaf=proof.leaf, siblings=proof.siblings,
            index_bits=proof.index_bits,
            root=(proof.root ^ 0xABCDEF),  # wrong root
            leaf_index=proof.leaf_index,
        )
        assert not bad_proof.verify()

    def test_rebuild_from_leaves(self, rng):
        tree1 = IdentityMerkleTree(depth=10)
        identities = [mul(G1, rand_scalar(rng)) for _ in range(7)]
        leaves = [identity_leaf(M) for M in identities]
        tree1.insert_batch(leaves)

        tree2 = IdentityMerkleTree.from_leaves(leaves, depth=10)
        assert tree2.root() == tree1.root()
        assert tree2.count == tree1.count

    def test_different_depths_different_roots(self, rng):
        M = mul(G1, rand_scalar(rng))
        tree10 = IdentityMerkleTree(depth=10)
        tree12 = IdentityMerkleTree(depth=12)
        tree10.insert_identity(M)
        tree12.insert_identity(M)
        assert tree10.root() != tree12.root()

    def test_path_bits_match_convention(self, rng):
        """The path index_bits convention matches the circom MerkleProof template:
        bit=0 means left (Poseidon(cur, sib)), bit=1 means right (Poseidon(sib, cur))."""
        from alberta_buck.wallet.poseidon import poseidon

        tree = IdentityMerkleTree(depth=4)
        # Insert 3 leaves so we get both left and right children.
        for _ in range(3):
            tree.insert_identity(mul(G1, rand_scalar(rng)))

        for i in range(tree.count):
            proof = tree.path(i)
            # Manually verify the path using the circom convention.
            cur = proof.leaf
            for sib, bit in zip(proof.siblings, proof.index_bits):
                if bit == 0:
                    cur = poseidon([cur, sib])
                else:
                    cur = poseidon([sib, cur])
            assert cur == proof.root


# ============================================================================
# Registry agent tests
# ============================================================================

class TestRegistryAgent:
    """KYC registry issuance and tree management."""

    def test_issue_identity(self, registry_key, client_keypair, sample_identity, rng):
        agent = RegistryAgent("ca-bc-2026", signing_key=registry_key)
        rec = agent.issue_identity(
            sample_identity, client_keypair.pk, rng=rng,
        )
        assert rec.serial == 1
        assert agent.identity_count == 1
        assert agent.sub_root != 0

        # The sealed certificate should be decryptable.
        sealed = rec.sealed
        unsealed = unseal_certificate(sealed, client_keypair.sk)
        assert unsealed.verify()

    def test_issue_batch(self, registry_key, client_keypair, sample_identity,
                          sample_identity2, rng):
        agent = RegistryAgent("ca-bc-2026", signing_key=registry_key)
        identities = [
            (sample_identity, client_keypair.pk),
            (sample_identity2, client_keypair.pk),
        ]
        records = agent.issue_batch(identities, rng=rng)
        assert len(records) == 2
        assert agent.identity_count == 2
        assert records[0].serial == 1
        assert records[1].serial == 2

    def test_membership_proof(self, registry_key, client_keypair, sample_identity, rng):
        agent = RegistryAgent("ca-bc-2026", signing_key=registry_key)
        rec = agent.issue_identity(
            sample_identity, client_keypair.pk, rng=rng,
        )
        proof = agent.membership_proof(rec.leaf_index)
        assert proof.verify()
        assert proof.root == agent.sub_root
        assert proof.leaf == rec.leaf

    def test_state_persistence(self, registry_key, client_keypair, sample_identity, rng):
        agent = RegistryAgent("ca-bc-2026", signing_key=registry_key)
        agent.issue_identity(sample_identity, client_keypair.pk, rng=rng)
        state = agent.state()

        # Restore.
        agent2 = RegistryAgent.from_state(state, signing_sk=registry_key.sk)
        assert agent2.registry_id == "ca-bc-2026"
        assert agent2.identity_count == 1
        assert agent2.sub_root == agent.sub_root

        # Can still issue.
        agent2.issue_identity(
            {"family_name": "New", "given_name": "Person"},
            client_keypair.pk, rng=rng,
        )
        assert agent2.identity_count == 2

    def test_different_serials(self, registry_key, client_keypair, sample_identity, rng):
        agent = RegistryAgent("ca-federal-2026", signing_key=registry_key)
        r1 = agent.issue_identity(sample_identity, client_keypair.pk, rng=rng)
        r2 = agent.issue_identity(
            {"family_name": "Other", "given_name": "Person"},
            client_keypair.pk, rng=rng,
        )
        assert r1.serial == 1
        assert r2.serial == 2
        assert r1.leaf_index == 0
        assert r2.leaf_index == 1


# ============================================================================
# Feature authority tests
# ============================================================================

class TestFeatureAuthority:
    """Feature attestation and tree management."""

    def test_attest_and_proof(self, rng):
        fa = FeatureAuthority("feature:age-over-18")
        M = mul(G1, rand_scalar(rng))
        rec = fa.attest(M)

        assert rec.leaf == identity_leaf(M)
        assert fa.has_identity(M)
        assert fa.identity_count == 1

        proof = fa.membership_proof_for_identity(M)
        assert proof is not None
        assert proof.verify()
        assert proof.root == fa.sub_root

    def test_attest_batch(self, rng):
        fa = FeatureAuthority("feature:has-license")
        identities = [mul(G1, rand_scalar(rng)) for _ in range(5)]
        records = fa.attest_batch(identities)
        assert len(records) == 5
        assert fa.identity_count == 5

        for i, M in enumerate(identities):
            proof = fa.membership_proof_for_identity(M)
            assert proof is not None
            assert proof.verify()

    def test_duplicate_rejected(self, rng):
        fa = FeatureAuthority("feature:region-bc")
        M = mul(G1, rand_scalar(rng))
        fa.attest(M)
        with pytest.raises(ValueError, match="already attested"):
            fa.attest(M)

    def test_revocation(self, rng):
        fa = FeatureAuthority("feature:age-over-18")
        M = mul(G1, rand_scalar(rng))
        fa.attest(M)
        assert fa.has_identity(M)

        idx = fa.revoke(M)
        assert idx is not None
        # After revocation, we need to push a new sub_root to the aggregator.
        new_root = fa.sub_root
        assert new_root != 0  # root is recomputed

    def test_feature_id_convention(self):
        with pytest.raises(ValueError, match="feature.*prefix"):
            FeatureAuthority("age-over-18")  # missing feature: prefix


# ============================================================================
# Central Merkle Service tests
# ============================================================================

class TestCentralMerkleService:
    """Aggregation of registry and feature sub-roots."""

    def test_enroll_registry(self, rng):
        cms = CentralMerkleService(depth=10)
        # Create a registry with one identity.
        reg_key = registry_keygen(rng)
        agent = RegistryAgent("ca-bc-2026", signing_key=reg_key)
        client_kp = identity_keygen(rng)
        agent.issue_identity(
            {"name": "Alice"}, client_kp.pk, rng=rng,
        )

        rec = cms.enroll_registry("ca-bc-2026", agent.sub_root)
        assert rec.kind == SubTreeKind.KYC
        assert cms.identity_root != 0

    def test_enroll_feature(self, rng):
        cms = CentralMerkleService(depth=10)
        fa = FeatureAuthority("feature:age-over-18")
        M = mul(G1, rand_scalar(rng))
        fa.attest(M)

        rec = cms.enroll_feature("feature:age-over-18", fa.sub_root)
        assert rec.kind == SubTreeKind.FEATURE
        assert cms.sub_tree_count == 1

    def test_aggregator_proof(self, rng):
        cms = CentralMerkleService(depth=10)

        reg_key = registry_keygen(rng)
        agent = RegistryAgent("ca-bc-2026", signing_key=reg_key)
        client_kp = identity_keygen(rng)
        agent.issue_identity({"name": "Alice"}, client_kp.pk, rng=rng)

        cms.enroll_registry("ca-bc-2026", agent.sub_root)
        proof = cms.aggregator_proof("ca-bc-2026")
        assert proof.verify()
        assert proof.aggregator_root == cms.identity_root
        assert proof.sub_root == agent.sub_root

    def test_full_proof(self, rng):
        cms = CentralMerkleService(depth=10)

        # Create a registry with identities.
        reg_key = registry_keygen(rng)
        agent = RegistryAgent("ca-bc-2026", signing_key=reg_key)
        client_kp = identity_keygen(rng)
        rec = agent.issue_identity({"name": "Alice"}, client_kp.pk, rng=rng)

        # Get sub-tree proof from the registry.
        sub_proof = agent.membership_proof(rec.leaf_index)
        Mx, My = point_to_words(rec.M)

        # Enroll and get full proof.
        cms.enroll_registry("ca-bc-2026", agent.sub_root)
        full = cms.full_proof("ca-bc-2026", sub_proof, M_x=Mx, M_y=My)
        assert full.verify()
        assert full.M_x == Mx
        assert full.M_y == My

    def test_sub_root_update(self, rng):
        cms = CentralMerkleService(depth=10)

        reg_key = registry_keygen(rng)
        agent = RegistryAgent("ca-bc-2026", signing_key=reg_key)
        client_kp = identity_keygen(rng)

        # Initial enrollment with empty tree root.
        empty_root = agent.sub_root
        cms.enroll_registry("ca-bc-2026", empty_root)

        # Issue an identity and update.
        agent.issue_identity({"name": "Alice"}, client_kp.pk, rng=rng)
        new_root = cms.update_sub_root("ca-bc-2026", agent.sub_root)
        assert new_root != empty_root
        assert new_root == cms.identity_root

    def test_composed_proof_kyc_plus_feature(self, rng):
        """AND-composed proof: M is in KYC registry AND in feature tree."""
        cms = CentralMerkleService(depth=10)

        # Set up a KYC registry.
        reg_key = registry_keygen(rng)
        agent = RegistryAgent("ca-bc-2026", signing_key=reg_key)
        client_kp = identity_keygen(rng)
        rec = agent.issue_identity({"name": "Alice"}, client_kp.pk, rng=rng)

        # Set up a feature authority.
        fa = FeatureAuthority("feature:age-over-18")
        fa.attest(rec.M)

        # Enroll both in the aggregator.
        cms.enroll_registry("ca-bc-2026", agent.sub_root)
        cms.enroll_feature("feature:age-over-18", fa.sub_root)

        # Get sub-tree proofs.
        kyc_proof = agent.membership_proof(rec.leaf_index)
        feat_proof = fa.membership_proof_for_identity(rec.M)

        Mx, My = point_to_words(rec.M)
        composed = cms.composed_proof(
            [("ca-bc-2026", kyc_proof), ("feature:age-over-18", feat_proof)],
            M_x=Mx, M_y=My,
        )
        assert composed.verify()
        assert len(composed.proofs) == 2
        # Both proofs share the same identity root.
        assert composed.proofs[0].identity_root == composed.proofs[1].identity_root

    def test_list_sub_trees_by_kind(self, rng):
        cms = CentralMerkleService(depth=10)

        reg_key = registry_keygen(rng)
        agent = RegistryAgent("ca-bc-2026", signing_key=reg_key)
        fa = FeatureAuthority("feature:age-over-18")

        cms.enroll_registry("ca-bc-2026", agent.sub_root)
        cms.enroll_feature("feature:age-over-18", fa.sub_root)

        kyc = cms.list_sub_trees(kind=SubTreeKind.KYC)
        feat = cms.list_sub_trees(kind=SubTreeKind.FEATURE)
        assert len(kyc) == 1
        assert len(feat) == 1
        assert kyc[0].sub_tree_id == "ca-bc-2026"
        assert feat[0].sub_tree_id == "feature:age-over-18"


# ============================================================================
# Client agent tests
# ============================================================================

class TestClientAgent:
    """Client-side certificate decryption and verification."""

    def test_receive_and_verify(self, registry_key, client_keypair, sample_identity, rng):
        agent = RegistryAgent("ca-bc-2026", signing_key=registry_key)
        rec = agent.issue_identity(
            sample_identity, client_keypair.pk, rng=rng,
        )

        client = ClientAgent(client_keypair.sk, client_keypair.pk)
        sealed = rec.sealed
        vi = client.receive_certificate(sealed, chainid=0)
        assert vi.registry_id == "ca-bc-2026"
        assert vi.serial == 1
        assert eq(vi.M, rec.M)

    def test_pin_registry_key(self, registry_key, client_keypair, sample_identity, rng):
        agent = RegistryAgent("ca-bc-2026", signing_key=registry_key)
        rec = agent.issue_identity(
            sample_identity, client_keypair.pk, rng=rng,
        )

        client = ClientAgent(client_keypair.sk, client_keypair.pk)
        # Correct pin.
        vi = client.receive_certificate(
            rec.sealed, registry_pk_expected=registry_key.pk,
        )
        assert vi is not None

        # Wrong pin.
        wrong_pk = mul(G1, rand_scalar(rng))
        with pytest.raises(ValueError, match="registry pk"):
            client.receive_certificate(
                rec.sealed, registry_pk_expected=wrong_pk,
            )

    def test_membership_storage(self, registry_key, client_keypair, sample_identity, rng):
        cms = CentralMerkleService(depth=10)
        agent = RegistryAgent("ca-bc-2026", signing_key=registry_key)
        rec = agent.issue_identity(
            sample_identity, client_keypair.pk, rng=rng,
        )
        cms.enroll_registry("ca-bc-2026", agent.sub_root)

        client = ClientAgent(client_keypair.sk, client_keypair.pk)
        sealed = rec.sealed
        client.receive_certificate(sealed)

        # Store membership proof.
        proof = agent.membership_proof(rec.leaf_index)
        client.add_membership("ca-bc-2026", proof)

        stored = client.get_membership("ca-bc-2026")
        assert stored is not None
        assert stored.verify()

    def test_full_flow_kyc_plus_feature(self, registry_key, client_keypair,
                                          sample_identity, rng):
        """End-to-end: registry issues cert, feature authority attests,
        client receives, central service aggregates, client gets full proof."""
        cms = CentralMerkleService(depth=10)

        # Registry issues identity.
        agent = RegistryAgent("ca-bc-2026", signing_key=registry_key)
        rec = agent.issue_identity(
            sample_identity, client_keypair.pk, rng=rng,
        )
        cms.enroll_registry("ca-bc-2026", agent.sub_root)

        # Feature authority attests.
        fa = FeatureAuthority("feature:age-over-18")
        fa.attest(rec.M)
        cms.enroll_feature("feature:age-over-18", fa.sub_root)

        # Client receives and verifies.
        client = ClientAgent(client_keypair.sk, client_keypair.pk)
        vi = client.receive_certificate(rec.sealed)
        assert vi is not None

        # Client stores membership proofs.
        client.add_membership("ca-bc-2026", agent.membership_proof(rec.leaf_index))
        client.add_membership("feature:age-over-18",
                              fa.membership_proof_for_identity(rec.M))

        # Client can produce a composed proof.
        kyc_proof = client.get_membership("ca-bc-2026")
        feat_proof = client.get_membership("feature:age-over-18")
        assert kyc_proof is not None
        assert feat_proof is not None

        Mx, My = point_to_words(rec.M)
        composed = cms.composed_proof(
            [("ca-bc-2026", kyc_proof), ("feature:age-over-18", feat_proof)],
            M_x=Mx, M_y=My,
        )
        assert composed.verify()


# ============================================================================
# Full identity issuance (cert + PS credential + NIZK)
# ============================================================================

class TestFullIdentityIssuance:
    """RegistryAgent.issue_full_identity() — certificate + PS credential + NIZK."""

    def test_issue_full_identity(self, registry_key, client_keypair, sample_identity, rng):
        from alberta_buck.wallet.ps import ps_keygen
        ps_kp = ps_keygen(rng=rng)
        agent = RegistryAgent("ca-bc-2026", signing_key=registry_key,
                              ps_keypair=ps_kp)

        rec = agent.issue_full_identity(
            sample_identity, client_kp=client_keypair,
            registrant_addr=0x411ce00000000000000000000000000000411ce,
            rng=rng,
        )
        assert rec.serial == 1
        assert rec.m == identity_scalar(canonical_identity_data(sample_identity))
        assert eq(rec.M, mul(G1, rec.m))

        # Certificate should be decryptable.
        unsealed = unseal_certificate(rec.sealed, client_keypair.sk)
        assert unsealed.verify()

        # The published presentation is NOT a verifiable signature on m (A').
        from alberta_buck.wallet.ps import ps_verify, PSSignature
        assert not ps_verify(ps_kp.pk_X, ps_kp.pk_Y,
                             PSSignature(rec.ps_presentation.A, rec.ps_presentation.B), rec.m)

        # Registration NIZK should verify.
        from alberta_buck.wallet.nizk import registration_verify
        assert registration_verify(
            rec.ps_presentation, rec.E_addr, rec.client_kp.pk,
            ps_kp.pk_X, ps_kp.pk_Y,
            rec.registration_proof, 0x411ce00000000000000000000000000000411ce,
        )

        # Tree should contain the identity.
        assert agent.identity_count == 1
        proof = agent.membership_proof(rec.leaf_index)
        assert proof.verify()
        assert proof.leaf == rec.leaf

    def test_issue_full_without_ps_raises(self, registry_key, client_keypair,
                                            sample_identity, rng):
        agent = RegistryAgent("ca-bc-2026", signing_key=registry_key)
        with pytest.raises(RuntimeError, match="no PS keypair"):
            agent.issue_full_identity(sample_identity, client_kp=client_keypair, rng=rng)

    def test_issue_full_batch(self, registry_key, sample_identity,
                                sample_identity2, rng):
        from alberta_buck.wallet.ps import ps_keygen
        ps_kp = ps_keygen(rng=rng)
        agent = RegistryAgent("ca-bc-2026", signing_key=registry_key,
                              ps_keypair=ps_kp)

        records = agent.issue_full_batch(
            [(sample_identity, None), (sample_identity2, None)],
            registrant_addrs=[0xA11000000000000000000000000000000000A11,
                              0xB0B00000000000000000000000000000000B0B],
            rng=rng,
        )
        assert len(records) == 2
        assert agent.identity_count == 2
        assert records[0].serial == 1
        assert records[1].serial == 2

    def test_full_identity_tree_membership(self, registry_key, sample_identity, rng):
        from alberta_buck.wallet.ps import ps_keygen
        ps_kp = ps_keygen(rng=rng)
        agent = RegistryAgent("ca-bc-2026", signing_key=registry_key,
                              ps_keypair=ps_kp, tree_depth=10)

        # Issue 5 identities, verify each one's Merkle path.
        identities = [
            {"name": f"Person_{i}", "dob": "1990-01-01", "id": f"ID-{i}"}
            for i in range(5)
        ]
        records = []
        for i, fields in enumerate(identities):
            rec = agent.issue_full_identity(
                fields,
                registrant_addr=0x1000000000000000000000000000000000000000 + i,
                rng=rng,
            )
            records.append(rec)

        assert agent.identity_count == 5
        for rec in records:
            proof = agent.membership_proof(rec.leaf_index)
            assert proof.verify()
            assert proof.leaf == rec.leaf
            assert proof.root == agent.sub_root


# ============================================================================
# Vector emission tests
# ============================================================================

class TestRegistryVectors:
    """Forge-compatible JSON vector emission."""

    def test_build_default_vectors(self, rng):
        from alberta_buck.registry.vectors import build_registry_vectors
        rv = build_registry_vectors(seed=0xDEF0)
        data = rv.to_json()

        assert "alice" in data["identities"]
        assert "bob" in data["identities"]
        assert data["identities"]["alice"]["leaf_index"] == 0
        assert data["identities"]["bob"]["leaf_index"] == 1
        assert data["aggregator_root"] is not None
        assert data["registry"]["sub_root"] is not None

        # Each identity should have a KYC membership proof.
        for label in ["alice", "bob"]:
            party = data["identities"][label]
            assert party.get("kyc_membership") is not None, \
                f"{label} missing kyc_membership"
            assert party["kyc_membership"]["root"] == data["registry"]["sub_root"]

    def test_build_with_features(self, rng):
        from alberta_buck.registry.vectors import build_registry_vectors
        rv = build_registry_vectors(
            seed=0x1234,
            identity_specs=[
                ("alice", {"name": "Alice", "age": "30"}, 0xA11CE00000000000000000000000000000A11CE),
                ("bob", {"name": "Bob", "age": "16"}, 0xB0B0000000000000000000000000000000B0B),
            ],
            feature_specs=["feature:age-over-18"],
            feature_assignments={"alice": ["feature:age-over-18"]},
        )
        data = rv.to_json()

        alice = data["identities"]["alice"]
        bob = data["identities"]["bob"]

        # Alice has the feature, Bob does not.
        assert "feature:age-over-18" in alice.get("feature_memberships", {})
        assert "feature:age-over-18" not in bob.get("feature_memberships", {})

        # Both have KYC proofs.
        assert alice.get("kyc_membership") is not None
        assert bob.get("kyc_membership") is not None

        # All sub-trees are enrolled.
        assert len(data["sub_trees"]) == 2  # KYC + 1 feature

    def test_write_to_disk(self, rng, tmp_path):
        from alberta_buck.registry.vectors import build_registry_vectors
        import json

        out = str(tmp_path / "registry")
        rv = build_registry_vectors(seed=0xABBA, output_dir=out)
        files = rv.write_all(out)

        # Verify files exist and are valid JSON.
        for name, path in sorted(files.items()):
            assert os.path.exists(path), f"missing {name}: {path}"
            with open(path) as f:
                data = json.load(f)
            if name == "combined":
                assert "aggregator_root" in data
            elif name == "root":
                assert "identityRoot" in data
            elif name.startswith("identity_"):
                assert "m" in data or "M" in data

        # The combined file should have all identities.
        with open(files["combined"]) as f:
            combined = json.load(f)
        assert len(combined["identities"]) == 2

    def test_vector_membership_verifies(self, rng):
        """The membership proofs emitted as vectors should verify against their
        respective sub-roots and the aggregator root."""
        from alberta_buck.registry.vectors import build_registry_vectors
        from alberta_buck.wallet.poseidon import poseidon

        rv = build_registry_vectors(seed=0xBEEF)
        data = rv.to_json()

        for label, party in data["identities"].items():
            kyc = party["kyc_membership"]
            # Verify the sub-tree path manually.
            cur = int(kyc["leaf"], 16)
            for sib_hex, bit in zip(kyc["siblings"], kyc["index_bits"]):
                sib = int(sib_hex, 16)
                cur = poseidon([cur, sib]) if bit == 0 else poseidon([sib, cur])
            assert cur == int(kyc["root"], 16), \
                f"{label}: kyc path does not reach sub_root"

    def test_cli_generates_output(self, rng, tmp_path):
        """The CLI entrypoint should produce vector files."""
        import subprocess, sys
        out = str(tmp_path / "cli_registry")
        result = subprocess.run(
            [sys.executable, "-m", "alberta_buck.registry.vectors",
             "--seed", "0xCAFE",
             "--identities", "alice,bob",
             "--output", out],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert os.path.exists(os.path.join(out, "registry_vectors.json"))
        assert os.path.exists(os.path.join(out, "identity_root.json"))


# ============================================================================
# Integration: multi-registry + multi-feature
# ============================================================================

class TestMultiRegistryIntegration:
    """Multiple registries and features feeding into a single aggregator."""

    def test_two_registries_one_client(self, rng):
        cms = CentralMerkleService(depth=10)

        # BC registry.
        reg_bc_key = registry_keygen(rng)
        reg_bc = RegistryAgent("ca-bc-2026", signing_key=reg_bc_key)
        client_kp = identity_keygen(rng)
        rec_bc = reg_bc.issue_identity(
            {"name": "Alice", "region": "BC"}, client_kp.pk, rng=rng,
        )

        # Federal registry (same person, different certificate).
        reg_fed_key = registry_keygen(rng)
        reg_fed = RegistryAgent("ca-federal-2026", signing_key=reg_fed_key)
        rec_fed = reg_fed.issue_identity(
            {"name": "Alice", "region": "BC", "nationality": "CA"},
            client_kp.pk, rng=rng,
        )

        # Both registries enroll.
        cms.enroll_registry("ca-bc-2026", reg_bc.sub_root)
        cms.enroll_registry("ca-federal-2026", reg_fed.sub_root)

        # Client can get proofs from either registry.
        client = ClientAgent(client_kp.sk, client_kp.pk)
        client.receive_certificate(rec_bc.sealed)
        client.receive_certificate(rec_fed.sealed)

        assert len(client.identities) == 2

    def test_identity_in_multiple_features(self, rng):
        """One identity can be in many feature trees."""
        cms = CentralMerkleService(depth=10)

        reg_key = registry_keygen(rng)
        agent = RegistryAgent("ca-bc-2026", signing_key=reg_key)
        client_kp = identity_keygen(rng)
        rec = agent.issue_identity(
            {"name": "Alice", "age": 30, "region": "BC"},
            client_kp.pk, rng=rng,
        )

        # Alice qualifies for three features.
        fa_age = FeatureAuthority("feature:age-over-18")
        fa_lic = FeatureAuthority("feature:has-license")
        fa_reg = FeatureAuthority("feature:region-bc")

        fa_age.attest(rec.M)
        fa_lic.attest(rec.M)
        fa_reg.attest(rec.M)

        # Enroll all.
        cms.enroll_registry("ca-bc-2026", agent.sub_root)
        cms.enroll_feature("feature:age-over-18", fa_age.sub_root)
        cms.enroll_feature("feature:has-license", fa_lic.sub_root)
        cms.enroll_feature("feature:region-bc", fa_reg.sub_root)

        # Client can compose any subset.
        client = ClientAgent(client_kp.sk, client_kp.pk)
        client.receive_certificate(rec.sealed)
        client.add_membership("ca-bc-2026", agent.membership_proof(rec.leaf_index))
        client.add_membership("feature:age-over-18",
                              fa_age.membership_proof_for_identity(rec.M))
        client.add_membership("feature:region-bc",
                              fa_reg.membership_proof_for_identity(rec.M))

        # Prove KYC + age + region (3-way AND).
        Mx, My = point_to_words(rec.M)
        composed = cms.composed_proof(
            [("ca-bc-2026", client.get_membership("ca-bc-2026")),
             ("feature:age-over-18", client.get_membership("feature:age-over-18")),
             ("feature:region-bc", client.get_membership("feature:region-bc"))],
            M_x=Mx, M_y=My,
        )
        assert composed.verify()
        assert len(composed.proofs) == 3
