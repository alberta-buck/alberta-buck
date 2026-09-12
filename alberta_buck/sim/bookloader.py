"""WP-15: the BOOK-LOADING attacker -- CARRY-CONVEXITY.org prediction 10 and
WAVE3.org test-plan class 9 (gate P10): an attacker with a USDC budget
loads a facility's book toward its cap, waits for K to move on the
position loop, unwinds, and books the round trip -- the risk of an
inventory-fed K.

THE ATTACK (side "sell", the default: the undertakings' weak-side LADDER,
the facility whose absorbed inventory moves s today)

  load    from load_day, every tick while the load window is open: buy BUCK
          in BUCK/USDC and sell it into the TOKEN/BUCK basket pools, every
          pool by the same impact (push_bp), so the COMMON mode moves and
          bvib reads a discount; the ladder (undertaking_agents.py) absorbs
          at its tranche bids and books absorbed inventory into the
          observer, so s > 0 and the position loop lowers K.  The attacker
          stops selling while bvib is already past dev_target (the ladder
          is throughput-bound by its own leg cap, and a deeper discount
          loads it no faster), and stops the phase when the ladder's rho
          (frame ut_rho) is at or below rho_target, when |s| reaches
          s_target (0 = not used), or when the window closes.
  hold    hold_days: K moves; the book carries.
  unwind  over unwind_days: buy BUCK back from the basket pools with the
          TOKEN taken (the ladder unwinds at par on the way), then sell the
          BUCK for USDC.
  book    the round trip: P&L is par-marked like ExcursionArbAgent (USDC +
          signed BUCK + TOKEN at pool prices / bvib) and summed by the
          snapshot (frame bl_pnl); K at the end of loading and at the start
          of the unwind are recorded (bl_k_load / bl_k_unwind).

side "buy" loads the STRONG side instead: TOKEN bought in the truth pools
is sold into the basket pools (bvib reads a premium; the undertakings issue
and book issued inventory, s < 0, K rises), and the exploit is the moved
K: with a zero-premium face (face_m > 0) the unwind sells BUCK beyond what
is held, drawing credit at the higher limit into the premium (the credit
arbs' Q4), the obligation marked at par.  Without a face it simply books
the round trip.

  knobs ([agents.BookLoaderAgent]): budget_m [20, 30] (the ONLY draw --
    first on the class's keyed stream; every later knob is a _spec): side
    "sell", load_day 365, load_days 60, hold_days 30, unwind_days 15,
    rho_target 0.5, s_target 0.0, dev_target 0.02, push_bp 150, face_m 0.
  counters (ctr -> frame, only when the agent exists): bl_phase (0 idle,
    1 loading, 2 holding, 3 unwinding, 4 done), bl_bought (BUCK bought in
    BUCK/USDC), bl_loaded (BUCK sold into the basket pools, or bought from
    them on the buy side), bl_unwound (the reverse leg), bl_sold (BUCK sold
    for USDC), bl_target (rho_target), bl_rho_load / bl_s_load (the
    ladder's rho and the observer's s when loading ended), bl_loaded_frac
    (1 - rho at the end of loading; |s| on the buy side), bl_held_days,
    bl_k_load, bl_k_unwind, bl_pnl (snapshot), bl_err.

Determinism (TELEMETRY.md): one draw (budget_m) on the class's own keyed
stream; absent at count 0, so every existing cell is byte-identical.
"""

from __future__ import annotations

from alberta_buck.sim.agents import _register
from alberta_buck.sim.equilibrium_agents import (
    FEE_DEN, _ProxyAgent, _agent_rng, _impact_cap,
)
from alberta_buck.sim.experiment import draw as _draw, spec as _spec
from alberta_buck.sim.gauge import active_reserves

E6 = 10 ** 6
E18 = 10 ** 18

IDLE, LOADING, HOLDING, UNWINDING, DONE = 0, 1, 2, 3, 4


