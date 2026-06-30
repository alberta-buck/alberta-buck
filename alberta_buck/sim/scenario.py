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


def build_historical(start=None, end=None, years=5.0, ticks_per_day=1,
                     seed=0xA1BC):
    """Rebalancing-style scenario backed by REAL historical data.

    Generates daily CSVs (gen_historical) for a window (default: the last
    `years` of available data) over PAXG (gold, USD), cbBTC (bitcoin, USD),
    NRGC (energy, CAD) and LABR (labour, CAD), then returns a Scenario the
    normal loop/deploy pipeline can run.  The basket targets equal shares
    (addBasketToken share 0) and pools seed to a common quote-depth, so the
    basket starts at equal weights by value on the start day.

    Imported lazily so importing this module never requires the quote source.
    """
    from alberta_buck.sim.gen_historical import gen
    files, n_days, _s, _e = gen(start=start, end=end, years=years)
    return Scenario(
        name="historical",
        tokens=[("PAXG", "PAX Gold", 18),
                ("cbBTC", "Coinbase Wrapped BTC", 8),
                ("NRGC", "Energy (CAD)", 18),
                ("LABR", "Labour (CAD)", 18)],
        csv_files=files,
        agents={"AnonymousArbAgent": 3,
                "TokenAccumulatorAgent": 4,     # one per token
                "MarketMakerWhale": 1,
                "BootstrapDMAgent": 24,
                "DirectMintAgent": 120,
                "DirectMintBuckAgent": 40},
        days=n_days,
        ticks_per_day=ticks_per_day,
        seed=seed,
    )
