"""Steppable sim driver: deploy + agent population + resumable timeline.

`loop.run()` deploys the system and advances the whole day/tick timeline in
one batch call.  `SimDriver` factors that same sequence into an object you
can advance one tick (or one day) at a time, so an interactive front-end
(the curses TUI in `tui.py`) can render live on-chain state *between* steps.

The stepping order is identical to `loop.run()` so a driven run and a batch
run produce the same chain history for a given seed:

  per day:  set ctr['day']/['refUsd']; pick the whale's snap tick + token order
  per tick: warp the clock; (whale snaps every TOKEN/USDC pool on its tick);
            shuffle the arbs; each arb/DM agent acts
  day end:  kctrl.compute(); snapshot.capture()

Nothing here imports curses -- it is plain logic, so it is unit-testable and
reusable by any front-end or batch caller.
"""

from __future__ import annotations

import random

from alberta_buck.sim import identity as idmod
from alberta_buck.sim.agents import REGISTRY, MarketMakerWhale
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.deploy import deploy
import alberta_buck.sim.rebalancer  # noqa: F401  triggers @_register
import alberta_buck.sim.direct_mint  # noqa: F401  triggers @_register
import alberta_buck.sim.director_agent  # noqa: F401  triggers @_register
from alberta_buck.sim.direct_mint import (
    BootstrapDMAgent, DirectMintAgent, DirectMintBuckAgent,
)
from alberta_buck.sim.proxy import ChainProxy
from alberta_buck.sim.snapshot import Snapshotter


