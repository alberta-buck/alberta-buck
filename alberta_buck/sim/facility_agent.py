"""The latent-credit auto-pay-down facility -- CARRY-CONVEXITY.org D6 (the
four rules) and 7.6 ("The facility"); WAVE3.org WP-6, requirement R8.

Poised capital is NOT BUCK: it is an insured asset behind a zero-premium
BuckCredit face with an unused limit.  The facility is the user-
parameterized machinery attached to that position:

  ISSUE   when the signal (a 1-day EWMA of basketValueInBuck) reads a
          PREMIUM, signal < 1 - x, and the line is under its deployment
          cap, drawn < max_frac * limit: activate coverage (Buck.mint
          through the proxy -- the mint activates, the spend draws) and
          PLACE the proceeds.  target "basket": deposit the BUCK into the
          basket under the single receipt class (basket.depositToken(buck,
          ...) -- the basket swaps half into the most-underweight TOKEN and
          LPs both sides, which pushes bvib UP, the corrective direction);
          target "usdc": sell the BUCK for USDC in BUCK/USDC.
  RETIRE  when the signal reads a DISCOUNT, signal > 1 + y, and something
          is owed or placed: redeem the open receipts (TOKEN payout, worth
          MORE BUCK than the principal at a discount -- that is the
          harvest), sell only as much TOKEN as repays the drawn line in the
          TOKEN/BUCK pools (impact-capped, ex-ante quoted against par),
          and best-effort burn the released coverage.  Retirement happens
          on the BUY (the BUCK delivered to the proxy lifts its signed raw
          balance toward zero); the burn closes the line.  Surplus TOKEN
          stays as inventory (basket exposure pays no demurrage; D6 rule 1).
          target "usdc": buy BUCK with the USDC instead.
  HOLD    inside the deadband, or when there is nothing to do.

The decision is a PURE function (`facility_decision`) so the thresholds,
caps and both directions are unit-testable without a chain; the agent
supplies the chain reads (drawn = -signedRawBalanceOf, limit = the face's
depreciated value x K, the market headroom) and executes the legs.

Signs and units (WAVE3.org "Signs and units"): bvib < 1 is BUCK DEAR
(premium; the facility issues, net issued BUCK, shadow inventory
negative); bvib > 1 is BUCK CHEAP (discount; the facility retires, net
absorbed).  BUCK/USDC/TOKEN-par amounts are 6-dec ints; bvib and the
signal are floats; K is 18-dec.

Counters (ctr; copied into every frame by snapshot.py, WP-6 block):
fac_issued / fac_retired (cumulative BUCK drawn into placements / repaid
onto the line), fac_deposits / fac_redeems (basket legs), fac_drawn /
fac_limit / fac_u (the population's live book, read from chain each act:
u = sum d_i / sum L_i, 7.6), fac_err (the last swallowed exception --
an unplumbed error is a silent dead agent).  fac_pnl is the par-marked
net worth (USDC + signed BUCK + TOKEN at reference + open receipts at
principal) summed by the snapshot the way exc_pnl is.

Determinism: one keyed RNG stream per (seed, "FacilityAgent", idx); the
draw order below is pinned (x, y, max_frac, face_m, sig_halflife,
max_impact_bp) -- add new draws AFTER these.  Scalar specs consume no
variate.  DirectMint's dm* counters are NOT touched: the facility keeps
its own books.
"""

from __future__ import annotations

from alberta_buck.sim.agents import _register
from alberta_buck.sim.equilibrium_agents import (
    FEE_DEN, _ProxyAgent, _agent_rng, _cp_out, _impact_cap,
)
from alberta_buck.sim.experiment import draw as _draw, spec as _spec

E6 = 10 ** 6
E18 = 10 ** 18

ISSUE = "issue"
RETIRE = "retire"
HOLD = "hold"


