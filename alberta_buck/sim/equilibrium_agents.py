"""Equilibrium-experiment agents -- the monetary feedback loop the
BuckKControllerDirect PID defends.

Three agent classes close the BUCK-K loop:

  * PidKeeperAgent      -- permissionlessly advances the PID every tick so
                           the controller runs on the 30-min money cadence
                           (not just the loop's once-per-day compute()).
  * FatCreditBorrowerAgent -- the monetary actor.  Pledges a POOL of
                           BuckCredit NFTs (its "properties"), activates the
                           whole face once, then modulates its DRAWN BUCK
                           toward `util_target * creditLimit`.  creditLimit
                           moves live with buckK (creditLimit =
                           collateralValue * buckK / 1e18), so when the
                           controller tightens K the limit shrinks and the
                           borrower is forced to deleverage.  Levering up
                           SELLS BUCK into the TOKEN/BUCK pools (pushing
                           basketValueInBuck > 1.0, which the controller
                           defends by lowering K); deleveraging BUYS BUCK
                           back out of those pools (pushing basketValue back
                           toward 1.0).  That negative feedback is the whole
                           point of the experiment.
  * SaverAgent          -- a counter-cyclical BUCK saver: "buys the dip,
                           sells the rip" on the floating BUCK/USDC pool
                           against a savings goal (buy below parity, sell
                           above, always keeping a reserve).  This is the
                           stabilizing private BUCK demand that damps the
                           borrower-driven oscillation.

A small set of "regime agents" (the first few borrowers and savers) also
redraw ONE knob every REGIME_DAYS, staggered so exactly one changes per
6-month window -- so a long run is repeatedly perturbed and re-settles.

All three self-register via @_register and are selected by build_equilibrium.

Economics note (deliberate simplification, documented for review): a
zero-premium BuckCredit mint only *activates* NFT-backed credit headroom --
it does not deliver spendable tokens beyond that headroom (poolPrincipal == 0,
so no balance is moved and totalSupply is unchanged).  BUCK only enters
circulation when the borrower SPENDS the headroom (transfers BUCK into a pool,
driving its signed raw negative == credit drawn).  So rather than re-minting a
`delta` each tick (which would only re-activate already-activated capacity),
the borrower activates its full property face once in setup() and then
modulates `drawn` directly by selling / buying BUCK against the standing,
K-scaled creditLimit.  This is economically identical to the negotiated design
(K-gated issuance/retirement) and far more robust.
"""

from __future__ import annotations

import hashlib
import random

from alberta_buck.sim import identity as idmod
from alberta_buck.sim.agents import Agent, _register
from alberta_buck.sim.chain import load_artifact
from alberta_buck.sim.router import MIN_SQRT_RATIO, MAX_SQRT_RATIO

FEE_DEN = 1_000_000
PARITY = 1_000_000                # micro-USDC per 1 BUCK at parity (1:1)

# -- regime-change scheduling ------------------------------------------- #
# A SMALL set of "regime agents" each redraw ONE primary knob once every
# REGIME_DAYS, staggered round-robin so exactly ONE agent changes per
# 6-month window (spaced far enough that the loop re-settles between
# shocks).  Over a 5-year run (~1826 days) this fires ~10 isolated events.
#
# Regime agents are the FIRST N_REGIME_BORROWERS FatCreditBorrowerAgents
# and the FIRST N_REGIME_SAVERS SaverAgents (assigned in setup() via a
# per-class counter).  Each is given a `global_regime_slot` in
# [0, N_REGIME_AGENTS); the agent scheduled for `period` is the one whose
# slot == period % N_REGIME_AGENTS.  All redraws come off the per-agent
# seeded RNG -- fully deterministic, no wall-clock.
REGIME_DAYS = 182
N_REGIME_BORROWERS = 3
N_REGIME_SAVERS = 1
N_REGIME_AGENTS = N_REGIME_BORROWERS + N_REGIME_SAVERS

# module-level cache of pool (token0, token1) so we don't re-read them on
# every swap across thousands of ticks.
_POOL_TOKENS: dict = {}


def _pool_tokens(d, pool_addr: str) -> tuple[str, str]:
    key = pool_addr.lower()
    hit = _POOL_TOKENS.get(key)
    if hit is None:
        pool_abi, _ = load_artifact("UniswapV3Pool")
        pool = d.w3.eth.contract(address=pool_addr, abi=pool_abi)
        hit = (pool.functions.token0().call(), pool.functions.token1().call())
        _POOL_TOKENS[key] = hit
    return hit


