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
import math
import random

from alberta_buck.sim import identity as idmod
from alberta_buck.sim.agents import Agent, _register
from alberta_buck.sim.chain import load_artifact
from alberta_buck.sim.direct_mint import DirectMintAgent
from alberta_buck.sim.experiment import draw as _draw, spec as _spec, sample as _sample
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



# -- ecosystem growth: arrival / departure schedules ---------------------- #
#
# "Scaling up" the BUCK ecosystem: a class's TOML count is its population
# CEILING; a per-class growth regime decides WHEN each ordinal arrives (or,
# for decline, departs).  Curves are exact and deterministic -- the ordinal-s
# agent activates on the first day the target active count reaches s+1 --
# plus an ENDOGENOUS neighbor-attraction term: every fully retired mortgage
# speeds the community clock (pending arrivals check a warped day), so
# success recruits.  Per-class knobs (all [agents.<Class>] overridable):
#
#   arrive_mode  = "immediate" (default) | "steady" | "scurve" | "decline"
#                  | "endow" (SaverAgent: immediate arrival WITH pre-window
#                  BUCK inventory of endow_m, self-issued at setup)
#   arrive_n0    = fraction of the ceiling active at t0        (0.25)
#   arrive_rate  = steady/decline annual rate                  (0.25 = 25%/yr)
#   arrive_peak  = scurve peak RELATIVE growth, per year       (2.0 = 200%/yr)
#   arrive_mid   = scurve midpoint, fraction of horizon        (0.5)
#   attract_gain = clock speed-up per retired neighbor         (0.05)

def _growth_target(mode: str, t: float, n0: float, rate: float,
                   peak: float, mid: float, years: float) -> float:
    """Target ACTIVE fraction of the class ceiling at horizon-fraction t."""
    if mode == "steady":
        return min(1.0, n0 * math.exp(rate * years * t))
    if mode == "scurve":
        k = 2.0 * peak * years          # peak relative growth k/2 per t-unit
        a = (1.0 - n0) / max(n0, 1e-9)
        return 1.0 / (1.0 + a * math.exp(-k * (t - mid)))
    if mode == "decline":
        return max(0.0, math.exp(-rate * years * t))
    return 1.0                          # immediate


def _growth_params(scenario, cls: str) -> tuple:
    mode = _spec(scenario, cls, "arrive_mode", "immediate")
    n0 = float(_spec(scenario, cls, "arrive_n0", 0.25))
    rate = float(_spec(scenario, cls, "arrive_rate", 0.25))
    peak = float(_spec(scenario, cls, "arrive_peak", 2.0))
    mid = float(_spec(scenario, cls, "arrive_mid", 0.5))
    return mode, n0, rate, peak, mid


def _growth_arrival_day(seq: int, count: int, scenario, cls: str,
                        r: random.Random) -> int | None:
    """First day ordinal `seq` (0-based) is active; None = never (ceiling
    not reached inside the window).  +-10d seeded jitter de-synchronizes
    same-day cohorts."""
    mode, n0, rate, peak, mid = _growth_params(scenario, cls)
    if mode == "immediate":
        return 0
    horizon = max(1, int(getattr(scenario, "days", 1)))
    years = horizon / 365.0
    if mode == "decline":
        return 0                        # decline: all start active; departures below
    for day in range(horizon + 1):
        if _growth_target(mode, day / horizon, n0, rate, peak, mid,
                          years) * count >= seq + 1:
            return max(0, day + r.randint(-10, 10)) if day > 0 else 0
    return None


def _growth_departure_day(seq: int, count: int, scenario, cls: str,
                          r: random.Random) -> int | None:
    """Decline mode: highest ordinals depart first as the target shrinks."""
    mode, n0, rate, peak, mid = _growth_params(scenario, cls)
    if mode != "decline":
        return None
    horizon = max(1, int(getattr(scenario, "days", 1)))
    years = horizon / 365.0
    for day in range(horizon + 1):
        if _growth_target(mode, day / horizon, n0, rate, peak, mid,
                          years) * count < seq + 1:
            return max(1, day + r.randint(-10, 10))
    return None


def _dt_days(scenario) -> float:
    """Calendar days one activation represents: day_step / ticks_per_day.

    Rate knobs (saver base_rate, borrower retire_rate / release_rate, the
    ff issue-rate signal) are PER-DAY; every act scales them by this, so
    daily flow is cadence-invariant -- the same at the equilibrium
    campaign's coarse macro mode (10 d/act) and the rebalancing cadence
    (0.25 d/act).  Level-seeking legs (issue-to-target, escrow top-up) need
    no scaling: acting more often only tracks the target more tightly.
    NB: this redefines the knobs -- the banked campaign vectors were run
    with per-ACT rates at 10 d/act (i.e. one-tenth the per-day flow the
    same numbers now mean); their tables stand as history."""
    return (max(1, getattr(scenario, "day_step", 1))
            / max(1, getattr(scenario, "ticks_per_day", 1)))