def facility_decision(signal, x, y, drawn, limit, max_frac, open_position,
                      headroom=None):
    """The facility's rule, pure.  Returns (action, size) with size in the
    caller's BUCK units.

    signal        the filtered bvib (1.0 = par; < 1 BUCK dear; > 1 cheap)
    x, y          the premium / discount thresholds (fractions of par)
    drawn         BUCK currently drawn on the line (>= 0)
    limit         the line's limit at the current K
    max_frac      the maximum fraction of the limit to deploy
    open_position True when a placement is still open (a basket receipt
                  to redeem) -- lets a retire fire even at drawn == 0
    headroom      optional market cap on one issue (None = uncapped)

      "issue"   signal < 1 - x and drawn < max_frac * limit;
                size = min(headroom, max_frac * limit - drawn)
      "retire"  signal > 1 + y and (drawn > 0 or open_position);
                size = drawn (the BUCK to repay; 0 = redeem/close only)
      "hold"    otherwise (the deadband 1 - x <= signal <= 1 + y, a line
                at its cap, or nothing owed / placed)
    """
    drawn = max(0, drawn)
    cap = max_frac * limit
    if signal < 1.0 - x:
        room = cap - drawn
        if room <= 0:
            return HOLD, 0
        size = room if headroom is None else min(room, headroom)
        if size <= 0:
            return HOLD, 0
        return ISSUE, int(size)
    if signal > 1.0 + y:
        if drawn > 0 or open_position:
            return RETIRE, int(drawn)
        return HOLD, 0
    return HOLD, 0


