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

import dataclasses
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
    """One cold-cache routing run under `backend`; returns the cache dict."""
    from alberta_buck.sim import identity as idmod
    from alberta_buck.sim.anvil import Anvil
    from alberta_buck.sim.loop import run
    from alberta_buck.sim.scenario import SCENARIOS

    tmp_cache = tmp_path / f"identity-cache-{backend}.json"
    monkeypatch.setattr(idmod, "_CACHE_PATH", tmp_cache)
    monkeypatch.setenv("BUCK_IDENTITY_BACKEND", backend)
    idmod.reset_sim_registry()
    try:
        sc = dataclasses.replace(SCENARIOS["routing"], days=1, ticks_per_day=1)
        with Anvil() as anvil:
            s = run(sc, anvil, verbose=False)
        assert s["all_eoa_verified"], (
            f"[{backend}] an EOA agent failed on-chain registration with"
            " freshly regenerated args"
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
        rec = sim.issue("Farmer", 0, 0xA11CE, rng, chainid=31337)
        assert registration_verify(
            rec.ps_sigma_rerand, rec.E_addr, rec.client_kp.pk,
            sim.ps_keypair.pk_X, sim.ps_keypair.pk_Y,
            rec.registration_proof, 0xA11CE, 31337,
        )
        assert not registration_verify(
            rec.ps_sigma_rerand, rec.E_addr, rec.client_kp.pk,
            sim.ps_keypair.pk_X, sim.ps_keypair.pk_Y,
            rec.registration_proof, 0xA11CE, 1,
        )
    finally:
        reset_sim_registry()


def test_legacy_registration_proof_is_not_a_cache_hit(monkeypatch):
    """A six-field proof must be regenerated, never sent to the v3 ABI."""
    from alberta_buck.sim import identity as idmod

    key = idmod._cache_key(7, "TestAgent", 0, 31337)
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
            rng=idmod.seeded_rng(7), chainid=31337,
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