def _growth_active(agent, day: int, ctr) -> bool:
    """Arrival gate with the neighbor-attraction warped clock."""
    if getattr(agent, "_growth_arrived", False):
        dep = getattr(agent, "depart_day", None)
        return dep is None or day < dep
    arrive = getattr(agent, "arrive_day", 0)
    if arrive is None:
        # Never scheduled -- but a thriving neighborhood can still recruit:
        # treat as "beyond horizon", reachable only via the warped clock.
        arrive = 10 ** 9
    gain = float(getattr(agent, "attract_gain", 0.0))
    eff = day * (1.0 + gain * ctr.get("neighborsRetired", 0))
    if eff >= arrive:
        agent._growth_arrived = True
        ctr["growthArrivals"] = ctr.get("growthArrivals", 0) + 1
        return True
    return False


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
        self.proxy = self._new_proxy(d)

    def _new_proxy(self, d):
        """Deploy + bind (public, non-carrying) one SimLP proxy contract."""
        proxy = d.chain.deploy("SimLP", sol_file="SimLP")
        d.chain.send(d.reg.functions.bindContract(
            proxy.address, idmod.BIND_PK, idmod.BIND_E, True, False))
        return proxy

    def _proxy_exec(self, d, target: str, data: bytes, proxy=None):
        return d.chain.send((proxy or self.proxy).functions.exec(target, data))

    def _swap_via_simlp(self, d, pool_addr: str, input_c, amount: int,
                        recipient: str, from_proxy=None) -> None:
        """Swap exactly `amount` of `input_c` into `pool_addr`, output to
        `recipient`.  The paying proxy (default self.proxy) first transfers
        the input to SimLP (whose swap callback pays the pool from its own
        balance)."""
        if amount <= 0:
            return
        t0, t1 = _pool_tokens(d, pool_addr)
        # BUCK is a lien-bearing balance: `balanceOf` is raw MINUS accrued
        # demurrage, and both the paying proxy and SimLP are Non-Carrying, so
        # each can hold `raw` while being able to spend strictly less.  Size
        # against the proxy's spendable BEFORE the transfer, or the transfer
        # reverts with "amount exceeds spendable".
        if input_c.address.lower() == d.buck.address.lower():
            payer = (from_proxy or self.proxy).address
            amount = min(int(amount),
                         max(0, d.buck.functions.balanceOf(payer).call()))
            if amount <= 0:
                return
        zero_for_one = input_c.address.lower() == t0.lower()
        sqrt_limit = MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1
        self._proxy_exec(
            d, input_c.address,
            input_c.encode_abi("transfer(address,uint256)",
                               args=[d.simlp.address, int(amount)]),
            proxy=from_proxy)
        # ... and again on SimLP's side: it pays the pool from its OWN
        # balance in the swap callback, and it carries a lien of its own from
        # whatever BUCK has passed through it.  Swapping more than it can
        # spend reverts inside the callback, which is what the traceback
        # points at rather than the transfer above.
        if input_c.address.lower() == d.buck.address.lower():
            amount = min(int(amount),
                         max(0, d.buck.functions.balanceOf(
                             d.simlp.address).call()))
            if amount <= 0:
                return
        d.chain.send(d.simlp.functions.swap(
            pool_addr, recipient, zero_for_one, int(amount),
            sqrt_limit, t0, t1))

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

    def _sell_capped(self, d, pool_addr: str, buck_amt: int) -> int:
        """Sell BUCK into `pool_addr`, first capping the amount to the
        contract's own live spendable (balanceOf == held-net-of-demurrage +
        unused credit) read right before the transfer.  Returns BUCK sold."""
        if buck_amt <= 0:
            return 0
        try:
            sp = d.buck.functions.balanceOf(self.proxy.address).call()
        except Exception:
            sp = buck_amt
        amt = min(buck_amt, max(0, sp))
        if amt < 10 ** 6:
            return 0
        self._sell_buck(d, pool_addr, amt)
        return amt

    def _buy_buck(self, d, pool_addr: str, input_c, want_buck: int,
                  fee: int, recipient: str = "") -> int:
        """Spend `input_c` held by the borrowing proxy to buy ~`want_buck`
        BUCK, delivered to `recipient` (default: the borrowing proxy, which
        climbs raw toward 0 == retires drawn credit; the escrow address
        funds the reserve instead).  Returns BUCK bought."""
        recipient = recipient or self.proxy.address
        held = d.chain.balance_of(input_c, self.proxy.address)
        if held == 0 or want_buck <= 0:
            return 0
        r_in = d.chain.balance_of(input_c, pool_addr)
        r_out = d.chain.balance_of(d.buck, pool_addr)
        need = self._amount_in_for_out(r_in, r_out, want_buck, fee)
        spend = min(held, need) if need else min(held, r_in // 10)
        if spend <= 0:
            return 0
        before = d.chain.balance_of(d.buck, recipient)
        self._swap_via_simlp(d, pool_addr, input_c, spend, recipient)
        return d.chain.balance_of(d.buck, recipient) - before



# -- insurance is switched OFF in this simulation -----------------------
#
# Both issuing agents create their BuckCredit with premiumRate 0, which makes
# `poolPrincipal` zero, which makes `Buck.mint` EXEMPT from the funding gate:
#
#     if (factor > 0 && poolPrincipal > 0) { require(preFundingBalance >= ...) }
#
# That is a deliberate loss of fidelity, and it is worth being precise about
# what is given up, because the gate is not a formality.
#
# `poolPrincipal` capitalizes the insurance pool as a perpetuity at an assumed
# 10% ROI -- POOL_ROI_INV = 10 in Buck.sol -- so it is the ANNUAL premium
# TIMES TEN, not the annual premium.  A 50bp policy on a $100,000 draw is
# $500/yr, hence $5,000 of principal; and fundingFactor is 1 + 10(b - p)/b,
# so at basketValueInBuck 1.10 the factor is 1.91 and the minter must be
# HOLDING $9,550 of BUCK -- about 9.5% of the draw -- before the mint is
# allowed.  That is a serious accumulation requirement.
#
# It does two jobs, both of which vanish here:
#
#   * it discourages issuance INTO inflation.  The factor rises exactly when
#     BUCK is undervalued, so the moment issuing more BUCK would hurt most is
#     the moment it costs most to arrange.
#   * it manufactures aggregate BUCK DEMAND from anyone who issues anyway,
#     because the reserve must be bought on the market first, so issuance
#     drags a bid along behind it.
#
# Turning it off makes issuance cheaper and less counter-cyclical than the
# real design, and removes a source of standing demand.  Read any result
# about issuance volume or parity with that in mind.
#
# TO RESTORE FIDELITY LATER, in rough order of effort:
#   1. a helper quoting the BUCK accumulation a draw requires -- quoteMint
#      gives poolPrincipal and fundingFactor is a live read, so
#      `required = poolPrincipal * factor / 1e18` is a two-call view;
#   2. a saving loop that actually funds it.  BuckCreditDebtorAgent has one
#      in outline (step 2 of its act) and it could not keep up: with a real
#      premium it managed 3 deploys against 77 gate refusals over 120 days,
#      because `balanceOf` counts unused credit headroom, headroom needs
#      activated coverage, and a NEW borrower holds nothing the gate accepts.
#      That bootstrap deadlock is the thing to model, not to route around;
#   3. the premium as a real cost in the ROI ledger, on BOTH the BUCK path
#      and the mortgage counterfactual.


def _impact_cap(reserve_in: int, max_impact_bp: int) -> int:
    """Largest input that moves a constant-product pool by at most `bp`.

    Adding dx to reserve x takes the price to 1/(1+f)^2 of where it was, with
    f = dx/x, so holding the move to `imp` gives

        f <= 1/sqrt(1 - imp) - 1

    which at 100bp is about 0.50% of the reserve.

    The point is that it is a bound on PRICE IMPACT rather than on size, and
    that is what a fixed fraction cannot express.  As pools deepen the same
    budget permits a proportionally larger trade, so a market that grows
    absorbs bigger entries at the same cost to the entrant -- which is the
    behaviour a real market has and "1% of the reserve" does not.  It also
    makes the throttle endogenous: many agents entering the same way deepen
    nothing and simply meet each other's impact, while agents arriving from
    different directions cancel and both get through.
    """
    if reserve_in <= 0 or max_impact_bp <= 0:
        return 0
    imp = min(0.99, max_impact_bp / 10_000.0)
    return int(reserve_in * (1.0 / math.sqrt(1.0 - imp) - 1.0))


@_register
class ArrivingDMAgent(DirectMintAgent):
    """Basket-side ORIGINATION flow: a DirectMintAgent with a staggered
    arrival day and spec-driven ticket size / churn.

    The stationary DirectMintAgent population models an ESTABLISHED
    depositor base (fixed count, Bernoulli churn around a steady entered
    fraction).  This subclass models depositors COMING ONLINE as the
    basket proves itself: each agent activates at its arrive_frac of the
    horizon and then behaves like its parent, so the basket's 100%-LTV
    (K-immune) tranche GROWS over the run -- the demand-side twin of the
    debtors' arrive_frac origination stagger.  Match the two rates so
    K-controlled credit supply and basket-minted supply grow together.

    Knobs ([agents.ArrivingDMAgent]): arrive_frac (default [0,1], uniform
    over the horizon), ticket_k (deposit size, $k, default [300,700]),
    enter_ptick / exit_ptick (per-tick Bernoulli probabilities, defaults
    2e-3 / 2e-3: entered fraction ~50%, mean hold ~125 days at 4
    ticks/day); arrive_mode = "endog" switches the fixed stagger to the
    demonstrated-stability hazard clock (dep_dev_ref, dep_halflife --
    see setup())."""

    def setup(self, d, scenario, rng) -> None:
        super().setup(d, scenario, rng)
        cls = type(self).__name__
        r = _agent_rng(scenario.seed, cls, self.idx)
        horizon = max(1, int(getattr(scenario, "days", 1)))
        # Draw order matters for reproducibility: arrive_frac first, then
        # ticket_k, matching the original stagger-only implementation.
        tau_frac = _draw(scenario, cls, "arrive_frac", r, (0.0, 1.0))
        self.SEED_USDC = int(_draw(scenario, cls, "ticket_k", r,
                                   (300, 700)) * 1_000 * 10 ** 6)
        self.ENTER_PROB_PER_TICK = float(
            _spec(scenario, cls, "enter_ptick", 2e-3))
        self.EXIT_PROB_PER_TICK = float(
            _spec(scenario, cls, "exit_ptick", 2e-3))
        # arrive_mode "endog": arrivals track DEMONSTRATED basket quality --
        # a pending depositor's clock runs at full (open-loop) speed while
        # the trailing EWMA peg deviation |bvib-1| stays within dep_dev_ref,
        # and slows in proportion as the basket wobbles (hazard =
        # dev_ref / max(dev_ref, ewma)).  tau reuses the arrive_frac spread,
        # so a perfectly stable basket reproduces the open-loop stagger and
        # an unstable one starves itself of new deposits.
        self._endog = (_spec(scenario, cls, "arrive_mode", "stagger")
                       == "endog")
        if self._endog:
            self._endog_tau = tau_frac * horizon
            self._endog_clock = 0.0
            self._endog_last = None
            self._ewma_dev = 0.0
            self._dev_ref = float(_spec(scenario, cls, "dep_dev_ref", 0.02))
            self._halflife = float(_spec(scenario, cls, "dep_halflife", 90.0))
            self.arrive_day = None      # set on endogenous arrival
        else:
            self.arrive_day = int(tau_frac * horizon)

    def act(self, d, scenario, day, tick, ctr) -> None:
        if self._endog and self.arrive_day is None:
            if tick == 0:
                if self._endog_last is None:
                    self._endog_last = day
                dd = max(0, day - self._endog_last)
                self._endog_last = day
                if dd:
                    try:
                        bvib = d.basket.functions.basketValueInBuck() \
                            .call() / 1e18
                    except Exception:
                        bvib = 1.0
                    alpha = 1.0 - 0.5 ** (dd / max(1e-9, self._halflife))
                    self._ewma_dev += alpha * (abs(bvib - 1.0)
                                               - self._ewma_dev)
                    hazard = self._dev_ref / max(self._dev_ref,
                                                 self._ewma_dev)
                    self._endog_clock += dd * hazard
                if self._endog_clock >= self._endog_tau:
                    self.arrive_day = day
                    ctr["endogDepositorArrivals"] = (
                        ctr.get("endogDepositorArrivals", 0) + 1)
            if self.arrive_day is None:
                return
        if day < self.arrive_day:
            return
        super().act(d, scenario, day, tick, ctr)


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
    """A realistic BUCK issuer/redeemer lifecycle against a pool of BuckCredit
    "properties".  Three mechanisms shape the standing draw, since the on-chain
    funding-factor gate is bypassed for zero-premium credit (poolPrincipal == 0)
    and would otherwise let issuance flood at t=0:

      (A) ADOPTION RAMP + SHOCK.  The pooled creditable capacity is not all
          available at once: `cap_frac(day)` is a logistic S-curve rising from
          ~0.15 to 1.0 over the sim horizon, so society takes up the credit
          gradually.  A minority of agents also get a one-off STEP shock at a
          seeded mid-run day (`+shock_mag`), a sudden society-wide uptake.  The
          effective target draw is `util_target * creditLimit * cap_frac(day)`,
          so issuance ramps IN instead of flooding, and a shock injects a wave.

      (B) ROLLING PRE-ISSUANCE FUNDING RESERVE (simulated).  Before issuing
          `delta` net BUCK the agent must HOLD a funding reserve in a separate
          ESCROW proxy, topping it up by BUYING BUCK (the pre-issuance demand
          the real funding gate would compel).  The per-issuance requirement
          scales with a funding factor
              ff = max(0, 1 + FF_CYC*(basketValueInBuck-1)/basketValueInBuck)
                   * (1 + FF_AMP*(issue_rate**FF_POW))
          counter-cyclical in BOTH directions -- the same shape as the
          on-chain fundingFactor(), which SATURATES AT ZERO in deflation --
          and super-linear in the SYSTEM-WIDE issuance rate (totalSupply
          delta since last step).  Above parity a hot uptake makes ff blow
          up, demanding heavy pre-BUCK demand (the throttle bites when the
          agent cannot fund it); below parity the toll melts away, so
          issuance is cheap exactly when new supply pushes TOWARD parity.

          The requirement is ROLLING, tied to OUTSTANDING drawn credit: each
          issuance pushes a (outstanding, ff) tranche; retiring credit pops
          tranches FIFO and moves their ff-scaled reserve to a PENDING-RELEASE
          buffer.  Pending reserve is sold back into the TOKEN/BUCK pools
          (the controlled observable) counter-cyclically -- only while
          basketValueInBuck <= ~parity, i.e. exactly when unwinding pushes
          TOWARD parity -- and the TOKEN proceeds are recycled to USDC via
          the deep truth pools, replenishing the funding budget.  This kills
          the two failure modes of the first (reverted) attempt AND of the
          cumulative design it replaces: the reserve no longer ratchets to a
          permanent net demand (which froze issuance for whole runs once
          budgets exhausted, leaving the PID open-loop), and the accounting
          is untangled -- drawn credit is read from the borrowing proxy's own
          signed balance (the escrow's holdings never contaminate it).

      (C) DISCOUNT-DRIVEN REDEMPTION ("money at a discount").  The retire/
          buy-back leg accelerates with the discount:
              retire_eff = retire_rate * (1 + DISC_GAIN*max(0, basketValueInBuck-1))
          so when BUCK is cheap vs the basket, redeemers buy BUCK hard --
          counter-cyclical demand -- on top of the forced deleverage when K
          tightens and the K-scaled creditLimit shrinks below the current draw.

    Net dynamic: strong issuance -> large ff -> pre-buy dominates the issuance
    sell -> net BUCK demand -> BUCK bid up -> the wave self-limits, and the
    ramp keeps the day-0 draw small, so basketValueInBuck no longer spikes at
    t=0.  See module docstring for the surrounding loop."""

    N_CREDITS = 8                 # pool of "property" NFTs
    # aggregate face sized ~$2-5M (6-dec BUCK wei), chosen per-agent in setup.

    CAP_FLOOR = 0.15              # adoption S-curve starts here (t=0)
    CAP_CEIL = 1.50              # cap on cap_frac (headroom for a shock)

    _regime_counter = 0           # per-class seq (reset in build_equilibrium)

    def setup(self, d, scenario, rng) -> None:
        self._rng = _agent_rng(scenario.seed, type(self).__name__, self.idx)
        r = self._rng
        # Regime slot: the first N_REGIME_BORROWERS borrowers are regime
        # agents (global slots 0..N_REGIME_BORROWERS-1).
        slot = type(self)._regime_counter
        type(self)._regime_counter += 1
        self._init_regime(slot, 0, N_REGIME_BORROWERS)
        # Heterogeneous knobs per agent.  Every draw goes through the
        # experiment layer: an attached experiment's [agents.FatCreditBorrowerAgent]
        # section overrides the coded default ([lo,hi] range or scalar).
        cls = type(self).__name__
        self.util_target = _draw(scenario, cls, "util_target", r, (0.60, 0.80))
        self.internal_share = _draw(scenario, cls, "internal_share", r, (0.30, 0.60))
        self.retire_rate = _draw(scenario, cls, "retire_rate", r, (0.20, 0.35))
        self.band = _draw(scenario, cls, "band", r, 0.05)   # deadband
        self._util_regime = _spec(scenario, cls, "util_regime", (0.40, 0.85))
        face_total = _draw(scenario, cls, "face_m", r, (2, 5)) * 1_000_000 * 10 ** 6

        # (A) Adoption ramp: logistic cap_frac(day) from CAP_FLOOR -> ~1.0.
        # `growth_slope` steepens the S; `mid_frac` places its midpoint as a
        # fraction of the horizon.  A minority (~shock_prob) get a one-off
        # step shock at a seeded day in the shock_window horizon fraction.
        self.growth_slope = _draw(scenario, cls, "growth_slope", r, (8.0, 14.0))
        self.mid_frac = _draw(scenario, cls, "mid_frac", r, (0.35, 0.55))
        self.shock_day = -1
        self.shock_mag = 0.0
        self.extra_cap = 0.0        # uptake_shock intervention adds here
        horizon = max(1, int(getattr(scenario, "days", 1)))
        if r.random() < _draw(scenario, cls, "shock_prob", r, 0.34):
            wlo, whi = _spec(scenario, cls, "shock_window", (0.25, 0.70))
            self.shock_day = r.randint(int(wlo * horizon), int(whi * horizon))
            self.shock_mag = _draw(scenario, cls, "shock_mag", r, (0.20, 0.40))
        self._shock_logged = False

        # (B) Rolling funding-reserve knobs.  ff super-linear in issue_rate,
        # counter-cyclical in the basket premium.  Seeded within the design
        # ranges so a rapid uptake makes ff blow up (strong pre-BUCK demand).
        self.ff_cyc = _draw(scenario, cls, "ff_cyc", r, (5.0, 10.0))
        self.ff_amp = _draw(scenario, cls, "ff_amp", r, (50.0, 200.0))
        self.ff_pow = _draw(scenario, cls, "ff_pow", r, (1.5, 2.0))
        # Reserve tranches: FIFO of [outstanding, ff]; requirement is
        # sum(outstanding * ff).  Retirement pops tranches into the
        # pending-release buffer, drip-sold back counter-cyclically.
        self._tranches: list[list] = []
        self.pending_release = 0
        self.release_rate = _draw(scenario, cls, "release_rate", r, (0.25, 0.50))
        self.release_eps = _draw(scenario, cls, "release_eps", r, 0.02)
        self._supply_prev = 0       # last-step system BUCK totalSupply

        # (C) Redemption discount gain.
        self.disc_gain = _draw(scenario, cls, "disc_gain", r, (3.0, 8.0))

        self._bind_proxy(d)
        # Escrow proxy: holds the funding reserve SEPARATE from the borrowing
        # account.  Drawn credit is then simply -signed(proxy) and the live
        # reserve simply balanceOf(escrow) -- chain truth (demurrage and all),
        # never entangled with the credit accounting.
        self.escrow = self._new_proxy(d)

        # Create the pool of BuckCredit NFTs (no depreciation, zero premium).
        now_ts = d.w3.eth.get_block("latest")["timestamp"]
        per = max(1, face_total // self.N_CREDITS)
        self._token_ids = []
        # Credits only land where the recipient asked for them.
        self._proxy_exec(d, d.credit.address, d.credit.encode_abi(
            "setCreditIssuer", args=[getattr(d.chain.deployer, "address", d.chain.deployer), True]))
        for _ in range(self.N_CREDITS):
            rcpt = d.chain.send(d.credit.functions.createCredit(
                self.proxy.address, 0, per, 0, 0, 0, now_ts, 0))
        # Activate the WHOLE face once: mint face_total (zero premium => this
        # only activates credit headroom; no BUCK enters circulation, raw
        # stays 0, drawn stays 0).  creditLimit is now face * buckK and moves
        # live with the controller.  The RAMP is applied to the TARGET, not the
        # NFT creation -- simpler and equivalent.
        self._face = per * self.N_CREDITS
        try:
            self._proxy_exec(
                d, d.buck.address,
                d.buck.encode_abi("mint(uint256)", args=[self._face]))
        except Exception as e:
            print(f"[fatborrower-{self.idx}] activate mint failed: {e!r}",
                  flush=True)

        # Seed a USDC funding budget so the agent can front the pre-issuance
        # BUCK demand (its reserve).  Issuance sells recycle USDC back in; when
        # the budget + recycled flow can't cover a hot reserve shortfall, the
        # throttle bites and the agent issues less.
        self.fund_budget = _draw(scenario, cls, "fund_budget_m", r,
                                 (3, 6)) * 1_000_000 * 10 ** 6
        try:
            d.chain.send(d.usdc.functions.mint(self.proxy.address,
                                               self.fund_budget))
        except Exception as e:
            print(f"[fatborrower-{self.idx}] fund mint failed: {e!r}",
                  flush=True)

    # -- regime change ----------------------------------------------------- #

    def _apply_regime(self, day) -> str:
        """Primary knob = util_target (target leverage).  A fresh target from
        the util_regime spec (default [0.40, 0.85]) shifts how hard this
        borrower pushes basketValue."""
        old = self.util_target
        self.util_target = _sample(self._util_regime, self._rng)
        return (f"day{day} borrower#{self.idx} util_target "
                f"{old:.3f}->{self.util_target:.3f}")

    # -- adoption ramp ----------------------------------------------------- #

    def _cap_frac(self, day, horizon) -> float:
        """(A) Logistic adoption S-curve in [CAP_FLOOR, ~1.0] over the horizon,
        plus a one-off step (`+shock_mag`) once `shock_day` is reached, plus
        any uptake_shock interventions accumulated in `extra_cap`."""
        h = max(1, int(horizon))
        x = day / h
        lg = 1.0 / (1.0 + math.exp(-self.growth_slope * (x - self.mid_frac)))
        cf = self.CAP_FLOOR + (1.0 - self.CAP_FLOOR) * lg
        if self.shock_day >= 0 and day >= self.shock_day:
            cf += self.shock_mag
        cf += self.extra_cap
        return max(0.0, min(self.CAP_CEIL, cf))

    # -- rolling reserve ----------------------------------------------------- #

    @property
    def reserve_req(self) -> int:
        """Rolling requirement: sum over open tranches of outstanding * ff
        (6-dec BUCK).  Falls as credit retires -- never a ratchet."""
        return int(sum(o * f for o, f in self._tranches))

    def _reserve_balance(self, d) -> int:
        """Live escrow BUCK (chain truth: demurrage-decayed)."""
        try:
            return d.chain.balance_of(d.buck, self.escrow.address)
        except Exception:
            return 0

    def _pop_tranches(self, retired: int) -> int:
        """Retire `retired` outstanding credit FIFO across tranches; return
        the ff-scaled reserve those tranches release (to pending)."""
        released, left = 0, retired
        while left > 0 and self._tranches:
            o, f = self._tranches[0]
            take = min(left, o)
            released += int(take * f)
            left -= take
            if take >= o:
                self._tranches.pop(0)
            else:
                self._tranches[0][0] = o - take
        return released

    def _release_pending(self, d, bvib: float, ctr, dt_days: float = 1.0) -> None:
        """Counter-cyclically unwind the pending-release reserve: sell a
        chunk of escrow BUCK into a TOKEN/BUCK pool -- which pushes
        basketValue UP -- only while bvib <= 1 + release_eps, i.e. exactly
        when that push is TOWARD parity.  TOKEN proceeds recycle to USDC via
        the deep truth pool, replenishing the borrowing proxy's funding
        budget for future pre-buys."""
        if self.pending_release < 10 ** 6:
            return
        if bvib > 1.0 + self.release_eps:
            return                     # unwinding now would push AWAY from parity
        bal = self._reserve_balance(d)
        avail = max(0, bal - self.reserve_req)   # requirement keeps first claim
        amt = min(self.pending_release, avail,
                  int(max(10 ** 6, self.pending_release
                          * min(1.0, self.release_rate * dt_days))))
        if amt < 10 ** 6:
            return
        i = self._rng.randrange(len(d.tokens))
        tok = d.tokens[i]
        before_tok = d.chain.balance_of(tok, self.escrow.address)
        try:
            self._swap_via_simlp(d, d.pool_buck[i], d.buck, amt,
                                 self.escrow.address, from_proxy=self.escrow)
        except Exception as e:
            ctr["fat_release_err"] = repr(e)[:200]
            return
        self.pending_release -= amt
        ctr["fatReleased"] = ctr.get("fatReleased", 0) + amt
        got_tok = d.chain.balance_of(tok, self.escrow.address) - before_tok
        if got_tok > 0:
            try:
                self._swap_via_simlp(d, d.pool_usdc[i], tok, got_tok,
                                     self.proxy.address, from_proxy=self.escrow)
            except Exception as e:
                ctr["fat_recycle_err"] = repr(e)[:200]

    # -- observability ------------------------------------------------------ #

    def channel_state(self, d) -> dict | None:
        """Live issuance-channel state for the snapshotter: the K-scaled
        limit, chain-truth drawn credit, and the reserve accounts.  Keeps
        snapshot.py decoupled from this agent's internal accounting."""
        if self.proxy is None:
            return None
        try:
            limit = d.buck.functions.creditLimit(self.proxy.address).call()
            signed = d.buck.functions.signedBalanceOf(self.proxy.address).call()
        except Exception:
            return None
        return {
            "limit": limit,
            "drawn": max(0, -signed),
            "reserve_held": self._reserve_balance(d),
            "reserve_req": self.reserve_req,
            "pending": self.pending_release,
        }

    # -- the loop ---------------------------------------------------------- #

    def act(self, d, scenario, day, tick, ctr) -> None:
        self._maybe_regime(d, day, ctr)
        if self.proxy is None:
            return
        try:
            limit = d.buck.functions.creditLimit(self.proxy.address).call()
            signed = d.buck.functions.signedBalanceOf(self.proxy.address).call()
            supply_now = d.buck.functions.totalSupply().call()
            bvib = d.basket.functions.basketValueInBuck().call() / 1e18
        except Exception as e:
            ctr["fat_err"] = repr(e)[:200]
            return

        # System-wide issuance rate since our last step -- the pre-issuance
        # demand signal.  totalSupply == sum_a max(0, signedRaw(a)), so it
        # rises exactly as BUCK is sold into circulation.
        prev = self._supply_prev if self._supply_prev else supply_now
        dtd = _dt_days(scenario)
        # Per-DAY issuance rate: the raw since-last-step delta divided by
        # the days one step represents, so ff's super-linear term reads the
        # same signal at any cadence.
        issue_rate = max(0.0, (supply_now - prev) / max(1, prev)) / dtd
        self._supply_prev = supply_now

        # One-off shock bookkeeping (observability).
        if self.shock_day >= 0 and day >= self.shock_day and not self._shock_logged:
            self._shock_logged = True
            ctr["fatShocksFired"] = ctr.get("fatShocksFired", 0) + 1
        if self.shock_day >= 0 and day == 0:
            ctr["fatShockSeeded"] = ctr.get("fatShockSeeded", 0) + 1

        # Drawn credit is chain truth: the borrowing proxy's own negative
        # signed balance.  The reserve lives in the separate escrow proxy and
        # can never contaminate this reading.
        drawn = max(0, -signed)

        # Counter-cyclical unwind of any pending-release reserve happens
        # every step, whatever branch the draw target selects below.
        self._release_pending(d, bvib, ctr, dtd)

        # (A) Adoption ramp (+ optional shock) scales the effective target.
        cap = self._cap_frac(day, getattr(scenario, "days", 1))
        target = int(self.util_target * limit * cap)
        dead = int(self.band * max(1, limit))
        n = len(d.tokens)

        if drawn < target - dead:
            # Lever up.  Never issue past currently spendable headroom.
            delta = target - drawn
            spendable = max(0, limit - drawn)
            delta = min(delta, spendable)
            if delta < 10 ** 6:            # sub-$1 moves: skip
                return

            # (B) Funding factor for THIS tranche: counter-cyclical in BOTH
            # directions (mirroring the on-chain fundingFactor(), which
            # SATURATES AT ZERO in deflation: base = max(0, 1 + cyc*(bv-1)/bv))
            # x super-linear in the system issuance rate.  Above parity a hot
            # uptake demands a heavy pre-buy toll; BELOW parity (BUCK rich,
            # the system starving for supply) the toll melts away, so
            # issuance is cheap exactly when it pushes TOWARD parity.  An
            # always->=1 floor here was the deflation trap: budgets drained
            # buying reserve BUCK at a premium, the issuance plant saturated
            # below the setpoint, and K pinned at its ceiling.
            base = max(0.0, 1.0 + self.ff_cyc * (bvib - 1.0) / max(bvib, 1e-9))
            ff = base * (1.0 + self.ff_amp * (issue_rate ** self.ff_pow))
            req = self.reserve_req
            bal = self._reserve_balance(d)
            need_add = int(delta * ff)
            # Top the escrow up toward (rolling requirement + this tranche)
            # by BUYING BUCK straight into the escrow -- the pre-issuance
            # demand.  Pending-release stock already in the escrow counts
            # toward the requirement, so a re-lever after a retire REUSES it
            # instead of double-buying.
            shortfall = (req + need_add) - bal
            if shortfall > 10 ** 6 and d.pool_ub:
                try:
                    got = self._buy_buck(d, d.pool_ub, d.usdc, shortfall,
                                         d.fee_ub,
                                         recipient=self.escrow.address)
                except Exception as e:
                    ctr["fat_prefund_err"] = repr(e)[:200]
                    got = 0
                if got > 0:
                    bal += got
                    ctr["fatPreFundBought"] = (
                        ctr.get("fatPreFundBought", 0) + got)

            # Throttle: only issue as much as the funded escrow supports
            # BEYOND the rolling requirement.  Unlike the old cumulative
            # design this recovers: retirement shrinks the requirement, so
            # the channel re-arms instead of ratcheting shut for good.
            # A zero toll (ff saturated at 0 in deflation) NEVER binds --
            # even when demurrage has nudged the escrow below the standing
            # requirement (avail < 0 == need_add).
            avail = bal - req
            if avail < need_add:
                if ff > 0:
                    delta_funded = int(max(0, avail) / ff)
                else:
                    delta_funded = delta      # zero toll: funding can't bind
                if delta_funded < delta:
                    ctr["fatThrottled"] = ctr.get("fatThrottled", 0) + 1
                delta = min(delta, delta_funded)
            if delta < 10 ** 6:
                return

            internal = int(self.internal_share * delta)
            external = delta - internal
            i = self._rng.randrange(n)     # which TOKEN/BUCK pool to push
            try:
                # Each leg is capped to the borrowing proxy's live spendable
                # (== unused credit; it holds no reserve BUCK any more) read
                # immediately before its transfer.  (A rare "exceeds
                # spendable" can still surface from the shared SimLP swap leg
                # under heavy flow; it is caught below, benign.)
                sold = self._sell_capped(d, d.pool_buck[i], internal)
                if d.pool_ub:
                    sold += self._sell_capped(d, d.pool_ub, external)
                if sold > 0:
                    ctr["fatEntries"] = ctr.get("fatEntries", 0) + 1
                    ctr["fatIssued"] = ctr.get("fatIssued", 0) + sold
                    # Commit a tranche for what actually sold; whatever part
                    # of the escrow now backs it is no longer pending.
                    self._tranches.append([sold, ff])
                    self.pending_release = min(
                        self.pending_release,
                        max(0, bal - self.reserve_req))
            except Exception as e:
                ctr["fat_sell_err"] = repr(e)[:200]

        elif drawn > target + dead:
            # (C) Deleverage / redemption.  K tightened (limit shrank) or the
            # ramp pulled the target down -> we are over-utilized.  DOCTRINE
            # NOTE: nothing on-chain forces this recovery -- an overdrawn
            # account simply cannot extend more credit, and the Jubilee fund
            # unwinds excess over time.  This buy-back is the borrower's own
            # VOLUNTARY utilization policy (the negative feedback the
            # equilibrium loop measures); `retire_rate` is its effort knob
            # (TOML-tunable to 0 to model a purely passive borrower).  Buy BUCK
            # back (removing it from the TOKEN/BUCK pool pulls basketValue
            # toward 1.0) and burn what we can.  The buy-back accelerates with
            # the discount -- "money at a discount": when BUCK is cheap vs the
            # basket, redeemers bid it back hard (counter-cyclical demand).
            excess = drawn - target
            retire_eff = min(1.0, self.retire_rate * dtd * (
                1.0 + self.disc_gain * max(0.0, bvib - 1.0)))
            want = int(retire_eff * excess)
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
            if got > 0:
                # Roll the reserve: retired tranches release their ff-scaled
                # reserve into the pending buffer (unwound counter-cyclically
                # by _release_pending), instead of standing as dead demand.
                self.pending_release += self._pop_tranches(got)
                # Best-effort burn to actually contract supply (deactivates a
                # sliver of credit; skipped if it would break solvency).
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
        # Heterogeneous, seeded knobs -- all experiment-overridable via
        # [agents.SaverAgent].  Scaled ~10x the first cut: private demand has
        # to be comparable to BUCK supply (~15-20M) to actually bid BUCK
        # toward the basket, not a rounding error against it.
        cls = type(self).__name__
        self._class_seq = slot                      # regime counter == ordinal
        self._class_count = getattr(scenario, "agents", {}).get(cls, 1)
        # "endow" arrives immediately like the default; it differs only in
        # starting with pre-window BUCK inventory (below, after the proxy
        # binds).
        arrive_mode = _spec(scenario, cls, "arrive_mode", "immediate")
        if arrive_mode not in ("immediate", "endow"):
            self.arrive_day = _growth_arrival_day(
                self._class_seq, self._class_count, scenario, cls, r)
            self.depart_day = _growth_departure_day(
                self._class_seq, self._class_count, scenario, cls, r)
        else:
            self.arrive_day = 0
            self.depart_day = None
        self.attract_gain = float(_spec(scenario, cls, "attract_gain", 0.05))
        self._departed = False
        self.savings_goal = _draw(scenario, cls, "savings_goal_m", r,
                                  (10, 30)) * 1_000_000 * 10 ** 6  # BUCK target
        self._base_rate_spec = _spec(scenario, cls, "base_rate_k", (200, 600))
        self.base_rate = _sample(self._base_rate_spec, r) * 1_000 * 10 ** 6
        # Gentler reactivity: big capital with hot gains over-corrected into
        # oscillation, so temper the discount/premium acceleration.
        self.disc_gain = _draw(scenario, cls, "disc_gain", r, (2.0, 6.0))
        self.prem_gain = _draw(scenario, cls, "prem_gain", r, (2.0, 6.0))
        # Reserve is a fraction of CURRENT holdings, not the goal -- a
        # goal-relative reserve was unreachable once BUCK got expensive, so
        # the sell leg never fired and the saver was buy-only.
        self.reserve_frac = _draw(scenario, cls, "reserve_frac", r, (0.30, 0.50))
        self.budget = _draw(scenario, cls, "budget_m", r,
                            (20, 60)) * 1_000_000 * 10 ** 6        # USDC pool
        self._spent = 0                            # net USDC deployed into BUCK
        self._bind_proxy(d)
        # Seed the proxy with its USDC budget to deploy over the run.
        d.chain.send(d.usdc.functions.mint(self.proxy.address, self.budget))
        # arrive_mode "endow": model the saver cohort that already existed
        # when the window opens.  Each saver arrives HOLDING endow_m BUCK
        # (positive signed balance -- totalSupply rises by the endowment),
        # transferred from SimLP against a fresh zero-premium credit, the
        # same par-value fiction that seeds the pools: SimLP carries the
        # drawn obligation, the saver owns the BUCK outright.  t0 is pure
        # balance-sheet expansion with ZERO market impact, and the sell
        # leg has inventory from the first premium instead of spending
        # the early run acquiring it through thin pools (the cold-start
        # transient this mode exists to remove).  NB: minting on the
        # saver's OWN credit would endow K-scaled *headroom* instead of
        # held BUCK (balanceOf counts unused creditLimit, which melts as
        # K falls) -- the transfer from a third party is what makes the
        # endowment real inventory.  The USDC budget is untouched.
        if arrive_mode == "endow":
            endow = int(_draw(scenario, cls, "endow_m", r,
                              (1, 3)) * 1_000_000 * 10 ** 6)
            if endow >= 10 ** 6:
                # Deploy-time SimLP seed formula: mint activates coverage,
                # freeing mint*K spendable, so size mint (and face) off the
                # live resting K with a 20% margin.
                k0 = d.kctrl.functions.buckK().call()
                mint_amt = (endow * 10 ** 18 // max(1, k0)) * 12 // 10
                face = max(2 * endow, mint_amt * 12 // 10)
                now_ts = d.w3.eth.get_block("latest")["timestamp"]
                d.chain.send(d.credit.functions.createCredit(
                    d.simlp.address, 0, face, 0, 0, 0, now_ts, 0))
                d.chain.send(d.simlp.functions.exec(
                    d.buck.address, d.buck.encode_abi(
                        "mint(uint256)", args=[mint_amt])))
                d.chain.send(d.simlp.functions.exec(
                    d.buck.address, d.buck.encode_abi(
                        "transfer", args=[self.proxy.address, endow])))

    def _apply_regime(self, day) -> str:
        """Primary knob = base_rate (savings cadence).  Redraw from the SAME
        spec as setup() -- an earlier pre-rescale range here (20k-60k vs the
        200k-600k setup range) cut a regime saver's demand 10x permanently
        and destabilized long runs."""
        old = self.base_rate
        self.base_rate = _sample(self._base_rate_spec, self._rng) * 1_000 * 10 ** 6
        return (f"day{day} saver#{self.idx} base_rate "
                f"{old // 10 ** 6}->{self.base_rate // 10 ** 6}")

    def act(self, d, scenario, day, tick, ctr) -> None:
        self._maybe_regime(d, day, ctr)
        if self.proxy is None or not d.pool_ub:
            return
        if not _growth_active(self, day, ctr):
            # Departure (decline regimes): liquidate BUCK savings once.
            if (self.depart_day is not None and day >= self.depart_day
                    and not self._departed):
                self._departed = True
                try:
                    holding = d.chain.balance_of(d.buck, self.proxy.address)
                    if holding > 10 ** 6:
                        self._swap_via_simlp(d, d.pool_ub, d.buck, holding,
                                             self.proxy.address)
                    ctr["growthDepartures"] = ctr.get("growthDepartures", 0) + 1
                except Exception as e:
                    ctr["saver_depart_err"] = repr(e)[:200]
            return
        try:
            # Value reference is the BASKET: BUCK is discounted when a basket costs
            # less than 1 BUCK (basketValueInBuck < 1).  This is the same
            # observable the controller defends, so buying leans into the peg.
            bvib = d.basket.functions.basketValueInBuck().call() / 1e18
            discount = max(0.0, bvib - 1.0)      # BUCK below basket value -> buy
            premium = max(0.0, 1.0 - bvib)       # BUCK above basket value -> sell
            holding = d.chain.balance_of(d.buck, self.proxy.address)
            held_usdc = d.chain.balance_of(d.usdc, self.proxy.address)

            dtd = _dt_days(scenario)      # base_rate is per-DAY
            if discount > 0 and holding < self.savings_goal \
                    and self._spent < self.budget:
                # Buy below value: accelerate accumulation with the discount.
                rate = int(self.base_rate * dtd
                           * (1.0 + self.disc_gain * discount))
                amt = min(rate, held_usdc, self.budget - self._spent)
                if amt < 10 ** 6:               # sub-$1 move: skip
                    return
                self._swap_via_simlp(d, d.pool_ub, d.usdc, amt,
                                     self.proxy.address)
                self._spent += amt
                ctr["saverBuys"] = ctr.get("saverBuys", 0) + 1
                ctr["saverSpent"] = ctr.get("saverSpent", 0) + amt

            elif premium > 0 and holding > 10 ** 6:
                # Spend above value: sell BUCK for USDC, keeping reserve_frac.
                ru = d.chain.balance_of(d.usdc, d.pool_ub)
                rb = d.chain.balance_of(d.buck, d.pool_ub)
                spot = ru * PARITY // rb if rb else 0
                want_usdc = int(self.base_rate * dtd
                                * (1.0 + self.prem_gain * premium))
                # BUCK to sell to realize ~want_usdc of USDC at current spot.
                sell = want_usdc * PARITY // spot if spot else 0
                keep = int(self.reserve_frac * holding)   # reserve vs holdings
                sell = min(sell, holding - keep)
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


@_register
class BuckCreditDebtorAgent(_ProxyAgent):
    """The HONEST mortgage debtor: fakes as little of the BUCK system as
    possible.  Where earlier debtor models (and FatCreditBorrowerAgent's
    escrow/tranche machinery) SIMULATED the funding reserve and Jubilee, this
    agent simply plays the real contracts:

      * its BuckCredit NFTs carry a REAL premiumRate, so Buck.mint's
        funding-factor gate applies for real: balanceOf(minter) must cover
        poolPrincipal * fundingFactor/1e18 BEFORE activation, and the
        insurance principal is genuinely paid to the insurance pool;
      * it SAVES for that gate: monthly, it accumulates a BUCK buffer by
        buying on the open market (the real pre-issuance demand the gate is
        designed to compel) -- preferring to buy when BUCK is at/below value;
      * it issues in TRANCHES: mint (activate) just before selling, so each
        tranche re-faces the live gate at the live fundingFactor;
      * Jubilee is NOT simulated: the fund accrues on-chain; this agent's
        obligations are pure chain truth (drawn = -signedBalanceOf), valued
        at par.

    The optimal control is the same theta law -- deploy while
    max(0, bvib-1) <= theta*apr and the mortgage remains -- plus a
    save_rate knob (fraction of spare cash routed to the BUCK buffer) and a
    staggered arrival day, so a population arrives over time with varying
    models.  Doctrine: no forced overdraw recovery (overdraw_effort,
    default 0).
    """

    MONTH = 30
    HARVEST_MONTHS = (8, 9)
    SINK = "0x000000000000000000000000000000000000dEaD"
    N_CREDITS = 4

    _arrival_seq = 0              # class ordinal (reset in build_equilibrium)

    def setup(self, d, scenario, rng) -> None:
        self._rng = _agent_rng(scenario.seed, type(self).__name__, self.idx)
        r = self._rng
        cls = type(self).__name__
        self._class_seq = type(self)._arrival_seq
        type(self)._arrival_seq += 1
        self._class_count = getattr(scenario, "agents", {}).get(cls, 1)
        m6 = 1_000 * 10 ** 6
        self.theta = _draw(scenario, cls, "theta", r, (0.0, 3.0))
        flip = "salary" if r.random() < 0.5 else "lumpy"
        self.pattern = str(_spec(scenario, cls, "pattern", "")) or flip
        self.apr = _draw(scenario, cls, "apr", r, (0.045, 0.065))
        self.mortgage = int(_draw(scenario, cls, "mortgage_k", r,
                                  (600, 1400)) * m6)
        self.income_annual = int(_draw(scenario, cls, "income_k", r,
                                       (180, 320)) * m6)
        face = int(_draw(scenario, cls, "face_k", r, (600, 1200)) * m6)
        # 0 => poolPrincipal 0 => funding gate exempt (see the module note).
        self.premium_rate = int(_draw(scenario, cls, "premium_bp", r, 0))
        self.save_rate = _draw(scenario, cls, "save_rate", r, (0.25, 0.75))
        self.retire_disc = _draw(scenario, cls, "retire_disc", r, 0.02)
        self.cash_buffer = int(_draw(scenario, cls, "buffer_k", r, 20) * m6)
        self.overdraw_effort = _draw(scenario, cls, "overdraw_effort", r, 0.0)
        # How hard this debtor is willing to push the BUCK/USDC route to get
        # its refinancing done.  This is the throttle that matters: a debtor
        # who dumps a whole tranche craters the exit it needs, lifts
        # basketValueInBuck and pulls K down on everyone -- so the market's
        # depth, not a hand-set tranche cap, is what paces refinancing.
        self.max_impact_bp = int(_draw(scenario, cls, "max_impact_bp", r,
                                       (25, 150)))
        # refi_mode "atomic": evaluate the WHOLE mortgage conversion ex ante
        # -- quote the BUCK needed to net the full USDC mortgage after
        # slippage and fees, check issuance capacity for that amount, and
        # execute the complete BuckCredit -> BUCK -> USDC -> payoff in one
        # act, or not at all (no partial commits, no premium paid on a
        # refinance that cannot complete).  "paced" (default) is the
        # original audited behavior: commit the tranche, then dribble the
        # exit through an impact cap.
        self._refi_atomic = (_spec(scenario, cls, "refi_mode", "paced")
                             == "atomic")
        self._atomic_B = 0
        horizon = max(1, int(getattr(scenario, "days", 1)))
        # Arrival: growth-regime schedule when arrive_mode is set; "endog"
        # replaces the fixed stagger with a HAZARD-driven clock (advanced in
        # act(): rate ~ credit attractiveness); else the legacy uniform
        # stagger over arrive_frac of the horizon.
        mode = _spec(scenario, cls, "arrive_mode", "immediate")
        self._endog = (mode == "endog")
        if self._endog:
            # tau = the day this debtor WOULD arrive at neutral (hazard 1.0)
            # attractiveness -- the same arrive_frac spread as the open-loop
            # stagger, so hazard==1 reproduces the "originate" arm exactly.
            self._endog_tau = _draw(scenario, cls, "arrive_frac", r,
                                    (0.0, 1.0)) * horizon
            self._endog_clock = 0.0
            self._endog_last = None
            self._arr_gain = float(_spec(scenario, cls, "arr_gain", 5.0))
            self._arr_max = float(_spec(scenario, cls, "arr_max", 3.0))
            self._arr_k_ref = float(_spec(scenario, cls, "arr_k_ref", 0.75))
            self.arrive_day = None      # set on endogenous arrival
        elif mode != "immediate":
            self.arrive_day = _growth_arrival_day(
                self._class_seq, self._class_count, scenario, cls, r)
        else:
            self.arrive_day = int(_draw(scenario, cls, "arrive_frac", r,
                                        (0.0, 0.5)) * horizon)
        self.attract_gain = float(_spec(scenario, cls, "attract_gain", 0.05))
        self._retired_flagged = False
        mrate = self.apr / 12.0
        self.payment = int(self.mortgage * mrate
                           / (1.0 - (1.0 + mrate) ** -300))
        self.tranche_cap = self.payment * 12

        self.hypo_mortgage = self.mortgage
        self.hypo_cash = 0
        # BUCK-path savings: the receipt this debtor holds in the BuckBasket,
        # and the USDC principal it put in.  The counterfactual has no
        # equivalent because every dollar of its income is owed to the bank.
        self._basket_receipt: int | None = None
        self._basket_in = 0
        start = self.arrive_day if self.arrive_day is not None else 0
        self._last_day = start
        self._last_month_day = start - self.MONTH
        self.deploys = 0
        self.throttled = 0
        # Cost telemetry (par-valued, 6-dec dollars): what the BUCK path
        # actually pays vs the counterfactual -- insurance principal
        # surrendered at mint, and the par-value lost (or gained, negative)
        # crossing the pool in either direction.  unwound/unwind_loss
        # isolate the voluntary-buyback leg (so passive Jubilee melt can be
        # distinguished from the agent's own purchases).
        self.premium_paid = 0
        self.trade_loss = 0
        self.unwound = 0
        self.unwind_loss = 0

        self._bind_proxy(d)
        now_ts = d.w3.eth.get_block("latest")["timestamp"]
        per = max(1, face // self.N_CREDITS)
        self._proxy_exec(d, d.credit.address, d.credit.encode_abi(
            "setCreditIssuer", args=[getattr(d.chain.deployer, "address", d.chain.deployer), True]))
        for _ in range(self.N_CREDITS):
            d.chain.send(d.credit.functions.createCredit(
                self.proxy.address, 0, per, 0, 0, 0, now_ts,
                self.premium_rate))
        self._face = per * self.N_CREDITS
        self._token_ids = [
            d.credit.functions.tokenOfOwnerByIndex(
                self.proxy.address, i).call()
            for i in range(self.N_CREDITS)]

    # -- instrumented market legs ------------------------------------------- #

    def _buy_track(self, d, want_buck: int) -> int:
        """_buy_buck through the BUCK/USDC pool, accumulating the par-value
        cost (USDC spent minus BUCK received; negative = bought below par)
        into trade_loss.  Returns BUCK bought."""
        before = d.chain.balance_of(d.usdc, self.proxy.address)
        got = self._buy_buck(d, d.pool_ub, d.usdc, want_buck, d.fee_ub)
        spent = before - d.chain.balance_of(d.usdc, self.proxy.address)
        self.trade_loss += spent - got
        return got

    # -- off-chain fiat legs (income + bank payments) ----------------------- #

    def _income(self, d, months: int, day) -> int:
        if self.pattern == "salary":
            amt = self.income_annual * months // 12
        else:
            month = (day // self.MONTH) % 12
            amt = (self.income_annual // len(self.HARVEST_MONTHS)
                   if month in self.HARVEST_MONTHS else 0)
        if amt > 0:
            d.chain.send(d.usdc.functions.mint(self.proxy.address, amt))
        return amt

    def _pay_bank(self, d, amt: int) -> int:
        held = d.chain.balance_of(d.usdc, self.proxy.address)
        pay = min(amt, held)
        if pay > 0:
            self._proxy_exec(
                d, d.usdc.address,
                d.usdc.encode_abi("transfer(address,uint256)",
                                  args=[self.SINK, int(pay)]))
        return pay

    # -- observability ------------------------------------------------------- #

    def octl_state(self, d) -> dict | None:
        if self.proxy is None:
            return None
        try:
            cash = d.chain.balance_of(d.usdc, self.proxy.address)
            signed = d.buck.functions.signedBalanceOf(
                self.proxy.address).call()
            limit = d.buck.functions.creditLimit(self.proxy.address).call()
        except Exception:
            return None
        drawn = max(0, -signed)
        held = max(0, signed)
        # Pure chain truth at par; the liability side is the chain's OWN
        # close-cost quote: drawn net of the accrued Jubilee relief on the
        # credits (BuckCredit.jubileeRelief -- the redemption discount that
        # melts ~2%/yr while the position is carried).
        jub = 0
        try:
            for tid in self._token_ids:
                jub += d.credit.functions.jubileeRelief(tid).call()
        except Exception:
            jub = 0
        jub = min(jub, drawn)
        # The BuckBasket position is part of this debtor's wealth.  Leaving
        # it out would repeat the `directMintPnl` mistake in reverse: money
        # that moved from a counted bucket into an uncounted one, read as a
        # loss.  Valued at the principal deposited, which is the same
        # convention Snapshot._agent_value uses for a BUCK-side deposit.
        basket = 0
        if self._basket_receipt is not None:
            try:
                dep = d.basket.functions.deposits(self._basket_receipt).call()
                basket = dep[0]
            except Exception:
                basket = self._basket_in
        nw = cash + held + basket - self.mortgage - drawn + jub
        return {"idx": self.idx, "theta": round(self.theta, 2),
                "pattern": self.pattern, "nw": nw,
                "hypo": self.hypo_cash - self.hypo_mortgage, "cash": cash,
                "basket": basket, "basket_in": self._basket_in,
                "limit": limit, "mortgage": self.mortgage, "drawn": drawn,
                "jub": jub, "deploys": self.deploys,
                "throttled": self.throttled,
                "premium_paid": self.premium_paid,
                "trade_loss": self.trade_loss,
                "unwound": self.unwound,
                "unwind_loss": self.unwind_loss,
                "apr": self.apr, "payment": self.payment,
                "income": self.income_annual,
                "active": bool(getattr(self, "_growth_arrived", False))}

    # -- atomic refinance planning ------------------------------------------- #

    def _atomic_plan(self, d, limit, drawn, unactivated, ctr):
        """Ex-ante evaluation of the WHOLE mortgage conversion.

        Quote the BUCK/USDC pool for the BUCK input needed to net the FULL
        remaining USDC mortgage after slippage and the pool fee (constant-
        product closed form on the full-range floating pool, +0.5% safety
        margin), then check the credit side can supply it: spendable
        headroom plus K-scaled unactivated face.  Returns (face units to
        mint, all-in execution discount vs par) and stashes the sale size
        in self._atomic_B; on any infeasibility returns (0, inf) -- nothing
        is minted, no premium is paid, the debtor simply waits.  This makes
        the population self-limiting: refinances execute only as the market
        can bear them, at full size or not at all."""
        M = int(self.mortgage)
        self._atomic_B = 0
        if M <= 10 ** 6 or not d.pool_ub:
            return 0, 0.0
        ru = d.chain.balance_of(d.usdc, d.pool_ub)
        rb = d.chain.balance_of(d.buck, d.pool_ub)
        if ru <= 0 or rb <= 0 or M * 5 >= ru * 4:
            # Needing >80% of the pool's USDC side is not a quote, it is a
            # liquidity hole; wait for depth.
            ctr["bcdAtomicDeclined"] = ctr.get("bcdAtomicDeclined", 0) + 1
            ctr.setdefault("bcdAtomicWhy", {})
            ctr["bcdAtomicWhy"]["depth"] = (
                ctr["bcdAtomicWhy"].get("depth", 0) + 1)
            return 0, float("inf")
        fee = (getattr(d, "fee_ub", 0) or 0) / 1e6
        bprime = M * rb // max(1, ru - M)
        need_b = int(bprime / max(1e-9, 1.0 - fee) * 1.005) + 10 ** 6
        d_eff = max(0.0, 1.0 - M / need_b)
        try:
            k = int(d.kctrl.functions.buckK().call())
        except Exception:
            k = 0
        spendable = max(0, limit - drawn)
        capacity = spendable + (unactivated * k // 10 ** 18) * 95 // 100
        if need_b > capacity:
            ctr["bcdAtomicDeclined"] = ctr.get("bcdAtomicDeclined", 0) + 1
            ctr.setdefault("bcdAtomicWhy", {})
            ctr["bcdAtomicWhy"]["capacity"] = (
                ctr["bcdAtomicWhy"].get("capacity", 0) + 1)
            return 0, float("inf")
        face_need = 0
        if need_b > spendable and k > 0:
            face_need = min(unactivated,
                            ((need_b - spendable) * 10 ** 18 // k)
                            * 105 // 100)
        self._atomic_B = need_b
        return face_need, d_eff

    # -- the loop -------------------------------------------------------------- #

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0 or self.proxy is None:
            return
        if getattr(self, "_endog", False) and self.arrive_day is None:
            # Endogenous origination: a pending debtor's clock advances at a
            # rate ~ how competitive BUCK issuance looks against their
            # traditional mortgage RIGHT NOW -- generous K-scaled capacity
            # (k/k_ref) times sell-side price advantage (BUCK at/above par:
            # 1 + arr_gain*(1-bvib)).  hazard==1 at (K==k_ref, bv==1)
            # reproduces the open-loop "originate" stagger exactly; a railed
            # controller begging for supply pulls arrivals in, a crushed K /
            # rich bv stalls them.  Capped at arr_max: adoption has real-
            # world frictions no price signal removes.
            if self._endog_last is None:
                self._endog_last = day
            dd = max(0, day - self._endog_last)
            self._endog_last = day
            if dd:
                try:
                    k = d.kctrl.functions.buckK().call() / 1e18
                    bvib = d.basket.functions.basketValueInBuck().call() / 1e18
                except Exception:
                    k, bvib = self._arr_k_ref, 1.0
                hazard = max(0.0, k / max(1e-9, self._arr_k_ref)) \
                    * max(0.0, 1.0 + self._arr_gain * (1.0 - bvib))
                self._endog_clock += dd * min(self._arr_max, hazard)
            if self._endog_clock < self._endog_tau:
                return
            self.arrive_day = day
            ctr["endogDebtorArrivals"] = ctr.get("endogDebtorArrivals", 0) + 1
        if not _growth_active(self, day, ctr):
            return
        if not getattr(self, "_started", False):
            # First active step (scheduled or attraction-warped arrival):
            # anchor the ledgers to the ACTUAL arrival day.
            self._started = True
            self._last_day = day
            self._last_month_day = day - self.MONTH
        days = max(0, day - self._last_day)
        self._last_day = day
        if days > 0:
            g = (1.0 + self.apr / 365.0) ** days
            self.mortgage = int(self.mortgage * g)
            self.hypo_mortgage = int(self.hypo_mortgage * g)
        if day - self._last_month_day < self.MONTH:
            return
        months = max(1, (day - self._last_month_day) // self.MONTH)
        self._last_month_day = day

        try:
            bvib = d.basket.functions.basketValueInBuck().call() / 1e18
            ff = d.kctrl.functions.fundingFactor().call()
            signed = d.buck.functions.signedBalanceOf(
                self.proxy.address).call()
            limit = d.buck.functions.creditLimit(self.proxy.address).call()
        except Exception as e:
            ctr["bcd_err"] = repr(e)[:200]
            return
        drawn = max(0, -signed)

        # 1. Income + mandatory mortgage service, both ledgers.
        inc = self._income(d, months, day)
        self.hypo_cash += inc
        due = min(self.payment * months, self.mortgage)
        self.mortgage -= self._pay_bank(d, due)
        hdue = min(self.payment * months, self.hypo_mortgage)
        hpaid = min(hdue, self.hypo_cash)
        self.hypo_cash -= hpaid
        self.hypo_mortgage -= hpaid

        disc = max(0.0, bvib - 1.0)

        # ENCUMBRANCE MATCHING.
        #
        # The comparison only means something if both paths keep the SAME
        # claim against the same asset.  The mortgage amortizes on schedule,
        # so `hypo_mortgage` IS the encumbrance trajectory, and the BUCK path
        # tracks it rather than drawing whatever it can.
        #
        # Net of Jubilee: relief melts the drawn balance at ~2%/yr, so an
        # effective liability of `drawn - jub` is what actually encumbers the
        # asset.  Matching means
        #
        #     drawn - jub == hypo_mortgage      =>   target = hypo_mortgage + jub
        #
        # and the melt is therefore a benefit -- it lets the BUCK side carry
        # more drawn for the same encumbrance, which is exactly the asymmetry
        # the comparison is meant to price.
        jub = 0
        try:
            for tid in self._token_ids:
                jub += d.credit.functions.jubileeRelief(tid).call()
        except Exception:
            jub = 0
        jub = min(jub, drawn)
        target = self.hypo_mortgage + jub

        # Tranche capacity comes from UNACTIVATED face: creditLimit only
        # reflects credit already activated by a mint, and the mint itself
        # is what activates -- so size against faceValue - activatedValue
        # (real chain reads), plus any already-activated unused headroom.
        unactivated = 0
        try:
            for tid in self._token_ids:
                face_v, act_v, _ = d.credit.functions.creditInfo(tid).call()
                unactivated += max(0, face_v - act_v)
        except Exception:
            unactivated = 0
        headroom = max(0, limit - drawn) + unactivated

        # 2. SAVE for the real funding gate: estimate the next tranche's
        #    insurance principal via quoteMint and top the BUCK buffer up to
        #    the fundingFactor-scaled requirement -- buying preferentially
        #    when BUCK is at/below its basket value (disc small).
        # Draw toward the encumbrance target, not toward a calendar.  On the
        # first month that is the whole mortgage: the refinance is a single
        # act, paced only by headroom and by what the exit route can absorb.
        # Atomic mode replaces this with the ex-ante whole-conversion plan:
        # want_tranche becomes the FACE to activate for the full quoted
        # sale, gate_disc the all-in execution discount vs par.
        if self._refi_atomic:
            want_tranche, gate_disc = self._atomic_plan(
                d, limit, drawn, unactivated, ctr)
            ready = self._atomic_B >= 10 ** 6
        else:
            want_tranche = min(max(0, target - drawn), headroom)
            gate_disc = disc
            ready = want_tranche >= 10 ** 6
        if want_tranche >= 10 ** 6 and self.mortgage > 10 ** 6:
            try:
                _, principal = d.buck.functions.quoteMint(
                    want_tranche, self._token_ids).call()
            except Exception:
                principal = 0
            required = principal * ff // 10 ** 18 if ff else 0
            bal = d.buck.functions.balanceOf(self.proxy.address).call()
            short = required - bal
            if short > 10 ** 6 and gate_disc <= max(self.theta * self.apr,
                                                    0.01):
                cash = d.chain.balance_of(d.usdc, self.proxy.address)
                budget = int(max(0, cash - self.cash_buffer)
                             * self.save_rate)
                if budget > 10 ** 6 and d.pool_ub:
                    try:
                        self._buy_track(d, min(short, budget))
                        ctr["bcdSaved"] = ctr.get("bcdSaved", 0) + 1
                    except Exception as e:
                        ctr["bcd_save_err"] = repr(e)[:200]

        # 3. THE CONTROL: activate a tranche through the REAL gate, then
        #    deploy it against the mortgage.  Atomic mode fires only when
        #    the whole conversion clears its ex-ante checks (ready); the
        #    face mint may be zero if spendable headroom already covers it.
        if gate_disc <= self.theta * self.apr and ready:
            mint_amt = min(want_tranche, unactivated)
            try:
                if mint_amt >= 10 ** 6:
                    pre = d.buck.functions.signedBalanceOf(
                        self.proxy.address).call()
                    self._proxy_exec(
                        d, d.buck.address,
                        d.buck.encode_abi("mint(uint256)", args=[mint_amt]))
                    post = d.buck.functions.signedBalanceOf(
                        self.proxy.address).call()
                    # mint activates credit (creditLimit += coverage) and
                    # debits the SIGNED balance by exactly poolPrincipal --
                    # the insurance premium is paid by drawing credit, so it
                    # surfaces in `drawn` (and hence nw); track it here so
                    # the advantage decomposition can separate it out.
                    self.premium_paid += max(0, pre - post)
                minted = True
            except Exception as e:
                minted = False       # retry later
                self.throttled += 1
                ctr["bcdThrottled"] = ctr.get("bcdThrottled", 0) + 1
                why = repr(e)[:120]
                ctr.setdefault("bcdWhy", {})
                ctr["bcdWhy"][why] = ctr["bcdWhy"].get(why, 0) + 1
            if minted and d.pool_ub:
                before = d.chain.balance_of(d.usdc, self.proxy.address)
                try:
                    if self._refi_atomic:
                        # The whole quoted sale in one act -- the slippage
                        # was computed and accepted before anything was
                        # committed, so no impact cap applies.
                        sold = self._sell_capped(d, d.pool_ub,
                                                 self._atomic_B)
                    else:
                        # Sell only what the route can absorb within this
                        # debtor's impact budget.  The remainder stays
                        # drawn and is sold on later ticks, so refinancing
                        # paces itself to market depth instead of to a
                        # calendar.
                        r_out = d.chain.balance_of(d.buck, d.pool_ub)
                        sold = self._sell_capped(
                            d, d.pool_ub,
                            min(want_tranche,
                                max(10 ** 6, _impact_cap(
                                    r_out, self.max_impact_bp))))
                except Exception as e:
                    ctr["bcd_sell_err"] = repr(e)[:200]
                    sold = 0
                if sold > 0:
                    got = d.chain.balance_of(
                        d.usdc, self.proxy.address) - before
                    self.trade_loss += sold - got
                    principal = min(got, self.mortgage)
                    self._pay_bank(d, principal)
                    self.mortgage -= principal
                    self.deploys += 1
                    ctr["bcdDeploys"] = ctr.get("bcdDeploys", 0) + 1
                    if self._refi_atomic and self.mortgage <= 10 ** 6:
                        ctr["bcdAtomicRefis"] = (
                            ctr.get("bcdAtomicRefis", 0) + 1)
        # 4. AMORTIZE THE CLAIM on the same schedule as the mortgage would
        #    have.  `hypo_mortgage` is the counterfactual's remaining
        #    balance, so buying BUCK back until `drawn - jub` meets it keeps
        #    both paths encumbering the asset identically month by month.
        #
        #    What it costs is the PRINCIPAL portion of the payment.  What the
        #    mortgage path additionally pays -- the INTEREST portion -- is
        #    what the BUCK path keeps, and early in a 300-month amortization
        #    that is most of the payment.  This is the arbitrage, and this is
        #    where it shows up as cash.
        if self.mortgage <= 10 ** 6:
            over = drawn - jub - self.hypo_mortgage
            cash = d.chain.balance_of(d.usdc, self.proxy.address)
            spare = max(0, cash - self.cash_buffer)
            if over > 10 ** 6 and spare > 10 ** 6 and d.pool_ub:
                r_out = d.chain.balance_of(d.buck, d.pool_ub)
                want = min(over, spare,
                           max(10 ** 6, _impact_cap(r_out, self.max_impact_bp)))
                try:
                    got = self._buy_track(d, want)
                    if got > 0:
                        ctr["bcdRepaid"] = ctr.get("bcdRepaid", 0) + got
                except Exception as e:
                    ctr["bcd_repay_err"] = repr(e)[:200]

            # 5. INVEST THE SURPLUS.  The counterfactual has no equivalent
            #    line: every dollar of its income is owed to the bank.  Here
            #    the interest that is never paid is free, and idle BUCK is
            #    what the BuckBasket exists to absorb -- so the comparison
            #    includes what that balance actually earns, not what it would
            #    earn if left in a drawer.
            cash = d.chain.balance_of(d.usdc, self.proxy.address)
            spare = max(0, cash - self.cash_buffer)
            if spare > 10 ** 6 and self._basket_receipt is None and d.pool_ub:
                try:
                    got = self._buy_track(d, spare)
                    if got > 10 ** 6:
                        self._proxy_exec(d, d.buck.address, d.buck.encode_abi(
                            "approve(address,uint256)",
                            args=[d.basket.address, got]))
                        rcpt = self._proxy_exec(
                            d, d.basket.address, d.basket.encode_abi(
                                "depositToken(address,uint256,uint256)",
                                args=[d.buck.address, got, 0]))
                        for log in rcpt["logs"]:
                            if log["topics"][0] == d.deposited_topic:
                                self._basket_receipt = int.from_bytes(
                                    log["topics"][2], "big")
                                break
                        self._basket_in += got
                        ctr["bcdInvested"] = ctr.get("bcdInvested", 0) + got
                except Exception as e:
                    ctr["bcd_invest_err"] = repr(e)[:200]

        if self.mortgage <= 10 ** 6 and not self._retired_flagged:
            # The attraction signal: a neighbor just became mortgage-free.
            self._retired_flagged = True
            ctr["neighborsRetired"] = ctr.get("neighborsRetired", 0) + 1

        # 4. Voluntary unwind only (doctrine: overdraw is not an emergency,
        #    and the drawn balance is an outstanding claim on OWN assets --
        #    there is never urgency to buy it back).  The obligation is
        #    par-valued at $1/BUCK, so a buyback creates value ONLY when the
        #    pool's USDC spot is BELOW par; the basket-relative discount
        #    (bvib) says nothing about the USD price actually paid.  Gate on
        #    spot <= 1 - retire_disc and size the bite so the buy itself
        #    cannot lift the pool past par: buying x out of reserve r_out
        #    moves spot p to p*(r_out/(r_out-x))^2, which stays <= 1 for
        #    x <= r_out*(1 - sqrt(p)).  (The old want=drawn slammed the
        #    whole obligation through the pool regardless of price and
        #    burned the cash pile: cash-for-slippage, adv collapse.)
        want = 0
        cash = d.chain.balance_of(d.usdc, self.proxy.address)
        spare = cash - self.cash_buffer
        if drawn > limit and self.overdraw_effort > 0:
            want = int((drawn - limit) * self.overdraw_effort)
        elif self.mortgage <= 10 ** 6 and drawn > 0:
            want = drawn
        if want > 10 ** 6 and spare > 10 ** 6 and d.pool_ub:
            r_in = d.chain.balance_of(d.usdc, d.pool_ub)
            r_out = d.chain.balance_of(d.buck, d.pool_ub)
            spot = r_in / r_out if r_out else 10.0
            if spot <= 1.0 - self.retire_disc:
                want = min(want, spare,
                           int(r_out * (1.0 - math.sqrt(spot))),
                           max(10 ** 6, _impact_cap(r_in,
                                                    self.max_impact_bp)))
                if want > 10 ** 6:
                    try:
                        tl0 = self.trade_loss
                        got = self._buy_track(d, want)
                        self.unwound += got
                        self.unwind_loss += self.trade_loss - tl0
                        if got > 0:
                            ctr["bcdRetired"] = ctr.get("bcdRetired", 0) + got
                    except Exception as e:
                        ctr["bcd_buy_err"] = repr(e)[:200]


# ── the allocator's hurdle ─────────────────────────────────────────────
#
# A real investor does not trade a fixed number of basis points off a dollar.
# BUCK is not a dollar stablecoin -- it is priced against real assets and
# labour, so its USD price is SUPPOSED to rise as commodities do, and a fixed
# band against 1.00 USDC mistakes that drift for a mispricing.  What an
# allocator actually asks is whether the whole round trip beats leaving the
# money in USDC over the period it expects to hold.  Two terms decide it:
#
#   edge    what the round trip is expected to earn in USD.  This needs TWO
#           terms, and using only the first is the mistake that cost this
#           agent 24% in the first 45 days of the 730-day run.
#
#           Write P_B for BUCK's USD price and P_K for the basket's, so that
#           bvib = P_K / P_B.  Then:
#
#             term 1   bvib - 1           BUCK cheap against the basket.
#                                         This is the K-quench recovery: the
#                                         controller drives bvib to its 1.0
#                                         setpoint.  (BuckKControllerDirect:
#                                         "error = setpoint - process;
#                                         -50_000 ppm == basket 5% rich" --
#                                         a rich basket is a cheap BUCK.)
#
#             term 2   MA(P_K)/P_K - 1    the basket cheap against its OWN
#                                         trend.  No forecast is needed for
#                                         this; it is the mean reversion the
#                                         whole design already rests on.
#
#           Term 1 alone is a SPREAD, and the agent does not hold a spread --
#           it holds an outright long, bought with USDC.  Its return is
#           therefore (spread closing) + (the basket's own USD drift), and
#           the second part is usually the larger.  Measured on day 0 of the
#           730-day run: term 1 said BUY at +8%, the spread then closed
#           exactly as predicted (BUCK -24%, basket -33%), and the position
#           still lost 24% because it was long into a falling market.  The
#           signal was right and the position was wrong.
#
#           The two terms compose, which is what makes this a correction
#           rather than a patch:
#
#             (P_K/P_B - 1) + (MA(P_K)/P_K - 1)  ~=  MA(P_K)/P_B - 1
#
#           "is BUCK cheap against where the basket is GOING", not against
#           where it happens to sit today.  With a flat basket MA == P_K and
#           it collapses back to bvib - 1, i.e. to the old rule in exactly
#           the case the old rule assumed.
#
#           The two variants take different references, because they end up
#           holding different things -- see TRACKS_BUCK below.
#
#   carry   the yield differential, per year, in REAL terms.  USDC earns a
#           T-bill and loses inflation; BUCK is inflation-neutral by
#           construction, so its real return is whatever the position itself
#           yields -- and that is where the two variants diverge.
#
#     BUY  when  edge + carry*horizon >  cost   (round trip clears its fees)
#     SELL when  edge + carry*horizon <  0      (entry cost is sunk, so the
#                                                gap between the two is
#                                                hysteresis, not indecision)
#
# The holder/basketeer asymmetry falls straight out of `carry` and is the
# whole point of the A/B: a HOLDER sits on loose BUCK and pays demurrage; a
# BASKETEER swaps it into TOKEN, holds no BUCK at all, and earns the
# rebalancing premium instead.  At the numbers below that is -3.0%/yr versus
# +1.5%/yr -- patience punishes one and pays the other, which is the whole
# result and does not depend on the inflation figure being aggressive.
USDC_YIELD = 0.045        # short T-bill / MMF / HYSA proxy (nominal)
TRUE_INFLATION = 0.035    # Deliberately CONSERVATIVE: near the headline CPI
                          # a sceptical reader already accepts, rather than
                          # the higher figure the shadow-inflation argument
                          # would justify.  It is the assumption that most
                          # flatters USDC, so the demand leg's edge here is a
                          # LOWER bound -- raise it and every conclusion below
                          # gets stronger, never weaker.
BASKET_PREMIUM = 0.025    # rebalancing premium, i.e. the EXCESS over
                          # buy-and-hold -- test/vectors/basket-flow-sim.json
                          # reports sharePriceExcessVsPassive 5.13% / 730d.
                          # NOT the 10%+ headline ROI, which is mostly the
                          # commodity drift a passive holder gets anyway.
DEMURRAGE = 0.020         # Buck.BASE_RATE_PER_YEAR (2e25 in SCALE 1e27)
ROUND_TRIP_COST = 0.005   # 5bp BUCK/USDC in + 30bp TOKEN/USDC out + slippage

# Conviction sizing: an allocator with a 4%/yr edge does not go all-in, and
# one with a 0.1% edge does not commit the same dollar.  Deployment scales
# linearly with the excess return and saturates, so the population's total
# demand is a readable function of how mispriced BUCK is.
FULL_ALLOC_EXCESS = 0.10  # excess return at which the book is fully sized
MAX_ALLOC = 0.75          # never commit the whole endowment


@_register
class DiscountBuckArbAgent(_ProxyAgent):
    """The time-for-profit BUCK arb (HOLDER variant): buys BUCK when the
    round trip beats USDC over its horizon, and simply holds.  Every issued
    BUCK has a forced future buyer -- the issuer's own redemption, or the
    Jubilee fund over ~50 years -- and the K-controller quenches inflation,
    so a BUCK cheap against the basket is a claim bought at a discount to
    its recovery.  Holding costs demurrage, so this variant's carry is
    NEGATIVE and only the recovery can pay for it: it needs a real
    discount, and time works against it.  Sells when the thesis dies.

    Both legs are impact-capped (a bite never pushes the pool past the
    band that justified it: x <= r*(1-sqrt(p)) buying, r*(sqrt(p)-1)
    selling), so the arb stabilizes without ever overshooting.

    Telemetry (ctr, per class): dbaBought / dbaSold (6-dec BUCK),
    dbaSpent / dbaRecv (6-dec USDC) -- PnL and inventory fall out.
    """

    CTR = "dba"

    # Real annual yield ON THE POSITION ITSELF.  Loose BUCK just pays
    # demurrage; the basketeer overrides this.
    POSITION_YIELD = -DEMURRAGE

    # This variant ends the holding period in BUCK, so BUCK's own price is
    # what its edge is measured against.  The basketeer overrides it.
    TRACKS_BUCK = True

    def setup(self, d, scenario, rng) -> None:
        self._rng = _agent_rng(scenario.seed, type(self).__name__, self.idx)
        r = self._rng
        cls = type(self).__name__
        m6 = 1_000 * 10 ** 6
        self.endow = int(_draw(scenario, cls, "endow_k", r, 500) * m6)
        # Heterogeneous patience and heterogeneous inflation belief: a
        # population that agrees on everything acts as one block and clears
        # nothing.  The horizon is what sets how much carry an investor can
        # bank against a given mispricing, so it is the knob that most
        # changes behaviour.
        self.horizon = _draw(scenario, cls, "horizon_yr", r, (0.5, 3.0))
        self.infl = _draw(scenario, cls, "inflation", r,
                          (TRUE_INFLATION - 0.015, TRUE_INFLATION + 0.015))
        self.usdc_yield = _draw(scenario, cls, "usdc_yield", r, USDC_YIELD)
        self.basket_yield = _draw(scenario, cls, "basket_yield", r,
                                  BASKET_PREMIUM)
        self.cost = _draw(scenario, cls, "round_trip_cost", r, ROUND_TRIP_COST)
        self.max_impact_bp = int(_draw(scenario, cls, "max_impact_bp", r,
                                       (25, 150)))
        # The trend window should match the holding period: a three-year
        # investor reads a longer trend than a six-month one.  Quarter of the
        # horizon, clamped to a month either side of sanity.
        self.ma_days = int(min(365.0, max(30.0, self.horizon * 365.0 / 4.0)))
        self._ma_pk: float | None = None     # EMA of the basket's USD price
        self._n_obs = 0
        self._deployed = 0        # USDC put at risk, net of proceeds
        self._bind_proxy(d)
        d.chain.send(d.usdc.functions.mint(self.proxy.address, self.endow))

    # -- the hurdle ----------------------------------------------------- #

    def _carry(self) -> float:
        """Real annual excess of this position over parking in USDC."""
        yield_on_position = (self.basket_yield
                             if self.POSITION_YIELD is None
                             else self.POSITION_YIELD)
        return yield_on_position - (self.usdc_yield - self.infl)

    def _bvib(self, d) -> float:
        """basketValueInBuck: >1 means the basket is rich, i.e. BUCK is cheap
        against its anchor and the controller is working to lift it."""
        try:
            return int(d.basket.functions.basketValueInBuck().call()) / 1e18
        except Exception:
            return 1.0

    def _basket_usd(self, d) -> float:
        """P_K: the basket's price in USD, from the two quantities we observe.

        bvib is the basket priced in BUCK and spot is BUCK priced in USD, so
        their product is the basket priced in USD -- the thing whose drift
        term 1 leaves out.
        """
        spot, _, _ = self._spot_ub(d)
        return self._bvib(d) * spot

    def observe(self, d) -> None:
        """Advance the basket-price trend by one daily sample."""
        pk = self._basket_usd(d)
        if pk <= 0.0:
            return
        if self._ma_pk is None:
            self._ma_pk = pk
        else:
            beta = 2.0 / (self.ma_days + 1.0)
            self._ma_pk += (pk - self._ma_pk) * beta
        self._n_obs += 1

    def _edge(self, d) -> float | None:
        """Expected USD gain from convergence, or None while the trend is cold.

        The reference differs by variant because the exposures differ:

          HOLDER      keeps BUCK, so it converges to the basket's trend value
                      and takes BOTH terms:  MA(P_K)/P_B - 1.

          BASKETEER   swaps BUCK into TOKEN and parks it.  Spend X USDC, get
                      X/P_B BUCK, convert to (X/P_B)(P_B/P_K) = X/P_K baskets
                      -- P_B cancels EXACTLY, so bvib is irrelevant to it and
                      its return is P_K'/P_K - 1.  It takes term 2 only:
                      MA(P_K)/P_K - 1.

        Feeding the basketeer a bvib signal was describing an exposure it
        does not hold.
        """
        if self._ma_pk is None or self._n_obs < self.ma_days:
            return None                      # no trend yet: do not guess
        if self.TRACKS_BUCK:
            ref, _, _ = self._spot_ub(d)     # P_B
        else:
            ref = self._basket_usd(d)        # P_K
        if ref <= 0.0:
            return None
        return self._ma_pk / ref - 1.0

    def _excess(self, d) -> float:
        """Expected excess return over USDC for the whole round trip.

        Returns 0.0 while the trend is cold, which reads as "no edge" to
        both the buy gate (needs > cost) and the sell gate (needs < 0), so a
        warming agent simply holds still.
        """
        edge = self._edge(d)
        if edge is None:
            return 0.0
        return edge + self._carry() * self.horizon

    def _target_deploy(self, excess: float) -> int:
        """Conviction sizing: how much of the endowment this edge justifies."""
        if excess <= 0:
            return 0
        frac = min(MAX_ALLOC, MAX_ALLOC * excess / FULL_ALLOC_EXCESS)
        return int(self.endow * frac)

    def _spot_ub(self, d):
        r_in = d.chain.balance_of(d.usdc, d.pool_ub)
        r_out = d.chain.balance_of(d.buck, d.pool_ub)
        return ((r_in / r_out) if r_out else 1.0), r_in, r_out

    def _held(self, d) -> int:
        s = d.buck.functions.signedBalanceOf(self.proxy.address).call()
        return max(0, s)

    def _buy_leg(self, d, day, ctr) -> int:
        _, r_in, _ = self._spot_ub(d)
        cash = d.chain.balance_of(d.usdc, self.proxy.address)
        if cash < 10 ** 6:
            return 0
        excess = self._excess(d)
        if excess <= self.cost:
            return 0                       # the round trip does not pay
        room = self._target_deploy(excess) - self._deployed
        if room < 10 ** 6:
            return 0                       # already sized to this conviction
        spot, _, r_out = self._spot_ub(d)
        # Impact cap: below par, never push the pool past the price that
        # justified the trade.  Above par that bound vanishes -- the hurdle
        # already said this is worth paying up for -- but "no bound" would
        # let one agent reprice the venue by itself, so fall back to a flat
        # slice of depth.
        # Two bounds, and they say different things.  Below par, never push
        # the pool past the price that justified the trade.  Always, never
        # move it more than this agent's impact budget -- which replaces the
        # old flat 2%-of-reserve and scales with the market instead.
        cap = _impact_cap(r_in, self.max_impact_bp)
        if spot < 1.0:
            cap = min(cap, int(r_out * (1.0 - math.sqrt(spot))))
        cap = max(10 ** 6, cap)
        want = min(cap, int(min(cash, room) / max(spot, 1e-9)))
        if want < 10 ** 6:
            return 0
        pre = cash
        got = self._buy_buck(d, d.pool_ub, d.usdc, want, d.fee_ub)
        spent = pre - d.chain.balance_of(d.usdc, self.proxy.address)
        k = self.CTR
        ctr[k + "Bought"] = ctr.get(k + "Bought", 0) + got
        ctr[k + "Spent"] = ctr.get(k + "Spent", 0) + spent
        self._deployed += spent
        return got

    def _sell_leg(self, d, day, ctr, amount=None) -> int:
        spot, _, r_out = self._spot_ub(d)
        held = self._held(d)
        # No `cost` term here: entry cost is already sunk, so the buy and
        # sell thresholds differ by exactly that -- the hysteresis that keeps
        # a position from churning on noise around its own hurdle.
        if held < 10 ** 6 or self._excess(d) >= 0:
            return 0
        cap = int(r_out * (math.sqrt(spot) - 1.0)) if spot > 1.0 else held
        cap = min(cap if cap > 0 else held,
                  max(10 ** 6, _impact_cap(r_out, self.max_impact_bp)))
        amt = min(held, cap)
        if amount is not None:
            amt = min(amt, amount)
        if amt < 10 ** 6:
            return 0
        pre = d.chain.balance_of(d.usdc, self.proxy.address)
        sold = self._sell_capped(d, d.pool_ub, amt)
        recv = d.chain.balance_of(d.usdc, self.proxy.address) - pre
        k = self.CTR
        ctr[k + "Sold"] = ctr.get(k + "Sold", 0) + sold
        ctr[k + "Recv"] = ctr.get(k + "Recv", 0) + recv
        self._deployed = max(0, self._deployed - recv)
        return sold

    def arb_state(self, d) -> dict | None:
        """Per-frame inventory for the snapshot.

        `parked` is the BUCK principal still sitting in open BuckBasket
        receipts.  Without it the mark omits exactly the capital a basketeer
        has put to work, and reads it as a loss the moment the agent
        deposits -- the same mistake `directMintPnl` makes.  `receipts` is
        kept as a count for the activity series.
        """
        if self.proxy is None:
            return None
        parked = 0
        for rid in getattr(self, "_receipts", []):
            try:
                dep = d.basket.functions.deposits(rid).call()
                parked += dep[0]            # buckPrincipal
            except Exception:
                pass
        return {"cls": self.CTR, "idx": self.idx,
                "cash": d.chain.balance_of(d.usdc, self.proxy.address),
                "held": self._held(d), "endow": self.endow,
                "parked": parked,
                "receipts": len(getattr(self, "_receipts", []))}

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0 or self.proxy is None:
            return
        self.observe(d)                      # trend first, then decide
        if not self._buy_leg(d, day, ctr):
            self._sell_leg(d, day, ctr)


@_register
class BuckPoolInvestorAgent(_ProxyAgent):
    """A stablecoin holder who doubles their exposure by LPing against BUCK.

    THE POSITION

    They already hold USDC on-chain, and already carry insurance on it -- an
    insurer with multi-sig authority over the account, who limits their loss
    exposure and charges for the service.  What is new is that the insurer
    now issues a BuckCredit NFT representing that same cover.  Nothing about
    the underlying arrangement changes; the NFT just makes the existing
    insurance legible to the BUCK system.

    That NFT is collateral.  They mint the BUCK it supports at the current K
    and pair it with the USDC they already had, so ONE pile of capital
    provides BOTH sides of a BUCK/USDC position and earns fees on twice the
    notional.  Unwinding is symmetric:

      BUCK appreciates -- the position converts toward USDC, so they hold
        fewer BUCK than they drew and must buy some back to release the lien.
      BUCK depreciates -- the position converts toward BUCK, so they hold
        more than they drew, release the lien immediately, and sell the rest.

    Insurance is FREE from this agent's point of view, and that is a
    modelling simplification with a real justification and a real cost.  The
    justification: a custodial stablecoin holder plausibly pays for this
    cover already, so the marginal cost of representing it as a BuckCredit is
    near zero to them.  The cost: premiumRate 0 means poolPrincipal 0, so
    Buck.mint is exempt from the funding-factor gate -- see the module note
    above for what that removes.  A later pass should charge the premium and
    make them accumulate the reserve like anyone else.

    FRONT-RUNNING THE CONTROLLER

    The pool position is concentrated, and where it sits is a directional
    view.  basketValueInBuck is the controller's own process variable, so it
    says which way K is about to push:

      bvib > 1   the basket is rich in BUCK, error is negative, K falls,
                 credit tightens, supply contracts -- BUCK should RISE
                 against USDC.  Sit ABOVE the price, holding BUCK, and sell
                 it into the rise.
      bvib < 1   K rises, credit loosens, supply expands -- BUCK should FALL.
                 Sit BELOW the price, holding USDC, and buy into the fall.

    A concentrated position is a bet that the price comes to you.  Placing it
    on the side the controller is pushing toward means the flow arrives,
    which is where the fees are.

    WIDTH IS A RISK APPETITE, AND IT VARIES

    The range is how much fluctuation this investor tolerates before their
    position goes one-sided.  Too narrow and it is out of range constantly,
    holding one token and earning nothing; too wide and the capital is spread
    thin and captures little of the intraday movement.  The right width is a
    judgement about volatility, and investors genuinely disagree about it --
    so `half_width_bp` is drawn per agent (150-900bp) alongside its own
    rebalancing patience.  A population that agreed would reposition in
    lockstep, all abandoning the same range on the same tick and all crowding
    the same new one, which is not a market.

    Repositioning only happens when the price actually leaves the range.
    Inside it, the position is working and moving it would just pay fees to
    realize a loss.

    Telemetry (ctr): bpiMinted / bpiPositions / bpiRepositions / bpiFeesUsd.
    """

    CTR = "bpi"
    N_CREDITS = 2

    def setup(self, d, scenario, rng) -> None:
        self._rng = _agent_rng(scenario.seed, type(self).__name__, self.idx)
        r = self._rng
        cls = type(self).__name__
        m6 = 1_000 * 10 ** 6
        self.stable = int(_draw(scenario, cls, "stable_k", r, (200, 900)) * m6)
        # Risk appetite, and the reason these do not move as one block.
        self.half_width_bp = int(_draw(scenario, cls, "half_width_bp", r,
                                       (150, 900)))
        # How far outside the range before bothering to move it: a small
        # tolerance stops a position thrashing at its own boundary.
        self.reposition_slack_bp = int(_draw(scenario, cls,
                                             "reposition_slack_bp", r,
                                             (25, 200)))
        self.max_impact_bp = int(_draw(scenario, cls, "max_impact_bp", r,
                                       (25, 150)))
        self._pos: tuple[int, int] | None = None     # (tickLower, tickUpper)
        self._liq = 0
        self._drawn = 0
        self._repositions = 0

        self._bind_proxy(d)
        d.chain.send(d.usdc.functions.mint(self.proxy.address, self.stable))
        # The insurer's NFT: the cover they already carry, made legible.
        # premiumRate 0 -- free to them, see the class docstring.
        now_ts = d.w3.eth.get_block("latest")["timestamp"]
        per = max(1, self.stable // self.N_CREDITS)
        self._proxy_exec(d, d.credit.address, d.credit.encode_abi(
            "setCreditIssuer",
            args=[getattr(d.chain.deployer, "address", d.chain.deployer), True]))
        for _ in range(self.N_CREDITS):
            d.chain.send(d.credit.functions.createCredit(
                self.proxy.address, 0, per, 0, 0, 0, now_ts, 0))
        self._face = per * self.N_CREDITS
        self._token_ids = [
            d.credit.functions.tokenOfOwnerByIndex(self.proxy.address, i).call()
            for i in range(self.N_CREDITS)]

    # -- pool geometry --------------------------------------------------- #

    def _pool_state(self, d):
        """(sqrtPriceX96, tick, tickSpacing, token0, token1) for BUCK/USDC."""
        pool_abi, _ = load_artifact("UniswapV3Pool")
        pool = d.w3.eth.contract(address=d.pool_ub, abi=pool_abi)
        slot0 = pool.functions.slot0().call()
        return (slot0[0], slot0[1], pool.functions.tickSpacing().call(),
                pool.functions.token0().call(), pool.functions.token1().call())

    def _bvib(self, d) -> float:
        try:
            return int(d.basket.functions.basketValueInBuck().call()) / 1e18
        except Exception:
            return 1.0

    @staticmethod
    def _bp_to_ticks(bp: int) -> int:
        """A tick IS a log price -- price = 1.0001^tick -- so a fractional
        move x spans ln(1+x)/ln(1.0001) ticks, which for small x is very
        nearly x in basis points.  900bp is 862 ticks, not 900, and using the
        exact form keeps a wide range from quietly being 4% narrower than
        asked for."""
        return max(1, int(math.log(1.0 + bp / 10_000.0) / math.log(1.0001)))

    @staticmethod
    def _sqrt_at_tick(t: int) -> int:
        """sqrtPriceX96 at a tick.  price = 1.0001^t, so sqrt is 1.0001^(t/2)."""
        return int((1.0001 ** (t / 2.0)) * (1 << 96))

    def _target_range(self, d, sp: int, tick: int, spacing: int,
                      buck_is_token0: bool,
                      amt0: int, amt1: int) -> tuple[int, int]:
        """Where to sit.  Two cases, and they are different situations.

        HOLDING BOTH TOKENS -- which is the normal state, and always the
        state right after drawing against the insurance -- the position
        STRADDLES the current price and deploys both sides.  That is the
        whole point of the structure: one pile of capital providing both legs
        and earning fees on twice the notional.  A single-sided position here
        would leave half the capital idle.

        The straddle is not centred.  A range spanning [A, B] around price P
        holds value in token1 roughly in proportion to (P - A) and in token0
        to (B - P), so to deploy BOTH sides fully the split has to match the
        mix already held -- "the natural bound implied by the mix".  Total
        width is this investor's tolerance; where P sits inside it is decided
        by what they are holding.

        HOLDING ONE TOKEN -- which happens after the price runs through the
        range and converts the position -- there is nothing to straddle with,
        so the new position ABUTS the current price on the side the
        controller is pushing toward.  bvib > 1 means K falls, supply
        contracts and BUCK should rise, so hold BUCK and sell into it; which
        side of the CURRENT TICK that is depends on orientation, because the
        pool price is token1/token0 and a BUCK rally is a falling price when
        BUCK is token1.
        """
        width = self._bp_to_ticks(self.half_width_bp)
        width = max(spacing, (width // spacing) * spacing)
        base = (tick // spacing) * spacing
        v0, v1 = max(0, amt0), max(0, amt1)
        both = v0 > 0 and v1 > 0 and min(v0, v1) * 20 >= max(v0, v1)
        if both:
            return self._straddle_for_mix(sp, base, spacing, 2 * width,
                                          v0, v1)
        bvib = self._bvib(d)
        if bvib == 1.0:
            return base - width, base + width
        above = (bvib > 1.0) == buck_is_token0
        return (base, base + 2 * width) if above else (base - 2 * width, base)

    def _straddle_for_mix(self, sp: int, base: int, spacing: int,
                          width: int, amt0: int, amt1: int) -> tuple[int, int]:
        """Place a range of `width` ticks so it consumes EXACTLY this mix.

        A concentrated position holds unequal amounts whenever it is not
        symmetric around the price in sqrt-space, and that asymmetry is the
        free parameter.  With the price P inside [A, B]:

            amount0 = L (sqrtB - sqrtP) / (sqrtP sqrtB)
            amount1 = L (sqrtP - sqrtA)

        so their ratio is fixed by where P sits in the range, and there is
        one placement that matches any mix on hand.  Solved by bisection on
        the low-side width rather than in closed form: the algebra is a
        quadratic in sqrtA, but bisection on the exact amount formulas avoids
        both the fixed-point rounding and the small-angle approximation that
        a width-proportional split quietly relies on.

        That approximation is also wrong in a way worth naming: widths track
        the VALUE split, amt0*P : amt1, not the raw amounts.  Near parity the
        difference is invisible, which is exactly why it would have survived.
        """
        Q96 = 1 << 96

        def amounts(lo_w: int) -> tuple[int, int]:
            lo = base - lo_w
            hi = base + (width - lo_w)
            sa, sb = self._sqrt_at_tick(lo), self._sqrt_at_tick(hi)
            spc = min(max(sp, sa + 1), sb - 1)
            a0 = (sb - spc) * Q96 // max(1, (spc * sb) // Q96)
            a1 = spc - sa
            return a0, a1

        want = amt1 / amt0 if amt0 else float("inf")
        lo_w, hi_w = spacing, max(spacing, width - spacing)
        for _ in range(40):
            mid = (lo_w + hi_w) // 2
            a0, a1 = amounts(mid)
            got = a1 / a0 if a0 else float("inf")
            if got < want:
                lo_w = mid            # more room below -> more token1
            else:
                hi_w = mid
            if hi_w - lo_w <= spacing:
                break
        lo_w = max(spacing, (lo_w // spacing) * spacing)
        return base - lo_w, base + max(spacing, width - lo_w)

    def _in_range(self, tick: int) -> bool:
        if self._pos is None:
            return False
        lo, hi = self._pos
        slack = self._bp_to_ticks(self.reposition_slack_bp)
        return (lo - slack) <= tick <= (hi + slack)

    # -- telemetry -------------------------------------------------------- #

    def arb_state(self, d) -> dict | None:
        if self.proxy is None:
            return None
        signed = d.buck.functions.signedBalanceOf(self.proxy.address).call()
        return {"cls": self.CTR, "idx": self.idx,
                "cash": d.chain.balance_of(d.usdc, self.proxy.address),
                "held": max(0, signed), "drawn": max(0, -signed),
                "endow": self.stable, "parked": 0,
                "width": self.half_width_bp,
                "repos": self._repositions,
                "receipts": 1 if self._pos else 0}

    def _exit_position(self, d, ctr) -> bool:
        """Burn the current range and collect everything owed, fees included.

        `burn` credits the owed amounts to the position; `collect` is what
        actually moves them, so both are needed and a burn alone would leave
        the capital in the pool.
        """
        if self._pos is None:
            return True
        lo, hi = self._pos
        pool_abi, _ = load_artifact("UniswapV3Pool")
        pool = d.w3.eth.contract(address=d.pool_ub, abi=pool_abi)
        try:
            self._proxy_exec(d, d.pool_ub, pool.encode_abi(
                "burn(int24,int24,uint128)",
                args=[lo, hi, int(getattr(self, "_liq", 0))]))
            self._proxy_exec(d, d.pool_ub, pool.encode_abi(
                "collect(address,int24,int24,uint128,uint128)",
                args=[self.proxy.address, lo, hi,
                      2 ** 128 - 1, 2 ** 128 - 1]))
        except Exception as e:
            ctr["bpi_err"] = repr(e)[:160]
            return False
        self._pos = None
        self._liq = 0
        return True

    def act(self, d, scenario, day, tick_i, ctr) -> None:
        if tick_i != 0 or self.proxy is None or not d.pool_ub:
            return
        try:
            sp, tick, spacing, t0, t1 = self._pool_state(d)
        except Exception as e:
            ctr["bpi_err"] = repr(e)[:160]
            return
        if self._pos is not None and self._in_range(tick):
            return                      # working: leave it alone
        buck_is_token0 = d.buck.address.lower() == t0.lower()
        # Draw the BUCK side against the insurance, once.
        if self._drawn == 0:
            room = 0
            for tid in self._token_ids:
                try:
                    face = d.credit.functions.depreciatedFaceValue(tid).call()
                    used = d.buck.functions.mintsBacked(tid).call()
                    room += max(0, face - used)
                except Exception:
                    pass
            want = min(room, self.stable)
            if want >= 10 ** 6:
                try:
                    self._proxy_exec(d, d.buck.address, d.buck.encode_abi(
                        "mint(uint256)", args=[want]))
                    self._drawn += want
                    ctr["bpiMinted"] = ctr.get("bpiMinted", 0) + want
                except Exception as e:
                    ctr["bpi_err"] = repr(e)[:160]
                    return
        # What is actually on hand, in token0/token1 terms.
        bal_b = max(0, d.buck.functions.balanceOf(self.proxy.address).call())
        bal_u = d.chain.balance_of(d.usdc, self.proxy.address)
        amt0, amt1 = (bal_b, bal_u) if buck_is_token0 else (bal_u, bal_b)
        lo, hi = self._target_range(d, sp, tick, spacing, buck_is_token0,
                                    amt0, amt1)
        if self._pos == (lo, hi):
            return

        # Concentrated-range liquidity.  The full-range formula from
        # deploy.py under-sizes a narrow band by the ratio of the widths --
        # 20-30x at 300-600bp -- which showed up as 95% of capital idle while
        # positions "placed" fine.  With the price INSIDE the range both legs
        # bind, so take the smaller:
        #     amount0 = L (sqrtB - sqrtP) Q96 / (sqrtP sqrtB)
        #     amount1 = L (sqrtP - sqrtA) / Q96
        Q96 = 1 << 96
        sa, sb = self._sqrt_at_tick(lo), self._sqrt_at_tick(hi)
        sp_c = min(max(sp, sa + 1), sb - 1)
        if sb <= sa:
            return
        cands = []
        if amt0 > 0 and sb > sp_c:
            cands.append(amt0 * sp_c * sb // (Q96 * (sb - sp_c)))
        if amt1 > 0 and sp_c > sa:
            cands.append(amt1 * Q96 // (sp_c - sa))
        # Shave the result: _straddle_for_mix bisects on the exact amount
        # formulas but then rounds the split to tickSpacing, which moves the
        # ratio slightly -- enough that the mint callback asked for more of
        # one token than the proxy held and reverted with
        # ERC20InsufficientBalance, silently, since bpi_err was copied into
        # no frame.  A margin costs a fraction of a percent of deployment.
        L = int(min(cands) * 0.995) if cands else 0
        if L < 1:
            return
        # Leave the old range first, or liquidity strands in a band the
        # price has already left and stops earning anything.
        if self._pos is not None:
            if not self._exit_position(d, ctr):
                return
            self._repositions += 1
            ctr["bpiRepositions"] = ctr.get("bpiRepositions", 0) + 1
        # The proxy IS a SimLP, so it can serve the V3 mint callback itself.
        try:
            d.chain.send(self.proxy.functions.mint(
                d.pool_ub, lo, hi, int(min(L, 2 ** 127 - 1)), t0, t1))
        except Exception as e:
            ctr["bpi_err"] = repr(e)[:160]
            self._pos = None
            return
        self._pos = (lo, hi)
        self._liq = int(min(L, 2 ** 127 - 1))
        ctr["bpiPositions"] = ctr.get("bpiPositions", 0) + 1


@_register
class BuckIssuerArbAgent(_ProxyAgent):
    """The SUPPLY side: mints over-valued BUCK and buys real assets with it.

    Every other actor in the rebalancing scenario either wants BUCK or is
    indifferent to it.  The discount arbs bid for it; the direct-mint agents
    pledge TOKEN; the debtors issue on a mortgage schedule that has nothing
    to do with what a BUCK is worth.  So when BUCK becomes over-valued there
    is nobody whose business it is to issue into that, and the price has no
    ceiling.

    The 730-day reverting run is what happens without this agent.  The demand
    leg bought 3.4M BUCK between days 460 and 550; the debtors answered with
    about 600k, one sixth, because their issuance follows amortization rather
    than opportunity; buckK ran to its 0.95 clamp and stayed there.
    basketValueInBuck fell from 1.05 to 0.80 -- BUCK 20% rich against its own
    anchor -- and every redemption from then on took BuckBasketProRata's
    deflation branch, converting depositors' TOKEN away to cover burns that
    the withdrawn liquidity no longer covered.  Treasury accrual stopped
    dead.  The break was not the price regime; it was a market with one side.

    This is the missing side, and it is the oldest trade there is: when your
    money is worth more than what it claims, issue it and buy the claim.

      bvib < 1   the basket is cheap in BUCK -- BUCK buys more real goods
                 than parity says it should.  DRAW BUCK against BuckCredit
                 collateral and spend it on TOKEN.  This is seigniorage: the
                 issuer keeps the difference, and the selling pressure is
                 what caps the appreciation.
      bvib > 1   BUCK is cheap against the basket.  Sell the TOKEN back, buy
                 BUCK, and retire the draw -- covering the position at a
                 discount to what it was issued at.

    Issuing is two steps, and the first one is easy to miss.  `Buck.mint`
    ACTIVATES a slice of the collateral's coverage -- `BuckCredit.currentValue`
    returns 0 while `activatedValue` is zero, so an un-activated NFT
    contributes nothing to `totalCurrentValue` and therefore nothing to
    `creditLimit`.  Only once coverage is activated does `balanceOf` report
    spendable headroom, and only then does selling draw the balance negative.
    Mint alone issues nothing: it debits the premium and opens the line.
    Retiring is the balance climbing back toward zero.

    Telemetry (ctr): biaDrawn / biaRetired (6-dec BUCK), biaBought /
    biaSold (6-dec USDC of TOKEN acquired and released).
    """

    CTR = "bia"
    N_CREDITS = 4

    def setup(self, d, scenario, rng) -> None:
        self._rng = _agent_rng(scenario.seed, type(self).__name__, self.idx)
        r = self._rng
        cls = type(self).__name__
        m6 = 1_000 * 10 ** 6
        # Collateral, not cash: this agent issues against assets it owns
        # rather than spending a war chest, which is what makes it an ISSUER
        # and bounds it by creditLimit (and therefore by buckK).
        face = int(_draw(scenario, cls, "face_k", r, (800, 1600)) * m6)
        # A real premium, so the funding-factor gate applies for real and a
        # blocked issuance shows up as a throttle rather than as silence.
        # 0 => poolPrincipal 0 => funding gate exempt (see the module note).
        self.premium_rate = int(_draw(scenario, cls, "premium_bp", r, 0))
        # How far BUCK must be rich before issuing, and how far back toward
        # parity before covering.  The gap between them is hysteresis.
        self.issue_at = _draw(scenario, cls, "issue_at", r, (0.010, 0.040))
        self.cover_at = _draw(scenario, cls, "cover_at", r, (0.000, 0.015))
        self.step_frac = _draw(scenario, cls, "step_frac", r, (0.05, 0.20))
        self.max_impact_bp = int(_draw(scenario, cls, "max_impact_bp", r,
                                       (25, 150)))
        self._drawn = 0
        self._activated = 0
        # Enough BUCK on hand to clear the funding gate, and no more.
        self.gate_buffer = int(_draw(scenario, cls, "gate_buffer_k", r, 25)
                               * 1_000 * 10 ** 6)
        self._bind_proxy(d)
        now_ts = d.w3.eth.get_block("latest")["timestamp"]
        per = max(1, face // self.N_CREDITS)
        self._proxy_exec(d, d.credit.address, d.credit.encode_abi(
            "setCreditIssuer",
            args=[getattr(d.chain.deployer, "address", d.chain.deployer), True]))
        for _ in range(self.N_CREDITS):
            d.chain.send(d.credit.functions.createCredit(
                self.proxy.address, 0, per, 0, 0, 0, now_ts, self.premium_rate))
        self._face = per * self.N_CREDITS
        self._token_ids = [
            d.credit.functions.tokenOfOwnerByIndex(self.proxy.address, i).call()
            for i in range(self.N_CREDITS)]
        # Working capital.  The funding gate wants the minter to hold BUCK
        # covering poolPrincipal * fundingFactor before activation, and
        # poolPrincipal is the PREMIUM rather than the draw, so this is small
        # -- but an issuer with no float cannot open a line at all.
        d.chain.send(d.usdc.functions.mint(
            self.proxy.address, int(_draw(scenario, cls, "float_k", r, 150)
                                    * 1_000 * 10 ** 6)))

    # -- state ---------------------------------------------------------- #

    def _bvib(self, d) -> float:
        try:
            return int(d.basket.functions.basketValueInBuck().call()) / 1e18
        except Exception:
            return 1.0

    def _headroom(self, d) -> int:
        """Spendable BUCK: held, plus unused credit.  Drawing is spending."""
        try:
            return int(d.buck.functions.balanceOf(self.proxy.address).call())
        except Exception:
            return 0

    def _tok_value(self, d) -> int:
        """USDC value of TOKEN inventory at pool prices."""
        v = 0
        for i, tc in enumerate(d.tokens):
            bal = d.chain.balance_of(tc, self.proxy.address)
            if bal <= 0:
                continue
            rt = d.chain.balance_of(tc, d.pool_usdc[i])
            ru = d.chain.balance_of(d.usdc, d.pool_usdc[i])
            if rt:
                v += bal * ru // rt
        return v

    # -- legs ----------------------------------------------------------- #

    def _issue(self, d, day, ctr) -> None:
        """Activate coverage, draw against it, and buy real assets."""
        # Remaining capacity is chain truth, not a local tally.  Tracking it
        # in Python over-counted -- _allocateMint caps each NFT at
        # `faceValue - mintsBacked`, and depreciation moves that underneath a
        # counter -- which produced 30 of 36 "insufficient credit allocation"
        # refusals in the diagnostic run.
        unactivated = 0
        for tid in self._token_ids:
            try:
                face = d.credit.functions.depreciatedFaceValue(tid).call()
                used = d.buck.functions.mintsBacked(tid).call()
                unactivated += max(0, face - used)
            except Exception:
                pass
        if unactivated < 10 ** 6:
            return
        amt = int(unactivated * self.step_frac)
        # Cap the bite so one issuance does not reprice the venue by itself.
        r_out = d.chain.balance_of(d.buck, d.pool_ub)
        amt = min(amt, max(10 ** 6, _impact_cap(r_out, self.max_impact_bp)),
                  unactivated)
        if amt < 10 ** 6:
            return
        # Step one: open the line.  This is where the funding-factor gate
        # applies, and where the premium is paid.
        try:
            self._proxy_exec(d, d.buck.address,
                             d.buck.encode_abi("mint(uint256)", args=[amt]))
            self._activated += amt
        except Exception as e:
            ctr["biaThrottled"] = ctr.get("biaThrottled", 0) + 1
            # Record the reason.  Swallowing it produced a confident wrong
            # story: the funding gate was blamed for these refusals, but
            # fundingFactor is 1 + 10(b-p)/b and returns ZERO once BUCK is
            # ~9% over-valued -- the gate is OFF exactly when this agent
            # wants to issue.  Whatever stops it is something else.
            why = repr(e)[:120]
            ctr.setdefault("biaWhy", {})
            ctr["biaWhy"][why] = ctr["biaWhy"].get(why, 0) + 1
            # Only top the float when the reason really is the gate.
            if "funding" not in why:
                return
            # Bounded, and only while the float is thin.  Buying BUCK is
            # DEMAND, which is the pressure this agent exists to relieve, so
            # topping the gate buffer must never become the agent's main
            # activity -- the first run spent an entire $150k float this way
            # and drew almost nothing.
            held = max(0, d.buck.functions.signedBalanceOf(
                self.proxy.address).call())
            if held < self.gate_buffer:
                cash = d.chain.balance_of(d.usdc, self.proxy.address)
                want = min(cash // 4, self.gate_buffer - held)
                if want > 10 ** 6:
                    try:
                        self._buy_buck(d, d.pool_ub, d.usdc, want, d.fee_ub)
                    except Exception:
                        pass
            return
        # Step two: spend it.  Now that coverage is activated, balanceOf
        # reports headroom and selling draws the signed balance negative.
        pre_u = d.chain.balance_of(d.usdc, self.proxy.address)
        sold = self._sell_capped(d, d.pool_ub, amt)
        if sold <= 0:
            return
        got = d.chain.balance_of(d.usdc, self.proxy.address) - pre_u
        self._drawn += sold
        ctr["biaDrawn"] = ctr.get("biaDrawn", 0) + sold
        # Spend the proceeds on the most underweight commodity: the issuer
        # wants real goods, and buying where the basket is short helps rather
        # than fights the mandate.
        i = (day + self.idx) % len(d.tokens)
        if got > 0:
            try:
                self._swap_via_simlp(d, d.pool_usdc[i], d.usdc, got,
                                     self.proxy.address)
                ctr["biaBought"] = ctr.get("biaBought", 0) + got
            except Exception as e:
                ctr["bia_err"] = repr(e)[:200]

    def _cover(self, d, day, ctr) -> None:
        """Sell the assets back and retire the draw."""
        if self._drawn <= 0:
            return
        for i, tc in enumerate(d.tokens):
            bal = d.chain.balance_of(tc, self.proxy.address)
            if bal <= 0:
                continue
            pre = d.chain.balance_of(d.usdc, self.proxy.address)
            try:
                self._swap_via_simlp(d, d.pool_usdc[i], tc, bal,
                                     self.proxy.address)
            except Exception as e:
                ctr["bia_err"] = repr(e)[:200]
                continue
            ctr["biaSold"] = ctr.get("biaSold", 0) + (
                d.chain.balance_of(d.usdc, self.proxy.address) - pre)
        cash = d.chain.balance_of(d.usdc, self.proxy.address)
        if cash < 10 ** 6:
            return
        want = min(self._drawn, int(cash))
        got = self._buy_buck(d, d.pool_ub, d.usdc, want, d.fee_ub)
        if got > 0:
            self._drawn = max(0, self._drawn - got)
            ctr["biaRetired"] = ctr.get("biaRetired", 0) + got

    def arb_state(self, d) -> dict | None:
        if self.proxy is None:
            return None
        signed = d.buck.functions.signedBalanceOf(self.proxy.address).call()
        return {"cls": self.CTR, "idx": self.idx,
                "cash": d.chain.balance_of(d.usdc, self.proxy.address),
                "held": max(0, signed), "drawn": max(0, -signed),
                "tok": self._tok_value(d), "endow": self._face,
                "parked": 0, "receipts": 0}

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0 or self.proxy is None:
            return
        bvib = self._bvib(d)
        if bvib < 1.0 - self.issue_at:
            self._issue(d, day, ctr)          # BUCK rich: issue into it
        elif bvib > 1.0 - self.cover_at:
            self._cover(d, day, ctr)          # back toward parity: cover


@_register
class DiscountBasketArbAgent(DiscountBuckArbAgent):
    """BASKETEER variant: buys cheap BUCK like the holder, then PARKS it --
    swaps BUCK -> TOKEN through the TOKEN/BUCK basket pools (the BUCK leaves
    circulation into pool inventory, lifting the BUCK/TOKEN ratios) and
    deposits the TOKEN into the BuckBasket for an LP receipt.

    The parked position holds no BUCK at all, so it pays NO demurrage and
    earns the rebalancing premium instead -- which flips the sign of the
    carry against the holder variant and is the whole content of the A/B.
    Where a holder needs a real discount and is punished for waiting, a
    basketeer is paid to wait and will buy through a modest premium.

    Harvests when the thesis dies: redeem the oldest receipt, sell any
    returned TOKEN back to USDC, and sell residual BUCK into the market.

    Extra telemetry: dbbParked (BUCK swapped into basket pools),
    dbbReceipts (open LP positions), dbbHarvests (receipts redeemed).
    """

    CTR = "dbb"

    # Parked value sits in TOKEN inside the basket, never as loose BUCK --
    # no demurrage.  None => use the agent's own basket-premium belief.
    POSITION_YIELD = None

    # It ends the holding period in TOKEN, not BUCK, so its edge is measured
    # against the basket.  bvib cancels out of its round trip entirely.
    TRACKS_BUCK = False

    def setup(self, d, scenario, rng) -> None:
        super().setup(d, scenario, rng)
        self._receipts: list[int] = []

    def _park_leg(self, d, day, ctr) -> None:
        # Spendable, not raw: `_held` reads signedBalanceOf, which is the
        # balance BEFORE the demurrage lien, and a park sized off it asks to
        # move BUCK the proxy cannot actually move.
        held = min(self._held(d),
                   max(0, d.buck.functions.balanceOf(self.proxy.address).call()))
        if held < 25_000 * 10 ** 6 or self._excess(d) < 0:
            return                          # do not park what we mean to sell
        i = (day + self.idx) % len(d.tokens)
        tc = d.tokens[i]
        pool = d.pool_buck[i]
        pre_tok = d.chain.balance_of(tc, self.proxy.address)
        # BUCK -> TOKEN in the basket pool: the parked BUCK becomes pool
        # inventory.  The bite has to stay inside the basket's OWN slippage
        # guard, which reverts any valuation (and therefore any depositor's
        # redeem) when a pool's spot leaves its TWAP band -- see
        # BuckBasketUniswapV3._enforceSlippageGuard, deployed here at 500bp.
        # On a constant-product pool a bite of fraction f of the input
        # reserve moves price by about 2f, so 1% is ~2% of move: comfortably
        # inside the band, and small enough that a POPULATION of basketeers
        # arriving the same day does not add up to a breach.  The earlier 5%
        # was sized for a two-agent A/B and skews the pool hard enough at
        # this population to make honest depositors' redeems revert.
        r_buck = d.chain.balance_of(d.buck, pool)
        amt = min(held, max(10 ** 6,
                            _impact_cap(r_buck, self.max_impact_bp)))
        self._swap_via_simlp(d, pool, d.buck, amt, self.proxy.address)
        got_tok = d.chain.balance_of(tc, self.proxy.address) - pre_tok
        if got_tok <= 0:
            return
        self._proxy_exec(d, tc.address, tc.encode_abi(
            "approve(address,uint256)", args=[d.basket.address, got_tok]))
        rcpt = self._proxy_exec(d, d.basket.address, d.basket.encode_abi(
            "depositToken(address,uint256,uint256)",
            args=[tc.address, got_tok, 0]))
        for log in rcpt["logs"]:
            if log["topics"][0] == d.deposited_topic:
                self._receipts.append(int.from_bytes(log["topics"][2], "big"))
                break
        ctr["dbbParked"] = ctr.get("dbbParked", 0) + amt
        ctr["dbbReceipts"] = ctr.get("dbbReceipts", 0) + 1

    def _harvest_leg(self, d, day, ctr) -> None:
        if not self._receipts or self._excess(d) >= 0:
            return                          # thesis still alive: keep waiting
        rid = self._receipts.pop(0)
        try:
            # Three-arg redeem: the ONLY overload both basket implementations
            # expose.  ProRata also has a two-arg form, but the traditional
            # BuckBasket does not, and the rebalancing A/B runs both.
            # redeemBp 0 means "all" on both (`redeemBp == 0 ? 10000`).
            self._proxy_exec(d, d.basket.address, d.basket.encode_abi(
                "redeem(uint256,uint256,uint256)", args=[rid, 0, 0]))
            ctr["dbbHarvests"] = ctr.get("dbbHarvests", 0) + 1
        except Exception as e:
            self._receipts.append(rid)
            ctr["dbb_err"] = repr(e)[:200]
            return
        # Redemption pays in TOKEN, never BUCK.  Turn it back into the
        # numeraire, or the capital never recycles and `_deployed` never
        # unwinds -- the position would look permanent no matter the thesis.
        for i, tc in enumerate(d.tokens):
            bal = d.chain.balance_of(tc, self.proxy.address)
            if bal <= 0:
                continue
            pre = d.chain.balance_of(d.usdc, self.proxy.address)
            try:
                self._swap_via_simlp(d, d.pool_usdc[i], tc, bal,
                                     self.proxy.address)
            except Exception as e:
                ctr["dbb_err"] = repr(e)[:200]
                continue
            recv = d.chain.balance_of(d.usdc, self.proxy.address) - pre
            ctr["dbbRecv"] = ctr.get("dbbRecv", 0) + recv
            self._deployed = max(0, self._deployed - recv)
        self._sell_leg(d, day, ctr)

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0 or self.proxy is None:
            return
        self.observe(d)                      # trend first, then decide
        if not self._buy_leg(d, day, ctr):
            self._harvest_leg(d, day, ctr)
        self._park_leg(d, day, ctr)


@_register
class MonetaryOpsAgent(_ProxyAgent):
    """The BuckBasket's operations desk, run as an agent.

    This is phase 2 of alberta-buck-operations.org: the four quadrants driven
    against the live chain, against real pools and real counterparties, BEFORE
    any contract change.  The point of doing it as an agent first is that it
    puts the basket's own sensor outside its own control loop while the
    dynamics are still being learned -- and the article is explicit that the
    parameterization is not converged.

    THE SIGNAL

    A Uniswap tick is a log price, so the MEAN of the basket's per-leg ladders
    is log(basketValueInBuck) -- the controller's own process variable, and
    the common mode the `pairs` engine deliberately discards.  Here it is read
    straight off basketValueInBuck() and run through the same seven-window
    ladder the PairsRebalanceDirector uses, measured on the 20-day rung.

    The rung is a correctness bound, not a preference.  Past roughly 80 days
    the lagged reading still says "dear" long after the market has gone cheap,
    so an inflation attack gets answered by ISSUING more.  Collocation at 20d
    is handled by the size bound instead: an operation capped at a fraction of
    a percent of depth per day cannot dominate a 20-day average.

    THE FOUR QUADRANTS

        bvib > 1 (BUCK cheap)          bvib < 1 (BUCK dear)
        Q1 ABSORB  buy BUCK, hold      Q3 SUPPLY  sell held BUCK
        Q2 RETIRE  buy BUCK, burn      Q4 ISSUE   mint, sell

    Q1/Q3 are temporary and self-reversing: what Q1 accumulates is what Q3
    sells back when the deviation turns.  Q2/Q4 are outright and change the
    size of the balance sheet, reached only when the deviation has stayed past
    the leash for `persist_days` OR inventory says the move is real.

    WHAT AN AGENT CANNOT DO, AND WHY IT MATTERS HERE

    An agent is not the basket, and two of the quadrants are weaker for it.
    `mintFromBasket` / `burnFromBasket` both require msg.sender == basket, so
    this agent works through the ordinary credit path like anyone else:

      * Q4 mints against its OWN BuckCredit, so it is bounded by
        creditLimit = totalCurrentValue x buckK.  The real basket's
        mintFromBasket bypasses K entirely.  (It is NOT bounded by the funding
        factor: that is 1 + 10(b-p)/b, which returns 0 once BUCK is ~9%
        over-valued -- the gate is off exactly when Q4 wants to fire.)

      * Q2 can only retire supply THIS AGENT issued.  `Buck.burn` does not
        destroy tokens -- it DEACTIVATES coverage ("you repay the credit,
        then release the coverage, as with any loan"), and nothing is
        subtracted from the caller's balance.  Retirement happens on the BUY:
        supply is sum_a max(0, signedRaw(a)), so an account holding drawn
        credit contributes nothing, and buying BUCK while drawn moves float
        into an account where it does not count.  Buying BEYOND the draw is
        inventory and changes no supply at all.  The burn's job is to close
        the line so the retirement cannot be undone by redrawing -- which is
        exactly the temporary/outright distinction Q1 and Q2 encode.

    That second point is why this agent opens a STANDING BOOK before it
    operates at all: it draws `open_frac` of its line and sells it, which
    both funds the
    TOKEN side that Q1 spends and creates the outstanding issue that Q2
    contracts.  A central bank's asset side is bought with money it issued;
    the same structure is what gives it room to operate in either direction.
    The residual gap -- retiring third-party float -- is exactly what phases 3
    and 4 (monetaryEffort / monetaryOperation) add, and it cannot be closed
    from outside the contract.

    WHICH POOL

    Every TOKEN/BUCK pool, spread evenly.  The common mode is by definition
    the part that is the same in all of them, so acting on one would inject a
    differential disturbance and fight the rebalancer for the same depth.
    Each leg is separately capped by _impact_cap against that pool's reserve.

    Operations are executed as capped swaps rather than as concentrated range
    orders, even though BuckPoolInvestorAgent has the machinery.  A resting
    range order earns fees while it waits, and that income would confound the
    comparison this agent exists to make -- is the desk profiting from
    monetary operations, or from being an LP?  Swaps isolate the operation.

    Telemetry (ctr): moQ1..moQ4 / moBought / moSold / moIssued / moRetired /
    moBurned / moPosLimit / moCumLimit / moDevBp / moWhy.
    """

    CTR = "mo"
    N_CREDITS = 4
    WINDOWS = (5, 10, 20, 40, 80, 160, 320)
    MEAS = 2                       # 20d: the knee of the sweep
    # Days spent establishing the book before the first operation.  Twice the
    # measurement window: the ladder is warm at 20d, but a book built at the
    # rate the impact bound allows is not, and a desk that starts operating
    # with a book it is still assembling cannot tell its own flow from the
    # market's.  At one window the book reached $637k of a $2.1M target and
    # Q1 spent it before persistence could ever escalate to Q2.
    ESTABLISH = 2

    def setup(self, d, scenario, rng) -> None:
        self._rng = _agent_rng(scenario.seed, type(self).__name__, self.idx)
        r = self._rng
        cls = type(self).__name__
        m6 = 1_000 * 10 ** 6
        # Defaults are the values monetary_ops.py converged on.  They are the
        # ones that stopped the runaways that model produced, at the sizes
        # that happened to work; they are not fitted.
        self.deadband = _draw(scenario, cls, "deadband", r, 0.010)
        self.leash = _draw(scenario, cls, "leash", r, 0.020)
        self.temp_frac = _draw(scenario, cls, "temp_frac", r, 0.004)
        self.perm_frac = _draw(scenario, cls, "perm_frac", r, 0.002)
        self.persist_days = int(_draw(scenario, cls, "persist_days", r, 30))
        self.inv_escalate = _draw(scenario, cls, "inv_escalate", r, 0.05)
        self.inv_max = _draw(scenario, cls, "inv_max", r, 0.10)
        self.max_outright = _draw(scenario, cls, "max_outright", r, 0.10)
        # A BACKSTOP, not the policy size.  At 40bp this bound was doing
        # all the sizing -- 0.2% of each reserve, which capped a $2.1M
        # book open at $10k/day and starved every quadrant behind it.
        # Loose enough that temp_frac governs and this only catches the
        # case where one pool is much thinner than the others.
        self.max_impact_bp = int(_draw(scenario, cls, "max_impact_bp", r, 100))
        self.open_frac = _draw(scenario, cls, "open_frac", r, 0.35)

        self._m: list[float | None] = [None] * len(self.WINDOWS)
        self._n = 0
        self._over = 0
        self._issued = 0            # cum BUCK put into circulation (Q4 + open)
        self._retired = 0           # cum BUCK taken back out (Q2)
        self._burned = 0            # cum coverage released
        self._dev = 0.0
        self.q = [0, 0, 0, 0]

        face = int(_draw(scenario, cls, "face_k", r, 6000) * m6)
        self._bind_proxy(d)
        now_ts = d.w3.eth.get_block("latest")["timestamp"]
        per = max(1, face // self.N_CREDITS)
        self._proxy_exec(d, d.credit.address, d.credit.encode_abi(
            "setCreditIssuer",
            args=[getattr(d.chain.deployer, "address", d.chain.deployer), True]))
        # premiumRate 0, consistent with the module note above: insurance is
        # switched off in this simulation on both issuing agents.
        for _ in range(self.N_CREDITS):
            d.chain.send(d.credit.functions.createCredit(
                self.proxy.address, 0, per, 0, 0, 0, now_ts, 0))
        self._face = per * self.N_CREDITS
        self._token_ids = [
            d.credit.functions.tokenOfOwnerByIndex(self.proxy.address, i).call()
            for i in range(self.N_CREDITS)]
        d.chain.send(d.usdc.functions.mint(
            self.proxy.address,
            int(_draw(scenario, cls, "float_k", r, 2000) * m6)))

    # -- signal ----------------------------------------------------------- #

    def _bvib(self, d) -> float:
        try:
            return int(d.basket.functions.basketValueInBuck().call()) / 1e18
        except Exception:
            return 1.0

    def _ladder(self, x: float) -> None:
        """Common mode at seven scales.  In a real implementation this is
        mean_i(leg_i.m[k]) over ladders the director already holds -- the EMA
        is linear, so the mean of the EMAs IS the EMA of the mean, and the
        signal costs no new state."""
        self._n += 1
        for k, w in enumerate(self.WINDOWS):
            b = 2.0 / (w + 1.0)
            cur = x if self._m[k] is None else self._m[k] + (x - self._m[k]) * b
            self._m[k] = cur

    # -- chain state ------------------------------------------------------ #

    def _pools(self, d) -> list:
        return [(i, p) for i, p in enumerate(d.pool_buck) if p]

    def _depth(self, d) -> int:
        """Aggregate BUCK depth across the basket's own pools."""
        return sum(d.chain.balance_of(d.buck, p) for _i, p in self._pools(d))

    def _signed(self, d) -> int:
        try:
            return int(d.buck.functions.signedBalanceOf(self.proxy.address).call())
        except Exception:
            return 0

    def _unactivated(self, d) -> int:
        """Remaining credit capacity, read from chain across BOTH contracts.

        depreciatedFaceValue lives on BuckCredit and mintsBacked on Buck, and
        depreciation moves the difference underneath any Python-side tally --
        which is what produced 30 of 36 'insufficient credit allocation'
        refusals when BuckIssuerArbAgent counted it locally.
        """
        room = 0
        for tid in self._token_ids:
            try:
                face = d.credit.functions.depreciatedFaceValue(tid).call()
                used = d.buck.functions.mintsBacked(tid).call()
                room += max(0, face - used)
            except Exception:
                pass
        return room

    def _tok_value(self, d) -> int:
        v = 0
        for i, tc in enumerate(d.tokens):
            bal = d.chain.balance_of(tc, self.proxy.address)
            if bal <= 0:
                continue
            rt = d.chain.balance_of(tc, d.pool_usdc[i])
            ru = d.chain.balance_of(d.usdc, d.pool_usdc[i])
            if rt:
                v += bal * ru // rt
        return v

    def _why(self, ctr, e) -> None:
        why = repr(e)[:120]
        ctr.setdefault("moWhy", {})
        ctr["moWhy"][why] = ctr["moWhy"].get(why, 0) + 1

    # -- primitives ------------------------------------------------------- #

    def _buy_buck_across(self, d, want_buck: int, ctr) -> int:
        """Spend TOKEN across every BUCK pool to acquire ~`want_buck`.

        Returns BUCK actually acquired, measured as the change in the proxy's
        SIGNED balance -- which is the honest number whether the purchase is
        retiring a draw (signed climbing toward 0) or accumulating inventory.
        `balanceOf` would not do: it counts unused credit headroom, so it moves
        when the credit line moves and not only when BUCK does.
        """
        pools = self._pools(d)
        if not pools or want_buck <= 0:
            return 0
        before = self._signed(d)
        share = max(1, want_buck // len(pools))
        for i, p in pools:
            tc = d.tokens[i]
            held = d.chain.balance_of(tc, self.proxy.address)
            if held <= 0:
                continue
            r_in = d.chain.balance_of(tc, p)
            r_out = d.chain.balance_of(d.buck, p)
            if r_in <= 0 or r_out <= 0:
                continue
            need = self._amount_in_for_out(r_in, r_out, share, d.fee_buck)
            spend = min(held, need or (r_in // 50),
                        _impact_cap(r_in, self.max_impact_bp))
            if spend <= 0:
                continue
            try:
                self._swap_via_simlp(d, p, tc, spend, self.proxy.address)
            except Exception as e:
                self._why(ctr, e)
        got = self._signed(d) - before
        if got > 0:
            ctr["moBought"] = ctr.get("moBought", 0) + got
        return max(0, got)

    def _sell_buck_across(self, d, want_buck: int, ctr) -> int:
        """Sell BUCK across every pool, acquiring TOKEN.  Returns BUCK sold."""
        pools = self._pools(d)
        if not pools or want_buck <= 0:
            return 0
        before = self._signed(d)
        share = max(1, want_buck // len(pools))
        for _i, p in pools:
            r_out = d.chain.balance_of(d.buck, p)
            if r_out <= 0:
                continue
            # Cap against the BUCK side: selling BUCK adds to that reserve,
            # and it is that reserve's move that shows up in bvib.
            amt = min(share, _impact_cap(r_out, self.max_impact_bp))
            if amt < 10 ** 6:
                continue
            try:
                self._swap_via_simlp(d, p, d.buck, amt, self.proxy.address)
            except Exception as e:
                self._why(ctr, e)
        sold = before - self._signed(d)
        if sold > 0:
            ctr["moSold"] = ctr.get("moSold", 0) + sold
        return max(0, sold)

    def _mint(self, d, amt: int, ctr) -> int:
        """Activate coverage.  Issues nothing by itself -- spending is what
        draws the signed balance negative and puts BUCK into circulation."""
        amt = min(amt, self._unactivated(d))
        if amt < 10 ** 6:
            return 0
        try:
            self._proxy_exec(d, d.buck.address,
                             d.buck.encode_abi("mint(uint256)", args=[amt]))
            return amt
        except Exception as e:
            self._why(ctr, e)
            ctr["moThrottled"] = ctr.get("moThrottled", 0) + 1
            return 0

    def _burn(self, d, amt: int, ctr) -> int:
        """Release coverage so the retired line cannot simply be redrawn."""
        if amt < 10 ** 6:
            return 0
        try:
            self._proxy_exec(d, d.buck.address,
                             d.buck.encode_abi("burn(uint256)", args=[amt]))
            self._burned += amt
            ctr["moBurned"] = ctr.get("moBurned", 0) + amt
            return amt
        except Exception as e:
            self._why(ctr, e)
            return 0

    # -- the standing book ------------------------------------------------ #

    def _fund(self, d, ctr) -> None:
        """Keep a TOKEN reserve on hand.

        Q1 spends TOKEN into the basket's own pools, and a desk that runs out
        of the asset it sells stops being a desk: in the first smoke run the
        agent spent its entire TOKEN inventory on day 20 and then sat inert
        for forty days at a 600bp deviation, having fired exactly one
        operation.  Conversion goes through the deep TOKEN/USDC pools, which
        are NOT the venue being operated on, so topping up does not itself
        move the signal being measured.
        """
        cash = d.chain.balance_of(d.usdc, self.proxy.address)
        if cash < 10 ** 6:
            return
        want = int(self._depth(d) * self.temp_frac * 2)      # ~2 days of ops
        if self._tok_value(d) >= want:
            return
        per = max(1, min(cash, want) // max(1, len(d.tokens)))
        for i, _tc in enumerate(d.tokens):
            r_in = d.chain.balance_of(d.usdc, d.pool_usdc[i])
            amt = min(per, _impact_cap(r_in, self.max_impact_bp),
                      d.chain.balance_of(d.usdc, self.proxy.address))
            if amt < 10 ** 6:
                continue
            try:
                self._swap_via_simlp(d, d.pool_usdc[i], d.usdc, amt,
                                     self.proxy.address)
                ctr["moFunded"] = ctr.get("moFunded", 0) + amt
            except Exception as e:
                self._why(ctr, e)

    def _open_book(self, d, ctr) -> None:
        """Build the standing book a slice per day during ladder warmup.

        Deferred out of setup() because the TOKEN/BUCK pools are seeded by
        BootstrapDMAgent's own setup and agent setup order is not guaranteed
        -- opening against empty pools would sell into nothing.  Built
        INCREMENTALLY, and only while the ladder is still warming, for two
        reasons: a single large sale is exactly the disturbance this agent
        exists to damp, and once measurement starts the book must be a
        given rather than something the desk is still assembling.
        """
        target = int(self._face * self.open_frac)
        if self._issued >= target:
            return
        want = min(target - self._issued,
                   int(self._depth(d) * self.temp_frac * 3))
        if self._mint(d, want, ctr) <= 0:
            return
        sold = self._sell_buck_across(d, want, ctr)
        if sold <= 0:
            return
        self._issued += sold
        ctr["moIssued"] = ctr.get("moIssued", 0) + sold
        ctr["moOpened"] = ctr.get("moOpened", 0) + sold

    # -- telemetry -------------------------------------------------------- #

    def arb_state(self, d) -> dict | None:
        if self.proxy is None:
            return None
        signed = self._signed(d)
        return {"cls": self.CTR, "idx": self.idx,
                "cash": d.chain.balance_of(d.usdc, self.proxy.address),
                "held": max(0, signed), "drawn": max(0, -signed),
                "tok": self._tok_value(d), "endow": self._face,
                "parked": 0, "receipts": 0,
                "devBp": int(1e4 * self._dev),
                "q": list(self.q)}

    # -- the loop --------------------------------------------------------- #

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0 or self.proxy is None:
            return
        # Advance the ladder ONCE per day, before any decision, or the level
        # lags the action by a tick.
        self._ladder(math.log(max(1e-9, self._bvib(d))))
        self._fund(d, ctr)
        if self._n <= self.ESTABLISH * self.WINDOWS[self.MEAS]:
            self._open_book(d, ctr)     # establish the desk first
            return
        dev = self._m[self.MEAS]
        self._dev = dev
        ctr["moDevBp"] = int(1e4 * dev)
        # Persistence is a DURATION, not an instantaneous velocity.  A short
        # velocity changes sign on noise, so "not turning" almost never held
        # and the outright quadrants were unreachable -- supply never moved
        # while half the mechanism looked healthy.
        self._over = self._over + 1 if abs(dev) > self.leash else 0
        if abs(dev) < self.deadband:
            return

        depth = self._depth(d)
        if depth <= 0:
            return
        signed = self._signed(d)
        held, drawn = max(0, signed), max(0, -signed)
        # Escalate on INVENTORY, not only on price.  Absorbing holds the
        # measured deviation down -- that IS absorbing -- so a persistence
        # test built on that deviation is suppressed by the very act it
        # polices.  Inventory is the one signal the operator's own action
        # cannot suppress, because it IS the operator's own action.
        inv = held / depth
        persistent = self._over >= self.persist_days or inv > self.inv_escalate

        try:
            supply = int(d.buck.functions.totalSupply().call())
        except Exception:
            supply = 0
        cum_cap = int(self.max_outright * supply) if supply else 0

        size = int(depth * (self.perm_frac if persistent else self.temp_frac))
        if size <= 0:
            return

        if dev > 0:
            # BUCK CHEAP: the basket costs more BUCK than it should.  Buy it.
            if inv > self.inv_max:
                ctr["moPosLimit"] = ctr.get("moPosLimit", 0) + 1
                return                     # hard position limit
            got = self._buy_buck_across(d, size, ctr)
            if got <= 0:
                return
            # Q2 needs a drawn line to retire against; without one the buy has
            # moved float from the pool to this account and changed no supply,
            # which is Q1 whatever we call it.
            room = (self._retired - self._issued) < cum_cap
            if persistent and drawn > 0 and room:
                # Supply fell on the BUY, not on the burn: `burn` deactivates
                # coverage, it does not destroy tokens -- "you repay the
                # credit, then release the coverage, as with any loan".  So
                # the buy-back is the retirement and the burn is what makes it
                # permanent, by closing the line so it cannot be redrawn.
                # Gating the burn on a POSITIVE held balance (as the first
                # cut did) meant it never fired while the draw was only
                # partly repaid, which is most of the time.
                self._retired += min(got, drawn)
                ctr["moRetired"] = ctr.get("moRetired", 0) + min(got, drawn)
                self._burn(d, min(got, drawn), ctr)
                self.q[1] += 1
                ctr["moQ2"] = ctr.get("moQ2", 0) + 1
            else:
                if persistent and drawn <= 0:
                    ctr["moNoBook"] = ctr.get("moNoBook", 0) + 1
                self.q[0] += 1
                ctr["moQ1"] = ctr.get("moQ1", 0) + 1
        else:
            # BUCK DEAR.  Sell it.
            room = (self._issued - self._retired) < cum_cap
            if persistent and room:
                minted = self._mint(d, size, ctr)
                if minted <= 0:
                    return
                sold = self._sell_buck_across(d, minted, ctr)
                if sold <= 0:
                    return
                self._issued += sold
                ctr["moIssued"] = ctr.get("moIssued", 0) + sold
                self.q[3] += 1
                ctr["moQ4"] = ctr.get("moQ4", 0) + 1
            else:
                if persistent:
                    ctr["moCumLimit"] = ctr.get("moCumLimit", 0) + 1
                # Q3 sells only what Q1 previously absorbed.
                if held < 10 ** 6:
                    return
                if self._sell_buck_across(d, min(held, size), ctr) <= 0:
                    return
                self.q[2] += 1
                ctr["moQ3"] = ctr.get("moQ3", 0) + 1