@_register
class FacilityAgent(_ProxyAgent):
    """The auto-pay-down facility as a sim agent (the executable requirement
    the eventual contract is held to; WAVE3.org "The stand-ins").  See the
    module docstring for the rule, the legs and the books.

    Knobs ([agents.FacilityAgent], all --set-able): x (0.03), y (0.03),
    max_frac (0.5), face_m ([1, 5] $M, the zero-premium BuckCredit face),
    sig_halflife (1.0 d), max_impact_bp ([50, 150]), target ("basket" |
    "usdc"), min_edge (0.0; the ex-ante par gate on the market legs).
    """

    CTR = "fac"
    MAX_DEPOSIT_DEV_BP = 100        # depositToken maxDeviationBp (as the DM)
    TELEMETRY_STRIDE = 1

    def __init__(self, idx: int):
        super().__init__(idx)
        self.x = self.y = self.max_frac = 0.0
        self.sig_halflife = 1.0
        self.max_impact_bp = 100
        self.min_edge = 0.0
        self.target = "basket"
        self._face = 0
        self._tid = None
        self._receipts: list[int] = []
        self._sig = None
        self._last_day = None
        self._refs: list[int] = []
        self._nw0 = 0
        self._issued = 0
        self._retired = 0
        self._deposits = 0
        self._redeems = 0
        self._burned = 0
        self._last_action = HOLD

    # -- setup ------------------------------------------------------------ #

    def setup(self, d, scenario, rng) -> None:
        self._rng = _agent_rng(scenario.seed, type(self).__name__, self.idx)
        r = self._rng
        cls = type(self).__name__
        # Draw order is PINNED (keyed-rng vectors); new draws go after
        # max_impact_bp.  Scalar specs consume no stream variate.
        self.x = float(_draw(scenario, cls, "x", r, 0.03))
        self.y = float(_draw(scenario, cls, "y", r, 0.03))
        self.max_frac = min(1.0, max(0.0, float(
            _draw(scenario, cls, "max_frac", r, 0.5))))
        face_m = _draw(scenario, cls, "face_m", r, (1, 5))
        self.sig_halflife = float(_draw(scenario, cls, "sig_halflife", r, 1.0))
        self.max_impact_bp = int(_draw(scenario, cls, "max_impact_bp", r,
                                       (50, 150)))
        self.target = str(_spec(scenario, cls, "target", "basket"))
        if self.target not in ("basket", "usdc"):
            raise ValueError(f"FacilityAgent target {self.target!r}: "
                             f"expected 'basket' or 'usdc'")
        self.min_edge = float(_spec(scenario, cls, "min_edge", 0.0))
        self._bind_proxy(d)
        # The latent-credit position: a zero-premium BuckCredit face the
        # proxy has never drawn (exactly ExcursionCreditArbAgent's), issued
        # by the deployer after the proxy opts it in as an insurer.
        self._face = int(float(face_m) * 1_000_000 * E6)
        now_ts = d.w3.eth.get_block("latest")["timestamp"]
        self._proxy_exec(d, d.credit.address, d.credit.encode_abi(
            "setCreditIssuer",
            args=[getattr(d.chain.deployer, "address", d.chain.deployer),
                  True]))
        d.chain.send(d.credit.functions.createCredit(
            self.proxy.address, 0, self._face, 0, 0, 0, now_ts, 0))
        self._tid = int(d.credit.functions.tokenOfOwnerByIndex(
            self.proxy.address, 0).call())
        self._nw0 = 0

    # -- chain reads ------------------------------------------------------ #

    def _k(self, d) -> int:
        try:
            return int(d.kctrl.functions.buckK().call())
        except Exception:
            return 0

    def _bvib(self, d) -> float:
        return int(d.basket.functions.basketValueInBuck().call()) / 1e18

    def _raw(self, d) -> int:
        """Signed RAW balance: negative == credit drawn (no demurrage on
        the drawn side, so raw is the exact obligation)."""
        return int(d.buck.functions.signedRawBalanceOf(
            self.proxy.address).call())

    def _drawn(self, d) -> int:
        return max(0, -self._raw(d))

    def _face_now(self, d) -> int:
        if self._tid is None:
            return 0
        try:
            return int(d.credit.functions.depreciatedFaceValue(
                self._tid).call())
        except Exception:
            return self._face

    def _activated(self, d) -> int:
        if self._tid is None:
            return 0
        try:
            return int(d.buck.functions.mintsBacked(self._tid).call())
        except Exception:
            return 0

    def _limit(self, d, k: int | None = None) -> int:
        """The line's limit at the current K: the face's depreciated value
        x K (what the participant COULD draw -- L_i of 7.6).  Activation is
        a mechanical sub-step (Buck.creditLimit counts activated coverage
        only), so the potential is read from the credit itself."""
        k = self._k(d) if k is None else k
        return self._face_now(d) * k // E18

    def _spendable(self, d) -> int:
        return max(0, int(d.buck.functions.balanceOf(
            self.proxy.address).call()))

    def _receipt_principal(self, d) -> int:
        p = 0
        for rid in self._receipts:
            try:
                p += int(d.basket.functions.deposits(rid).call()[0])
            except Exception:
                pass
        return p

    def _tok_par(self, d) -> int:
        """TOKEN inventory at the day's reference USD prices (== par BUCK)."""
        refs = self._refs
        v = 0
        for i, tc in enumerate(d.tokens):
            if i >= len(refs) or not refs[i]:
                continue
            bal = d.chain.balance_of(tc, self.proxy.address)
            if bal > 0:
                v += bal * refs[i] // (10 ** d.dec[i])
        return v

    def _nw(self, d) -> int:
        """Par-marked net worth (1 BUCK == 1 USDC == 1 par basket): USDC +
        signed BUCK (held net of demurrage, minus drawn) + TOKEN at
        reference + open receipts at their BUCK principal."""
        u = d.chain.balance_of(d.usdc, self.proxy.address)
        s = int(d.buck.functions.signedBalanceOf(self.proxy.address).call())
        return u + s + self._tok_par(d) + self._receipt_principal(d)

    # -- telemetry -------------------------------------------------------- #

    def telemetry_static(self) -> dict:
        return {"x": self.x, "y": self.y, "max_frac": self.max_frac,
                "face": self._face, "sig_halflife": self.sig_halflife,
                "max_impact_bp": self.max_impact_bp, "target": self.target,
                "min_edge": self.min_edge, "nw0": self._nw0}

    def telemetry(self, d) -> dict | None:
        if self.proxy is None:
            return None
        rec = self._telemetry_common(d)
        if self._sig is not None:
            rec["sig"] = round(self._sig, 6)
        rec["drawn"] = self._drawn(d)
        rec["limit"] = self._limit(d)
        rec["util"] = (rec["drawn"] / rec["limit"]) if rec["limit"] else 0.0
        rec["rc"] = len(self._receipts)
        rec["rp"] = self._receipt_principal(d)
        rec["tk"] = self._tok_par(d)
        rec["nw"] += rec["rp"] + rec["tk"]
        rec["act"] = self._last_action
        return rec

    def arb_state(self, d) -> dict | None:
        if self.proxy is None:
            return None
        signed = int(d.buck.functions.signedBalanceOf(
            self.proxy.address).call())
        return {"cls": self.CTR, "idx": self.idx,
                "cash": d.chain.balance_of(d.usdc, self.proxy.address),
                "held": max(0, signed), "drawn": self._drawn(d),
                "limit": self._limit(d), "tok": self._tok_par(d),
                "endow": self._face, "parked": self._receipt_principal(d),
                "receipts": len(self._receipts),
                "issued": self._issued, "retired": self._retired}

    # -- the act ---------------------------------------------------------- #

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0 or self.proxy is None:
            return
        self._refs = list(ctr.get("refUsd", []))
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
        self._last_action = HOLD
        try:
            k = self._k(d)
            drawn = self._drawn(d)
            limit = self._limit(d, k)
            headroom = self._headroom(d)
            action, size = facility_decision(
                self._sig, self.x, self.y, drawn, limit, self.max_frac,
                bool(self._receipts), headroom)
            if action == ISSUE:
                self._issue(d, size, k, bvib, ctr)
            elif action == RETIRE:
                self._retire(d, size, k, ctr)
            self._last_action = action
        except Exception as e:
            ctr["fac_err"] = repr(e)[:200]
        finally:
            self._book(d, ctr)

    def _book(self, d, ctr) -> None:
        """The population's live book: each agent writes its own (drawn,
        limit) read from chain; the sums are what the frame carries."""
        try:
            entry = (self._drawn(d), self._limit(d))
        except Exception:
            return
        book = ctr.setdefault("facBook", {})
        book[self.idx] = entry
        dr = sum(v[0] for v in book.values())
        li = sum(v[1] for v in book.values())
        ctr["fac_drawn"] = dr
        ctr["fac_limit"] = li
        ctr["fac_u"] = (dr / li) if li else 0.0

    def _headroom(self, d) -> int:
        """Market cap on one issue: the basket target swaps ~half the deposit
        into one (unknown a priori) TOKEN/BUCK pool, so 2 x the tightest
        pool's BUCK impact cap; the usdc target sells into BUCK/USDC."""
        if self.target == "usdc":
            if not d.pool_ub:
                return 0
            return _impact_cap(d.chain.balance_of(d.buck, d.pool_ub),
                               self.max_impact_bp)
        cap = None
        for i in range(len(d.tokens)):
            rb = d.chain.balance_of(d.buck, d.pool_buck[i])
            c = _impact_cap(rb, self.max_impact_bp)
            cap = c if cap is None else min(cap, c)
        return 2 * (cap or 0)

    # -- issue -------------------------------------------------------------- #

    def _ensure_headroom(self, d, size: int, k: int, ctr) -> int:
        """Activate enough coverage that `size` is spendable (the mint
        activates; balanceOf then reports the K-scaled headroom).  Capped by
        the face's unactivated remainder, read from chain."""
        sp = self._spendable(d)
        if sp >= size or k <= 0:
            return min(sp, size)
        m = ((size - sp) * E18 // k) * 105 // 100
        m = min(m, max(0, self._face_now(d) - self._activated(d)))
        if m >= E6:
            self._proxy_exec(d, d.buck.address, d.buck.encode_abi(
                "mint(uint256)", args=[int(m)]))
        return min(self._spendable(d), size)

    def _issue(self, d, size: int, k: int, bvib: float, ctr) -> None:
        if size < E6:
            return
        if self.target == "usdc":
            self._issue_usdc(d, size, k, ctr)
            return
        # Live sanity gate: the deposit buys TOKEN at the pools' BUCK
        # prices; only worth doing while BUCK is actually dear.
        if bvib > 1.0 - self.min_edge:
            return
        amt = self._ensure_headroom(d, size, k, ctr)
        if amt < E6:
            return
        self._proxy_exec(d, d.buck.address, d.buck.encode_abi(
            "approve(address,uint256)", args=[d.basket.address, int(amt)]))
        raw0 = self._raw(d)
        rcpt = self._proxy_exec(d, d.basket.address, d.basket.encode_abi(
            "depositToken(address,uint256,uint256)",
            args=[d.buck.address, int(amt), self.MAX_DEPOSIT_DEV_BP]))
        rid = None
        for log in rcpt["logs"]:
            if log["topics"][0] == d.deposited_topic:
                rid = int.from_bytes(bytes(log["topics"][2]), "big")
                break
        if rid is None:
            raise RuntimeError("depositToken emitted no Deposited event")
        self._receipts.append(rid)
        # The BUCK that actually left the proxy is the draw (the basket
        # refunds what investFromBucks did not consume).
        issued = max(0, raw0 - self._raw(d))
        self._issued += issued
        self._deposits += 1
        ctr["fac_issued"] = ctr.get("fac_issued", 0) + issued
        ctr["fac_deposits"] = ctr.get("fac_deposits", 0) + 1

    def _issue_usdc(self, d, size: int, k: int, ctr) -> None:
        if not d.pool_ub:
            return
        ru = d.chain.balance_of(d.usdc, d.pool_ub)
        rb = d.chain.balance_of(d.buck, d.pool_ub)
        fee = (getattr(d, "fee_ub", 0) or 0) / 1e6
        y = min(size, _impact_cap(rb, self.max_impact_bp))
        if y < E6:
            return
        out = _cp_out(rb, ru, y, fee)
        if out <= 0 or out / y < 1.0 + self.min_edge:
            return                  # the pool does not pay par + edge
        amt = self._ensure_headroom(d, y, k, ctr)
        if amt < E6:
            return
        raw0 = self._raw(d)
        sold = self._sell_capped(d, d.pool_ub, amt)
        if sold <= 0:
            return
        issued = max(0, raw0 - self._raw(d))
        self._issued += issued
        ctr["fac_issued"] = ctr.get("fac_issued", 0) + issued

    # -- retire ------------------------------------------------------------- #

    def _retire(self, d, size: int, k: int, ctr) -> None:
        drawn0 = self._drawn(d)
        if self.target == "basket":
            self._redeem_all(d, ctr)
            self._sell_tokens_for_buck(d, ctr)
        else:
            self._buy_buck_usdc(d, ctr)
        drawn1 = self._drawn(d)
        retired = max(0, drawn0 - drawn1)
        if retired > 0:
            self._retired += retired
            ctr["fac_retired"] = ctr.get("fac_retired", 0) + retired
        self._release_coverage(d, k, ctr)

    def _redeem_all(self, d, ctr) -> None:
        """Full redeem of every open receipt (TOKEN payout to the proxy).
        redeemBp 0 == 100%; loss budget 0 == unlimited (the DM's call), a
        no-op at a discount where the sell-high draw covers the burn."""
        still = []
        for rid in self._receipts:
            try:
                self._proxy_exec(d, d.basket.address, d.basket.encode_abi(
                    "redeem(uint256,uint256,uint256)", args=[int(rid), 0, 0]))
                self._redeems += 1
                ctr["fac_redeems"] = ctr.get("fac_redeems", 0) + 1
            except Exception as e:
                ctr["fac_err"] = f"redeem {rid}: {e!r}"[:200]
                still.append(rid)
        self._receipts = still

    def _sell_tokens_for_buck(self, d, ctr) -> None:
        """Sell TOKEN inventory into the TOKEN/BUCK pools for BUCK delivered
        to the proxy (raw climbs toward zero == the line is repaid), only as
        much as the drawn line needs, one impact-capped bite per pool, each
        gated by an ex-ante constant-product quote against par (the TOKEN
        must fetch at least its reference value in BUCK x (1 + min_edge))."""
        fee = (getattr(d, "fee_buck", 0) or 0) / 1e6
        fee_pip = int(fee * FEE_DEN)
        refs = self._refs
        for i, tc in enumerate(d.tokens):
            want = self._drawn(d)
            if want < E6:
                break
            held = d.chain.balance_of(tc, self.proxy.address)
            if held <= 0:
                continue
            rt = d.chain.balance_of(tc, d.pool_buck[i])
            rb = d.chain.balance_of(d.buck, d.pool_buck[i])
            if rt <= 0 or rb <= 0:
                continue
            amt = min(held, _impact_cap(rt, self.max_impact_bp),
                      self._amount_in_for_out(rt, rb, want, fee_pip) or held)
            if amt <= 0:
                continue
            out = _cp_out(rt, rb, amt, fee)
            ref = refs[i] if i < len(refs) else 0
            par = amt * ref // (10 ** d.dec[i]) if ref else 0
            if out <= 0 or par <= 0 or out < par * (1.0 + self.min_edge):
                continue            # this pool does not pay a discount
            try:
                self._swap_via_simlp(d, d.pool_buck[i], tc, amt,
                                     self.proxy.address)
            except Exception as e:
                ctr["fac_err"] = f"sell tok{i}: {e!r}"[:200]

    def _buy_buck_usdc(self, d, ctr) -> None:
        if not d.pool_ub:
            return
        drawn = self._drawn(d)
        cash = d.chain.balance_of(d.usdc, self.proxy.address)
        if drawn < E6 or cash < E6:
            return
        ru = d.chain.balance_of(d.usdc, d.pool_ub)
        rb = d.chain.balance_of(d.buck, d.pool_ub)
        fee = (getattr(d, "fee_ub", 0) or 0) / 1e6
        x = min(cash, _impact_cap(ru, self.max_impact_bp))
        if x < E6:
            return
        out = _cp_out(ru, rb, x, fee)
        if out <= 0 or x / out > 1.0 - self.min_edge:
            return                  # no discount at this size
        self._buy_buck(d, d.pool_ub, d.usdc, min(out, drawn), d.fee_ub)

    def _release_coverage(self, d, k: int, ctr) -> None:
        """Best-effort burn: release as much activated coverage as the
        remaining draw allows (Buck requires used <= creditLimit after the
        burn), so a retired line cannot simply be redrawn for free.  The
        live creditLimit is what the contract checks, and it is the
        activated coverage's CURRENT (depreciated) value x K -- so the
        coverage the draw pins is act * used / creditLimit, not used / K
        (the latter under-counted it and the burn reverted)."""
        if k <= 0:
            return
        act = self._activated(d)
        used = self._drawn(d)
        try:
            live = int(d.buck.functions.creditLimit(self.proxy.address).call())
        except Exception:
            live = 0
        if used > 0 and live <= 0:
            return                              # nothing releasable
        # 2% on the PINNED coverage, not on the remainder: the burn itself
        # runs buckK.compute() before the check, and in a discount K steps
        # down, so the limit inside the tx is a step below the one read here.
        keep = (act * used * 102 + live * 100 - 1) // (live * 100) if used else 0
        b = int(act - keep)
        if b < E6:
            return
        for amt in (b, b // 2):
            if amt < E6:
                break
            try:
                self._proxy_exec(d, d.buck.address, d.buck.encode_abi(
                    "burn(uint256)", args=[int(amt)]))
                self._burned += amt
                ctr["fac_burned"] = ctr.get("fac_burned", 0) + amt
                return
            except Exception as e:
                err = f"burn {amt}: {e!r}"[:200]
        ctr["fac_err"] = err
