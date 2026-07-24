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
        if quote_path(d.w3, d.erc20_abi, hops, move_amt, balance_of) == 0:
            return
        if self._exec(d, d.tokens[sell_i], move_amt, toks, False, ctr):
            ctr["directorTrades"] = ctr.get("directorTrades", 0) + 1
