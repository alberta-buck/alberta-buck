"""DirectMintAgent -- staggered entry/exit direct-mint LP providers.

The BuckBasket contract now handles the buy-low-sell-high logic internally:
  * depositToken() routes the deposit to the most underweight TOKEN/BUCK pool,
    swapping through the BUCK nexus if the deposited token differs from the
    target pool's token.
  * redeem(receiptId, redeemBp, 0) withdraws from the most OVERWEIGHT pool,
    returns only TOKEN to the user, and keeps profit BUCKs for treasury
    re-investment.  redeemBp=0 means "redeem entire deposit."

Agents fund themselves with a token (cycling through the basket), call
depositToken(), hold, then redeem.  The contract does the rest.

Bootstrap: N initial agents (one per token) enter at day 0-2, naturally
seeding all empty pools.  Subsequent agents enter on a staggered schedule.
"""

from __future__ import annotations

from alberta_buck.sim.agents import Agent, _register


@_register
class DirectMintAgent(Agent):
    """Direct-mint LP provider.  Entry chooses any basket token; the
    BuckBasket contract routes it to the most underweight pool.  Exit
    withdraws from the most overweight pool (TOKEN only return)."""

    SEED_USDC = 2_000_000 * 10 ** 6    # ~$2M per agent (day-0 prices)
    HOLD_MIN = 90                       # minimum hold (days)
    HOLD_MAX = 180                      # maximum hold (days)
    ENTRY_INTERVAL = 30                 # stagger between later agents

    _counter: int = 0                   # class-level instance counter

    def __init__(self, idx: int):
        super().__init__(idx)
        self._entry_day: int | None = None
        self._exit_day: int | None = None
        self._receipt_id: int | None = None
        self._principal_tok: int = 0    # raw token units deposited
        self._principal_buck: int = 0   # BUCK minted at deposit
        self._deposit_token_idx: int | None = None
        self._entered = False
        self._exited = False
        self._seq = DirectMintAgent._counter
        DirectMintAgent._counter += 1

    def setup(self, d, scenario, rng) -> None:
        super().setup(d, scenario, rng)
        N = len(d.tokens)
        seq = self._seq
        # Bootstrap: first N agents (one per token) enter at days 0..N-1.
        # Remainder staggered every ENTRY_INTERVAL after bootstrap.
        if seq < N:
            entry = seq              # days 0, 1, 2 — one per token
        else:
            entry = N + (seq - N) * self.ENTRY_INTERVAL
        hold = self.HOLD_MIN + (seq * 37 + 13) % (self.HOLD_MAX - self.HOLD_MIN)
        self._entry_day = min(entry, scenario.days - 1)
        self._exit_day = min(entry + hold, scenario.days)

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0:
            return
        if not self._entered and day >= self._entry_day:
            self._enter(d, scenario, ctr)
        elif (self._entered and not self._exited
              and self._receipt_id is not None and day >= self._exit_day):
            self._exit(d, ctr)

    def _enter(self, d, scenario, ctr) -> None:
        """Fund with a basket token and deposit.  The contract routes to the
        most underweight pool (buy low)."""
        N = len(d.tokens)
        # Cycle through tokens so bootstrap agents each bring a different one.
        tok_idx = self._seq % N
        tc = d.tokens[tok_idx]
        ref0 = scenario.prices.ref(tok_idx, 0)
        seed = self.SEED_USDC * (10 ** d.dec[tok_idx]) // ref0

        # Mint and approve token; depositToken handles the rest.
        d.chain.send(tc.functions.mint(self.address, seed))
        d.chain.send(tc.functions.approve(d.basket.address, seed),
                     sender=self.account)
        try:
            from alberta_buck.sim.deploy import DEPOSITED_TOPIC
            rcpt = d.chain.send(
                d.basket.functions.depositToken(tc.address, seed, 0),
                sender=self.account)
            for log in rcpt["logs"]:
                if log["topics"][0] == DEPOSITED_TOPIC:
                    self._receipt_id = int.from_bytes(log["topics"][2], "big")
                    break
            if self._receipt_id is not None:
                dep = d.basket.functions.deposits(self._receipt_id).call()
                self._principal_buck = dep[0]    # buckPrincipal
                self._principal_tok = dep[1]      # tokenPrincipal
                self._deposit_token_idx = tok_idx
                self._entered = True
                ctr["dmEntries"] = ctr.get("dmEntries", 0) + 1
                ctr["dmOutstandingBuck"] = (
                    ctr.get("dmOutstandingBuck", 0) + self._principal_buck)
        except Exception as e:
            print(f"[dm-{self.idx}] _enter failed: {e!r}", flush=True)
            self._receipt_id = None

    def deposit_info(self, d) -> tuple | None:
        """Return (token_idx, principal_tok, principal_buck) for LP value."""
        if (self._receipt_id is None or self._deposit_token_idx is None
                or self._exited):
            return None
        return (self._deposit_token_idx,
                self._principal_tok, self._principal_buck)

    def _exit(self, d, ctr) -> None:
        """Redeem the receipt NFT.  Contract withdraws from the most
        overweight pool (sell high), returns only TOKEN."""
        self._exited = True
        if self._receipt_id is None:
            return
        try:
            from alberta_buck.sim.deploy import REDEEMED_TOPIC
            rcpt = d.chain.send(
                d.basket.functions.redeem(self._receipt_id, 0, 0),
                sender=self.account)
            print(f"[dm-{self.idx}] redeemed receiptId={self._receipt_id}",
                  flush=True)
            ctr["dmExits"] = ctr.get("dmExits", 0) + 1
            ctr["dmOutstandingBuck"] = (
                ctr.get("dmOutstandingBuck", 0) - self._principal_buck)
            # Track retained BUCK profit for treasury share.  New event
            # shape (post-#1 fix): Redeemed carries only receipt-level
            # aggregates; per-pool TOKEN payouts are in RedeemedFromPool.
            for log in rcpt["logs"]:
                if log["topics"][0] == REDEEMED_TOPIC:
                    from eth_abi import decode
                    _, retainedBuck, _ = decode(
                        ["uint256", "uint256", "uint256"],
                        log["data"])
                    ctr["treasuryBuck"] = (
                        ctr.get("treasuryBuck", 0) + retainedBuck)
                    break
        except Exception as e:
            print(f"[dm-{self.idx}] redeem failed: {e!r}", flush=True)
            ctr["dmExitFails"] = ctr.get("dmExitFails", 0) + 1