@_register
class BookLoaderAgent(_ProxyAgent):
    """The book-loading attacker (module docstring)."""

    CTR = "bl"

    def setup(self, d, scenario, rng) -> None:
        cls = type(self).__name__
        self._rng = _agent_rng(scenario.seed, cls, self.idx)
        r = self._rng
        # Draw order pinned: budget_m first (the only draw); add new draws
        # AFTER it.  Scalar specs consume no variate.
        self.budget = int(_draw(scenario, cls, "budget_m", r,
                                (20, 30)) * 1_000_000 * E6)
        self.side = str(_spec(scenario, cls, "side", "sell"))
        self.load_day = int(_spec(scenario, cls, "load_day", 365))
        self.load_days = max(1, int(_spec(scenario, cls, "load_days", 60)))
        self.hold_days = int(_spec(scenario, cls, "hold_days", 30))
        self.unwind_days = max(1, int(_spec(scenario, cls, "unwind_days", 15)))
        self.rho_target = float(_spec(scenario, cls, "rho_target", 0.5))
        self.s_target = float(_spec(scenario, cls, "s_target", 0.0))
        self.dev_target = float(_spec(scenario, cls, "dev_target", 0.02))
        self.push_bp = int(_spec(scenario, cls, "push_bp", 150))
        self.face_m = float(_spec(scenario, cls, "face_m", 0.0))
        self._bind_proxy(d)
        d.chain.send(d.usdc.functions.mint(self.proxy.address, self.budget))
        self._face = 0
        if self.face_m > 0:
            face = int(self.face_m * 1_000_000 * E6)
            now_ts = d.w3.eth.get_block("latest")["timestamp"]
            self._proxy_exec(d, d.credit.address, d.credit.encode_abi(
                "setCreditIssuer",
                args=[getattr(d.chain.deployer, "address", d.chain.deployer),
                      True]))
            d.chain.send(d.credit.functions.createCredit(
                self.proxy.address, 0, face, 0, 0, 0, now_ts, 0))
            self._face = face
        self._nw0 = self.budget
        self.phase = IDLE
        self.bought = self.loaded = self.unwound = self.sold = 0
        self.rho_load = 1.0
        self.s_load = 0.0
        self.k_load = self.k_unwind = 0.0
        self._load_end = self.load_day + self.load_days
        self._hold_end = None
        self._unwind_end = None
        self._tpb = None

    def bootstrap(self, d, scenario, ctr) -> None:
        for k in ("bl_phase", "bl_bought", "bl_loaded", "bl_unwound",
                  "bl_sold", "bl_held_days"):
            ctr.setdefault(k, 0)
        for k in ("bl_rho_load", "bl_s_load", "bl_loaded_frac", "bl_k_load",
                  "bl_k_unwind"):
            ctr.setdefault(k, 0.0)
        ctr["bl_target"] = self.rho_target
        ctr.setdefault("bl_err", "")

    # -- telemetry ------------------------------------------------------- #

    def telemetry_static(self) -> dict:
        return {"budget": self.budget, "side": self.side,
                "load_day": self.load_day, "load_days": self.load_days,
                "hold_days": self.hold_days, "unwind_days": self.unwind_days,
                "rho_target": self.rho_target, "s_target": self.s_target,
                "dev_target": self.dev_target, "push_bp": self.push_bp,
                "face": self._face, "nw0": self._nw0}

    def telemetry(self, d) -> dict | None:
        if self.proxy is None:
            return None
        rec = self._telemetry_common(d)
        try:
            rec["bk"] = self._book_par(d, self._bvib(d))
            rec["nw"] += rec["bk"]
        except Exception:
            pass
        rec.update({"phase": self.phase, "bought": self.bought,
                    "loaded": self.loaded, "unwound": self.unwound,
                    "sold": self.sold, "k_load": round(self.k_load, 6),
                    "k_unwind": round(self.k_unwind, 6),
                    "pnl": rec["nw"] - self._nw0})
        return rec

    def arb_state(self, d) -> dict | None:
        if self.proxy is None:
            return None
        try:
            s = int(d.buck.functions.signedBalanceOf(self.proxy.address).call())
            book = self._book_par(d, self._bvib(d))
        except Exception:
            return None
        return {"cls": self.CTR, "idx": self.idx,
                "cash": d.chain.balance_of(d.usdc, self.proxy.address),
                "held": max(0, s), "drawn": max(0, -s), "book": book,
                "phase": self.phase, "loaded": self.loaded,
                "unwound": self.unwound}

    def _nw(self, d) -> int:
        """Par-marked net worth: USDC + signed BUCK + the TOKEN book."""
        u = d.chain.balance_of(d.usdc, self.proxy.address)
        s = d.buck.functions.signedBalanceOf(self.proxy.address).call()
        try:
            bk = self._book_par(d, self._bvib(d))
        except Exception:
            bk = 0
        return u + s + bk

    # -- reads ------------------------------------------------------------ #

    def _bvib(self, d) -> float:
        return d.basket.functions.basketValueInBuck().call() / 1e18

    def _k(self, d) -> float:
        try:
            return d.kctrl.functions.buckK().call() / 1e18
        except Exception:
            return 0.0

    def _s(self, d) -> float:
        obs = getattr(d, "observer", None)
        if obs is None:
            return 0.0
        try:
            return obs.functions.aggregatePosition().call() / 1e18
        except Exception:
            return 0.0

    def _book_par(self, d, bvib: float) -> int:
        """The TOKEN holdings at pool prices in PAR units (6-dec)."""
        val = 0
        for i, tok in enumerate(d.tokens):
            held = d.chain.balance_of(tok, self.proxy.address)
            if held <= 0:
                continue
            rt = d.chain.balance_of(tok, d.pool_buck[i])
            rb = d.chain.balance_of(d.buck, d.pool_buck[i])
            if rt > 0:
                val += held * rb // rt
        return int(val / max(1e-9, bvib))

    def _states(self, d) -> list[tuple[int, int]]:
        return [(d.chain.balance_of(d.tokens[i], d.pool_buck[i]),
                 d.chain.balance_of(d.buck, d.pool_buck[i]))
                for i in range(len(d.tokens))]

    # -- legs -------------------------------------------------------------- #

    def _ub_buy(self, d, want: int) -> int:
        """USDC -> BUCK in BUCK/USDC, impact-capped; returns BUCK got."""
        cash = d.chain.balance_of(d.usdc, self.proxy.address)
        ru, rb = active_reserves(d.chain, d.pool_ub, d.usdc, d.buck)
        fee = int(((getattr(d, "fee_ub", 0) or 0) / 1e6) * FEE_DEN)
        x = min(cash, _impact_cap(ru, self.push_bp),
                self._amount_in_for_out(ru, rb, want, fee))
        if x < E6:
            return 0
        before = d.chain.balance_of(d.buck, self.proxy.address)
        self._swap_via_simlp(d, d.pool_ub, d.usdc, x, self.proxy.address)
        got = max(0, d.chain.balance_of(d.buck, self.proxy.address) - before)
        self.bought += got
        return got

    def _ub_sell(self, d, want: int) -> int:
        """BUCK -> USDC in BUCK/USDC, impact-capped; returns BUCK sold."""
        _ru, rb = active_reserves(d.chain, d.pool_ub, d.usdc, d.buck)
        y = min(want, _impact_cap(rb, self.push_bp))
        sold = self._sell_capped(d, d.pool_ub, y)
        self.sold += sold
        return sold

    def _basket_sell(self, d, budget: int) -> int:
        """BUCK -> TOKEN across the basket pools, each pool moved by the
        same impact (the common mode), the total capped by `budget` BUCK.
        Returns BUCK sold."""
        st = self._states(d)
        caps = [_impact_cap(rb, self.push_bp) if rb > 0 else 0 for _rt, rb in st]
        total = sum(caps)
        if total <= 0 or budget < E6:
            return 0
        scale = min(1.0, budget / total)
        before = d.chain.balance_of(d.buck, self.proxy.address)
        for i, y in enumerate(caps):
            y = int(y * scale)
            if y >= E6:
                self._swap_via_simlp(d, d.pool_buck[i], d.buck, y,
                                     self.proxy.address)
        return max(0, before - d.chain.balance_of(d.buck, self.proxy.address))

    def _basket_buy(self, d) -> int:
        """TOKEN -> BUCK across the basket pools from the holdings, each
        pool moved by at most push_bp.  Returns BUCK got."""
        st = self._states(d)
        before = d.chain.balance_of(d.buck, self.proxy.address)
        for i, (rt, _rb) in enumerate(st):
            held = d.chain.balance_of(d.tokens[i], self.proxy.address)
            x = min(held, _impact_cap(rt, self.push_bp)) if rt > 0 else 0
            if x > 0:
                self._swap_via_simlp(d, d.pool_buck[i], d.tokens[i], x,
                                     self.proxy.address)
        return max(0, d.chain.balance_of(d.buck, self.proxy.address) - before)

    def _truth_buy(self, d) -> int:
        """USDC -> TOKEN in the truth pools, one impact-capped bite per
        pool sized to what the basket pool will take.  Returns USDC spent."""
        cash = d.chain.balance_of(d.usdc, self.proxy.address)
        if cash < E6:
            return 0
        fee = int(((getattr(d, "fee_usdc", 0) or 0) / 1e6) * FEE_DEN)
        st = self._states(d)
        spent = 0
        share = cash // max(1, len(d.tokens))
        for i, (rt, _rb) in enumerate(st):
            want_t = _impact_cap(rt, self.push_bp) if rt > 0 else 0
            if want_t <= 0:
                continue
            ru, rtu = active_reserves(d.chain, d.pool_usdc[i], d.usdc,
                                      d.tokens[i])
            x = min(share, _impact_cap(ru, self.push_bp),
                    self._amount_in_for_out(ru, rtu, want_t, fee))
            if x < E6:
                continue
            before = d.chain.balance_of(d.usdc, self.proxy.address)
            self._swap_via_simlp(d, d.pool_usdc[i], d.usdc, x,
                                 self.proxy.address)
            spent += before - d.chain.balance_of(d.usdc, self.proxy.address)
        return spent

    # -- the phases --------------------------------------------------------- #

    def _load_tick(self, d, ctr) -> None:
        bvib = self._bvib(d)
        if self.side == "sell":
            if bvib >= 1.0 + self.dev_target:
                return                       # deep enough: let the ladder work
            st = self._states(d)
            want = sum(_impact_cap(rb, self.push_bp) for _rt, rb in st if rb > 0)
            spendable = max(0, int(d.buck.functions.balanceOf(
                self.proxy.address).call()))
            if spendable < want:
                self._ub_buy(d, want - spendable)
                spendable = max(0, int(d.buck.functions.balanceOf(
                    self.proxy.address).call()))
            self.loaded += self._basket_sell(d, spendable)
        else:
            if bvib <= 1.0 - self.dev_target:
                return                       # dear enough: let the desk issue
            self._truth_buy(d)
            self.loaded += self._basket_buy(d)

    def _load_done(self, d, ctr) -> bool:
        rho = float(ctr.get("ut_rho", 1.0))
        s = self._s(d)
        if self.side == "sell" and rho <= self.rho_target:
            return True
        if self.s_target and abs(s) >= self.s_target:
            return True
        return False

    def _unwind_tick(self, d) -> None:
        if self.side == "sell":
            self.unwound += self._basket_buy(d)
            held = max(0, int(d.buck.functions.signedBalanceOf(
                self.proxy.address).call()))
            if held >= E6:
                self._ub_sell(d, held)
        else:
            # Sell BUCK for USDC: what is held and, with a face, the credit
            # the moved K now allows (the exploit -- drawn at par, sold dear).
            spendable = max(0, int(d.buck.functions.balanceOf(
                self.proxy.address).call()))
            if self._face == 0:
                spendable = min(spendable, max(0, int(
                    d.buck.functions.signedBalanceOf(self.proxy.address).call())))
            if spendable >= E6:
                self._ub_sell(d, spendable)
            # Leftover TOKEN back into the basket pools for BUCK.
            self.unwound += self._basket_buy(d)

    def act(self, d, scenario, day, tick, ctr) -> None:
        if self.proxy is None or not d.pool_ub or not d.pool_buck:
            return
        try:
            if self.phase == IDLE and day >= self.load_day:
                self.phase = LOADING
            if self.phase == LOADING:
                if day >= self._load_end or self._load_done(d, ctr):
                    self.phase = HOLDING
                    self.rho_load = float(ctr.get("ut_rho", 1.0))
                    self.s_load = self._s(d)
                    self.k_load = self._k(d)
                    self._hold_end = day + self.hold_days
                else:
                    self._load_tick(d, ctr)
            if self.phase == HOLDING and day >= self._hold_end:
                self.phase = UNWINDING
                self.k_unwind = self._k(d)
                self._unwind_end = day + self.unwind_days
            if self.phase == UNWINDING:
                if day >= self._unwind_end:
                    self.phase = DONE
                else:
                    self._unwind_tick(d)
        except Exception as e:
            ctr["bl_err"] = repr(e)[:160]
        ctr["bl_phase"] = self.phase
        ctr["bl_bought"] = self.bought
        ctr["bl_loaded"] = self.loaded
        ctr["bl_unwound"] = self.unwound
        ctr["bl_sold"] = self.sold
        ctr["bl_rho_load"] = self.rho_load
        ctr["bl_s_load"] = self.s_load
        ctr["bl_loaded_frac"] = ((1.0 - self.rho_load) if self.side == "sell"
                                 else abs(self.s_load))
        ctr["bl_k_load"] = self.k_load
        ctr["bl_k_unwind"] = self.k_unwind
        if self.phase >= HOLDING and self._hold_end is not None:
            ctr["bl_held_days"] = max(0, min(self.hold_days,
                                             day - (self._hold_end - self.hold_days)))
