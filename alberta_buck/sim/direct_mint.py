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
``BuckBasket.redeem`` -- the canonical Phase 1b way to put backing into
the BuckBasket.  TOKEN agents first buy their chosen commodity from the
deep external TOKEN/USDC pool, then pledge that TOKEN into its own
TOKEN/BUCK pool.  BUCK agents mint externally backed BUCK through
``BuckCredit``/``Buck.mint`` and deposit those idle BUCK balances into the
most-underweight BuckBasket pool.  The basket itself does not optimize
commodity routes.
"""

from __future__ import annotations

import random

from alberta_buck.sim import identity as idmod
from alberta_buck.sim.agents import Agent, _register
from alberta_buck.sim.chain import load_artifact
from alberta_buck.sim.router import MIN_SQRT_RATIO, MAX_SQRT_RATIO


class _DMBase(Agent):
    """Shared deposit/redeem state + helpers for DM-family agents.

    Subclasses choose when to call ``_enter`` / ``_exit``; this base
    provides the BuckBasket plumbing and the event-decoded counter
    bookkeeping (``ctr['dmEntries']``, ``ctr['dmOutstandingBuck']``,
    ``ctr['treasuryBuck']``, etc.) the loop's teardown and summary
    expect.
    """

    SEED_USDC = 100_000 * 10 ** 6    # smaller stochastic entries

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

    def _token_idx(self, d, token_addr: str) -> int | None:
        token_addr = token_addr.lower()
        for i, c in enumerate(d.tokens):
            if c.address.lower() == token_addr:
                return i
        return None

    def _buy_token_from_usdc(self, d, tok_idx: int, usdc_in: int) -> int:
        """Spend USDC through the deep external TOKEN/USDC pool and return
        the received TOKEN amount."""
        tc = d.tokens[tok_idx]
        pool_abi, _ = load_artifact("UniswapV3Pool")
        pool_addr = d.pool_usdc[tok_idx]
        pool = d.w3.eth.contract(address=pool_addr, abi=pool_abi)
        t0 = pool.functions.token0().call()
        t1 = pool.functions.token1().call()
        zero_for_one = d.usdc.address.lower() == t0.lower()
        sqrt_limit = (
            MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1
        )

        # before = tc.functions.balanceOf(self.address).call()
        before = d.chain.balance_of(tc, self.address)
        d.chain.send(d.usdc.functions.mint(self.address, usdc_in))
        d.chain.send(d.usdc.functions.transfer(d.simlp.address, usdc_in),
                     sender=self.account)
        d.chain.send(d.simlp.functions.swap(
            pool_addr, self.address, zero_for_one, int(usdc_in),
            sqrt_limit, t0, t1), sender=self.account)
        #return tc.functions.balanceOf(self.address).call() - before
        return d.chain.balance_of(tc, self.address) - before

    def _record_deposit(
        self, d, ctr, receipt_id: int, token_idx: int | None,
        invested_value: int
    ) -> None:
        dep = d.basket.functions.deposits(receipt_id).call()
        self._principal_buck = dep[0]
        self._principal_tok = dep[1]
        self._deposit_token_idx = token_idx
        self._entered = True
        self._exited = False
        ctr["dmEntries"] = ctr.get("dmEntries", 0) + 1
        ctr["dmOutstandingBuck"] = (
            ctr.get("dmOutstandingBuck", 0) + self._principal_buck)
        ctr["dmTotalInvested"] = (
            ctr.get("dmTotalInvested", 0) + invested_value)

    def _enter(self, d, scenario, ctr, tok_idx: int) -> None:
        """Buy TOKEN from the external TOKEN/USDC pool, then deposit it."""
        tc = d.tokens[tok_idx]
        seed = self._buy_token_from_usdc(d, tok_idx, self.SEED_USDC)
        if seed == 0:
            return

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
                self._record_deposit(
                    d, ctr, self._receipt_id, tok_idx, self.SEED_USDC)
                ctr["dmTokenEntries"] = ctr.get("dmTokenEntries", 0) + 1
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

    SEED_USDC = 250_000 * 10 ** 6

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
    steady-state entered fraction ~17% and mean holding time ~200 ticks
    (~50 days at 4 ticks/day).  The scenario uses more agents with smaller
    tickets so each entry is modest relative to basket depth.
    """

    ENTER_PROB_PER_TICK: float = 1e-3
    EXIT_PROB_PER_TICK:  float = 5e-3
    SEED_USDC = 100_000 * 10 ** 6

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
        # the shared `rng`.  random.Random rejects tuples -- fold the
        # key triple into a single 64-bit-safe int by hashing the bytes.
        import hashlib
        seed_bytes = (
            int(scenario.seed).to_bytes(32, "big", signed=False)
            + b"DirectMintAgent"
            + int(self._seq).to_bytes(8, "big", signed=False)
        )
        self._rng = random.Random(
            int.from_bytes(hashlib.blake2b(seed_bytes, digest_size=16).digest(),
                           "big"))

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


