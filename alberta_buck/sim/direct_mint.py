"""DirectMintAgent -- staggered entry/exit direct-mint LP providers.

Each agent enters on a scheduled day (staggered every ~30 days), chooses
the most underweight TOKEN/BUCK pool based on basket target weights,
deposits into it via BuckBasket.depositToken(), holds for months, then
redeems via BuckBasket.redeem().

The staggered entry/exit cadence provides natural rebalancing:
  * On entry: agents deposit into underweight pools, bringing them toward
    target by minting BUCK and adding liquidity.
  * On exit: agents redeem their receipts, withdrawing from pools that
    were underweight at entry (and may now be closer to target).  The
    BuckBasket's 50/50 profit split retains half the LP earnings plus
    any rebalancing edge.

Agents never hold BUCK directly and do not trade on the open market.
Their ROI comes from the BuckBasket's accumulated LP fees + rebalancing
profit share returned at redemption.
"""

from __future__ import annotations

from alberta_buck.sim.agents import Agent, _register
from alberta_buck.sim.snapshot import _bal


@_register
class DirectMintAgent(Agent):
    """Staggered direct-mint LP provider with smart pool selection.

    Each instance is assigned an entry_day and a random holding period.
    On entry_day: reads pool value weights, finds the most underweight
    pool, gets funded with that token, and calls depositToken().
    On exit_day: calls redeem() and records the result.
    Between entry and exit: does nothing (act() is a no-op).
    """

    SEED_USDC = 2_000_000 * 10 ** 6    # ~$2M per agent (day-0 prices)
    HOLD_MIN = 90                       # minimum hold (days)
    HOLD_MAX = 180                      # maximum hold (days)
    ENTRY_INTERVAL = 30                 # new agent every N days

    def __init__(self, idx: int):
        super().__init__(idx)
        self._entry_day: int | None = None
        self._exit_day: int | None = None
        self._receipt_id: int | None = None
        self._deposit_token_idx: int | None = None
        self._principal_tok: int = 0    # raw token units deposited
        self._principal_buck: int = 0   # BUCK minted at deposit
        self._entered = False
        self._exited = False

    def setup(self, d, scenario, rng) -> None:
        super().setup(d, scenario, rng)
        # Schedule entry/exit.  First agent enters day 0; subsequent
        # agents are staggered by ENTRY_INTERVAL.  Each holds for a
        # random period.
        entry = (self.idx % 20) * self.ENTRY_INTERVAL  # supports up to 20 agents
        hold = self.HOLD_MIN + (self.idx * 37 + 13) % (self.HOLD_MAX - self.HOLD_MIN)
        self._entry_day = min(entry, scenario.days - 1)
        self._exit_day = min(entry + hold, scenario.days)

    def _pool_price(self, d, i: int) -> int:
        """Implied BUCK per 1 whole token from the TOKEN/BUCK pool."""
        tc = d.tokens[i]
        pb = d.pool_buck[i]
        rt = _bal(tc, pb)
        rb = _bal(d.buck, pb)
        if rt == 0:
            return 0
        return rb * (10 ** d.dec[i]) // rt

    def act(self, d, scenario, day, tick, ctr) -> None:
        # Only act on tick 0 of the relevant day.
        if tick != 0:
            return

        if not self._entered and day >= self._entry_day:
            self._enter(d, scenario, ctr)
        elif (self._entered and not self._exited
              and self._receipt_id is not None and day >= self._exit_day):
            print(f"[dm-{self.idx}] exiting day={day} exit_day={self._exit_day}")
            self._exit(d, ctr)

    def _enter(self, d, scenario, ctr) -> None:
        """Choose the most underweight pool and deposit into it."""
        N = len(d.tokens)

        # Read pool prices and compute target vs actual value weights.
        prices = [self._pool_price(d, i) for i in range(N)]
        if any(p == 0 for p in prices):
            print(f"[dm-{self.idx}] zero prices {prices}")
            return

        target_val = []
        for i in range(N):
            try:
                c = d.basket.functions.constituents(i).call()
                ba = c[2]  # basketAmount
            except Exception:
                ba = 0
            target_val.append(ba * prices[i])
        tv_sum = sum(target_val)
        if tv_sum == 0:
            print(f"[dm-{self.idx}] tv_sum=0 target_val={target_val}")
            return

        actual_val = []
        for i in range(N):
            rt = _bal(d.tokens[i], d.pool_buck[i])
            actual_val.append(rt * prices[i] // (10 ** d.dec[i]))
        av_sum = sum(actual_val)
        if av_sum == 0:
            print(f"[dm-{self.idx}] av_sum=0 actual_val={actual_val}")
            return

        target_w = [v / tv_sum for v in target_val]
        actual_w = [v / av_sum for v in actual_val]
        dev = [actual_w[i] - target_w[i] for i in range(N)]

        # Pick the most underweight pool.
        tgt_idx = min(range(N), key=lambda i: dev[i])

        # Fund with the target token.
        tc = d.tokens[tgt_idx]
        ref0 = scenario.prices.ref(tgt_idx, 0)
        seed = self.SEED_USDC * (10 ** d.dec[tgt_idx]) // ref0
        # Mint can be from anyone; approve + depositToken must be from the
        # agent so transferFrom pulls the agent's tokens.
        d.chain.send(tc.functions.mint(self.address, seed))
        d.chain.send(tc.functions.approve(d.basket.address, seed),
                     sender=self.account)
        try:
            rcpt = d.chain.send(
                d.basket.functions.depositToken(tc.address, seed, 0),
                sender=self.account)
            # Extract receiptId from the Deposited event.
            from alberta_buck.sim.deploy import DEPOSITED_TOPIC
            for log in rcpt["logs"]:
                if log["topics"][0] == DEPOSITED_TOPIC:
                    self._receipt_id = int.from_bytes(log["topics"][2], "big")
                    break
            if self._receipt_id is not None:
                dep = d.basket.functions.deposits(self._receipt_id).call()
                self._principal_tok = dep[1]
                self._principal_buck = dep[2]
                self._deposit_token_idx = tgt_idx
                self._entered = True
                ctr["dmEntries"] = ctr.get("dmEntries", 0) + 1
        except Exception as e:
            print(f"[dm-{self.idx}] _enter failed: {e!r}")
            self._receipt_id = None

    def _exit(self, d, ctr) -> None:
        """Redeem the receipt NFT."""
        self._exited = True
        if self._receipt_id is None:
            return
        try:
            d.chain.send(d.basket.functions.redeem(self._receipt_id, 0),
                         sender=self.account)
            print(f"[dm-{self.idx}] redeemed receiptId={self._receipt_id}")
            ctr["dmExits"] = ctr.get("dmExits", 0) + 1
        except Exception as e:
            print(f"[dm-{self.idx}] redeem failed: {e!r}")
            ctr["dmExitFails"] = ctr.get("dmExitFails", 0) + 1
