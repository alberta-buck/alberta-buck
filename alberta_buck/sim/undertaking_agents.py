"""The Convertibility Undertakings desk -- CARRY-CONVEXITY.org D3, 7.4 and
8.3; WAVE3.org WP-2.  A proof of concept in a Python agent over the real
contracts: ONE agent stands in for the basket's undertakings desk with the
authority the eventual Solidity will have (a zero-premium BuckCredit face
sized "unlimited" for issuance; a TOKEN book sized reserve_frac x NAV for
absorption), so the economics can be judged by the catalogue's cells
before any contract exists.

Both sides trade the BUNDLE: BUCK against the constituent TOKENs in
basketAmount proportions, in the TOKEN/BUCK pools that define bvib, per
leg capped at C_leg = leg_bp of the pool's own reserve per act (the
liveness bound of BuckBasketOps.maxLegBp -- the redemption guard must
never brick).  Every trade is gated by an EX-ANTE quote at the capped
size, in the excursion agents' style: no edge at this size, no trade.

SIGNAL.  A 1-day EWMA of basketValueInBuck (sig_halflife); the agent acts
at tick 0 daily.  bvib > 1: BUCK cheap; bvib < 1: BUCK dear.

STRONG SIDE (BUCK dear).  When the signal is below 1 / (1 + p*eps) the
desk mints BUCK against the face and sells it for the bundle, accepting
at least (1 + p*eps) bundles per BUCK at the capped size: the standing
offer of D3, unlimited in aggregate, bounded per act by C_leg.  Booked as
ut_issued (cumulative) and ut_issued_open (the standing position).  The
MIRROR unwind: when the signal is back at or above par the desk buys BUCK
back with TOKEN toward the drawn balance at no worse than (1 + eps/2)
bundles per BUCK -- retirement at par, the credit base's Q2 with a
best-effort burn -- booked as ut_retired.

WEAK SIDE (BUCK cheap).  The TOKEN book is struck as the LADDER of
`alberta_buck.sim.ladder`: tranche k bids (1 - eps - k*delta) bundles per
BUCK for phi of the reserve remaining.  When the signal is beyond the
current tranche's edge (1 / b_k) the desk buys BUCK with TOKEN from the
book, capped per pool at C_leg AND at the tranche's remaining size, at no
more than b_k bundles per BUCK; the bundles spent fill the ladder.
Booked as ut_absorbed / ut_absorbed_open, with ut_rho (R / R0) and
ut_tranche.  The unwind is AT PAR (D1, TraHK-style: never held out for a
premium): when the signal is back within eps/2 of par from the discount
side the desk sells the absorbed BUCK back for the bundle at no less than
(1 - eps/2) bundles per BUCK, replenishing the book; the ladder is reset
when the open position is fully unwound.  A strong-side sale while
absorbed inventory is open disposes of that inventory first (the market
paying a premium is the one case D1 sells it above par); a weak-side buy
while issued BUCK is open retires that first.  The two books are the
monetary book's issued / absorbed columns, and their difference is the
desk's net inventory I_d that the shadow controller (WP-3a) will read.

Counters (ctr -> snapshot frame): ut_issued, ut_issued_open, ut_retired,
ut_absorbed, ut_absorbed_open, ut_unwound (BUCK, 6-dec, summed over
desks), ut_rho (float), ut_tranche (int; last desk to act), ut_trades,
ut_err (repr of the last exception).  Net worth is par-marked like
ExcursionArbAgent (USDC + signed BUCK + the TOKEN book at pool prices /
bvib); arb_state() carries the per-desk inventory into frame["arb2"].

Knobs ([agents.UndertakingAgent], all drawable [lo, hi] or scalar, off
the class's own keyed rng stream; draw order pinned): eps 0.03, p 2.0,
delta 0.01, phi 0.25, reserve_frac 0.5, leg_bp 40, face_m 1000,
sig_halflife 1.0, kappa 0.0.  Endows NOTHING in BUCK.

LOCAL SKEW (WP-14; CARRY-CONVEXITY.org D7 "The lever count"; WAVE3.org
R16).  Each side's edge is skewed by the desk's OWN inventory,

    band = band0 + kappa * q / cap

with q = absorbed_open - issued_open (BUCK; absorbed positive), cap the
symmetric notional reserve_frac x NAV struck with the book (decision 11:
the strong side's governance notional and the ladder's R0 are the same
number), fill f = q / cap in [-1, 1] and skew = kappa * f in eps units
(`skew_bands`).  Long ABSORBED inventory (f > 0): the weak side's eps
becomes eps_w = eps + skew -- the ladder's bids deepen, the at-par unwind
fires at sig <= 1 + eps_w / 2 (sooner) and accepts down to 1 - eps_w / 2
-- and the strong side's eps becomes eps_s = eps - skew, so the absorbed
inventory is offered at a smaller premium.  Long ISSUED inventory (f < 0):
the mirror -- the strong side needs the deeper premium eps_s = eps +
|skew|, the at-par retire fires at sig >= 1 - |skew| / 2 (sooner) and pays
up to 1 + eps_s / 2, and the weak side's bid tightens to eps_w = eps -
|skew|, retiring the issued book sooner.  A facility long inventory bids
lower and offers sooner (Avellaneda-Stoikov), so its own flow reverts its
own book while K mid-ranges only the common mode.  Both edges are floored
at 0 (never through par, D1) and capped at EPS_MAX so the ladder's first
bid stays positive.  kappa 0 (the default) hands every branch the very
same floats as before the skew existed, so every banked cell is
byte-identical.  Knob kappa (drawn AFTER sig_halflife, the last of the
pinned order); counters sk_ut (the applied skew, eps units), sk_ut_f (the
fill) and sk_ut_n (trades executed under a non-zero skew), written only
when a skew is applied; note(kind="skew") carries the why before the sends
(TELEMETRY.md v2).
"""