@_register
class DirectMintBuckAgent(_DMBase):
    """Probabilistic BUCK depositor.

    These agents model holders with idle externally backed BUCK balances.
    They create zero-premium BuckCredit headroom during setup, mint BUCK
    through the normal public ``Buck.mint`` path at entry time, then deposit
    those BUCKs into BuckBasket.
    """

    is_eoa = False
    ENTER_PROB_PER_TICK: float = 1e-3
    EXIT_PROB_PER_TICK:  float = 5e-3
    SEED_USDC = 1_000 * 10 ** 6
    MAX_DEPOSIT_SLIPPAGE_BP = 100
    CREDIT_MULTIPLE = 20

    _counter: int = 0

    def __init__(self, idx: int):
        super().__init__(idx)
        self._seq = DirectMintBuckAgent._counter
        DirectMintBuckAgent._counter += 1
        self._rng: random.Random | None = None
        self.proxy = None

    @property
    def address(self) -> str:
        if self.proxy is not None:
            return self.proxy.address
        return "0x" + "0" * 40

    def setup(self, d, scenario, rng) -> None:
        import hashlib
        seed_bytes = (
            int(scenario.seed).to_bytes(32, "big", signed=False)
            + b"DirectMintBuckAgent"
            + int(self._seq).to_bytes(8, "big", signed=False)
        )
        self._rng = random.Random(
            int.from_bytes(hashlib.blake2b(seed_bytes, digest_size=16).digest(),
                           "big"))

        self.proxy = d.chain.deploy("SimLP", sol_file="SimLP")
        d.chain.send(d.reg.functions.bindContract(
            self.proxy.address, idmod.BIND_PK, idmod.BIND_E, True, False))

        now_ts = d.w3.eth.get_block("latest")["timestamp"]
        face = self.SEED_USDC * self.CREDIT_MULTIPLE
        d.chain.send(d.credit.functions.createCredit(
            self.proxy.address, 0, face, 0, 0, 0, now_ts, 0))

    def _proxy_exec(self, d, target: str, data: bytes):
        return d.chain.send(self.proxy.functions.exec(target, data))

    def _enter_buck(self, d, ctr) -> None:
        target_buck = self.SEED_USDC
        self._proxy_exec(
            d, d.buck.address,
            d.buck.encode_abi("mint(uint256)", args=[target_buck]))
        spendable = d.buck.functions.balanceOf(self.proxy.address).call()
        buck_amt = min(target_buck, spendable)
        if buck_amt == 0:
            return
        self._proxy_exec(
            d, d.buck.address,
            d.buck.encode_abi(
                "approve(address,uint256)", args=[d.basket.address, buck_amt]))
        try:
            from alberta_buck.sim.deploy import DEPOSITED_TOPIC
            rcpt = self._proxy_exec(
                d, d.basket.address,
                d.basket.encode_abi(
                    "depositToken(address,uint256,uint256)",
                    args=[
                        d.buck.address,
                        buck_amt,
                        self.MAX_DEPOSIT_SLIPPAGE_BP,
                    ]))
            for log in rcpt["logs"]:
                if log["topics"][0] == DEPOSITED_TOPIC:
                    self._receipt_id = int.from_bytes(log["topics"][2], "big")
                    break
            if self._receipt_id is not None:
                dep = d.basket.functions.deposits(self._receipt_id).call()
                tok_idx = self._token_idx(d, dep[2])
                self._record_deposit(
                    d, ctr, self._receipt_id, tok_idx, buck_amt)
                ctr["dmBuckEntries"] = ctr.get("dmBuckEntries", 0) + 1
        except Exception as e:
            print(f"[{type(self).__name__.lower()}-{self.idx}] _enter failed: {e!r}",
                  flush=True)
            self._receipt_id = None

    def _exit(self, d, ctr) -> None:
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
            rcpt = self._proxy_exec(
                d, d.basket.address,
                d.basket.encode_abi(
                    "redeem(uint256,uint256,uint256)",
                    args=[self._receipt_id, 0, 0]))
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
            self._receipt_id = None
            self._principal_tok = 0
            self._principal_buck = 0
            self._deposit_token_idx = None
            self._entered = False
        except Exception as e:
            print(f"[{type(self).__name__.lower()}-{self.idx}] redeem failed: {e!r}",
                  flush=True)
            ctr["dmExitFails"] = ctr.get("dmExitFails", 0) + 1

    def act(self, d, scenario, day, tick, ctr) -> None:
        if self._rng is None:
            return
        if not self._entered:
            if self._rng.random() < self.ENTER_PROB_PER_TICK:
                self._enter_buck(d, ctr)
        else:
            if self._rng.random() < self.EXIT_PROB_PER_TICK:
                self._exit(d, ctr)
