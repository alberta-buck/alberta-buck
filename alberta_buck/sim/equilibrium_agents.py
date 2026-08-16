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

    def _release_pending(self, d, bvib: float, ctr) -> None:
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
                  int(max(10 ** 6, self.pending_release * self.release_rate)))
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
        issue_rate = max(0.0, (supply_now - prev) / max(1, prev))
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
        self._release_pending(d, bvib, ctr)

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
            retire_eff = self.retire_rate * (
                1.0 + self.disc_gain * max(0.0, bvib - 1.0))
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
        if _spec(scenario, cls, "arrive_mode", "immediate") != "immediate":
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

            elif premium > 0 and holding > 10 ** 6:
                # Spend above value: sell BUCK for USDC, keeping reserve_frac.
                ru = d.chain.balance_of(d.usdc, d.pool_ub)
                rb = d.chain.balance_of(d.buck, d.pool_ub)
                spot = ru * PARITY // rb if rb else 0
                want_usdc = int(self.base_rate * (1.0 + self.prem_gain * premium))
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
        self.premium_rate = int(_draw(scenario, cls, "premium_bp", r,
                                      (50, 150)))
        self.save_rate = _draw(scenario, cls, "save_rate", r, (0.25, 0.75))
        self.retire_disc = _draw(scenario, cls, "retire_disc", r, 0.02)
        self.cash_buffer = int(_draw(scenario, cls, "buffer_k", r, 20) * m6)
        self.overdraw_effort = _draw(scenario, cls, "overdraw_effort", r, 0.0)
        horizon = max(1, int(getattr(scenario, "days", 1)))
        # Arrival: growth-regime schedule when arrive_mode is set; else the
        # legacy uniform stagger over arrive_frac of the horizon.
        if _spec(scenario, cls, "arrive_mode", "immediate") != "immediate":
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
        nw = cash + held - self.mortgage - drawn + jub
        return {"idx": self.idx, "theta": round(self.theta, 2),
                "pattern": self.pattern, "nw": nw,
                "hypo": self.hypo_cash - self.hypo_mortgage, "cash": cash,
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

    # -- the loop -------------------------------------------------------------- #

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0 or self.proxy is None:
            return
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
        want_tranche = min(self.tranche_cap, headroom, self.mortgage)
        if want_tranche >= 10 ** 6 and self.mortgage > 10 ** 6:
            try:
                _, principal = d.buck.functions.quoteMint(
                    want_tranche, self._token_ids).call()
            except Exception:
                principal = 0
            required = principal * ff // 10 ** 18 if ff else 0
            bal = d.buck.functions.balanceOf(self.proxy.address).call()
            short = required - bal
            if short > 10 ** 6 and disc <= max(self.theta * self.apr, 0.01):
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
        #    deploy it against the mortgage.
        if self.mortgage > 10 ** 6 and disc <= self.theta * self.apr \
                and want_tranche >= 10 ** 6:
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
            except Exception:
                minted = False       # gate said no: save more, retry later
                self.throttled += 1
                ctr["bcdThrottled"] = ctr.get("bcdThrottled", 0) + 1
            if minted and d.pool_ub:
                before = d.chain.balance_of(d.usdc, self.proxy.address)
                try:
                    sold = self._sell_capped(d, d.pool_ub, want_tranche)
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
                           int(r_out * (1.0 - math.sqrt(spot))))
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
        cap = (int(r_out * (1.0 - math.sqrt(spot))) if spot < 1.0
               else max(10 ** 6, r_out // 50))
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
        amt = min(held, cap if cap > 0 else held)
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
        self.premium_rate = int(_draw(scenario, cls, "premium_bp", r, (25, 75)))
        # How far BUCK must be rich before issuing, and how far back toward
        # parity before covering.  The gap between them is hysteresis.
        self.issue_at = _draw(scenario, cls, "issue_at", r, (0.010, 0.040))
        self.cover_at = _draw(scenario, cls, "cover_at", r, (0.000, 0.015))
        self.step_frac = _draw(scenario, cls, "step_frac", r, (0.05, 0.20))
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
        unactivated = max(0, self._face - self._activated)
        if unactivated < 10 ** 6:
            return
        amt = int(unactivated * self.step_frac)
        # Cap the bite so one issuance does not reprice the venue by itself.
        r_out = d.chain.balance_of(d.buck, d.pool_ub)
        amt = min(amt, max(10 ** 6, r_out // 50), unactivated)
        if amt < 10 ** 6:
            return
        # Step one: open the line.  This is where the funding-factor gate
        # applies, and where the premium is paid.
        try:
            self._proxy_exec(d, d.buck.address,
                             d.buck.encode_abi("mint(uint256)", args=[amt]))
            self._activated += amt
        except Exception:
            ctr["biaThrottled"] = ctr.get("biaThrottled", 0) + 1
            # Not enough float to clear the gate: convert some USDC to BUCK
            # so the next attempt can.  This is the counter-cyclical demand
            # the gate is designed to compel.
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
        amt = min(held, max(10 ** 6, r_buck // 100))
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
