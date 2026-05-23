"""DirectMintAgent -- multi-cycle direct-mint LP providers.

Each agent cycles through ~4 entry/exit rounds spread across the
simulation horizon.  On entry it deposits into the most underweight
pool (buy low); on exit it redeems from the most overweight pool
(sell high).  The contract retains profit BUCKs for treasury compounding.

Bootstrap: the first N agents (one per basket token) seed empty pools
before tick 0.  All agents then cycle for the remainder of the sim.
"""

from __future__ import annotations

from alberta_buck.sim.agents import Agent, _register


@_register
class DirectMintAgent(Agent):
    """Multi-cycle direct-mint LP provider.

    Each agent is assigned a schedule of (entry_day, exit_day) slots
    covering the full simulation horizon.  It enters at each entry day,
    holds, exits at the exit day, then repeats until its schedule is
    exhausted."""

    SEED_USDC = 2_000_000 * 10 ** 6    # ~$2M per round (day-0 prices)
    CYCLES = 4                          # rounds per agent
    _counter: int = 0

    def __init__(self, idx: int):
        super().__init__(idx)
        self._schedule: list[tuple[int, int]] = []  # (entry_day, exit_day)
        self._cycle_idx: int = 0
        self._receipt_id: int | None = None
        self._principal_tok: int = 0
        self._principal_buck: int = 0
        self._deposit_token_idx: int | None = None
        self._entered: bool = False
        self._exited: bool = False
        self._seq = DirectMintAgent._counter
        DirectMintAgent._counter += 1

    def setup(self, d, scenario, rng) -> None:
        super().setup(d, scenario, rng)
        N = len(d.tokens)
        seq = self._seq
        days = scenario.days

        # Phase-shift so agents are spread evenly across the horizon.
        total = max(DirectMintAgent._counter, 1)
        offset = int(days * seq / total)

        for r in range(self.CYCLES):
            # Entry: spread rounds across the agent's window.
            t0 = offset + int((days - offset) * r / self.CYCLES)
            t0 = min(t0, days - 1)
            # Hold: 1/4 to 1/2 of remaining horizon.
            remaining = max(days - t0, 30)
            hold = remaining * (50 + (seq + r * 7) % 30) // 100
            hold = max(hold, max(10, days // (self.CYCLES * 2)))
            t1 = min(t0 + hold, days)
            self._schedule.append((t0, t1))

        # Bootstrap agents (seq < N): ensure first round is at day 0.
        if seq < N and len(self._schedule) > 0:
            self._schedule[0] = (0, self._schedule[0][1])

    def bootstrap(self, d, scenario, ctr) -> None:
        """Bootstrap path: deposit before tick 0."""
        if self._cycle_idx >= len(self._schedule):
            return
        entry_day, _ = self._schedule[self._cycle_idx]
        if entry_day == 0 and not self._entered:
            self._enter(d, scenario, ctr)

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0:
            return
        if self._cycle_idx >= len(self._schedule):
            return
        entry_day, exit_day = self._schedule[self._cycle_idx]

        if not self._entered and day >= entry_day:
            self._enter(d, scenario, ctr)
        elif (self._entered and not self._exited
              and self._receipt_id is not None
              and day >= exit_day):
            self._exit(d, ctr)
            # Advance to next cycle.
            self._cycle_idx += 1
            self._entered = False
            self._exited = False
            self._receipt_id = None
            self._principal_tok = 0
            self._principal_buck = 0
            self._deposit_token_idx = None

    def _enter(self, d, scenario, ctr) -> None:
        """Fund with a basket token and deposit via BuckBasket."""
        N = len(d.tokens)
        tok_idx = self._seq % N
        tc = d.tokens[tok_idx]
        ref0 = scenario.prices.ref(tok_idx, 0)
        seed = self.SEED_USDC * (10 ** d.dec[tok_idx]) // ref0

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
                self._principal_buck = dep[0]
                self._principal_tok = dep[1]
                self._deposit_token_idx = tok_idx
                self._entered = True
                ctr["dmEntries"] = ctr.get("dmEntries", 0) + 1
                ctr["dmOutstandingBuck"] = (
                    ctr.get("dmOutstandingBuck", 0) + self._principal_buck)
                ctr["dmTotalInvested"] = (
                    ctr.get("dmTotalInvested", 0) + seed)
        except Exception as e:
            print(f"[dm-{self.idx}] _enter failed: {e!r}", flush=True)
            self._receipt_id = None

    def deposit_info(self, d) -> tuple | None:
        if (self._receipt_id is None or self._deposit_token_idx is None
                or self._exited):
            return None
        return (self._deposit_token_idx,
                self._principal_tok, self._principal_buck)

    def _exit(self, d, ctr) -> None:
        """Redeem the receipt NFT.  Tracks treasury retainedBuck from
        Redeemed event and total TOKEN returned from RedeemedFromPool."""
        self._exited = True
        if self._receipt_id is None:
            return
        try:
            from eth_abi import decode
            from alberta_buck.sim.deploy import REDEEMED_TOPIC
            from web3 import Web3
            RFP_TOPIC = Web3.keccak(text=(
                "RedeemedFromPool(uint256,address,address,"
                "uint256,uint256,uint128)"))
            rcpt = d.chain.send(
                d.basket.functions.redeem(self._receipt_id, 0, 0),
                sender=self.account)
            print(f"[dm-{self.idx}] redeemed receiptId={self._receipt_id}",
                  flush=True)
            ctr["dmExits"] = ctr.get("dmExits", 0) + 1
            ctr["dmOutstandingBuck"] = (
                ctr.get("dmOutstandingBuck", 0) - self._principal_buck)
            for log in rcpt["logs"]:
                if log["topics"][0] == RFP_TOPIC:
                    tokToUser, _, _ = decode(
                        ["uint256", "uint256", "uint128"],
                        log["data"])
                    ctr["dmTotalReturned"] = (
                        ctr.get("dmTotalReturned", 0) + tokToUser)
                elif log["topics"][0] == REDEEMED_TOPIC:
                    _, retainedBuck, _ = decode(
                        ["uint256", "uint256", "uint256"],
                        log["data"])
                    ctr["treasuryBuck"] = (
                        ctr.get("treasuryBuck", 0) + retainedBuck)
        except Exception as e:
            print(f"[dm-{self.idx}] redeem failed: {e!r}", flush=True)
            ctr["dmExitFails"] = ctr.get("dmExitFails", 0) + 1
