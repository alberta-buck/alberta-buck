"""Real cryptographic identities for the simulation — backed by the registry.

EOA agents get a real IdentityRegistry.register() with an issuer-signed PS
credential + registration NIZK bound to that EOA's address.  Contracts get a
public bindContract Identity.  The identity lifecycle is managed by
alberta_buck.registry.RegistryAgent, which also maintains a Poseidon Merkle
tree of registered identities — giving the sim Merkle membership proofs for
free as a side effect of registration.

Deterministic EOA + registration-args cache (test/vectors/identity-cache.json,
local and untracked): the first run with a given seed pays the NIZK-prove cost
(~5 ms per registration on the kernel, ~0.2 s on py_ecc); later runs hit the
cache.  Entries are deterministic, so deleting the file costs only time.  Cache
key = (seed, class_name, agent_idx, schema, chainid, registry); each entry also
carries its Merkle membership data.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from eth_account import Account
from eth_account.signers.local import LocalAccount

from alberta_buck.wallet.bn254 import G1, mul, point_to_words, rand_scalar
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.ps import ps_keygen, ps_sign, ps_present
from alberta_buck.wallet.elgamal import identity_keygen, elgamal_encrypt
from alberta_buck.wallet.nizk import registration_prove
from alberta_buck.registry.certificate import registry_keygen
from alberta_buck.registry.registry import RegistryAgent, FullRegistrationRecord

# Legacy placeholder (pk, E) = G1 generator.  Production bindContract no
# longer accepts these from an unregistered caller; use bind_as_operator
# after the sender has registered.
_G = point_to_words(G1)                       # (1, 2)
BIND_PK = _G
BIND_E = (_G, _G)                              # ElGamalCT (R, C)

_CACHE_PATH = Path(__file__).resolve().parents[2] / "test" / "vectors" / "identity-cache.json"
# Schema 6 is protocol v2: the registration transcript's domain tag and the
# identity scalar's tag both changed, so every schema-5 proof is structurally
# valid and cryptographically stale -- it would revert `bad FS challenge`.  The
# bump turns those entries into misses, and the next save drops them.
_CACHE_SCHEMA                   = 6

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

    RegistrationProof grew from six to eight fields in schema 3 and to nine
    (A' presentation: C1 replaces A_ps, s_b added) in schema 5.  Treat old
    or partially-written entries as cache misses instead of handing a stale
    tuple to web3's ABI encoder.  Schema 6 kept the shape and changed the
    transcript, which is why the schema number -- part of every key -- is
    what retires those entries, not this check.
    """
    return (
        isinstance(data, list)
        and len(data) in (4, 5)
        and isinstance(data[3], list)
        and len(data[3]) == 9
    )


