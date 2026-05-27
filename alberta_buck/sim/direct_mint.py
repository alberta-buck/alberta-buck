"""DirectMint agents -- probabilistic direct-mint LP providers.

Two concrete agent classes share the deposit/redeem machinery:

- ``BootstrapDMAgent`` deposits once at bootstrap (before tick 0) into a
  fixed token (one agent per basket token) and never exits.  Used to seed
  empty BuckBasket TOKEN/BUCK pools so any scenario that depends on
  TOKEN/BUCK liquidity (e.g. routing-arb tests) has live pools by tick 0.

- ``DirectMintAgent`` makes per-tick statistical decisions: when IDLE,
  enter the basket with probability ``ENTER_PROB_PER_TICK``; when
  ENTERED, exit with probability ``EXIT_PROB_PER_TICK``.  Each agent
  picks a random target token at entry time.  The population produces a
  Poisson-ish arrival process and a geometric holding-time distribution
  without any global schedule.

Each agent's RNG is seeded deterministically from ``(scenario.seed,
agent.idx)`` so runs are reproducible.

The deposit/redeem path goes through ``BuckBasket.depositToken`` /
``BuckBasket.redeem`` -- the canonical Phase 1b way to put real backing
TOKEN into a TOKEN/BUCK pool (BuckBasket mints fresh BUCK against the
deposit via ``Buck.mintFromBasket``, no NFT-credit machinery involved).
"""

from __future__ import annotations

import random

from alberta_buck.sim.agents import Agent, _register


class _DMBase(Agent):
    """Shared deposit/redeem state + helpers for DM-family agents.

    Subclasses choose when to call ``_enter`` / ``_exit``; this base
    provides the BuckBasket plumbing and the event-decoded counter
    bookkeeping (``ctr['dmEntries']``, ``ctr['dmOutstandingBuck']``,
    ``ctr['treasuryBuck']``, etc.) the loop's teardown and summary
    expect.
    """

    SEED_USDC = 2_000_000 * 10 ** 6    # ~$2M per round (day-0 prices)

    def __init__(self, idx: int):
        super().__init__(idx)
        self._receipt_id: int | None = None
        self._principal_tok: int = 0
        self._principal_buck: int = 0
        self._deposit_token_idx: int | None = None
        self._entered: bool = False
        self._exited: bool = False

    def deposit_info(self, d) -> tuple | None:
        if (self._receipt_id is None or self._deposit_token_idx is None
                or self._exited):
            return None
        return (self._deposit_token_idx,
                self._principal_tok, self._principal_buck)

    def _enter(self, d, scenario, ctr, tok_idx: int) -> None:
        """Mint TOKEN to self, approve BuckBasket, depositToken."""
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
                self._exited = False
                ctr["dmEntries"] = ctr.get("dmEntries", 0) + 1
                ctr["dmOutstandingBuck"] = (
                    ctr.get("dmOutstandingBuck", 0) + self._principal_buck)
                ctr["dmTotalInvested"] = (
                    ctr.get("dmTotalInvested", 0) + seed)
        except Exception as e:
            print(f"[{type(self).__name__.lower()}-{self.idx}] _enter failed: {e!r}",
                  flush=True)
            self._receipt_id = None

    def _exit(self, d, ctr) -> None:
        """Redeem the receipt NFT.  Tracks treasury retainedBuck and the
        TOKEN returned to the holder."""
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
            print(f"[{type(self).__name__.lower()}-{self.idx}] "
                  f"redeemed receiptId={self._receipt_id}", flush=True)
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
            # Reset state so the agent can re-enter on a later tick
            # (stochastic DMs).  Bootstrap DMs override _exit to a no-op
            # so this path is never hit.
            self._receipt_id = None
            self._principal_tok = 0
            self._principal_buck = 0
            self._deposit_token_idx = None
            self._entered = False
        except Exception as e:
            print(f"[{type(self).__name__.lower()}-{self.idx}] redeem failed: {e!r}",
                  flush=True)
            ctr["dmExitFails"] = ctr.get("dmExitFails", 0) + 1


@_register
class BootstrapDMAgent(_DMBase):
    """Deposit once at bootstrap into a fixed token (one agent per token
    by ``idx`` order); never exit.  Used to seed empty TOKEN/BUCK pools
    so any downstream scenario has live liquidity by tick 0.

    Token assignment is round-robin on the agent's instance index
    relative to the per-class counter; with N agents and N tokens,
    agent k deposits into token k.  More agents than tokens cycle round.
    """

    _counter: int = 0

    def __init__(self, idx: int):
        super().__init__(idx)
        self._seq = BootstrapDMAgent._counter
        BootstrapDMAgent._counter += 1

    def bootstrap(self, d, scenario, ctr) -> None:
        if self._entered:
            return
        tok_idx = self._seq % len(d.tokens)
        self._enter(d, scenario, ctr, tok_idx)

    def act(self, d, scenario, day, tick, ctr) -> None:
        # Bootstrap agents are pinned LPs -- they never exit and never
        # re-enter.  The deposit is made in bootstrap() before tick 0.
        return


@_register
class DirectMintAgent(_DMBase):
    """Probabilistic LP provider.

    On each tick, transitions IDLE <-> ENTERED via Bernoulli trials.
    With ``ENTER_PROB_PER_TICK = 1e-3`` and ``EXIT_PROB_PER_TICK = 5e-3``,
    steady-state entered fraction ~17%, mean holding time ~200 ticks
    (~50 days at 4 ticks/day), and a 50-agent population over a 365-day
    horizon produces ~50 entries+exits -- matching the prior schedule-
    based target without any setup-time date math.
    """

    ENTER_PROB_PER_TICK: float = 1e-3
    EXIT_PROB_PER_TICK:  float = 5e-3

    _counter: int = 0

    def __init__(self, idx: int):
        super().__init__(idx)
        self._seq = DirectMintAgent._counter
        DirectMintAgent._counter += 1
        self._rng: random.Random | None = None      # set in setup()

    def setup(self, d, scenario, rng) -> None:
        super().setup(d, scenario, rng)
        # Per-agent RNG keyed off (scenario.seed, agent.idx) for
        # reproducibility independent of the order other agents consume
        # the shared `rng`.
        self._rng = random.Random((scenario.seed, "DirectMintAgent", self._seq))

    def act(self, d, scenario, day, tick, ctr) -> None:
        if self._rng is None:
            return
        if not self._entered:
            if self._rng.random() < self.ENTER_PROB_PER_TICK:
                tok_idx = self._rng.randrange(len(d.tokens))
                self._enter(d, scenario, ctr, tok_idx)
        else:
            if self._rng.random() < self.EXIT_PROB_PER_TICK:
                self._exit(d, ctr)
