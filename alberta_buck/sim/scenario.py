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
    tokens: list                       # [(symbol, name, decimals[, weightBp]), ...]
    csv_files: list                    # parallel to tokens
    agents: dict                       # {agent_type_name: count}
    days: int = 120
    ticks_per_day: int = 4
    seed: int = 0xA1BC
    day_step: int = 1                  # calendar days advanced per iteration
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
    `years` of available data) over the recomposed USD basket -- LABR (US wage),
    CNST (construction), FOOD (retail food), NRGC (energy), PAXG (gold) and
    cbBTC (bitcoin) -- then returns a Scenario the normal loop/deploy pipeline
    can run.  Weights are left equal (addBasketToken share 0) here; the weighted
    composition lives in build_equilibrium.

    Imported lazily so importing this module never requires the quote source.
    """
    from alberta_buck.sim.gen_historical import gen
    files, n_days, _s, _e = gen(start=start, end=end, years=years)
    return Scenario(
        name="historical",
        tokens=[("LABR", "Labour (US wage)", 18),
                ("CNST", "Construction", 18),
                ("FOOD", "Retail Food", 18),
                ("NRGC", "Energy", 18),
                ("PAXG", "PAX Gold", 18),
                ("cbBTC", "Coinbase Wrapped BTC", 8)],
        csv_files=files,
        agents={"AnonymousArbAgent": 3,
                "TokenAccumulatorAgent": 6,     # one per token
                "MarketMakerWhale": 1,
                "BootstrapDMAgent": 24,
                "DirectMintAgent": 120,
                "DirectMintBuckAgent": 40},
        days=n_days,
        ticks_per_day=ticks_per_day,
        seed=seed,
    )


def build_equilibrium(start=None, end=None, years=None, ticks_per_day=48,
                      seed=0xA1BC):
    """The BUCK-K feedback experiment on REAL historical price feeds.

    Same six tokens + real CSVs as build_historical -- but recomposed toward
    real-economy M2-laggards with EXPLICIT basis-point weights (anchor ~86%:
    LABR 2400 / CNST 2200 / FOOD 2000 / NRGC 2000; satellite ~14%: PAXG 800 /
    cbBTC 600) -- plus the agent population that adds the *monetary* feedback
    the PID defends:

      * whale + arbs + accumulators + bootstrap DMs keep the RWA price
        feeds and TOKEN/BUCK pools live (the exogenous truth channel);
      * FatCreditBorrowerAgents issue/retire BUCK against a pool of
        BuckCredit "properties", their draw gated by the live, K-scaled
        creditLimit -- this pushes basketValueInBuck around;
      * SaverAgents add idle-BUCK demand on the floating BUCK/USDC pool;
      * a PidKeeperAgent advances the controller on the 30-min money tick.

    Equilibrium = basketValueInBuck -> ~1.0 with buckK settled.  The window
    defaults to the last ~1.5 years (shorter than historical so a run
    finishes quickly); `ticks_per_day=48` gives a 30-min money+PID cadence
    (the whale still snaps once/day since the CSVs are daily).

    Imports the equilibrium agents lazily (registers them in the agent
    REGISTRY) and gen_historical lazily so plain imports stay cheap.
    """
    import alberta_buck.sim.equilibrium_agents as eqm  # registers agents
    # Reset per-class regime-slot counters so back-to-back builds in one
    # process assign slots from 0 (loop.py resets DM counters but does not
    # know about these equilibrium agents).
    eqm.FatCreditBorrowerAgent._regime_counter = 0
    eqm.SaverAgent._regime_counter = 0
    from alberta_buck.sim.gen_historical import gen
    files, n_days, _s, _e = gen(start=start, end=end,
                                years=1.5 if years is None else years)
    return Scenario(
        name="equilibrium",
        # (sym, name, decimals, weightBp) -- recomposed M2-laggard basket.
        # Order matches gen_historical BINDINGS (== csv_files order).  Weights
        # sum to 10000 bp; threaded into addBasketToken at deploy.
        tokens=[("LABR", "Labour (US wage)", 18, 2400),
                ("CNST", "Construction", 18, 2200),
                ("FOOD", "Retail Food", 18, 2000),
                ("NRGC", "Energy", 18, 2000),
                ("PAXG", "PAX Gold", 18, 800),
                ("cbBTC", "Coinbase Wrapped BTC", 8, 600)],
        csv_files=files,
        agents={"MarketMakerWhale": 1,
                "AnonymousArbAgent": 3,
                "TokenAccumulatorAgent": 6,      # one per token
                "BootstrapDMAgent": 24,          # seed TOKEN/BUCK pools
                "FatCreditBorrowerAgent": 5,     # K-gated BUCK issuers
                "SaverAgent": 4,                 # basket-anchored BUCK demand
                "PidKeeperAgent": 1},            # advances the PID each tick
        days=n_days,
        ticks_per_day=ticks_per_day,
        seed=seed,
    )
