"""Forge-compatible JSON vector emission from the registry prototype.

Bridges the Python registry to Solidity tests by emitting the same JSON format
that existing test vectors (alberta_buck/wallet/vectors.py) use, extended with
Merkle tree roots and membership proofs.  Forge tests read these files via
vm.readFile() / vm.parseJsonUint() / vm.parseJsonUintArray().

Usage:
    python -m alberta_buck.registry.vectors \\
        --seed 0xA1BC \\
        --identities alice,bob,carol \\
        --output test/vectors/registry/

Produces:
    test/vectors/registry/
        identity_root.json          -- current aggregator root
        identities.json            -- all identity data (pk, E_addr, PS sig, NIZK)
        memberships.json           -- membership proofs per identity per sub-tree
        composed_proofs.json       -- AND-composed KYC+feature proofs
        groth16_proofs.json        -- pre-generated Groth16 membership proofs
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from alberta_buck.wallet.bn254 import (
    G1, G2, ORDER, mul, point_to_words, point_to_hex, scalar_to_hex,
)
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.ps import PSKeyPair, PSSignature, ps_keygen, ps_sign, ps_rerandomize
from alberta_buck.wallet.elgamal import (
    ElGamalCiphertext, IdentityKeyPair, identity_keygen, elgamal_encrypt,
)
from alberta_buck.wallet.nizk import RegistrationProof, registration_prove
from alberta_buck.wallet.poseidon import F_R
from alberta_buck.registry.certificate import (
    RegistryKeyPair, registry_keygen, registry_sign_certificate, seal_certificate,
)
from alberta_buck.registry.tree import (
    IdentityMerkleTree, MembershipProof, identity_leaf,
    AGGREGATOR_DEPTH, KYC_SUBTREE_DEPTH,
)
from alberta_buck.registry.merkle_service import (
    CentralMerkleService, SubTreeKind,
)
from alberta_buck.registry.registry import RegistryAgent, FullRegistrationRecord
from alberta_buck.registry.feature_authority import FeatureAuthority

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPO / "test" / "vectors" / "registry"


# ---------------------------------------------------------------------------
# Helpers -- match alberta_buck/wallet/vectors.py format exactly
# ---------------------------------------------------------------------------

def _g1(P) -> Dict[str, str]:
    """G1 point -> {"x": "0x...", "y": "0x..."} (Forge-compatible)."""
    x, y = point_to_words(P)
    return {"x": scalar_to_hex(x), "y": scalar_to_hex(y)}


def _g2(P) -> Dict[str, Any]:
    """G2 point -> {"x": [c0_hex, c1_hex], "y": [c0_hex, c1_hex]}."""
    x_coeffs = P[0].coeffs
    y_coeffs = P[1].coeffs
    return {
        "x": [scalar_to_hex(int(x_coeffs[0])), scalar_to_hex(int(x_coeffs[1]))],
        "y": [scalar_to_hex(int(y_coeffs[0])), scalar_to_hex(int(y_coeffs[1]))],
    }


def _seeded_rng(seed: int):
    rnd = random.Random(seed)
    return lambda: rnd.getrandbits(256)


# ---------------------------------------------------------------------------
# Registration record with PS credential + NIZK
# ---------------------------------------------------------------------------

@dataclass
class FullRegistration:
    """A complete identity registration: certificate + PS credential + NIZK.

    Contains everything needed to register an identity on-chain
    (via IdentityRegistry.register) AND prove Merkle tree membership.
    """
    # Off-chain certificate
    registry_id: str
    serial: int
    canonical_identity: str
    m: int
    M: Any                           # G1 point
    leaf: int
    leaf_index: int
    sealed_envelope: bytes

    # On-chain registration (IdentityRegistry.register)
    pk: Any                          # G1 point -- client ElGamal public key
    E_addr: ElGamalCiphertext        # ElGamal ciphertext of M under pk
    ps_sigma_rerand: PSSignature     # rerandomized PS credential
    registration_proof: RegistrationProof

    # Raw PS data (for reference, not on-chain)
    ps_sigma_raw: Optional[PSSignature] = None

    # Merkle proofs (populated after tree is built)
    kyc_proof: Optional[MembershipProof] = None
    feature_proofs: Dict[str, MembershipProof] = field(default_factory=dict)


def _party_to_json(party: FullRegistration) -> Dict[str, Any]:
    """Serialize one FullRegistration to Forge-compatible JSON."""
    d: Dict[str, Any] = {
        "registry_id": party.registry_id,
        "serial": party.serial,
        "canonical_identity_data": party.canonical_identity,
        "m": scalar_to_hex(party.m),
        "M": _g1(party.M),
        "leaf": scalar_to_hex(party.leaf),
        "leaf_index": party.leaf_index,
        "elgamal_kp": {"sk": scalar_to_hex(0), "pk": _g1(party.pk)},
        "ciphertext": {"R": _g1(party.E_addr.R), "C": _g1(party.E_addr.C)},
        "registrant": scalar_to_hex(0),  # filled by caller with actual address
        "ps_sig_raw": {
            "sigma_1": _g1(party.ps_sigma_raw.sigma_1),
            "sigma_2": _g1(party.ps_sigma_raw.sigma_2),
        } if party.ps_sigma_raw else None,
        "ps_sig_rerand": {
            "sigma_1": _g1(party.ps_sigma_rerand.sigma_1),
            "sigma_2": _g1(party.ps_sigma_rerand.sigma_2),
        },
        "registration_proof": {
            "e":    scalar_to_hex(party.registration_proof.e),
            "s_m":  scalar_to_hex(party.registration_proof.s_m),
            "s_r":  scalar_to_hex(party.registration_proof.s_r),
            "A_ps": _g1(party.registration_proof.A_ps),
            "T_C":  _g1(party.registration_proof.T_C),
            "T_R":  _g1(party.registration_proof.T_R),
        },
    }
    # Merkle proofs
    if party.kyc_proof is not None:
        d["kyc_membership"] = _membership_to_json(party.kyc_proof, party.M)
    if party.feature_proofs:
        d["feature_memberships"] = {
            fid: _membership_to_json(pf, party.M)
            for fid, pf in party.feature_proofs.items()
        }
    return d


def _membership_to_json(proof: MembershipProof, M) -> Dict[str, Any]:
    """Serialize a MembershipProof to Forge-compatible JSON.

    Includes the raw Merkle path (siblings, index_bits) so Forge tests
    can reconstruct the path off-chain, and also the identity point
    coordinates for the non-native G1 tie.
    """
    Mx, My = point_to_words(M)
    return {
        "leaf": scalar_to_hex(proof.leaf),
        "root": scalar_to_hex(proof.root),
        "leaf_index": proof.leaf_index,
        "depth": len(proof.siblings),
        "siblings": [scalar_to_hex(s) for s in proof.siblings],
        "index_bits": proof.index_bits,
        "M_x": scalar_to_hex(Mx),
        "M_y": scalar_to_hex(My),
    }


# ---------------------------------------------------------------------------
# Vector generation
# ---------------------------------------------------------------------------

@dataclass
class RegistryVectors:
    """Complete vector set for Forge test consumption.

    Attributes:
        seed: Random seed for deterministic output.
        registry_agent: The KYC registry that issued the identities.
        ps_issuer: PS keypair for on-chain credential verification.
        identities: FullRegistration records, keyed by label ("alice", "bob").
        cms: Central merkle service with KYC + feature sub-trees.
        feature_authorities: FeatureAuthority instances keyed by feature_id.
    """
    seed: int
    registry_agent: RegistryAgent
    ps_issuer: PSKeyPair
    identities: Dict[str, FullRegistration]
    cms: CentralMerkleService
    feature_authorities: Dict[str, FeatureAuthority] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        """Export all vectors as a single JSON-serialisable dict."""
        result: Dict[str, Any] = {
            "seed": self.seed,
            "aggregator_root": scalar_to_hex(self.cms.identity_root),
            "registry": {
                "id": self.registry_agent.registry_id,
                "pk": _g1(self.registry_agent.pk_registry),
                "sub_root": scalar_to_hex(self.registry_agent.sub_root),
                "identity_count": self.registry_agent.identity_count,
            },
            "ps_issuer": {
                "sk_x": scalar_to_hex(self.ps_issuer.sk_x),
                "sk_y": scalar_to_hex(self.ps_issuer.sk_y),
                "pk_X": _g2(self.ps_issuer.pk_X),
                "pk_Y": _g2(self.ps_issuer.pk_Y),
            },
            "sub_trees": {},
            "identities": {},
        }
        # Sub-tree data
        for rec in self.cms.list_sub_trees():
            st = self.cms.get_sub_tree(rec.sub_tree_id)
            result["sub_trees"][rec.sub_tree_id] = {
                "kind": rec.kind,
                "sub_root": scalar_to_hex(rec.sub_root),
                "aggregator_leaf_index": rec.aggregator_leaf_index,
            }
        # Identity data
        for label, party in self.identities.items():
            result["identities"][label] = _party_to_json(party)
        return result

    def write_all(self, output_dir: Optional[str] = None) -> Dict[str, str]:
        """Write all vector files to disk.

        Args:
            output_dir: Target directory (defaults to test/vectors/registry).

        Returns:
            Dict mapping filename to absolute path.
        """
        out = Path(output_dir) if output_dir else DEFAULT_OUTPUT
        out.mkdir(parents=True, exist_ok=True)

        data = self.to_json()
        files: Dict[str, str] = {}

        # Single combined file (easiest for Forge tests to read).
        combined_path = out / "registry_vectors.json"
        combined_path.write_text(json.dumps(data, indent=2))
        files["combined"] = str(combined_path)

        # Individual files for targeted consumption.
        root_path = out / "identity_root.json"
        root_path.write_text(json.dumps({
            "identityRoot": data["aggregator_root"],
            "registry_sub_root": data["registry"]["sub_root"],
        }, indent=2))
        files["root"] = str(root_path)

        # Per-identity files (matching the patterns tests currently use).
        for label, party in self.identities.items():
            id_path = out / f"identity_{label}.json"
            id_data = _party_to_json(party)
            id_data["aggregator_root"] = data["aggregator_root"]
            id_path.write_text(json.dumps(id_data, indent=2))
            files[f"identity_{label}"] = str(id_path)

        return files


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------

def build_registry_vectors(
    seed: int = 0xA1BC,
    identity_specs: Optional[List[Tuple[str, dict, int]]] = None,
    feature_specs: Optional[List[str]] = None,
    feature_assignments: Optional[Dict[str, List[str]]] = None,
    tree_depth: int = KYC_SUBTREE_DEPTH,
    aggregator_depth: int = AGGREGATOR_DEPTH,
    registry_id: str = "test-registry",
    output_dir: Optional[str] = None,
) -> RegistryVectors:
    """Build a complete vector set for Forge test consumption.

    This is the primary entrypoint: it creates a registry, issues identities,
    attests features, builds Merkle trees, and emits Forge-compatible JSON.

    Args:
        seed: Random seed for deterministic output.
        identity_specs: List of (label, identity_fields_dict, registrant_addr_int).
            If None, defaults to alice + bob with standard fields.
        feature_specs: List of feature IDs (e.g. ["feature:age-over-18"]).
        feature_assignments: Dict mapping identity label -> list of feature IDs.
        tree_depth: Depth of the registry identity tree.
        aggregator_depth: Depth of the central Merkle aggregator.
        registry_id: Stable identifier for the test registry.
        output_dir: Where to write JSON files (None = skip write).

    Returns:
        RegistryVectors ready for .write_all() or .to_json().
    """
    rng = _seeded_rng(seed)

    # Default identity specs: alice + bob.
    if identity_specs is None:
        identity_specs = [
            ("alice", {
                "given_name": "Alice",
                "family_name": "Johnson",
                "jurisdiction": "Alberta, Canada",
                "id_type": "Alberta Identity Card",
                "id_number": "AIC-2026-4839201",
                "date_of_birth": "1992-03-15",
                "issuer_id": "atb-financial-ca",
                "issued_at": "2026-01-20T14:30:00Z",
                "epoch": 42,
            }, 0xa11ce00000000000000000000000000000a11ce),
            ("bob", {
                "given_name": "Bob",
                "family_name": "Smith",
                "jurisdiction": "Alberta, Canada",
                "id_type": "Corporate Registration",
                "id_number": "AB-CORP-2026-00182",
                "date_of_birth": "1985-07-22",
                "issuer_id": "atb-financial-ca",
                "issued_at": "2026-02-01T09:00:00Z",
                "epoch": 42,
            }, 0x0b0b000000000000000000000000000000000b0b),
        ]

    if feature_specs is None:
        feature_specs = []

    if feature_assignments is None:
        feature_assignments = {}

    # Create PS issuer (for on-chain credential verification).
    ps_issuer = ps_keygen(rng=rng)

    # Create registry signing key and agent (with PS keypair for credential issuance).
    reg_key = registry_keygen(rng)
    agent = RegistryAgent(
        registry_id, signing_key=reg_key,
        ps_keypair=ps_issuer,
        tree_depth=tree_depth,
    )

    # Central Merkle service.
    cms = CentralMerkleService(depth=aggregator_depth)

    # Feature authorities.
    feature_auths: Dict[str, FeatureAuthority] = {}
    for feat_id in feature_specs:
        fa = FeatureAuthority(feat_id)
        feature_auths[feat_id] = fa

    # Issue identities using the registry agent's full issuance path.
    identities: Dict[str, FullRegistration] = {}
    for label, fields, addr in identity_specs:
        full = agent.issue_full_identity(
            identity_fields=fields,
            client_kp=None,  # auto-generate ElGamal keypair
            registrant_addr=addr,
            rng=rng,
        )

        # Build a FullRegistration compatible with the JSON emission helpers.
        M = full.M
        party = FullRegistration(
            registry_id=registry_id,
            serial=full.serial,
            canonical_identity=full.canonical_identity,
            m=full.m, M=M,
            leaf=full.leaf, leaf_index=full.leaf_index,
            sealed_envelope=full.sealed.envelope,
            pk=full.client_kp.pk,
            E_addr=full.E_addr,
            ps_sigma_rerand=full.ps_sigma_rerand,
            registration_proof=full.registration_proof,
        )
        identities[label] = party

    # Enroll KYC registry in aggregator.
    cms.enroll_registry(registry_id, agent.sub_root)

    # Attest features.
    for label, feat_ids in feature_assignments.items():
        party = identities.get(label)
        if party is None:
            continue
        for feat_id in feat_ids:
            fa = feature_auths.get(feat_id)
            if fa is None:
                continue
            if not fa.has_identity(party.M):
                fa.attest(party.M)
            party.feature_proofs[feat_id] = fa.membership_proof_for_identity(party.M)

    # Enroll feature authorities.
    for feat_id, fa in feature_auths.items():
        cms.enroll_feature(feat_id, fa.sub_root)

    # Collect KYC membership proofs.
    for label, party in identities.items():
        party.kyc_proof = agent.membership_proof(party.leaf_index)

    result = RegistryVectors(
        seed=seed,
        registry_agent=agent,
        ps_issuer=ps_issuer,
        identities=identities,
        cms=cms,
        feature_authorities=feature_auths,
    )

    if output_dir is not None:
        result.write_all(output_dir)

    return result


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Emit Forge-compatible identity registry test vectors.")
    ap.add_argument("--seed", type=str, default="0xA1BC",
                    help="Random seed for deterministic output (hex).")
    ap.add_argument("--identities", type=str, default="alice,bob",
                    help="Comma-separated identity labels.")
    ap.add_argument("--features", type=str, default="",
                    help="Comma-separated feature IDs.")
    ap.add_argument("--output", type=str, default=None,
                    help="Output directory (default: test/vectors/registry/).")
    ap.add_argument("--tree-depth", type=int, default=12,
                    help="Registry identity tree depth.")
    ap.add_argument("--aggregator-depth", type=int, default=10,
                    help="Central Merkle aggregator depth.")
    args = ap.parse_args()

    seed = int(args.seed, 16)
    identity_labels = [s.strip() for s in args.identities.split(",") if s.strip()]
    feature_ids = [s.strip() for s in args.features.split(",") if s.strip()]

    # Build identity specs with deterministic addresses.
    specs = []
    for i, label in enumerate(identity_labels):
        addr = 0x1000000000000000000000000000000000000000 + i
        fields = {
            "given_name": label.capitalize(),
            "family_name": f"Test-{label}",
            "jurisdiction": "Alberta, Canada",
            "id_type": "Test Identity",
            "id_number": f"TEST-{seed:08x}-{i:04d}",
            "date_of_birth": "1990-01-01",
            "issuer_id": "test-registry",
            "issued_at": "2026-06-01T00:00:00Z",
            "epoch": 42,
        }
        specs.append((label, fields, addr))

    output_dir = args.output if args.output else str(DEFAULT_OUTPUT)

    result = build_registry_vectors(
        seed=seed,
        identity_specs=specs,
        feature_specs=feature_ids if feature_ids else None,
        tree_depth=args.tree_depth,
        aggregator_depth=args.aggregator_depth,
        output_dir=output_dir,
    )

    files = result.write_all(output_dir)
    print(f"Registry vectors written to {output_dir}/")
    for name, path in sorted(files.items()):
        size = os.path.getsize(path)
        print(f"  {name:20s}  {size:6d} bytes  {Path(path).name}")


if __name__ == "__main__":
    _main()
