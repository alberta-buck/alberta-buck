"""Identity-cache regeneration gate (Phase 3 acceptance).

``test/vectors/identity-cache.json`` is an append-only accumulation across
sim runs and code epochs (65 of its entries still carry the pre-merkle
4-element format), so byte-comparing a fresh regeneration against the
committed file would compare code epochs, not backends.  The invariant the
kernel port must preserve is:

1. Regenerating the cache from scratch is BACKEND-INVARIANT: a cold run
   under ``BUCK_IDENTITY_BACKEND=py`` (the executable spec) and one under
   the compiled kernel produce byte-identical caches -- same draws, same
   proofs, same merkle positions.
2. The freshly regenerated (not cached) registration args VERIFY on-chain:
   the real sim loop registers every EOA agent through
   ``IdentityRegistry.register`` and ``isVerified()`` holds.

Skips cleanly if anvil/web3 or the kernel binding is unavailable.

Registration Fiat-Shamir binds block.chainid.  Sim Anvil/PyrevmAnvil
default to 31337; issuance must use that live chain id (not a hardcoded 1)
or on-chain register reverts `bad FS challenge`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from alberta_buck.wallet import kernel_active

anvil_missing = shutil.which("anvil") is None
web3_missing = False
try:
    import web3  # noqa: F401
except Exception:
    web3_missing = True

_REPO = Path(__file__).resolve().parents[2]


def _cold_run(tmp_path: Path, monkeypatch, backend: str) -> dict:
    """Cold-cache two accounts and register both on the current contract."""
    from alberta_buck.sim import identity as idmod
    from alberta_buck.sim.anvil import Anvil
    from alberta_buck.sim.chain import Chain

    tmp_cache = tmp_path / f"identity-cache-{backend}.json"
    monkeypatch.setattr(idmod, "_CACHE_PATH", tmp_cache)
    monkeypatch.setenv("BUCK_IDENTITY_BACKEND", backend)
    idmod.reset_sim_registry()
    try:
        with Anvil() as anvil:
            gov, issuer_addr = anvil.w3.eth.accounts[:2]
            chain = Chain(anvil.w3, gov)
            registry = chain.deploy("IdentityRegistry", gov)
            sim_registry = idmod.get_sim_registry(7)
            chain.send(
                registry.functions.trustIssuer(
                    issuer_addr, idmod.pspubkey_arg(sim_registry.ps_keypair)
                ),
                sender=gov,
            )

            rng = idmod.seeded_rng(7)
            for idx in range(2):
                account, args = idmod.cached_eoa_setup(
                    7, "CacheGate", idx, sim_registry.ps_keypair, rng,
                    int(anvil.w3.eth.chain_id), int(registry.address, 16),
                )
                anvil.set_balance(account.address, 10**18)
                chain.send(
                    registry.functions.register(issuer_addr, *args),
                    sender=account,
                )
                assert registry.functions.isVerified(account.address).call(), (
                    f"[{backend}] freshly generated registration did not verify"
                )
    finally:
        idmod.reset_sim_registry()
    return json.loads(tmp_cache.read_text())


def test_sim_issue_binds_anvil_default_chainid():
    """Cheap gate: sim issuance must Fiat-Shamir at Anvil's default 31337.

    test_identity_cache_regeneration_backend_invariant is the full on-chain
    check (skipped without anvil).  This test does not need a node.
    """
    from alberta_buck.sim.identity import SimRegistry, reset_sim_registry
    from alberta_buck.sim.identity import seeded_rng
    from alberta_buck.wallet.nizk import registration_verify

    reset_sim_registry()
    try:
        rng = seeded_rng(1)
        sim = SimRegistry(1)
        rec = sim.issue("Farmer", 0, 0xA11CE, rng, chainid=31337,
                        registry=0x1D1D)
        assert registration_verify(
            rec.ps_presentation, rec.E_addr, rec.client_kp.pk,
            sim.ps_keypair.pk_X, sim.ps_keypair.pk_Y,
            rec.registration_proof, 0xA11CE, 31337, 0x1D1D,
        )
        assert not registration_verify(
            rec.ps_presentation, rec.E_addr, rec.client_kp.pk,
            sim.ps_keypair.pk_X, sim.ps_keypair.pk_Y,
            rec.registration_proof, 0xA11CE, 1, 0x1D1D,
        )
    finally:
        reset_sim_registry()


def test_legacy_registration_proof_is_not_a_cache_hit(monkeypatch):
    """A six-field proof must be regenerated, never sent to the current ABI."""
    from alberta_buck.sim import identity as idmod

    key = idmod._cache_key(7, "TestAgent", 0, 31337, 0x1D1D)
    legacy = [
        [1, 2],
        [[1, 2], [1, 2]],
        [[1, 2], [1, 2]],
        [1, 2, 3, [1, 2], [1, 2], [1, 2]],
    ]

    monkeypatch.setattr(idmod, "_load_cache", lambda: {key: legacy})

    class RegenerateSentinel(Exception):
        pass

    def regenerate(_seed):
        raise RegenerateSentinel

    monkeypatch.setattr(idmod, "get_sim_registry", regenerate)
    with pytest.raises(RegenerateSentinel):
        idmod.cached_eoa_setup(
            7, "TestAgent", 0, issuer=None,
            rng=idmod.seeded_rng(7), chainid=31337, registry=0x1D1D,
        )


@pytest.mark.skipif(anvil_missing or web3_missing,
                    reason="anvil or web3 not available")
@pytest.mark.skipif(not kernel_active(),
                    reason="kernel binding not built (make nix-core-build-py)")
def test_identity_cache_regeneration_backend_invariant(tmp_path, monkeypatch):
    fast = _cold_run(tmp_path, monkeypatch, "kernel")
    ref = _cold_run(tmp_path, monkeypatch, "py")

    assert fast, "cold run produced no cache entries"
    assert sorted(fast) == sorted(ref)
    for key in ref:
        assert fast[key] == ref[key], f"backend-divergent cache entry: {key}"
