"""Scenario configuration.

A scenario = token set + price CSVs + agent population + timeline.  New
scenarios (Equilibrium / KArb / Lifecycle) are added as another `Scenario`
instance plus the Agent subclasses they need -- `loop.py`/`snapshot.py`
are scenario-agnostic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from alberta_buck.sim.prices import Prices

# The BuckBasket's monetary-operations desk, run as an agent
# (alberta-buck-operations.org, phase 2).  OFF by default so the committed
# vectors stay the baseline; the comparison is a pair of runs on one seed:
#
#     make nix-sim-rebalancing-revert                       # baseline
#     SIM_MONETARY_OPS=1 make nix-sim-rebalancing-revert    # operations on
#
# One agent, not a population: the basket has one operations desk, and a
# single actor keeps the A/B attributable to the mechanism rather than to a
# crowd of them meeting each other's impact.
MONETARY_OPS = int(os.environ.get("SIM_MONETARY_OPS", "0") or 0)


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
    experiment: Any = None             # attached Experiment (deploy/agent/
                                       # intervention overrides); None = defaults
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
            "DirectMintBuckAgent": 75,
            # The DEMAND leg.  Everyone above is indifferent to what a BUCK
            # is worth -- the DM agents pledge TOKEN, the arbs only chase
            # cross-pool cycles -- so nothing leaned against BUCK drifting
            # off parity.  These compare the whole round trip against leaving
            # the money in USDC and buy when it wins, from a finite budget
            # rather than freshly minted supply.  Both variants run so the
            # holder/basketeer carry asymmetry is visible in one render.
            "DiscountBasketArbAgent": 12,
            "DiscountBuckArbAgent": 4,
            # The SUPPLY side, and the market has no ceiling without it.
            # Every agent above either wants BUCK or ignores it, and the
            # debtors issue on a mortgage schedule rather than because BUCK
            # is dear.  So a sustained bid drove basketValueInBuck to 0.80 in
            # the reverting run, buckK sat on its 0.95 clamp, and every
            # redemption fell into the basket's deflation branch.  These
            # issue BUCK against BuckCredit collateral when it is rich and
            # buy real assets with it, then cover when it returns to parity.
            "BuckIssuerArbAgent": 6,
            # Stablecoin holders who represent their existing custodial
            # insurance as a BuckCredit, mint the BUCK it supports, and LP
            # both sides of BUCK/USDC -- one pile of capital, twice the
            # notional earning fees.  Their concentrated positions are a
            # directional view: basketValueInBuck says which way K is about
            # to push, so they sit on that side and let the flow come to
            # them.  Widths are drawn per agent so they do not reposition in
            # lockstep.
            "BuckPoolInvestorAgent": 8,
            # The ISSUANCE leg, and the reason BUCK_K has anything to act on.
            # buckK reaches the economy only through creditLimit, so without
            # credit borrowers the controller pushes on a channel carrying
            # none of the growth, winds its integral down and sits on the
            # floor -- which is exactly what earlier runs of this scenario
            # showed.  These are the honest debtors: real premiumRate, real
            # funding-factor gate, obligations that are pure chain truth.
            # Sized against the DEMAND flow, not by taste.  buckK sitting on
            # its 0.95 clamp is the controller asking for issuance an economy
            # cannot supply, so the supply side has to be able to answer the
            # arbitrage that keeps it there: at K=0.95 a holder of insured
            # collateral swaps ~2%/yr of real interest cost for a one-time ~1%
            # premium, with no principal schedule -- payback under six months.
            # Nobody leaves that alone, so the sim should not either.
            #
            # 24 debtors x ~$900k collateral is ~$16M of issuance capacity at
            # K=0.75, against the ~$17.8M the demand leg actually bought over
            # 730 days.  The rate is not the constraint (monthly cadence at
            # one year of payments per tranche is ~$21M/yr); the COLLATERAL
            # is, and 4 agents carried under $4M of it.
            "BuckCreditDebtorAgent": 24,
            # Advances the BasketRebalanceDirector's amortized MA signals a
            # bounded slice per tick and executes its advisory efforts
            # (sell-side hint -> BUCK -> buy-side hint) through the router.
            "DirectorKeeperAgent": 1,
            # Turns the crank on BuckBasketOps.monetaryOperation().  Inert
            # unless --basket ops deployed the two-mode shell, so it costs one
            # skipped call a day on every other run and needs no roster switch.
            "MonetaryKeeperAgent": 1,
            # The COMMON mode.  Everything above trades the differences
            # between commodities; this reads their mean -- which is
            # basketValueInBuck, the controller's own process variable -- and
            # runs the four quadrants against it.  SIM_MONETARY_OPS=1.
            **({"MonetaryOpsAgent": MONETARY_OPS} if MONETARY_OPS else {})},
    days=365,
    ticks_per_day=4,
)

# The same population and timeline as REBALANCING, on price series that
# oscillate with the same volatility but carry ZERO net drift and end exactly
# where they begin (alberta_buck/sim/gen_prices.py --regime revert).
#
# The committed trend CSVs bake in +8%/+15%/+2% annual drift, which confounds
# every reversion claim measured against them: a rebalancing premium is a
# statement about harvesting oscillation, and a demand agent is judged on
# buying cheap, but in a market that rises throughout, buy-and-hold beats
# both for a reason unrelated to either mechanism.  Here there is no trend
# left to collect, so whatever a policy or an agent earns, it earned from the
# oscillation.  This is the regime the BuckBasket's charter actually
# describes -- commodities that physics forces to revert.
REBALANCING_REVERT = Scenario(
    name="rebalancing-revert",
    tokens=REBALANCING.tokens,
    csv_files=["paxg-rev.csv", "cbbtc-rev.csv", "aoil-rev.csv"],
    agents=dict(REBALANCING.agents),
    days=REBALANCING.days,
    ticks_per_day=REBALANCING.ticks_per_day,
)

SCENARIOS = {ROUTING.name: ROUTING,
             REBALANCING.name: REBALANCING,
             REBALANCING_REVERT.name: REBALANCING_REVERT}


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
                "DirectMintBuckAgent": 40,
                # Advances the rebalance director's amortized signals a
                # bounded slice per tick and executes its advisory efforts.
                "DirectorKeeperAgent": 1},
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
    eqm.BuckCreditDebtorAgent._arrival_seq = 0
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
