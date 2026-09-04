"""The sim's chain handle: buck_core's Web3Session under its original name.

The generic session -- send/call/deploy with expect=OK|REVERT declared
expectations, JSONL journaling, LocalAccount + unlocked-account senders,
the memoized balance cache, revert-reason replay -- lives in
core/python/buck_core/session.py (alberta-buck-platform.org, Layer 1).
This module keeps the sim-facing names stable: Chain, load_artifact,
repo_root, and the gas constants.

Importing alberta_buck (this module's parent package) runs the bootstrap
sys.path shim that makes buck_core importable from a repo checkout.
"""

from __future__ import annotations

from buck_core.artifacts import load_artifact, repo_root          # noqa: F401
from buck_core.session import (                                   # noqa: F401
    CALL_GAS, DEPLOY_GAS, Expect, Journal, Web3Session,
)

# Original private names, kept for any straggling imports.
_DEPLOY_GAS = DEPLOY_GAS
_CALL_GAS = CALL_GAS


class Chain(Web3Session):
    """The sim's Web3Session; see buck_core.session for the full API."""

    # -- WAVE3.org decision 8: the pool-state memo (alberta_buck.sim.gauge) -- #

    def pool_state(self, pool: str) -> tuple[int, int, int, str, str]:
        """(sqrtPriceX96, tick, liquidity, token0, token1) of a V3 pool,
        memoized under the balance cache's lifecycle: a hit is exactly as
        fresh as a memoized balanceOf, and clear_balance_cache() drops both.
        The contract wrapper and the immutable token pair are kept for the
        life of the handle."""
        state = self.__dict__
        cache = state.setdefault("_pool_state_cache", {})
        key = pool.lower()
        hit = cache.get(key)
        if hit is None:
            from alberta_buck.sim.gauge import pool_contract, read_pool_state
            contracts = state.setdefault("_pool_contracts", {})
            pc = contracts.get(key)
            if pc is None:
                pc = contracts[key] = pool_contract(self.w3, pool)
            hit = cache[key] = read_pool_state(self.w3, pool, pc)
        return hit

    def clear_balance_cache(self) -> None:
        super().clear_balance_cache()
        cache = self.__dict__.get("_pool_state_cache")
        if cache:
            cache.clear()
