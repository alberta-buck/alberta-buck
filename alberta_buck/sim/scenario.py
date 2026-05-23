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
            "TokenAccumulatorAgent": 3,   # one per token (idx % N)
            "MarketMakerWhale": 1},
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
            # First 3 (one per token) bootstrap before tick 0; remainder
            # enter weekly (ENTRY_INTERVAL=7) with 30–90-day holds, so
            # the sim sees ~50 entries + ~50 exits — enough churn to
            # observe treasury share accumulating over the year.
            "DirectMintAgent": 50},
    days=365,
    ticks_per_day=4,
)

SCENARIOS = {ROUTING.name: ROUTING, REBALANCING.name: REBALANCING}