from __future__ import annotations

from alberta_buck.sim.agents import _register
from alberta_buck.sim.equilibrium_agents import (
    FEE_DEN, ExcursionArbAgent, _ProxyAgent, _agent_rng, _cp_out,
)
from alberta_buck.sim.experiment import draw as _draw
from alberta_buck.sim.ladder import Ladder

E6 = 10 ** 6
E18 = 10 ** 18
EPS_MAX = 0.45                  # a skewed edge never past this (bid > 0)


def _clamp_eps(e: float) -> float:
    return 0.0 if e < 0.0 else (EPS_MAX if e > EPS_MAX else e)


def skew_bands(eps: float, kappa: float, q: int, cap: int
               ) -> tuple[float, float, float, float]:
    """(eps_w, eps_s, skew, fill): the local skew of D7 on the undertakings'
    two edges (module doc).  fill = q / cap in [-1, 1] (absorbed positive);
    skew = kappa * fill in eps units; the weak (absorbing) side's band is
    eps + skew and the strong (issuing) side's eps - skew, each floored at
    0 and capped at EPS_MAX.  With kappa 0, cap 0 or an empty book the
    return is (eps, eps, 0.0, fill) with eps the very same float."""
    fill = 0.0
    if cap > 0 and q:
        fill = max(-1.0, min(1.0, q / cap))
    if not kappa or fill == 0.0:
        return eps, eps, 0.0, fill
    skew = kappa * fill
    return _clamp_eps(eps + skew), _clamp_eps(eps - skew), skew, fill


def _basket_nav(d) -> int:
    """Total BUCK value of the basket pools (TOKEN + BUCK sides), 6-dec --
    the snapshot's basketNav, computed the same way so the book is sized
    against the number the frames carry."""
    total = 0
    for i in range(len(d.tokens)):
        rt = d.chain.balance_of(d.tokens[i], d.pool_buck[i])
        rb = d.chain.balance_of(d.buck, d.pool_buck[i])
        if rt == 0 or rb == 0:
            continue
        p = rb * (10 ** d.dec[i]) // rt
        total += rt * p // (10 ** d.dec[i])
        total += rb
    return total


