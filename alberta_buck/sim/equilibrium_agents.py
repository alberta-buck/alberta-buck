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
        zero_for_one = input_c.address.lower() == t0.lower()
        sqrt_limit = MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1
        self._proxy_exec(
            d, input_c.address,
            input_c.encode_abi("transfer(address,uint256)",
                               args=[d.simlp.address, int(amount)]),
            proxy=from_proxy)
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
class OptimalControlDebtorAgent(_ProxyAgent):
    """A mortgage debtor solving the 'when to deploy BUCK credit' control
    problem -- Perry's optimal-control question: the drain of interest-
    bearing debt is itself a cost, so retiring the mortgage can beat waiting
    even when the BUCK trades at a DISCOUNT to its basket, if the interest
    saved outweighs the discount paid.

    On-chain mechanics mirror FatCreditBorrowerAgent's zero-premium
    activation: pledge a pool of property NFTs, activate the face once ->
    live K-scaled credit headroom.  Deploying credit = SELL BUCK into the
    floating BUCK/USDC pool (draws credit; proceeds are USDC on the proxy)
    -> retire mortgage principal.  Unwinding = BUY BUCK back with USDC
    (climbs the signed balance toward 0).  The mortgage and income are
    off-chain constructs held in agent fields; their USDC legs mint
    (income) and burn-to-sink (bank payments) so cash is chain truth.

    THE CONTROL LAW (aggressiveness theta, in years-of-interest):

        discount = max(0, basketValueInBuck - 1)      # cost of selling now
        deploy while mortgage > 0 and discount <= theta * apr

    theta = 0 is the conservative policy the sims used until now (deploy
    only at premium/parity); theta = 2 tolerates an immediate discount cost
    of up to two years' interest saving.  A ladder of thetas x income
    patterns (salary = monthly; lumpy = two harvest payments a year) runs in
    ONE world, so every strategy faces identical prices, and each agent
    carries a no-BUCK counterfactual ledger (same income, same mortgage,
    no credit deployments) -- the net-worth advantage is then strategy
    alone.  Simplification, documented: no rolling funding-reserve is
    modeled for these debtors (the FatCreditBorrower population supplies
    the system's funding-factor dynamics); adding the pre-buy toll to the
    deploy cost is the natural refinement.
    """

    THETAS = [0.0, 0.25, 1.0, 3.0]      # years-of-interest tolerance ladder
    N_CREDITS = 4
    MONTH = 30
    HARVEST_MONTHS = (8, 9)             # lumpy income lands in these months
    SINK = "0x000000000000000000000000000000000000dEaD"

    _seq = 0                            # ladder position (reset per build)

    def setup(self, d, scenario, rng) -> None:
        self._rng = _agent_rng(scenario.seed, type(self).__name__, self.idx)
        r = self._rng
        cls = type(self).__name__
        seq = type(self)._seq
        type(self)._seq += 1
        self.theta = self.THETAS[seq % len(self.THETAS)]
        self.pattern = "salary" if (seq // len(self.THETAS)) % 2 == 0 else "lumpy"

        m6 = 1_000 * 10 ** 6            # $1k in 6-dec micro-USDC
        self.apr = _draw(scenario, cls, "apr", r, 0.055)
        self.mortgage = int(_draw(scenario, cls, "mortgage_k", r, 1000) * m6)
        self.income_annual = int(_draw(scenario, cls, "income_k", r, 240) * m6)
        face = int(_draw(scenario, cls, "face_k", r, 900) * m6)
        self.retire_disc = _draw(scenario, cls, "retire_disc", r, 0.02)
        self.cash_buffer = int(_draw(scenario, cls, "buffer_k", r, 20) * m6)
        # Voluntary overdraw recovery effort (0 = doctrine: none needed).
        self.overdraw_effort = _draw(scenario, cls, "overdraw_effort", r, 0.0)
        self.jubilee_rate = _draw(scenario, cls, "jubilee_rate", r, 0.02)
        self.jubilee_relief = 0    # accrued lien dissolution (6-dec BUCK)
        # 25-year annuity payment on the initial principal.
        mrate = self.apr / 12.0
        self.payment = int(self.mortgage * mrate / (1.0 - (1.0 + mrate) ** -300))
        self.tranche_cap = self.payment * 12

        # Counterfactual (no-BUCK) ledger: same income, same payments.
        self.hypo_mortgage = self.mortgage
        self.hypo_cash = 0
        self._last_day = 0
        self._last_month_day = -self.MONTH   # first month fires at day 0
        self.deploys = 0
        self.deployed_buck = 0

        self._bind_proxy(d)
        now_ts = d.w3.eth.get_block("latest")["timestamp"]
        per = max(1, face // self.N_CREDITS)
        for _ in range(self.N_CREDITS):
            d.chain.send(d.credit.functions.createCredit(
                self.proxy.address, 0, per, 0, 0, 0, now_ts, 0))
        self._face = per * self.N_CREDITS
        try:
            self._proxy_exec(
                d, d.buck.address,
                d.buck.encode_abi("mint(uint256)", args=[self._face]))
        except Exception as e:
            print(f"[octl-{self.idx}] activate mint failed: {e!r}", flush=True)

    # -- off-chain money legs (USDC mint == income; burn-to-sink == bank) -- #

    def _income(self, d, months_elapsed: int, day) -> int:
        if self.pattern == "salary":
            amt = self.income_annual * months_elapsed // 12
        else:
            month = (day // self.MONTH) % 12
            amt = (self.income_annual // len(self.HARVEST_MONTHS)
                   if month in self.HARVEST_MONTHS else 0)
        if amt > 0:
            d.chain.send(d.usdc.functions.mint(self.proxy.address, amt))
        return amt

    def _pay_bank(self, d, amt: int) -> int:
        """Send `amt` of the proxy's USDC to the bank sink; return paid."""
        held = d.chain.balance_of(d.usdc, self.proxy.address)
        pay = min(amt, held)
        if pay > 0:
            self._proxy_exec(
                d, d.usdc.address,
                d.usdc.encode_abi("transfer(address,uint256)",
                                  args=[self.SINK, int(pay)]))
        return pay

    # -- observability ------------------------------------------------------ #

    def octl_state(self, d) -> dict | None:
        if self.proxy is None:
            return None
        try:
            cash = d.chain.balance_of(d.usdc, self.proxy.address)
            signed = d.buck.functions.signedBalanceOf(self.proxy.address).call()
            ru = d.chain.balance_of(d.usdc, d.pool_ub)
            rb = d.chain.balance_of(d.buck, d.pool_ub)
        except Exception:
            return None
        px = ru * PARITY // rb if rb else PARITY      # micro-USDC per BUCK
        drawn = max(0, -signed)
        held = max(0, signed)
        # The Jubilee fund melts the obligation ~2%/yr: value the liability
        # net of accrued relief (the system dissolves that much of the lien).
        # BUCK legs are valued AT PAR: the obligation is BUCK-denominated,
        # retirement timing is the holder's option, and Jubilee melts it --
        # instantaneous pool spot injects pure mark-to-market noise into a
        # long-horizon wealth comparison (measured: +/- $0.5-3M swings on a
        # ~$600k draw).  `px` stays in the record for MTM diagnostics.
        eff_drawn = max(0, drawn - self.jubilee_relief)
        nw = cash + held - self.mortgage - eff_drawn
        hypo_nw = self.hypo_cash - self.hypo_mortgage
        try:
            limit = d.buck.functions.creditLimit(self.proxy.address).call()
        except Exception:
            limit = -1
        return {"idx": self.idx, "theta": self.theta, "pattern": self.pattern,
                "nw": nw, "hypo": hypo_nw, "cash": cash, "limit": limit,
                "px": px, "mortgage": self.mortgage, "drawn": drawn,
                "jub": self.jubilee_relief, "deploys": self.deploys}

    # -- the loop ------------------------------------------------------------ #

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0 or self.proxy is None:
            return
        days = day - self._last_day
        self._last_day = day
        if days > 0:
            # Daily-compounded interest accrual, both ledgers.
            g = (1.0 + self.apr / 365.0) ** days
            self.mortgage = int(self.mortgage * g)
            self.hypo_mortgage = int(self.hypo_mortgage * g)

        if day - self._last_month_day < self.MONTH:
            return
        months = max(1, (day - self._last_month_day) // self.MONTH)
        self._last_month_day = day

        try:
            bvib = d.basket.functions.basketValueInBuck().call() / 1e18
            limit = d.buck.functions.creditLimit(self.proxy.address).call()
            signed = d.buck.functions.signedBalanceOf(self.proxy.address).call()
        except Exception as e:
            ctr["octl_err"] = repr(e)[:200]
            return
        drawn = max(0, -signed)

        # 0. Jubilee relief: the fund melts outstanding obligations ~2%/yr.
        self.jubilee_relief = min(
            drawn,
            self.jubilee_relief + int(drawn * self.jubilee_rate
                                      * (months * self.MONTH) / 365.0))

        # 1. Income (both ledgers earn identically).
        inc = self._income(d, months, day)
        self.hypo_cash += inc

        # 2. Mandatory mortgage service, both ledgers.
        due = min(self.payment * months, self.mortgage)
        paid = self._pay_bank(d, due)
        self.mortgage -= paid
        hdue = min(self.payment * months, self.hypo_mortgage)
        hpaid = min(hdue, self.hypo_cash)
        self.hypo_cash -= hpaid
        self.hypo_mortgage -= hpaid

        # 3. THE CONTROL: deploy BUCK credit against the interest drain.
        disc = max(0.0, bvib - 1.0)
        if self.mortgage > 10 ** 6 and disc <= self.theta * self.apr:
            spendable = max(0, limit - drawn)
            tranche = min(self.tranche_cap, spendable,
                          self.mortgage * PARITY // PARITY)
            if tranche >= 10 ** 6 and d.pool_ub:
                before = d.chain.balance_of(d.usdc, self.proxy.address)
                try:
                    sold = self._sell_capped(d, d.pool_ub, tranche)
                except Exception as e:
                    ctr["octl_sell_err"] = repr(e)[:200]
                    sold = 0
                if sold > 0:
                    got = d.chain.balance_of(
                        d.usdc, self.proxy.address) - before
                    principal = min(got, self.mortgage)
                    self._pay_bank(d, principal)
                    self.mortgage -= principal
                    self.deploys += 1
                    self.deployed_buck += sold
                    ctr["octlDeploys"] = ctr.get("octlDeploys", 0) + 1
                    ctr["octlDeployed"] = (
                        ctr.get("octlDeployed", 0) + sold)

        # 4. Unwind: forced when K tightens past the limit; opportunistic
        #    when the mortgage is gone and BUCK is at a discount (cheap).
        want = 0
        if drawn > limit and self.overdraw_effort > 0:
            # Voluntary only: on-chain nothing forces recovery -- the account
            # just cannot extend more credit while over the K-scaled limit.
            want = int((drawn - limit) * self.overdraw_effort)
        elif self.mortgage <= 10 ** 6 and drawn > 0 and disc >= self.retire_disc:
            cash = d.chain.balance_of(d.usdc, self.proxy.address)
            spare = max(0, cash - self.cash_buffer)
            if spare > 10 ** 6:
                want = drawn
        if want > 10 ** 6 and d.pool_ub:
            try:
                got = self._buy_buck(d, d.pool_ub, d.usdc, want, d.fee_ub)
                if got > 0:
                    ctr["octlRetired"] = ctr.get("octlRetired", 0) + got
            except Exception as e:
                ctr["octl_buy_err"] = repr(e)[:200]