def _agent_rng(seed: int, class_name: str, idx: int) -> random.Random:
    """Per-agent deterministic RNG keyed off (seed, class, idx)."""
    seed_bytes = (
        int(seed).to_bytes(32, "big", signed=False)
        + class_name.encode()
        + int(idx).to_bytes(8, "big", signed=False)
    )
    return random.Random(
        int.from_bytes(hashlib.blake2b(seed_bytes, digest_size=16).digest(),
                       "big"))


class _ProxyAgent(Agent):
    """Base for the two proxy (SimLP-backed, identity-bound) agents.  Holds
    the deploy+bind boilerplate and the transfer-to-simlp + simlp.swap
    primitive that both selling and buying BUCK share."""

    is_eoa = False

    def __init__(self, idx: int):
        super().__init__(idx)
        self._rng: random.Random | None = None
        self.proxy = None
        self.is_regime = False
        self.global_regime_slot = -1
        self._last_period = 0

    @property
    def address(self) -> str:
        if self.proxy is not None:
            return self.proxy.address
        return "0x" + "0" * 40

    # -- regime-change scaffolding ---------------------------------------- #

    def _init_regime(self, slot_in_class: int, class_offset: int,
                     n_regime_in_class: int) -> None:
        """Decide whether this instance is a regime agent and, if so, its
        global round-robin slot.  Called once from setup()."""
        if slot_in_class < n_regime_in_class:
            self.is_regime = True
            self.global_regime_slot = class_offset + slot_in_class
        else:
            self.is_regime = False
            self.global_regime_slot = -1
        self._last_period = 0

    def _maybe_regime(self, d, day, ctr) -> None:
        """If this regime agent is the one scheduled for the current
        6-month period, redraw its primary knob (subclass _apply_regime).
        Deterministic off self._rng; never raises."""
        if not self.is_regime:
            return
        period = day // REGIME_DAYS
        if period <= self._last_period:
            return
        # Advance our own clock every period so a later period still fires
        # even when this period was not our turn.
        self._last_period = period
        if period % N_REGIME_AGENTS != self.global_regime_slot:
            return
        try:
            note = self._apply_regime(day)
            ctr["regimeEvents"] = ctr.get("regimeEvents", 0) + 1
            ctr["regimeNote"] = note
        except Exception as e:
            ctr["regime_err"] = repr(e)[:200]

    def _apply_regime(self, day) -> str:  # pragma: no cover - overridden
        return ""

    def _bind_proxy(self, d) -> None:
        """Deploy a SimLP proxy and bind it public + NON-carrying (so BUCK it
        holds can go negative == draw credit; matches the SimLP binding)."""
        self.proxy = d.chain.deploy("SimLP", sol_file="SimLP")
        d.chain.send(d.reg.functions.bindContract(
            self.proxy.address, idmod.BIND_PK, idmod.BIND_E, True, False))

    def _proxy_exec(self, d, target: str, data: bytes):
        return d.chain.send(self.proxy.functions.exec(target, data))

    def _swap_via_simlp(self, d, pool_addr: str, input_c, amount: int,
                        recipient: str) -> None:
        """Swap exactly `amount` of `input_c` into `pool_addr`, output to
        `recipient`.  The proxy first transfers the input to SimLP (whose
        swap callback pays the pool from its own balance)."""
        if amount <= 0:
            return
        t0, t1 = _pool_tokens(d, pool_addr)
        zero_for_one = input_c.address.lower() == t0.lower()
        sqrt_limit = MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1
        self._proxy_exec(
            d, input_c.address,
            input_c.encode_abi("transfer(address,uint256)",
                               args=[d.simlp.address, int(amount)]))
        d.chain.send(d.simlp.functions.swap(
            pool_addr, recipient, zero_for_one, int(amount),
            sqrt_limit, t0, t1))


@_register
class PidKeeperAgent(Agent):
    """A registered EOA that advances the PID every tick (permissionless
    compute()).  compute() only does work once dT (30 min) has elapsed, which
    is exactly the money-tick cadence (ticks_per_day=48 => 1800s/tick), so the
    controller integrates on the same clock as the borrowers act."""

    is_eoa = True

    def act(self, d, scenario, day, tick, ctr) -> None:
        try:
            d.chain.send(d.kctrl.functions.compute())
            ctr["pidComputes"] = ctr.get("pidComputes", 0) + 1
        except Exception as e:
            ctr["pid_err"] = repr(e)[:200]


