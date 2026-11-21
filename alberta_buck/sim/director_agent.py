"""The retired keepers' cast slots.

DirectorKeeperAgent poked the pairs rebalance director and executed its advice on the pro-rata
basket; MonetaryKeeperAgent turned the monetary desk's crank (monetaryOperation) on the ops shell and
on the desk beside the earlier equity upkeep. The pro-rata and ops baskets, the earlier upkeep and the
pairs director are retired (owner, 2026-11-20): the basket machine's wheel samples and works its own
desk. FenceKeeperAgent's basket went before them.

The agents stay in the cast and do nothing: an agent's number seeds its identity and its random
stream, so taking one out would renumber every agent after it and no saved world would reproduce.
DirectorKeeperAgent keeps its setup (it funds its own wallet) for the same reason: its transactions
are part of every saved world's history.
"""

from __future__ import annotations

from alberta_buck.sim.agents import Agent, _register


@_register
class DirectorKeeperAgent(Agent):
    """A retired slot: it drove the pairs rebalance director."""

    SEED_USDC = 2_000_000 * 10 ** 6     # ~$2M per token, as it always was

    def setup(self, d, scenario, rng) -> None:
        super().setup(d, scenario, rng)
        for i in range(len(d.tokens)):
            c = d.tokens[i]
            ref0 = scenario.prices.ref(i, 0)
            seed = self.SEED_USDC * (10 ** d.dec[i]) // ref0
            d.chain.send(c.functions.mint(self.address, seed))

    def act(self, d, scenario, day, tick, ctr) -> None:
        return


@_register
class MonetaryKeeperAgent(Agent):
    """A retired slot: it turned the ops desk's crank."""

    def act(self, d, scenario, day, tick, ctr) -> None:
        return


@_register
class FenceKeeperAgent(Agent):
    """A retired slot.  It turned the crank on the Fence basket, which is gone."""

    def act(self, d, scenario, day, tick, ctr) -> None:
        return
