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
            # One BootstrapDMAgent per token seeds the TOKEN/BUCK pools
            # before tick 0 (deposit-once-never-exit), giving arbs real
            # liquidity to route through.  No stochastic churn here --
            # ROUTING isolates arb dynamics, not LP turnover.
            "BootstrapDMAgent": 3},
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
            # Three pinned LPs floor the pools at bootstrap so the
            # arb-stabilization narrative is well-defined from tick 0.
            "BootstrapDMAgent": 3,
            # 50 stochastic LPs: per-tick Bernoulli enter/exit with tuned
            # probabilities (see DirectMintAgent.{ENTER,EXIT}_PROB).  These
            # agents enter with random commodity TOKENs, so entry LPs into
            # that token's own pool; the basket-side rebalance effect comes
            # from redemption allocation selling overweight pools.
            "DirectMintAgent": 50},
    days=365,
    ticks_per_day=4,
)

SCENARIOS = {ROUTING.name: ROUTING, REBALANCING.name: REBALANCING}