class SimDriver:
    """Owns a deployed sim and advances its timeline incrementally.

    Construct it with a live `Anvil` handle (the TUI / caller owns the anvil
    lifecycle); `deploy()` + agent setup + bootstrap all run in `__init__`,
    so the chain is at "tick 0, day 0, nothing acted yet" when it returns.
    """

    def __init__(self, scenario, anvil, basket_impl="prorata", verbose=False):
        self.scenario = scenario
        self.anvil = anvil
        self.basket_impl = basket_impl
        w3 = anvil.w3
        self.w3 = w3
        self.chain = Chain(w3, w3.eth.accounts[0])
        self._rng = idmod.seeded_rng(scenario.seed)
        self._prng = random.Random(scenario.seed)

        self.d = deploy(self.chain, anvil, scenario, self._rng,
                        basket_impl=basket_impl, verbose=verbose)

        # --- build + register the agent population ------------------- #
        # Reset per-class counters so back-to-back runs in one process do
        # not accumulate stale seq numbers (bootstrap token assignment
        # depends on seq < N).
        BootstrapDMAgent._counter = 0
        DirectMintAgent._counter = 0
        DirectMintBuckAgent._counter = 0
        self.agents, idx = [], 0
        for cls_name, n in scenario.agents.items():
            cls = REGISTRY[cls_name]
            for _ in range(n):
                self.agents.append(cls(idx)); idx += 1
        for a in self.agents:
            a.setup(self.d, scenario, self._rng)
        self.whales = [a for a in self.agents
                       if isinstance(a, MarketMakerWhale)]
        self.arbs = [a for a in self.agents if a not in self.whales]

        # --- pre-tick bootstrap phase ------------------------------- #
        self.ctr = {"directTrades": 0, "cycleTrades": 0, "ubTrades": 0}
        for a in self.agents:
            a.bootstrap(self.d, scenario, self.ctr)

        # --- experiment: price overlay + scripted interventions ------ #
        # Mirrors loop.run(): interventions fire at each day start, so a
        # driven (TUI) run reproduces a batch run's schedule.  (day_step
        # coarse mode is a loop.run concern; the driver steps single days.)
        exp = getattr(scenario, "experiment", None)
        self._iv = None
        if exp is not None:
            from alberta_buck.sim.experiment import Interventions, PriceOverlay
            if not isinstance(scenario.prices, PriceOverlay):
                scenario.prices = PriceOverlay(scenario.prices)
            self._iv = Interventions(exp, self.d, scenario, self.agents,
                                     self.arbs, self.ctr, self._rng)

        # --- snapshot + capital baselines --------------------------- #
        self.snap = Snapshotter(self.d, scenario)
        if exp is not None:
            self.snap.meta["experiment"] = exp.resolved()
            if self._iv is not None:
                self.snap.meta["interventions_applied"] = self._iv.applied
        self.init_val = self.snap.agg_value(self.agents, 0)
        self.reb_init = self.snap._agent_value(
            self.agents, 0, "BuckBasketRebalancerAgent")
        self.dm_init = self.snap._agent_value(
            self.agents, 0, ("DirectMintAgent", "DirectMintBuckAgent"))

        # --- async display-read proxy ------------------------------- #
        # The UI reads live state through this (memoized, refreshed off the
        # UI thread).  It is read-only and on its own connection, so it never
        # touches the timeline writes above.
        self.proxy = ChainProxy(self.d)

        # --- timeline cursor ---------------------------------------- #
        self.ts = w3.eth.get_block("latest")["timestamp"] + 10
        self.tick_secs = max(60, 86_400 // scenario.ticks_per_day)
        self.day = 0
        self.tick = 0
        self.done = False
        # Per-day plan (whale snap tick + token order), set at each tick 0.
        self._whale_tick = 0
        self._whale_order: list[int] = []
        self.last_error: str | None = None

    # -- timeline ------------------------------------------------------ #

    @property
    def days(self) -> int:
        return self.scenario.days

    @property
    def ticks_per_day(self) -> int:
        return self.scenario.ticks_per_day

    def _begin_day(self) -> None:
        s = self.scenario
        n_tok = len(self.d.tokens)
        if self._iv is not None:
            self._iv.apply_due(self.day)
        self.ctr["day"] = self.day
        self.ctr["refUsd"] = [s.prices.ref(i, self.day) for i in range(n_tok)]
        self._whale_tick = self._prng.randrange(s.ticks_per_day)
        self._whale_order = list(range(n_tok))
        self._prng.shuffle(self._whale_order)

    def step_tick(self) -> bool:
        """Advance exactly one tick.  Returns True while the sim has more to
        run, False once the final day's final tick has completed."""
        if self.done:
            return False
        d, s, ctr = self.d, self.scenario, self.ctr
        if self.tick == 0:
            self._begin_day()

        self.ts += self.tick_secs
        self.anvil.warp_to(self.ts)
        d.chain.clear_balance_cache()

        if self.tick == self._whale_tick:
            for wagent in self.whales:
                for whale_tok in self._whale_order:
                    wagent.snap(d, s, self.day, whale_tok, ctr)

        order = self.arbs[:]
        self._prng.shuffle(order)
        for a in order:
            try:
                a.act(d, s, self.day, self.tick, ctr)
            except Exception as e:        # one bad agent must not stall the UI
                self.last_error = f"{type(a).__name__}-{a.idx}: {e!r}"[:300]

        self.tick += 1
        if self.tick >= s.ticks_per_day:
            # End of day: settle the controller and capture a snapshot frame.
            try:
                d.chain.send(d.kctrl.functions.compute())
            except Exception:
                pass
            self.snap.capture(self.day, ctr, self.agents,
                              self.init_val, self.reb_init, self.dm_init)
            self.tick = 0
            self.day += 1
            if self.day >= s.days:
                self.done = True
        # The chain advanced: mark every memoized display read stale so the
        # next render refreshes it in the background.
        self.proxy.bump()
        return not self.done

    def step_day(self) -> bool:
        """Advance to the start of the next day (run out the current day)."""
        start_day = self.day
        running = not self.done
        while running and self.day == start_day:
            running = self.step_tick()
        return running

    # -- convenience --------------------------------------------------- #

    @property
    def progress(self) -> float:
        total = max(1, self.days * self.ticks_per_day)
        return (self.day * self.ticks_per_day + self.tick) / total

    def write(self, path=None):
        """Persist captured day-end frames as the standard sim JSON vector."""
        return self.snap.write(path)

    def close(self) -> None:
        """Stop the proxy's background worker (call on UI teardown)."""
        self.proxy.stop()
