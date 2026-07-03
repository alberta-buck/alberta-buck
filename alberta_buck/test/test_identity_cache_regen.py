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