@_register
class FatCreditBorrowerAgent(_ProxyAgent):
    """Models many actors issuing/redeeming BUCK against a pool of BuckCredit
    "properties".  Levers toward `util_target * creditLimit`; the controller
    tightening K forces deleverage.  See module docstring for the loop."""

    N_CREDITS = 8                 # pool of "property" NFTs
    # aggregate face sized ~$2-5M (6-dec BUCK wei), chosen per-agent in setup.

    _regime_counter = 0           # per-class seq (reset in build_equilibrium)

    def setup(self, d, scenario, rng) -> None:
        self._rng = _agent_rng(scenario.seed, type(self).__name__, self.idx)
        r = self._rng
        # Regime slot: the first N_REGIME_BORROWERS borrowers are regime
        # agents (global slots 0..N_REGIME_BORROWERS-1).
        slot = type(self)._regime_counter
        type(self)._regime_counter += 1
        self._init_regime(slot, 0, N_REGIME_BORROWERS)
        # Heterogeneous knobs per agent.
        self.util_target = r.uniform(0.60, 0.80)   # draw this frac of limit
        self.internal_share = r.uniform(0.30, 0.60)  # sold into TOKEN/BUCK
        self.retire_rate = r.uniform(0.20, 0.35)   # frac of excess retired/step
        self.band = 0.05                            # deadband
        face_total = r.randint(2, 5) * 1_000_000 * 10 ** 6   # $2-5M, 6-dec

        self._bind_proxy(d)

        # Create the pool of BuckCredit NFTs (no depreciation, zero premium).
        now_ts = d.w3.eth.get_block("latest")["timestamp"]
        per = max(1, face_total // self.N_CREDITS)
        self._token_ids = []
        for _ in range(self.N_CREDITS):
            rcpt = d.chain.send(d.credit.functions.createCredit(
                self.proxy.address, 0, per, 0, 0, 0, now_ts, 0))
        # Activate the WHOLE face once: mint face_total (zero premium => this
        # only activates credit headroom; no BUCK enters circulation, raw
        # stays 0, drawn stays 0).  creditLimit is now face * buckK and moves
        # live with the controller.
        self._face = per * self.N_CREDITS
        try:
            self._proxy_exec(
                d, d.buck.address,
                d.buck.encode_abi("mint(uint256)", args=[self._face]))
        except Exception as e:
            print(f"[fatborrower-{self.idx}] activate mint failed: {e!r}",
                  flush=True)

    # -- market primitives ------------------------------------------------- #

    def _amount_in_for_out(self, r_in: int, r_out: int, want_out: int,
                           fee: int) -> int:
        """Constant-product input needed to receive ~`want_out` of the output
        token, net of `fee` (pip).  Capped so we never ask for the whole
        reserve."""
        if want_out <= 0 or r_in == 0 or r_out == 0:
            return 0
        cap = r_out * 9 // 10
        out = min(want_out, cap)
        if out >= r_out:
            return 0
        eff = out * r_in // (r_out - out)
        return eff * FEE_DEN // (FEE_DEN - fee) + 1

    def _sell_buck(self, d, pool_addr: str, buck_amt: int) -> None:
        """Sell BUCK into `pool_addr`, receiving the other token back to the
        proxy (drives signed raw negative == draws credit == issues BUCK)."""
        self._swap_via_simlp(d, pool_addr, d.buck, buck_amt, self.proxy.address)

    def _buy_buck(self, d, pool_addr: str, input_c, want_buck: int,
                  fee: int) -> int:
        """Spend held `input_c` to buy ~`want_buck` BUCK back into the proxy
        (climbs raw toward 0 == retires drawn credit).  Returns BUCK bought."""
        held = d.chain.balance_of(input_c, self.proxy.address)
        if held == 0 or want_buck <= 0:
            return 0
        r_in = d.chain.balance_of(input_c, pool_addr)
        r_out = d.chain.balance_of(d.buck, pool_addr)
        need = self._amount_in_for_out(r_in, r_out, want_buck, fee)
        spend = min(held, need) if need else min(held, r_in // 10)
        if spend <= 0:
            return 0
        before = d.chain.balance_of(d.buck, self.proxy.address)
        self._swap_via_simlp(d, pool_addr, input_c, spend, self.proxy.address)
        return d.chain.balance_of(d.buck, self.proxy.address) - before

    # -- regime change ----------------------------------------------------- #

    def _apply_regime(self, day) -> str:
        """Primary knob = util_target (target leverage).  A fresh target in
        [0.40, 0.85] shifts how hard this borrower pushes basketValue."""
        old = self.util_target
        self.util_target = self._rng.uniform(0.40, 0.85)
        return (f"day{day} borrower#{self.idx} util_target "
                f"{old:.3f}->{self.util_target:.3f}")

    # -- the loop ---------------------------------------------------------- #

    def act(self, d, scenario, day, tick, ctr) -> None:
        self._maybe_regime(d, day, ctr)
        if self.proxy is None:
            return
        try:
            limit = d.buck.functions.creditLimit(self.proxy.address).call()
            signed = d.buck.functions.signedBalanceOf(self.proxy.address).call()
        except Exception as e:
            ctr["fat_err"] = repr(e)[:200]
            return
        drawn = -signed if signed < 0 else 0
        target = int(self.util_target * limit)
        dead = int(self.band * max(1, limit))
        n = len(d.tokens)

        if drawn < target - dead:
            # Lever up: sell fresh BUCK.  Never sell more than currently
            # spendable headroom (creditLimit - drawn).
            delta = target - drawn
            spendable = max(0, limit - drawn)
            delta = min(delta, spendable)
            if delta < 10 ** 6:            # sub-$1 moves: skip
                return
            internal = int(self.internal_share * delta)
            external = delta - internal
            i = self._rng.randrange(n)     # which TOKEN/BUCK pool to push
            try:
                if internal > 0:
                    self._sell_buck(d, d.pool_buck[i], internal)
                if external > 0 and d.pool_ub:
                    self._sell_buck(d, d.pool_ub, external)
                ctr["fatEntries"] = ctr.get("fatEntries", 0) + 1
                ctr["fatIssued"] = ctr.get("fatIssued", 0) + delta
            except Exception as e:
                ctr["fat_sell_err"] = repr(e)[:200]

        elif drawn > target + dead:
            # Deleverage: K tightened, limit shrank, we are over-utilized.
            # Buy BUCK back (removing it from the TOKEN/BUCK pool pulls
            # basketValue back toward 1.0) and burn what we can.
            excess = drawn - target
            want = int(self.retire_rate * excess)
            if want < 10 ** 6:
                return
            i = self._rng.randrange(n)
            got = 0
            try:
                # Prefer buying back from the TOKEN/BUCK pool (direct control
                # channel: shrinks basketValue) using held TOKEN, then top up
                # from the floating pool using held USDC.
                got += self._buy_buck(d, d.pool_buck[i], d.tokens[i], want,
                                      d.fee_buck)
                if got < want and d.pool_ub:
                    got += self._buy_buck(d, d.pool_ub, d.usdc, want - got,
                                          d.fee_ub)
                ctr["fatRetires"] = ctr.get("fatRetires", 0) + 1
                ctr["fatRetired"] = ctr.get("fatRetired", 0) + got
            except Exception as e:
                ctr["fat_buy_err"] = repr(e)[:200]
            # Best-effort burn to actually contract supply (deactivates a
            # sliver of credit; skipped if it would break post-burn solvency).
            if got > 0:
                try:
                    self._proxy_exec(
                        d, d.buck.address,
                        d.buck.encode_abi("burn(uint256)", args=[int(got)]))
                    ctr["fatBurned"] = ctr.get("fatBurned", 0) + got
                except Exception:
                    pass


@_register
class SaverAgent(_ProxyAgent):
    """Counter-cyclical BUCK saver -- "buy below value, spend above value",
    providing the stabilizing private BUCK demand that pulls BUCK toward the
    basket (the same peg the controller defends).

    The value reference is the BASKET, not the US dollar: BUCK is cheap when
    one basket costs MORE than 1 BUCK (basketValueInBuck > 1).  Each step it
    reads basketValueInBuck (18-dec) and computes:
        discount = max(0, basketValueInBuck - 1.0)   # BUCK below basket value
        premium  = max(0, 1.0 - basketValueInBuck)   # BUCK above basket value
    On a discount it ACCUMULATES toward `savings_goal`, spending USDC on BUCK
    at base_rate*(1 + disc_gain*discount) -- the cheaper BUCK is vs the basket,
    the harder it buys (bidding BUCK up toward the basket).  On a premium it
    SELLS BUCK back to USDC at ~base_rate*(1 + prem_gain*premium) worth, never
    below `reserve` (BUCK kept for a future obligation).  This leans INTO the
    divergence the controller is also fighting, instead of anchoring to $1."""

    _regime_counter = 0           # per-class seq (reset in build_equilibrium)

    def setup(self, d, scenario, rng) -> None:
        self._rng = _agent_rng(scenario.seed, type(self).__name__, self.idx)
        r = self._rng
        # Regime slot: the first N_REGIME_SAVERS savers are regime agents
        # (global slots N_REGIME_BORROWERS .. N_REGIME_AGENTS-1).
        slot = type(self)._regime_counter
        type(self)._regime_counter += 1
        self._init_regime(slot, N_REGIME_BORROWERS, N_REGIME_SAVERS)
        # Heterogeneous, seeded knobs.  Scaled ~10x the first cut: private
        # demand has to be comparable to BUCK supply (~15-20M) to actually
        # bid BUCK toward the basket, not a rounding error against it.
        self.savings_goal = r.randint(10, 30) * 1_000_000 * 10 ** 6  # BUCK target
        self.base_rate = r.randint(200_000, 600_000) * 10 ** 6       # USDC/step
        self.disc_gain = r.uniform(5.0, 15.0)     # accelerate buys on the dip
        self.prem_gain = r.uniform(5.0, 15.0)     # accelerate sells on the rip
        self.reserve = int(self.savings_goal * r.uniform(0.20, 0.40))
        self.budget = r.randint(20, 60) * 1_000_000 * 10 ** 6        # USDC pool
        self._spent = 0                            # net USDC deployed into BUCK
        self._bind_proxy(d)
        # Seed the proxy with its USDC budget to deploy over the run.
        d.chain.send(d.usdc.functions.mint(self.proxy.address, self.budget))

    def _apply_regime(self, day) -> str:
        """Primary knob = base_rate (savings cadence)."""
        old = self.base_rate
        self.base_rate = self._rng.randint(20_000, 60_000) * 10 ** 6
        return (f"day{day} saver#{self.idx} base_rate "
                f"{old // 10 ** 6}->{self.base_rate // 10 ** 6}")

    def act(self, d, scenario, day, tick, ctr) -> None:
        self._maybe_regime(d, day, ctr)
        if self.proxy is None or not d.pool_ub:
            return
        try:
            # Value reference is the BASKET: BUCK is cheap when a basket costs
            # more than 1 BUCK (basketValueInBuck > 1).  This is the same
            # observable the controller defends, so buying leans into the peg.
            bvib = d.basket.functions.basketValueInBuck().call() / 1e18
            discount = max(0.0, bvib - 1.0)      # BUCK below basket value -> buy
            premium = max(0.0, 1.0 - bvib)       # BUCK above basket value -> sell
            holding = d.chain.balance_of(d.buck, self.proxy.address)
            held_usdc = d.chain.balance_of(d.usdc, self.proxy.address)

            if discount > 0 and holding < self.savings_goal \
                    and self._spent < self.budget:
                # Buy below value: accelerate accumulation with the discount.
                rate = int(self.base_rate * (1.0 + self.disc_gain * discount))
                amt = min(rate, held_usdc, self.budget - self._spent)
                if amt < 10 ** 6:               # sub-$1 move: skip
                    return
                self._swap_via_simlp(d, d.pool_ub, d.usdc, amt,
                                     self.proxy.address)
                self._spent += amt
                ctr["saverBuys"] = ctr.get("saverBuys", 0) + 1
                ctr["saverSpent"] = ctr.get("saverSpent", 0) + amt

            elif premium > 0 and holding > self.reserve:
                # Spend above value: sell BUCK for USDC, down to `reserve`.
                ru = d.chain.balance_of(d.usdc, d.pool_ub)
                rb = d.chain.balance_of(d.buck, d.pool_ub)
                spot = ru * PARITY // rb if rb else 0
                want_usdc = int(self.base_rate * (1.0 + self.prem_gain * premium))
                # BUCK to sell to realize ~want_usdc of USDC at current spot.
                sell = want_usdc * PARITY // spot if spot else 0
                sell = min(sell, holding - self.reserve)
                if sell < 10 ** 6:              # sub-$1 move: skip
                    return
                before = d.chain.balance_of(d.usdc, self.proxy.address)
                self._swap_via_simlp(d, d.pool_ub, d.buck, sell,
                                     self.proxy.address)
                recv = d.chain.balance_of(d.usdc, self.proxy.address) - before
                # Selling replenishes the deployable USDC budget.
                self._spent = max(0, self._spent - recv)
                ctr["saverSells"] = ctr.get("saverSells", 0) + 1
                ctr["saverSold"] = ctr.get("saverSold", 0) + recv
            # else: hold.
        except Exception as e:
            ctr["saver_err"] = repr(e)[:200]
