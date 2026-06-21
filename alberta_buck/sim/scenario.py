"""Scenario configuration.

A scenario = token set + price CSVs + agent population + timeline.  New
scenarios (Equilibrium / KArb / Lifecycle) are added as another `Scenario`
instance plus the Agent subclasses they need -- `loop.py`/`snapshot.py`
are scenario-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alberta_buck.sim.prices import Prices


@dataclass
class Scenario:
    name: str
    tokens: list                       # [(symbol, name, decimals), ...]
    csv_files: list                    # parallel to tokens
    agents: dict                       # {agent_type_name: count}
    days: int = 120
    ticks_per_day: int = 4
    seed: int = 0xA1BC
    prices: Prices = field(init=False)

    def __post_init__(self):
        self.prices = Prices(self.csv_files)
        if self.days > self.prices.days:
            self.days = self.prices.days


ROUTING = Scenario(
    name="routing",
    tokens=[("PAXG", "PAX Gold", 18),
            ("cbBTC", "Coinbase Wrapped BTC", 8),
            ("AOIL", "Alberta Oil", 18)],
    csv_files=["paxg.csv", "cbbtc.csv", "aoil.csv"],
    agents={"AnonymousArbAgent": 3,
            "TokenAccumulatorAgent": 3,    # one per token (idx % N)
            "MarketMakerWhale": 1,
            # Small pinned TOKEN LPs seed the TOKEN/BUCK pools before tick
            # 0 (deposit-once-never-exit), giving arbs real liquidity to
            # route through.  No stochastic churn here -- ROUTING isolates
            # arb dynamics, not LP turnover.
            "BootstrapDMAgent": 12},
    days=120,
    ticks_per_day=4,
)

REBALANCING = Scenario(
    name="rebalancing",
    tokens=[("PAXG", "PAX Gold", 18),
            ("cbBTC", "Coinbase Wrapped BTC", 8),
            ("AOIL", "Alberta Oil", 18)],
    csv_files=["paxg.csv", "cbbtc.csv", "aoil.csv"],
    agents={"AnonymousArbAgent": 3,
            "TokenAccumulatorAgent": 3,
            "MarketMakerWhale": 1,
            # Many smaller pinned TOKEN LPs floor the pools at bootstrap so
            # the arb-stabilization narrative is well-defined from tick 0.
            "BootstrapDMAgent": 24,
            # Smaller stochastic TOKEN LPs: each buys its chosen commodity
            # from the deep TOKEN/USDC pool, then pledges that TOKEN into
            # its own TOKEN/BUCK pool.
            "DirectMintAgent": 300,
            # Smaller stochastic BUCK holders: mint externally backed BUCK
            # and deposit it into the currently most-underweight pool, with
            # BuckBasket enforcing its BUCK->TOKEN slippage guard.
            "DirectMintBuckAgent": 75},
    days=365,
    ticks_per_day=4,
)

SCENARIOS = {ROUTING.name: ROUTING, REBALANCING.name: REBALANCING}