def _tok_per_basket(d) -> list[int]:
    """TOKEN raw units per PAR basket (1e6 raw BUCK at parity), from the
    basket's own basketAmount vector (ExcursionArbAgent._tok_per_basket)."""
    tpb = []
    for i in range(len(d.tokens)):
        try:
            ba = int(d.basket.functions.constituents(i).call()[2])
        except Exception:
            ba = 0
        tpb.append(ba * (10 ** d.dec[i]) // 10 ** 30)
    return tpb


def _book(ctr, key: str, delta: int) -> None:
    ctr[key] = ctr.get(key, 0) + delta


@_register
class UndertakingAgent(_ProxyAgent):
    """The undertakings desk (module docstring)."""

    CTR = "ut"

    def setup(self, d, scenario, rng) -> None:
        cls = type(self).__name__
        self._rng = _agent_rng(scenario.seed, cls, self.idx)
        r = self._rng
        # Draw order is pinned (keyed-rng vectors): eps, p, delta, phi,
        # reserve_frac, leg_bp, face_m, sig_halflife, kappa (WP-14).  Scalar
        # specs consume no draws; a [lo, hi] spec draws from this class's own
        # stream.
        self.eps = float(_draw(scenario, cls, "eps", r, 0.03))
        self.p = float(_draw(scenario, cls, "p", r, 2.0))
        self.delta = float(_draw(scenario, cls, "delta", r, 0.01))
        self.phi = float(_draw(scenario, cls, "phi", r, 0.25))
        self.reserve_frac = float(_draw(scenario, cls, "reserve_frac", r, 0.5))
        self.leg_bp = int(_draw(scenario, cls, "leg_bp", r, 40))
        self.face_m = float(_draw(scenario, cls, "face_m", r, 1000))
        self.sig_halflife = float(_draw(scenario, cls, "sig_halflife", r, 1.0))
        self.kappa = float(_draw(scenario, cls, "kappa", r, 0.0))   # WP-14
        self._skew = 0.0
        self._bind_proxy(d)
        # The "unlimited" zero-premium face, created the way the credit
        # base does (ExcursionCreditArbAgent): premiumRate 0 exempts the
        # mint from the funding gate; creditLimit = face * K.
        face = int(self.face_m * 1_000_000 * E6)
        self._face = face
        now_ts = d.w3.eth.get_block("latest")["timestamp"]
        self._proxy_exec(d, d.credit.address, d.credit.encode_abi(
            "setCreditIssuer",
            args=[getattr(d.chain.deployer, "address", d.chain.deployer),
                  True]))
        d.chain.send(d.credit.functions.createCredit(
            self.proxy.address, 0, face, 0, 0, 0, now_ts, 0))
        self.ladder: Ladder | None = None
        self._book0 = 0             # the TOKEN book at par when struck
        self._nw0 = 0
        self._tpb = None
        self._sig = None
        self._last_day = None
        self.issued = self.issued_open = self.retired = 0
        self.absorbed = self.absorbed_open = self.unwound = 0
        self.trades = 0

    # -- the book --------------------------------------------------------- #

    def bootstrap(self, d, scenario, ctr) -> None:
        """Strike the book once the basket pools are live (the DM agents'
        bootstraps seed them); falls through to the first act otherwise."""
        self._maybe_build_book(d, scenario)
        self._book_cap(ctr)

    def _book_cap(self, ctr) -> None:
        """WP-14: the desks' symmetric notional (reserve_frac x NAV at the
        strike, = the ladder's R0) summed into ctr["ut_cap"] -- the cap of
        both per-class stabilizers (shadow_book.py)."""
        if self._book0 <= 0:
            return
        book = ctr.setdefault("utCapBook", {})
        book[self.idx] = self._book0
        ctr["ut_cap"] = sum(book.values())

    def _maybe_build_book(self, d, scenario) -> None:
        if self.ladder is not None:
            return
        nav = _basket_nav(d)
        if nav <= 0:
            return
        book = int(nav * self.reserve_frac)
        # Constituent TOKENs in basket proportions worth `book` at the
        # day-0 reference prices (ExcursionArbAgent's neutral="basket" leg).
        for i, tok in enumerate(d.tokens):
            w = ExcursionArbAgent._weight(scenario, d, i)
            ref = max(1, scenario.prices.ref(i, 0))
            amt = int(book * w) * (10 ** d.dec[i]) // ref
            if amt > 0:
                d.chain.send(tok.functions.mint(self.proxy.address, amt))
        self._book0 = book
        self._nw0 = book
        # The ladder is in bundles (par baskets); p = 1: the bundle path.
        self.ladder = Ladder(self.eps, self.delta, self.phi, book / E6,
                             p=1.0)

    def _tok_per_basket(self, d) -> list[int]:
        if self._tpb is None:
            self._tpb = _tok_per_basket(d)
        return self._tpb

    def _book_par(self, d, bvib: float) -> int:
        """The TOKEN book marked at pool prices, in PAR units (6-dec):
        raw BUCK value / bvib (ExcursionArbAgent._baskets_par)."""
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

    def _bvib(self, d) -> float:
        return d.basket.functions.basketValueInBuck().call() / 1e18

    # -- telemetry ------------------------------------------------------- #

    def telemetry_static(self) -> dict:
        return {"eps": self.eps, "p": self.p, "delta": self.delta,
                "phi": self.phi, "reserve_frac": self.reserve_frac,
                "leg_bp": self.leg_bp, "face": self._face,
                "sig_halflife": self.sig_halflife,
                "book0": self._book0, "nw0": self._nw0,
                # WP-14: reported only when set, so a kappa-0 cell's meta
                # (the roster's knobs) stays byte-identical.
                **({"kappa": self.kappa} if self.kappa else {})}

    def telemetry(self, d) -> dict | None:
        if self.proxy is None:
            return None
        rec = self._telemetry_common(d)
        if self._sig is not None:
            rec["sig"] = round(self._sig, 6)
        rec.update({"issued": self.issued, "issued_open": self.issued_open,
                    "retired": self.retired, "absorbed": self.absorbed,
                    "absorbed_open": self.absorbed_open,
                    "unwound": self.unwound,
                    "rho": round(self.ladder.rho, 6) if self.ladder else 1.0,
                    "tranche": self.ladder.tranche if self.ladder else 0})
        try:
            rec["bk"] = self._book_par(d, self._bvib(d))
            rec["nw"] += rec["bk"]
        except Exception:
            pass
        return rec

    def arb_state(self, d) -> dict | None:
        """Per-frame inventory for the snapshot's arb2: BUCK held, drawn,
        the TOKEN book in par units, the ladder's rho and tranche."""
        if self.proxy is None:
            return None
        try:
            s = int(d.buck.functions.signedBalanceOf(self.proxy.address).call())
            book = self._book_par(d, self._bvib(d))
        except Exception:
            return None
        return {"cls": type(self).__name__, "idx": self.idx,
                "held": max(0, s), "drawn": max(0, -s), "book": book,
                "issued_open": self.issued_open,
                "absorbed_open": self.absorbed_open,
                "rho": round(self.ladder.rho, 6) if self.ladder else 1.0,
                "tranche": self.ladder.tranche if self.ladder else 0}

    def _nw(self, d) -> int:
        """Par-marked net worth: USDC + signed BUCK + the TOKEN book."""
        u = d.chain.balance_of(d.usdc, self.proxy.address)
        s = d.buck.functions.signedBalanceOf(self.proxy.address).call()
        try:
            bk = self._book_par(d, self._bvib(d))
        except Exception:
            bk = 0
        return u + s + bk

    # -- act -------------------------------------------------------------- #

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0 or self.proxy is None or not d.pool_buck:
            return
        try:
            bvib = self._bvib(d)
        except Exception:
            return
        if self._last_day is None:
            self._last_day = day
            self._sig = bvib
        dd = max(0, day - self._last_day)
        self._last_day = day
        if dd:
            alpha = 1.0 - 0.5 ** (dd / max(1e-9, self.sig_halflife))
            self._sig += alpha * (bvib - self._sig)
        try:
            self._maybe_build_book(d, scenario)
            self._book_cap(ctr)
            if self.ladder is not None:
                self._act(d, self._sig, ctr)
        except Exception as e:
            ctr["ut_err"] = repr(e)[:160]
        L = self.ladder
        ctr["ut_rho"] = round(L.rho, 6) if L else 1.0
        ctr["ut_tranche"] = L.tranche if L else 0

    def _signed(self, d) -> int:
        return int(d.buck.functions.signedBalanceOf(self.proxy.address).call())

    def _reconcile(self, ctr, signed: int) -> None:
        """Square the books with the chain's single signed balance.  By
        the netting rules at most one book is open, and it lives in the
        sign of that balance: a draw gone (retired by purchases, or the
        dust of rounding) closes the issued book; held inventory gone
        (sold, or melted by the demurrage lien) closes the absorbed book
        and returns the ladder to its struck state."""
        if self.issued_open > 0 and -signed < E6:
            res = self.issued_open
            self.retired += res
            self.issued_open = 0
            _book(ctr, "ut_retired", res)
            _book(ctr, "ut_issued_open", -res)
        if self.absorbed_open > 0 and signed < E6:
            res = self.absorbed_open
            self.unwound += res
            self.absorbed_open = 0
            _book(ctr, "ut_unwound", res)
            _book(ctr, "ut_absorbed_open", -res)
            self.ladder.reset()

    def _act(self, d, sig, ctr) -> None:
        L = self.ladder
        signed = self._signed(d)
        self._reconcile(ctr, signed)
        drawn = max(0, -signed)
        held = max(0, signed)
        # WP-14: the local skew on both edges (module doc).  kappa 0 hands
        # back eps itself on both sides and a zero skew, so every band
        # below is the same float it was before the skew existed.
        q_open = self.absorbed_open - self.issued_open
        eps_w, eps_s, skew, fill = skew_bands(self.eps, self.kappa, q_open,
                                              self._book0)
        L.eps = eps_w
        self._skew = skew
        if skew:
            ctr["sk_ut"] = round(skew, 8)
            ctr["sk_ut_f"] = round(fill, 6)
            self.note(d, "skew", fill=fill, skew=skew, eps_w=eps_w,
                      eps_s=eps_s, sig=sig, q=q_open, cap=self._book0)
        sold_today = bought_today = False
        # -- strong side: sell BUCK dear for the bundle; retire at par ---- #
        if sig < 1.0 / (1.0 + self.p * eps_s):
            sold, _bundles = self._sell_leg(d, None, 1.0 + self.p * eps_s,
                                            ctr)
            if sold > 0:
                sold_today = True
                self._trade(ctr)
                u = min(sold, self.absorbed_open)   # dispose absorbed first
                if u > 0:
                    self._unwind(ctr, u)
                iss = sold - u
                if iss > 0:
                    self.issued += iss
                    self.issued_open += iss
                    _book(ctr, "ut_issued", iss)
                    _book(ctr, "ut_issued_open", iss)
        elif (sig >= 1.0 + skew / 2.0 and self.issued_open > 0
              and drawn > 0):
            want = min(self.issued_open, drawn)
            if want >= E6:
                got, _bundles = self._buy_leg(d, want, 1.0 + eps_s / 2.0,
                                              None)
                if got > 0:
                    bought_today = True
                    self._trade(ctr)
                    self._retire(d, ctr, min(got, self.issued_open))
                    self._reconcile(ctr, self._signed(d))
        # -- weak side: the ladder absorbs cheap BUCK; unwinds at par ----- #
        if (not bought_today and not L.exhausted and sig > L.edge_bvib()):
            cap = int(L.capacity() * E6)
            if cap >= E6:
                got, bundles = self._buy_leg(d, None, L.bid(), cap)
                if got > 0:
                    self._trade(ctr)
                    r = min(got, self.issued_open)  # retire issued first
                    if r > 0:
                        self._retire(d, ctr, r)
                    a = got - r
                    if a > 0:
                        self.absorbed += a
                        self.absorbed_open += a
                        _book(ctr, "ut_absorbed", a)
                        _book(ctr, "ut_absorbed_open", a)
                        L.fill(bundles * a / got / E6)
        elif (not sold_today and self.absorbed_open > 0
              and sig <= 1.0 + eps_w / 2.0):
            want = min(self.absorbed_open, held)
            if want >= E6:
                sold, _bundles = self._sell_leg(d, want, 1.0 - eps_w / 2.0,
                                                ctr)
                if sold > 0:
                    self._trade(ctr)
                    self._unwind(ctr, min(sold, self.absorbed_open))
                    self._reconcile(ctr, self._signed(d))

    def _trade(self, ctr) -> None:
        self.trades += 1
        _book(ctr, "ut_trades", 1)
        if self._skew:
            _book(ctr, "sk_ut_n", 1)          # WP-14: a skewed act

    def _retire(self, d, ctr, amt: int) -> None:
        """Book `amt` of issued BUCK retired (the buy already climbed the
        signed balance toward 0); best-effort burn deactivates coverage."""
        if amt <= 0:
            return
        self.retired += amt
        self.issued_open -= amt
        _book(ctr, "ut_retired", amt)
        _book(ctr, "ut_issued_open", -amt)
        try:
            self._proxy_exec(d, d.buck.address, d.buck.encode_abi(
                "burn(uint256)", args=[int(amt)]))
        except Exception:
            pass

    def _unwind(self, ctr, amt: int) -> None:
        """Book `amt` of absorbed BUCK sold back; the ladder is reset once
        the open position is gone (the reserve is back in the book) --
        here on the books, and in _reconcile on the chain's balance."""
        if amt <= 0:
            return
        self.unwound += amt
        self.absorbed_open -= amt
        _book(ctr, "ut_unwound", amt)
        _book(ctr, "ut_absorbed_open", -amt)
        if self.absorbed_open < E6:
            _book(ctr, "ut_absorbed_open", -self.absorbed_open)
            self.absorbed_open = 0
            self.ladder.reset()

    # -- bundle legs: one pro-rata bite across the TOKEN/BUCK pools -------- #

    def _states(self, d) -> list[tuple[int, int]]:
        return [(d.chain.balance_of(d.tokens[i], d.pool_buck[i]),
                 d.chain.balance_of(d.buck, d.pool_buck[i]))
                for i in range(len(d.tokens))]

    def _sell_leg(self, d, want: int | None, min_bpb: float, ctr
                  ) -> tuple[int, int]:
        """Sell BUCK for the bundle: buy f par baskets of TOKEN with BUCK
        across the pools in basketAmount proportions.  f is sized by the
        per-pool leg cap (leg_bp of the pool's BUCK reserve) and by `want`
        BUCK; the ex-ante quote requires >= `min_bpb` bundles per BUCK at
        that size.  Mints against the face when the spendable balance is
        short (issuance).  Returns (buck_sold, bundles_received 6-dec)."""
        tpb = self._tok_per_basket(d)
        fee = (getattr(d, "fee_buck", 0) or 0) / 1e6
        fee_pip = int(fee * FEE_DEN)
        st = self._states(d)
        N = len(d.tokens)
        f = None
        for i in range(N):
            rt, rb = st[i]
            if tpb[i] <= 0 or rt <= 0 or rb <= 0:
                continue
            cost_i = max(1, tpb[i] * rb // rt)      # raw BUCK per par basket
            cap_i = rb * self.leg_bp // 10_000       # C_leg: BUCK in, per act
            fi = cap_i * E6 // cost_i
            f = fi if f is None else min(f, fi)
        if f is None or f < E6:
            return 0, 0
        needs = [self._amount_in_for_out(st[i][1], st[i][0],
                                         f * tpb[i] // E6, fee_pip)
                 if tpb[i] > 0 else 0 for i in range(N)]
        spend = sum(needs)
        if spend <= 0:
            return 0, 0
        if want is not None and spend > want:
            scale = want / spend
            f = int(f * scale)
            needs = [int(n * scale) for n in needs]
            spend = sum(needs)
        if f < E6 or spend < E6:
            return 0, 0
        if f / spend < min_bpb:
            return 0, 0                     # no ex-ante edge at this size
        spendable = max(0, d.buck.functions.balanceOf(
            self.proxy.address).call())
        if spendable < spend:
            try:
                k = int(d.kctrl.functions.buckK().call())
            except Exception:
                k = 0
            if k > 0:
                m = ((spend - spendable) * E18 // k) * 105 // 100
                try:
                    self._proxy_exec(d, d.buck.address, d.buck.encode_abi(
                        "mint(uint256)", args=[int(m)]))
                except Exception as ex:
                    ctr["ut_mint_err"] = repr(ex)[:120]
        before = d.chain.balance_of(d.buck, self.proxy.address)
        for i in range(N):
            if needs[i] > 0:
                self._swap_via_simlp(d, d.pool_buck[i], d.buck, needs[i],
                                     self.proxy.address)
        sold = max(0, before - d.chain.balance_of(d.buck, self.proxy.address))
        bundles = f * sold // spend if spend else 0
        return sold, bundles

    def _quote_out(self, st, tpb, f: int, fee: float) -> int:
        return sum(_cp_out(st[i][0], st[i][1], f * tpb[i] // E6, fee)
                   for i in range(len(tpb)) if tpb[i] > 0)

    def _buy_leg(self, d, want: int | None, max_bpb: float,
                 f_cap: int | None) -> tuple[int, int]:
        """Buy BUCK with the bundle: sell f par baskets of TOKEN from the
        book into the pools in basketAmount proportions.  f is sized by
        the per-pool leg cap (leg_bp of the pool's TOKEN reserve), the
        holdings, `f_cap` (the tranche's remaining size, 6-dec) and `want`
        BUCK; the ex-ante quote requires <= `max_bpb` bundles per BUCK at
        that size.  Returns (buck_got, bundles_spent 6-dec)."""
        tpb = self._tok_per_basket(d)
        fee = (getattr(d, "fee_buck", 0) or 0) / 1e6
        st = self._states(d)
        N = len(d.tokens)
        f = None
        for i in range(N):
            rt, rb = st[i]
            if tpb[i] <= 0 or rt <= 0 or rb <= 0:
                continue
            held_t = d.chain.balance_of(d.tokens[i], self.proxy.address)
            cap_i = min(held_t, rt * self.leg_bp // 10_000)
            fi = cap_i * E6 // tpb[i]
            f = fi if f is None else min(f, fi)
        if f is None or f < E6:
            return 0, 0
        if f_cap is not None:
            f = min(f, f_cap)
        out = self._quote_out(st, tpb, f, fee)
        if want is not None and 0 < want < out:
            f = f * want // out
            out = self._quote_out(st, tpb, f, fee)
        if f < E6 or out <= 0:
            return 0, 0
        if f / out > max_bpb:
            return 0, 0                     # no ex-ante edge at this size
        before = d.chain.balance_of(d.buck, self.proxy.address)
        for i in range(N):
            amt = f * tpb[i] // E6
            if amt > 0:
                self._swap_via_simlp(d, d.pool_buck[i], d.tokens[i], amt,
                                     self.proxy.address)
        got = max(0, d.chain.balance_of(d.buck, self.proxy.address) - before)
        return got, f
