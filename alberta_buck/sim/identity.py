"""Real cryptographic identities for the simulation — backed by the registry.

EOA agents get a real IdentityRegistry.register() with an issuer-signed PS
credential + registration NIZK bound to that EOA's address.  Contracts get a
public bindContract Identity.  The identity lifecycle is managed by
alberta_buck.registry.RegistryAgent, which also maintains a Poseidon Merkle
tree of registered identities — giving the sim Merkle membership proofs for
free as a side effect of registration.

Deterministic EOA + registration-args cache (test/vectors/identity-cache.json):
the first run with a given seed incurs the full NIZK-prove cost; subsequent
runs hit the cache (~instant).  Cache key = (seed_hex, class_name, agent_idx).
The cache now also stores Merkle membership data.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Any, Callable, Optional

from eth_account import Account
from eth_account.signers.local import LocalAccount

from alberta_buck.wallet.bn254 import G1, mul, point_to_words, rand_scalar
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.ps import ps_keygen, ps_sign, ps_rerandomize
from alberta_buck.wallet.elgamal import identity_keygen, elgamal_encrypt
from alberta_buck.wallet.nizk import registration_prove
from alberta_buck.registry.certificate import registry_keygen
from alberta_buck.registry.registry import RegistryAgent, FullRegistrationRecord

# bindContract uses the G1 generator (1,2) as a non-zero placeholder pk/E so
# isVerified() is true — exactly what the Forge tests pass (BN254.g1()).
_G = point_to_words(G1)                       # (1, 2)
BIND_PK = _G
BIND_E = (_G, _G)                              # ElGamalCT (R, C)

_CACHE_PATH = Path(__file__).resolve().parents[2] / "test" / "vectors" / "identity-cache.json"
_CACHE_SCHEMA = 3

# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    if _CACHE_PATH.exists():
        try:
            return json.loads(_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _valid_cache_entry(data: Any) -> bool:
    """Return whether ``data`` matches the current registration ABI.

    RegistrationProof grew from six to eight fields in schema 3.  Treat old
    or partially-written entries as cache misses instead of handing a stale
    tuple to web3's ABI encoder.
    """
    return (
        isinstance(data, list)
        and len(data) in (4, 5)
        and isinstance(data[3], list)
        and len(data[3]) == 8
    )


def _save_cache(cache: dict) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(cache))


def _cache_key(seed: int, class_name: str, idx: int, chainid: int = 31337) -> str:
    return f"{seed:08x}:{class_name}:{idx}:v{_CACHE_SCHEMA}:{chainid}"


def _deterministic_key(seed: int, class_name: str, idx: int) -> bytes:
    """Derive a reproducible EOA private key from seed + class + idx."""
    import hashlib
    material = f"{seed}:{class_name}:{idx}".encode()
    h = hashlib.sha256(material).digest()
    h = hashlib.sha256(h + b"eoa").digest()
    return h


# ---------------------------------------------------------------------------
# Registry-backed identity issuance
# ---------------------------------------------------------------------------

class SimRegistry:
    """A persistent registry agent for one simulation run.

    Holds a RegistryAgent with a PS keypair, and accumulates identities
    in its Merkle tree as agents are set up.  After all agents are registered,
    the sub_root can be pushed to a CentralMerkleService for on-chain root
    updates.

    Args:
        seed: Deterministic seed for the registry keypair.
        registry_id: Stable identifier (default: "sim-registry").
        tree_depth: Depth of the identity Merkle tree.
    """

    def __init__(self, seed: int, registry_id: str = "sim-registry",
                 tree_depth: int = 12) -> None:
        rng = seeded_rng(seed)
        self.ps_keypair = ps_keygen(rng=rng)
        sign_key = registry_keygen(rng)
        self.agent = RegistryAgent(
            registry_id,
            signing_key=sign_key,
            ps_keypair=self.ps_keypair,
            tree_depth=tree_depth,
        )
        self._seed = seed
        self._identity_count = 0

    @property
    def sub_root(self) -> int:
        return self.agent.sub_root

    @property
    def identity_count(self) -> int:
        return self._identity_count

    def issue(self, class_name: str, idx: int, eoa_addr: int,
              rng: Callable[[], int], chainid: int) -> FullRegistrationRecord:
        """Issue a full identity for one sim agent.

        Args:
            class_name: Agent class name (used for identity fields).
            idx: Agent index within its class.
            eoa_addr: Ethereum address (uint160) for Fiat-Shamir binding.
            rng: Seeded random generator.
            chainid: Live EVM chain id (Anvil default 31337).  Bound into
                the registration NIZK; must match block.chainid at register().

        Returns:
            FullRegistrationRecord with cert, PS credential, NIZK, and
            Merkle tree position.
        """
        fields = fields_for(class_name, idx)
        rec = self.agent.issue_full_identity(
            identity_fields=fields,
            client_kp=None,  # auto-generate ElGamal keypair
            chainid=chainid,
            registrant_addr=eoa_addr,
            rng=rng,
        )
        self._identity_count += 1
        return rec

    def membership_proof(self, leaf_index: int):
        """Get the Merkle proof for a registered identity."""
        return self.agent.membership_proof(leaf_index)


# Global registry instance (lazily initialized per sim run).
_sim_registry: Optional[SimRegistry] = None


def get_sim_registry(seed: int) -> SimRegistry:
    """Get or create the persistent SimRegistry for this run."""
    global _sim_registry
    if _sim_registry is None:
        _sim_registry = SimRegistry(seed)
    return _sim_registry


def reset_sim_registry() -> None:
    """Reset the global registry (for test isolation)."""
    global _sim_registry
    _sim_registry = None


# ---------------------------------------------------------------------------
# Serialization helpers (maintain backward-compatible wire format)
# ---------------------------------------------------------------------------

def _to_serializable(rec: FullRegistrationRecord) -> list:
    """Convert FullRegistrationRecord to the cached JSON-serialisable form.

    The wire format matches the original register_args() tuple:
        (pk, E_arg, sig_arg, proof_arg, merkle_data)
    where merkle_data is new (leaf_index, leaf, sub_root).
    """
    g1 = lambda P: tuple(point_to_words(P))
    pk = g1(rec.client_kp.pk)
    E_arg = (g1(rec.E_addr.R), g1(rec.E_addr.C))
    sig_arg = (g1(rec.ps_sigma_rerand.sigma_1), g1(rec.ps_sigma_rerand.sigma_2))
    proof_arg = (
        rec.registration_proof.e, rec.registration_proof.s_m,
        rec.registration_proof.s_r, rec.registration_proof.s_sk,
        g1(rec.registration_proof.A_ps),
        g1(rec.registration_proof.T_C),
        g1(rec.registration_proof.T_R),
        g1(rec.registration_proof.T_key),
    )
    merkle_data = (rec.leaf_index, rec.leaf, rec.membership_proof.root if rec.membership_proof else 0)
    return [pk, E_arg, sig_arg, proof_arg, merkle_data]


def _from_serializable(data: list) -> tuple:
    """Reconstruct register_args tuple from cached JSON."""
    def tup(x):
        if isinstance(x, list):
            return tuple(tup(v) for v in x)
        return x
    args = tuple(tup(v) for v in data)
    if len(args) == 5 and isinstance(args[4], tuple) and len(args[4]) == 3:
        # Cache format stores (leaf_index, identity_leaf, sub_root); the
        # on-chain 6-arg register overload expects only identityLeaf.
        return (*args[:4], args[4][1])
    return args


# ---------------------------------------------------------------------------
# Public API (backward-compatible)
# ---------------------------------------------------------------------------

def cached_eoa_setup(seed: int, class_name: str, idx: int, issuer,
                     rng: Callable[[], int], chainid: int) -> tuple[LocalAccount, tuple]:
    """Return (account, register_args) for an agent, using disk cache.

    The EOA private key is deterministic (seed + class + idx), so the address
    is stable across runs.  Registration args are cached per key (including
    chainid, which the NIZK Fiat-Shamir binds); only the first run pays the
    NIZK-prove cost.

    If a SimRegistry is active, delegates to it for identity issuance
    (which also populates the Merkle tree).  Otherwise falls back to the
    original direct PS credential issuance (backward-compatible).
    """
    pk_bytes = _deterministic_key(seed, class_name, idx)
    account = Account.from_key(pk_bytes)
    addr_int = int(account.address, 16)

    cache = _load_cache()
    key = _cache_key(seed, class_name, idx, chainid)
    if key in cache and _valid_cache_entry(cache[key]):
        return account, _from_serializable(cache[key])

    # Generate fresh registration args and cache them.
    reg = get_sim_registry(seed)
    rec = reg.issue(class_name, idx, addr_int, rng, chainid)
    # Attach the membership proof now (tree is current after issuance).
    rec.membership_proof = reg.membership_proof(rec.leaf_index)
    args = _to_serializable(rec)
    cache[key] = args
    _save_cache(cache)
    return account, _from_serializable(args)


def seeded_rng(seed: int) -> Callable[[], int]:
    r = random.Random(seed)
    return lambda: r.getrandbits(256)


def make_issuer(rng: Callable[[], int]):
    """An issuer PS keypair (==> trustIssuer(addr, pspubkey_arg)).

    When using the registry-backed path, prefer get_sim_registry(seed).ps_keypair
    instead — the registry holds the canonical PS keypair for the simulation run.
    """
    return ps_keygen(rng=rng)


def _g2(P) -> tuple:
    x, y = P[0].coeffs, P[1].coeffs
    return ((int(x[0]), int(x[1])), (int(y[0]), int(y[1])))


def pspubkey_arg(issuer) -> tuple:
    """PSPubKey{ G2 X; G2 Y } for IdentityRegistry.trustIssuer."""
    return (_g2(issuer.pk_X), _g2(issuer.pk_Y))


def register_args(issuer, eoa_addr: int, fields: dict,
                   rng: Callable[[], int], chainid: int) -> tuple:
    """Args for IdentityRegistry.register(issuer, pk, E, sigma, proof),
    bound to eoa_addr (must equal the tx sender).

    This is the legacy standalone path — use cached_eoa_setup() which now
    delegates to the SimRegistry for identity issuance and Merkle tree
    integration.  `chainid` must match block.chainid at submit time.
    """
    canonical = canonical_identity_data(fields)
    m = identity_scalar(canonical)
    sigma = ps_sign(issuer, m, rng=rng)
    sigma_p, _ = ps_rerandomize(sigma, rng=rng)
    kp = identity_keygen(rng=rng)
    r = rand_scalar(rng)
    E = elgamal_encrypt(mul(G1, m), kp.pk, r)
    pf = registration_prove(sigma_p, m, r, kp.pk, E, eoa_addr, kp.sk,
                            chainid, rng=rng)

    g1 = lambda P: tuple(point_to_words(P))
    pk = g1(kp.pk)
    E_arg = (g1(E.R), g1(E.C))
    sig_arg = (g1(sigma_p.sigma_1), g1(sigma_p.sigma_2))
    proof_arg = (pf.e, pf.s_m, pf.s_r, pf.s_sk, g1(pf.A_ps), g1(pf.T_C),
                 g1(pf.T_R), g1(pf.T_key))
    return pk, E_arg, sig_arg, proof_arg


def fields_for(name: str, i: int) -> dict:
    """Distinct identity fields per agent (so each m is unique)."""
    return {
        "given_name":    name,
        "family_name":   f"Agent{i:04d}",
        "jurisdiction":  "Alberta, Canada",
        "id_type":       "Alberta Identity Card",
        "id_number":     f"AIC-2026-{i:07d}",
        "date_of_birth": "1990-01-01",
        "issuer_id":     "atb-financial-ca",
        "issued_at":     "2026-01-01T00:00:00Z",
        "epoch":         42,
    }