def _save_cache(cache: dict) -> None:
    """Keep only current-schema entries, and replace the file atomically.

    Parallel runs (sim matrix jobs) share this file: each sees either the old
    cache or the new one, never a torn write that would load as empty.  The
    last writer wins, and an entry it drops is only a later miss.
    """
    live = {k: v for k, v in cache.items() if f":v{_CACHE_SCHEMA}:" in k}
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_CACHE_PATH.parent, prefix=_CACHE_PATH.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(live, f)
        os.replace(tmp, _CACHE_PATH)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _cache_key(seed: int, class_name: str, idx: int, chainid: int = 31337,
               registry: int = 0) -> str:
    return f"{seed:08x}:{class_name}:{idx}:v{_CACHE_SCHEMA}:{chainid}:{registry:040x}"


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
              rng: Callable[[], int], chainid: int,
              registry: int) -> FullRegistrationRecord:
        """Issue a full identity for one sim agent.

        Args:
            class_name: Agent class name (used for identity fields).
            idx: Agent index within its class.
            eoa_addr: Ethereum address (uint160) for Fiat-Shamir binding.
            rng: Seeded random generator.
            chainid: Live EVM chain id (Anvil default 31337).  Bound into
                the registration NIZK; must match block.chainid at register().
            registry: Live IdentityRegistry contract address.

        Returns:
            FullRegistrationRecord with cert, PS credential, NIZK, and
            Merkle tree position.
        """
        fields = fields_for(class_name, idx)
        rec = self.agent.issue_full_identity(
            identity_fields=fields,
            client_kp=None,  # auto-generate ElGamal keypair
            chainid=chainid,
            registry_addr=registry,
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
    sig_arg = (g1(rec.ps_presentation.A), g1(rec.ps_presentation.B))
    proof_arg = (
        rec.registration_proof.e, rec.registration_proof.s_m,
        rec.registration_proof.s_b,
        rec.registration_proof.s_r, rec.registration_proof.s_sk,
        g1(rec.registration_proof.C1),
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
    # Cache may store merkle (leaf_index, identity_leaf, sub_root) or a
    # bare identityLeaf.  On-chain register refuses unconstrained leaves,
    # so callers always get the 5-arg (pk, E, sigma, proof) tuple.
    if len(args) >= 5:
        return args[:4]
    return args


# ---------------------------------------------------------------------------
# Public API (backward-compatible)
# ---------------------------------------------------------------------------

def cached_eoa_setup(seed: int, class_name: str, idx: int, issuer,
                     rng: Callable[[], int], chainid: int,
                     registry: int) -> tuple[LocalAccount, tuple]:
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
    key = _cache_key(seed, class_name, idx, chainid, registry)
    if key in cache and _valid_cache_entry(cache[key]):
        return account, _from_serializable(cache[key])

    # Generate fresh registration args and cache them.
    reg = get_sim_registry(seed)
    rec = reg.issue(class_name, idx, addr_int, rng, chainid, registry)
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
    """PSPubKey{ G2 X; G2 Y; G1 Y1 } for IdentityRegistry.trustIssuer."""
    return (_g2(issuer.pk_X), _g2(issuer.pk_Y), tuple(point_to_words(issuer.pk_Y1)))


def register_args(issuer, eoa_addr: int, fields: dict,
                   rng: Callable[[], int], chainid: int,
                   registry: int) -> tuple:
    """Args for IdentityRegistry.register(issuer, pk, E, presentation, proof),
    bound to eoa_addr (must equal the tx sender).

    This is the legacy standalone path — use cached_eoa_setup() which now
    delegates to the SimRegistry for identity issuance and Merkle tree
    integration.  `chainid` must match block.chainid at submit time.
    """
    canonical = canonical_identity_data(fields)
    m = identity_scalar(canonical)
    sigma = ps_sign(issuer, m, rng=rng)
    pres, _a, b = ps_present(sigma, issuer.pk_Y1, rng=rng)
    kp = identity_keygen(rng=rng)
    r = rand_scalar(rng)
    E = elgamal_encrypt(mul(G1, m), kp.pk, r)
    pf = registration_prove(pres, b, m, r, kp.pk, E, eoa_addr, kp.sk,
                            chainid, rng=rng, registry=registry)

    g1 = lambda P: tuple(point_to_words(P))
    pk = g1(kp.pk)
    E_arg = (g1(E.R), g1(E.C))
    sig_arg = (g1(pres.A), g1(pres.B))
    proof_arg = (pf.e, pf.s_m, pf.s_b, pf.s_r, pf.s_sk, g1(pf.C1), g1(pf.T_C),
                 g1(pf.T_R), g1(pf.T_key))
    return pk, E_arg, sig_arg, proof_arg


def register_deployer(chain, reg, issuer_addr, issuer, rng, chainid: int,
                      sender=None, class_name: str = "Deployer") -> tuple:
    """Register `sender` (default chain.deployer) with a real PS credential.

    Returns (pk, E) suitable for subsequent bind_as_operator calls.
    """
    sender = sender if sender is not None else chain.deployer
    addr = sender.address if hasattr(sender, "address") else sender
    args = register_args(issuer, int(addr, 16), fields_for(class_name, 0),
                         rng, chainid, int(reg.address, 16))
    chain.send(reg.functions.register(issuer_addr, *args), sender=sender)
    return args[0], args[1]


def bind_as_operator(chain, reg, target, is_public: bool = True,
                     is_carrying: bool = True, sender=None) -> None:
    """Certified-operator bind: copy the already-registered sender's (pk, E)."""
    sender = sender if sender is not None else chain.deployer
    addr = sender.address if hasattr(sender, "address") else sender
    pk = reg.functions.pkOf(addr).call()
    E = reg.functions.ciphertextOf(addr).call()
    chain.send(reg.functions.bindContract(target, pk, E, is_public, is_carrying),
               sender=sender)


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
