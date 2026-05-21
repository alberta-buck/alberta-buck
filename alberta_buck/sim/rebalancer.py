"""BuckBasketRebalancerAgent -- basket-weight rebalancing arbitrageur.

Simulates what a *correct* BuckBasket would do: monitors all TOKEN/BUCK
pools, compares actual value weights to the basket-defined target weights,
and rebalances by swapping from overweight pools into underweight ones.

The agent never holds BUCK directly -- it uses the Universal Router's
pre-fund route (payerIsUser=false) for 2-hop swaps through the BUCK nexus:
  overweight_token -> BUCK -> underweight_token

Profits come from mean-reversion: selling expensive (overweight) tokens
and buying cheap (underweight) ones.  When prices revert, the rebalancer's
portfolio gains value.  This is the arb edge that a correctly-designed
BuckBasket would split with its direct-mint depositors.
"""

from __future__ import annotations

from alberta_buck.sim.agents import Agent, _register
from alberta_buck.sim.router import quote_path, encode_path


@_register
class BuckBasketRebalancerAgent(Agent):
    """Rebalances TOKEN/BUCK pools toward basket target weights.

    Holds a diversified portfolio of all basket tokens.  Each tick it
    reads pool spot prices, computes the basket-defined target value
    weights vs actual pool compositions, and -- when a pool pair exceeds
    the threshold -- swaps overweight token for underweight token through
    the Universal Router.
    """

    SEED_USDC = 2_000_000 * 10 ** 6     # ~$2M per token (sized in USD so
                                         # all positions are comparable)
    POOL_FRAC_BP = 50                    # cap each fill at 0.5% of pool depth
    REBALANCE_FRAC = 0.50               # move 50% of the deviation per tick
    THRESHOLD = 0.05                     # only act when weight deviation >5%

    def setup(self, d, scenario, rng) -> None:
        super().setup(d, scenario, rng)
        # Fund with all basket tokens proportionally.
        for i in range(len(d.tokens)):
            c = d.tokens[i]
            ref0 = scenario.prices.ref(i, 0)
            seed = self.SEED_USDC * (10 ** d.dec[i]) // ref0
            d.chain.send(c.functions.mint(self.address, seed))
        # Cache basket constituent data (static after addBasketToken).
        self._basket_amount: list[int] = []
        self._load_basket(d)

    def _load_basket(self, d) -> None:
        """Cache basketAmount_i from the on-chain BuckBasket."""
        self._basket_amount = []
        for i in range(len(d.tokens)):
            c = d.basket.functions.constituents(i).call()
            self._basket_amount.append(c[2])  # Constituent.basketAmount

    def _pool_price(self, d, i: int) -> int:
        """Implied BUCK per 1 whole token from the TOKEN/BUCK pool,
        in 18-dec fixed-point.  Returns 0 if the pool has no reserves."""
        tc = d.tokens[i]
        pb = d.pool_buck[i]
        rt = tc.functions.balanceOf(pb).call()
        rb = d.buck.functions.balanceOf(pb).call()
        if rt == 0:
            return 0
        return rb * (10 ** d.dec[i]) // rt

    def act(self, d, scenario, day, tick, ctr) -> None:
        N = len(d.tokens)
        prices = [self._pool_price(d, i) for i in range(N)]
        if any(p == 0 for p in prices):
            return

        # -- target weights from basket definition ---------------------- #
        # targetValue_i = basketAmount_i * poolPrice_i  (proportional)
        target_val = [self._basket_amount[i] * prices[i] for i in range(N)]
        tv_sum = sum(target_val)
        if tv_sum == 0:
            return
        target_w = [v / tv_sum for v in target_val]

        # -- actual weights from pool composition ----------------------- #
        # actualValue_i ~ tokenReserve_i * poolPrice_i  (BUCK value of
        # the token side; should equal BUCK side in equilibrium)
        actual_val = []
        for i in range(N):
            tc = d.tokens[i]
            pb = d.pool_buck[i]
            rt = tc.functions.balanceOf(pb).call()
            actual_val.append(rt * prices[i] // (10 ** d.dec[i]))
        av_sum = sum(actual_val)
        if av_sum == 0:
            return
        actual_w = [v / av_sum for v in actual_val]

        # -- record weights for snapshot ------------------------------- #
        ctr.setdefault("poolWeights", []).append(
            [(float(actual_w[i]), float(target_w[i])) for i in range(N)])

        # -- find max overweight / max underweight --------------------- #
        dev = [actual_w[i] - target_w[i] for i in range(N)]
        ov_i = max(range(N), key=lambda i: dev[i])
        un_i = min(range(N), key=lambda i: dev[i])
        spread = dev[ov_i] - dev[un_i]
        if spread < self.THRESHOLD:
            return

        # -- size the rebalance trade ---------------------------------- #
        # Move REBALANCE_FRAC of the deviation value (in BUCK terms),
        # converted to overweight-token units.
        move_value = int(spread * av_sum * self.REBALANCE_FRAC)
        move_amt = move_value // prices[ov_i] if prices[ov_i] else 0
        if move_amt == 0:
            return

        # Cap at agent's balance.
        bal = d.tokens[ov_i].functions.balanceOf(self.address).call()
        move_amt = min(move_amt, bal)
        # Cap at pool depth fraction.
        pool_tok_res = d.tokens[ov_i].functions.balanceOf(d.pool_buck[ov_i]).call()
        max_amt = pool_tok_res * self.POOL_FRAC_BP // 10_000
        move_amt = min(move_amt, max_amt)
        if move_amt == 0:
            return

        # -- build the 2-hop route: ov -> BUCK -> un ------------------- #
        ov_tok_addr = d.tokens[ov_i].address
        un_tok_addr = d.tokens[un_i].address
        B = d.buck.address
        fb = d.fee_buck
        toks = [ov_tok_addr, fb, B, fb, un_tok_addr]
        hops = [
            (d.pool_buck[ov_i], ov_tok_addr, B, fb),
            (d.pool_buck[un_i], B, un_tok_addr, fb),
        ]

        # Only enter if the fill is profitable (net >0, after fees).
        w3, ab = d.w3, d.erc20_abi
        out_amt = quote_path(w3, ab, hops, move_amt)
        if out_amt == 0:
            return

        self._exec(d, d.tokens[ov_i], move_amt, toks,
                   False, ctr)
        ctr["rebalanceTrades"] = ctr.get("rebalanceTrades", 0) + 1
