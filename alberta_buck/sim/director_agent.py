"""DirectorKeeperAgent -- drives the BasketRebalanceDirector state machine.

Demonstrates the amortized-activation pattern end-to-end on Anvil: each tick
the keeper calls `director.poke(budget)` with a SMALL work budget (the same
bounded slice any deposit/redeem activation would carry), so the director's
per-pool moving-average signals advance piecemeal as the sim runs.  When the
director's advisory efforts light up, the keeper executes them: a 2-hop
Universal Router swap from the strongest sell-side pool (redeemHint) into the
strongest buy-side pool (depositHint), sized by the advised effort (bp of
basket NAV per epoch).

Contrast with BuckBasketRebalancerAgent, which recomputes instantaneous spot
weights every tick and trades on a 5% threshold: this keeper trades only when
the director's delayed-MA regime logic says an excursion is levelling off (or
the leash binds), so it trades far less often, later, and deeper.
"""

from __future__ import annotations

from alberta_buck.sim.agents import Agent, _register
from alberta_buck.sim.router import quote_path


@_register
class DirectorKeeperAgent(Agent):
    """Pokes the rebalance director and executes its advisory efforts."""

    SEED_USDC = 2_000_000 * 10 ** 6     # ~$2M per token, like the rebalancer
    POKE_BUDGET = 2                      # constituents advanced per activation
    POOL_FRAC_BP = 50                    # cap each fill at 0.5% of pool depth

    def setup(self, d, scenario, rng) -> None:
        super().setup(d, scenario, rng)
        for i in range(len(d.tokens)):
            c = d.tokens[i]
            ref0 = scenario.prices.ref(i, 0)
            seed = self.SEED_USDC * (10 ** d.dec[i]) // ref0
            d.chain.send(c.functions.mint(self.address, seed))

    def _pool_price(self, d, i: int) -> int:
        tc = d.tokens[i]
        pb = d.pool_buck[i]
        rt = d.chain.balance_of(tc, pb)
        rb = d.chain.balance_of(d.buck, pb)
        if rt == 0:
            return 0
        return rb * (10 ** d.dec[i]) // rt

    def act(self, d, scenario, day, tick, ctr) -> None:
        if d.director is None:          # legacy basket: nothing to drive
            return
        if str(getattr(d, "basket_impl", "")).startswith("equity"):
            return                      # the equity basket rebalances itself (its wheel)

        # 1. Advance the state machine by a bounded slice -- the same work
        #    any deposit/redeem activation would carry.
        d.chain.send(d.director.functions.poke(self.POKE_BUDGET),
                     sender=self.account, gas=3_000_000)
        ctr["directorPokes"] = ctr.get("directorPokes", 0) + 1

        # 2. Read the aggregated advice (cheap views).
        sell_i = d.director.functions.redeemHint().call()
        buy_i = d.director.functions.depositHint().call()
        NONE = (1 << 256) - 1
        if sell_i == NONE or buy_i == NONE or sell_i == buy_i:
            return
        effort_bp = -d.director.functions.effortOf(sell_i).call()
        if effort_bp <= 0:
            return

        # 3. Size: effort is bp of basket NAV per epoch; spread one epoch's
        #    advice across the day's ticks.  NAV proxy: 2x sum of pool BUCK.
        N = len(d.tokens)
        nav = 2 * sum(d.chain.balance_of(d.buck, d.pool_buck[i])
                      for i in range(N))
        px = self._pool_price(d, sell_i)
        if px == 0:
            return
        move_value = nav * effort_bp // 10_000 // scenario.ticks_per_day
        move_amt = move_value * (10 ** d.dec[sell_i]) // px
        move_amt = min(move_amt,
                       d.chain.balance_of(d.tokens[sell_i], self.address))
        pool_res = d.chain.balance_of(d.tokens[sell_i], d.pool_buck[sell_i])
        move_amt = min(move_amt, pool_res * self.POOL_FRAC_BP // 10_000)
        if move_amt == 0:
            return

        # 4. Execute: sell_i -> BUCK -> buy_i through the Universal Router.
        s_addr = d.tokens[sell_i].address
        b_addr = d.tokens[buy_i].address
        B = d.buck.address
        fb = d.fee_buck
        toks = [s_addr, fb, B, fb, b_addr]
        hops = [(d.pool_buck[sell_i], s_addr, B, fb),
                (d.pool_buck[buy_i], B, b_addr, fb)]
        balance_of = lambda token, holder: d.chain.balance_of(
            token, holder, d.erc20_abi)
        if quote_path(d.w3, d.erc20_abi, hops, move_amt, balance_of,
                      chain=d.chain) == 0:
            return
        if self._exec(d, d.tokens[sell_i], move_amt, toks, False, ctr):
            ctr["directorTrades"] = ctr.get("directorTrades", 0) + 1


@_register
class MonetaryKeeperAgent(Agent):
    """Drives monetaryOperation() -- the desk's only trigger: on the ops shell
    (the pro-rata basket with the desk inside it), or on the EquityDesk beside
    an equity basket ("equity-ops": the desk's own contract and account, and
    this agent its own invoker).

    The basket is the actor here; this agent just turns the crank, exactly as
    DirectorKeeperAgent does for the rebalancer.  Everything that decides what
    happens -- the common mode, the deadband and leash, persistence, the three
    bounds, which pools get hit -- lives in the contracts.  That is the whole
    point of the ops basket over the MonetaryOpsAgent prototype: the policy is
    on-chain and permissionless, so nothing about the outcome depends on a
    privileged off-chain actor being well behaved.

    The entry point is once-per-director-epoch (86400s), so calling it every
    tick is harmless -- the extra calls revert StepAlreadyDone and are counted
    rather than swallowed.

    Every revert is a legible state and each is counted separately, because
    the interesting failures here are the QUIET ones:

      mkIdle      the desk wanted to absorb and had no TOKEN left to spend.
                  It funds Q1/Q2 from the assets its own issuance bought, so
                  this is the desk out of ammunition -- the state that
                  decides whether it can defend anything.
      mkBound     a bound bit: inventory ceiling, or the cumulative
                  balance-sheet ceiling.  These exist to stop the desk
                  substituting a fast fix for the slow, structural withdrawal
                  of BUCK that K performs through creditLimit, so a run where
                  they never bind has not tested the overlap at all.
      mkNoAdvice  inside the deadband: nothing to do, which is most days --
                  PROVIDED the director is being sampled (below).

    The desk's signal comes from the director, which advances only when
    poked.  DirectorKeeperAgent pokes it on the pro-rata ops basket; it skips
    equity baskets, and a run can cast it away to remove the director's
    rebalancing.  So wherever it will not poke, this agent pokes the director
    itself before asking for advice (mkPokes).  Until 2026-09-30 nothing did,
    and the equity desk read an unsampled director: NoAdvice on every day of
    every equity run, a desk that never operated while looking merely calm.

    Telemetry (ctr): mkQ1..mkQ4 / mkOps / mkIdle / mkBound / mkNoAdvice /
    mkDone / mkPokes / mkCm / mkOutstanding / mkBuckHeld / mkOffset / mkSlippage /
    mk_err.
    """

    # Quadrant index -> counter suffix, matching the article's numbering.
    QUADRANT = {1: "mkQ1", 2: "mkQ2", 3: "mkQ3", 4: "mkQ4"}

    @staticmethod
    def _desk(d):
        """The desk's contract: the EquityDesk when there is one, else the
        ops shell."""
        return d.desk if getattr(d, "desk", None) is not None else d.basket

    @staticmethod
    def _signal_driven(d, scenario) -> bool:
        """Does DirectorKeeperAgent poke the desk's director in this run?  On
        the pro-rata ops basket, when it is cast; never on an equity basket."""
        if str(getattr(d, "basket_impl", "")).startswith("equity"):
            return False
        agents = getattr(scenario, "agents", None) or {}
        return int(agents.get("DirectorKeeperAgent", 0)) > 0

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0 or getattr(d, "basket_impl", "") not in ("ops", "equity-ops"):
            return
        if getattr(d, "director", None) is not None and not self._signal_driven(d, scenario):
            try:
                d.chain.send(d.director.functions.pokeAll(),
                             sender=self.account, gas=6_000_000)
                ctr["mkPokes"] = ctr.get("mkPokes", 0) + 1
            except Exception as e:
                ctr["mk_err"] = repr(e)[:160]
        if getattr(d, "director", None) is not None:
            # The signal itself (the common mode on its 20-day rung, tick*1e9;
            # a view): zero only while the director has never sampled.
            try:
                ctr["mkCm"] = int(d.director.functions.commonMode(2).call())
            except Exception:
                pass
        try:
            q = self._desk(d).functions.monetaryOperation().call(
                {"from": self.address})
        except Exception as e:
            self._classify(ctr, e)
            return
        try:
            d.chain.send(self._desk(d).functions.monetaryOperation(),
                         sender=self.account, gas=6_000_000)
        except Exception as e:
            self._classify(ctr, e)
            return
        ctr["mkOps"] = ctr.get("mkOps", 0) + 1
        key = self.QUADRANT.get(int(q))
        if key:
            ctr[key] = ctr.get(key, 0) + 1
        self._observe(d, ctr)

    def _observe(self, d, ctr) -> None:
        """Book state, read from chain rather than tallied here.  A Python
        tally of an on-chain book drifts the moment anything else touches it,
        and on this branch that mistake has already produced two confident
        wrong readings."""
        try:
            ctr["mkOutstanding"] = int(
                self._desk(d).functions.monetaryOutstanding().call())
            ctr["mkBuckHeld"] = int(
                self._desk(d).functions.monetaryBuckHeld().call())
            # What the desk's own inventory has taken out of the deviation K
            # measures.  If this grows while K stops moving, the desk is
            # suppressing the very forcing its position is a bet on.
            ctr["mkOffset"] = int(
                self._desk(d).functions.monetaryDeviationOffset().call())
            # The desk's remaining ammunition, per pool.  Q1/Q2 spend TOKEN,
            # and "how much is left" is the difference between a desk that is
            # holding station and one that has been spent out.
            ctr["mkTokHeld"] = [
                int(self._desk(d).functions.monetaryTokenHeld(i).call())
                for i in range(len(d.tokens))]
            ctr["mkNavBuck"] = int(
                self._desk(d).functions.monetaryTokenValue().call())
        except Exception as e:
            ctr["mk_err"] = repr(e)[:160]

    # Custom-error SELECTORS.  web3 surfaces an error the ABI cannot decode
    # as a bare 4-byte selector, so matching on the name binned all 60
    # reverts of a smoke run into "other" and hid that the basket simply had
    # no director wired.  Match the selector; keep the name as a fallback.
    SELECTOR = {
        "0x3c430e99": "mkIdle",         # MonetaryIdle()
        "0x2a200215": "mkBound",        # MonetaryBound()
        "0x95a4fa2c": "mkNoAdvice",     # NoAdvice()
        "0x6ce47dce": "mkDone",         # StepAlreadyDone()
        "0xf2365b5b": "mkNoValue",      # NoValue()
        "0x51ed450c": "mkNoDirector",   # DirectorUnset()
        "0x7dd37f70": "mkSlippage",     # Slippage()
    }

    @classmethod
    def _classify(cls, ctr, e) -> None:
        why = repr(e)
        for sel, key in cls.SELECTOR.items():
            if sel in why:
                ctr[key] = ctr.get(key, 0) + 1
                return
        for sig, key in (("MonetaryIdle", "mkIdle"),
                         ("MonetaryBound", "mkBound"),
                         ("NoAdvice", "mkNoAdvice"),
                         ("StepAlreadyDone", "mkDone"),
                         ("NoValue", "mkNoValue"),
                         ("DirectorUnset", "mkNoDirector")):
            if sig in why:
                ctr[key] = ctr.get(key, 0) + 1
                return
        # Selector-only reverts (no ABI decode): record the raw reason rather
        # than dropping it into the generic bucket.
        ctr["mk_err"] = why[:160]
        ctr["mkOtherErr"] = ctr.get("mkOtherErr", 0) + 1


@_register
class FenceKeeperAgent(Agent):
    """Turns the crank on BuckBasketFence.fenceRebalance().

    One constituent per activation -- the same amortization the director uses
    -- because a re-strike burns and re-mints a band and there is no reason
    for one activation to carry all of them.

    Everything that decides what happens is on-chain: the K budget is
    arithmetic on the TWAP, the band centre is the TWAP, and the harvest is
    whatever `collect` returns.  This agent chooses nothing.

    The number to watch is `fkFootprint` against `fkBudget`.  The basket's
    whole claim to helping K rests on the first tracking the second: if the
    footprint stops following the budget down, the K-scaling is not biting
    and this basket is no better than the one it replaces.

    Telemetry (ctr): fkStruck / fkMinted / fkBurned / fkNav / fkShares /
    fkFootprint / fkBudget / fk_err.
    """

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0 or getattr(d, "basket_impl", "") != "fence":
            return
        n = len(d.tokens)
        if n == 0:
            return
        i = day % n                       # amortized: one band per day
        try:
            delta = d.basket.functions.fenceRebalance(i).call(
                {"from": self.address})
        except Exception as e:
            ctr["fk_err"] = repr(e)[:160]
            ctr["fkFailed"] = ctr.get("fkFailed", 0) + 1
            return
        try:
            d.chain.send(d.basket.functions.fenceRebalance(i),
                         sender=self.account, gas=8_000_000)
        except Exception as e:
            ctr["fk_err"] = repr(e)[:160]
            ctr["fkFailed"] = ctr.get("fkFailed", 0) + 1
            return
        ctr["fkStruck"] = ctr.get("fkStruck", 0) + 1
        if delta > 0:
            ctr["fkMinted"] = ctr.get("fkMinted", 0) + int(delta)
        elif delta < 0:
            ctr["fkBurned"] = ctr.get("fkBurned", 0) + int(-delta)
        self._observe(d, ctr)

    def _observe(self, d, ctr) -> None:
        """Read the book from chain.  A Python tally of an on-chain book
        drifts the moment anything else touches it."""
        try:
            ctr["fkNav"] = int(d.basket.functions.fenceNav().call())
            ctr["fkShares"] = int(d.basket.functions.totalShares().call())
            # The footprint is what K is supposed to be able to move: BUCK
            # inside the bands plus whatever is idle at the basket.
            foot = int(d.chain.balance_of(d.buck, d.basket.address))
            for i in range(len(d.tokens)):
                foot += int(d.basket.functions.fenceAmounts(i).call()[0])
            ctr["fkFootprint"] = foot
            ctr["fkBudget"] = int(d.basket.functions.buckBudget().call())
        except Exception as e:
            ctr["fk_err"] = repr(e)[:160]
