"""Wave 4's organic households (doc/ORGANIC-SCALE.org, track T15).

The first role, in the order the stand needs it: the EXTERNAL-DEBT
RETIREE.  It is the BuckCreditDebtorAgent's household -- the same insured
home, USD mortgage, income, ledgers and legs, audited to the ledger --
with the one thing the thin cast faked replaced by a decision.

The endog debtor arrives by a formula: a clock advancing at
(K / 0.75) x (1 + 5 (1 - bvib)), capped at 3x.  That line IS the sim's
supply flow per unit K (WP-16's g), an assumed elasticity.  The retiree
has no arrival: it is a household from day 0, paying its mortgage and its
insurance premium in USD, and each month it asks alberta_buck.sim.carry
whether moving (some of) that debt into BUCK credit pays -- the external
rate saved, the premium it stops paying, the Jubilee relief, against the
insurance deposit's opportunity and carried age, the execution discount,
the switching cost and, off its renewal date, the prepayment penalty.

Two changes from the debtor follow from that:

  * PARTIAL refinancing.  The atomic debtor converts the whole mortgage or
    nothing, so K acts on it as a threshold (LTV <= K).  The retiree
    retires what K lets it draw and keeps the rest external, and draws more
    when K rises: the supply answers K as a flow.
  * Insurance as an OUTLAY (master fe3083f).  Before it joins, the real
    path pays the external premium as a cost, like the counterfactual.
    Joining stops it; each mint's deposit (ten years' premium on the
    coverage activated) is an asset the ledger counts at par (`deposit`),
    and the kernel charges it only its opportunity and carried age.
  * Buck.mint's arithmetic.  mint(amount) settles a NET amount: coverage C
    raises the limit K C and debits the deposit e C, so a BUCK drawn beyond
    the activated headroom needs 1 / (K - e) of coverage.  (The debtor sizes
    the mint in face units and meets "insufficient credit allocation" near
    its last unactivated face; the retiree asks the net amount.)

The ledger identity extends the debtor's by the costs only the real path
pays in cash:

    nw - hypo = interest_saved + jub - trade_loss + hypo_premium
                - ext_premium - (premium_paid - deposit)
                - switch_paid - penalty_paid
"""
from __future__ import annotations

from alberta_buck.sim import carry
from alberta_buck.sim.agents import _register
from alberta_buck.sim.equilibrium_agents import (
    BuckCreditDebtorAgent, _growth_active,
)
from alberta_buck.sim.experiment import draw as _draw
from alberta_buck.sim.gauge import active_reserves

M6 = 10 ** 6


@_register
class ExternalDebtRetireeAgent(BuckCreditDebtorAgent):
    """A household with a USD mortgage on an insured home that refinances
    into BUCK credit when, and as far as, the carry comparison says it pays.

    Knobs beyond the debtor's ([agents.ExternalDebtRetireeAgent]):
      term_months     the mortgage's renewal term (60: a five-year term)
      renew_frac      where in its term the mortgage starts ([0, 1): a
                      seasoned ladder of renewal dates)
      penalty_months  prepayment penalty off renewal, months of interest (3)
      switch_k        one-time switching cost, $k (legal, appraisal) ([1, 3])
      risk            /yr charged for owing basket-indexed BUCK ([0, 0.01])
    `theta` keeps its debtor meaning, the payback years the carry has to
    repay one-time costs; `apr` is the external rate."""

    _arrival_seq = 0

    def setup(self, d, scenario, rng) -> None:
        super().setup(d, scenario, rng)
        r = self._rng
        cls = type(self).__name__
        # A household from day 0: no arrival clock, no stagger.
        self._endog = False
        self.arrive_day = 0
        self._last_day = 0
        self._last_month_day = -self.MONTH
        self.term_months = int(_draw(scenario, cls, "term_months", r, 60))
        self.renew_in = int(_draw(scenario, cls, "renew_frac", r, (0.0, 1.0))
                            * self.term_months)
        self.penalty_months = float(_draw(scenario, cls, "penalty_months",
                                          r, 3.0))
        self.switch_cost = int(_draw(scenario, cls, "switch_k", r, (1.0, 3.0))
                               * 1_000 * M6)
        self.risk = float(_draw(scenario, cls, "risk", r, (0.0, 0.01)))
        self.joined = False
        self.month_no = 0
        self.refis = 0
        # Cash the real path pays that the debtor's never did.
        self.ext_premium = 0        # the external premium, until it joins
        self.switch_paid = 0
        self.penalty_paid = 0
        self._last_verdict: dict | None = None

    # -- observability ------------------------------------------------------- #

    def telemetry_static(self) -> dict:
        out = super().telemetry_static()
        out.update(term_months=self.term_months, renew_in=self.renew_in,
                   penalty_months=self.penalty_months,
                   switch=self.switch_cost, risk=round(self.risk, 4))
        return out

    def telemetry(self, d) -> dict | None:
        out = super().telemetry(d)
        if out is not None:
            out.update(joined=1 if self.joined else 0, refis=self.refis,
                       ext_premium=self.ext_premium)
        return out

    def octl_state(self, d) -> dict | None:
        st = super().octl_state(d)
        if st is not None:
            st.update(joined=self.joined, refis=self.refis,
                      ext_premium=self.ext_premium,
                      switch_paid=self.switch_paid,
                      penalty_paid=self.penalty_paid)
        return st

    # -- the loop -------------------------------------------------------------- #

    def _at_renewal(self) -> bool:
        return (self.month_no - self.renew_in) % self.term_months == 0

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0 or self.proxy is None:
            return
        if not _growth_active(self, day, ctr):
            return
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
        self.month_no += months

        try:
            ff = d.kctrl.functions.fundingFactor().call()
            k = d.kctrl.functions.buckK().call()
            signed = d.buck.functions.signedBalanceOf(
                self.proxy.address).call()
            limit = d.buck.functions.creditLimit(self.proxy.address).call()
        except Exception as e:
            ctr["rtr_err"] = repr(e)[:200]
            return
        drawn = max(0, -signed)

        # 1. Income, the insurance premium and the mortgage payment, both
        #    ledgers.  The counterfactual pays its premium as a cost; so does
        #    the real path until it joins, after which BuckCredit insures.
        inc = self._income(d, months, day)
        self.hypo_cash += inc
        prem = self._face0 * self.premium_rate // 10_000 * months // 12
        hprem = min(prem, self.hypo_cash)
        self.hypo_cash -= hprem
        self.hypo_premium += hprem
        if not self.joined and prem > 0:
            self.ext_premium += self._pay_bank(d, prem)
        due = min(self.payment * months, self.mortgage)
        self.mortgage -= self._pay_bank(d, due)
        hdue = min(self.payment * months, self.hypo_mortgage)
        hpaid = min(hdue, self.hypo_cash)
        self.hypo_cash -= hpaid
        self.hypo_mortgage -= hpaid

        jub = 0
        unactivated = 0
        try:
            for tid in self._token_ids:
                jub += d.credit.functions.jubileeRelief(tid).call()
                face_v, act_v, _ = d.credit.functions.creditInfo(tid).call()
                unactivated += max(0, face_v - act_v)
        except Exception:
            pass
        jub = min(jub, drawn)

        # 2. DECIDE, and refinance what pays.
        if self.mortgage > M6 and d.pool_ub:
            self._decide(d, ff, k, limit, drawn, unactivated, ctr)

        # 3. Once the external debt is gone: the debtor's amortization of the
        #    claim, its investment of the surplus, and the voluntary unwind.
        self._carry_tail(d, drawn, limit, jub, ctr)

    def _decide(self, d, ff, k, limit, drawn, unactivated, ctr) -> None:
        spendable = max(0, limit - drawn)
        ru, rb = active_reserves(d.chain, d.pool_ub, d.usdc, d.buck)
        fee = (getattr(d, "fee_ub", 0) or 0) / 1e6
        at_renewal = self._at_renewal()
        terms = carry.RefiTerms(
            debt=self.mortgage / M6, rate=self.apr, face=self._face0 / M6,
            premium_bp=self.premium_rate, joined=self.joined,
            payback_y=self.theta, k=k / 1e18, headroom=spendable / M6,
            unactivated=unactivated / M6,
            fixed_cost=0.0 if self.joined else self.switch_cost / M6,
            penalty_months=0.0 if at_renewal else self.penalty_months,
            risk=self.risk)
        v = carry.best_refinance(terms, ru / M6, rb / M6, fee)
        if not v.go:
            ctr["rtrWaits"] = ctr.get("rtrWaits", 0) + 1
            return
        buck = int(v.buck * M6) + M6
        fixed = 0 if self.joined else self.switch_cost
        penalty = int(v.parts["penalty"] * -M6)
        cash = d.chain.balance_of(d.usdc, self.proxy.address)
        if cash < fixed + penalty:
            # The one-time costs are paid in cash, up front.
            ctr["rtrCashWait"] = ctr.get("rtrCashWait", 0) + 1
            return

        # Buck.mint takes the NET amount the new coverage settles: coverage
        # C settles C x (1 - e), and each credit settles at most its
        # unactivated face x (1 - e) -- ask a hair under that.
        e_bp = self.premium_rate * carry.POOL_ROI_INV
        amount = 0
        if v.coverage > 0:
            net_cap = unactivated * (10_000 - e_bp) // 10_000
            amount = min(int(v.coverage * M6) * (10_000 - e_bp) // 10_000
                       * 1_002 // 1_000,
                       net_cap - net_cap // 10_000)
        # The funding gate: hold poolPrincipal x fundingFactor BEFORE the
        # mint.  Short, the household SAVES (buys BUCK) and waits a month.
        if amount >= M6 and ff:
            try:
                _, principal = d.buck.functions.quoteMint(
                    amount, self._token_ids).call()
            except Exception:
                principal = 0
            # +0.5%: the gate is strict (balance >= required) and Buck.mint
            # runs compute() before it reads the factor, which can move it.
            required = principal * ff // 10 ** 18 * 1_005 // 1_000
            bal = d.buck.functions.balanceOf(self.proxy.address).call()
            short = required - bal
            if short > 0:
                budget = int(max(0, cash - fixed - penalty - self.cash_buffer)
                             * self.save_rate)
                if budget > M6:
                    try:
                        self._buy_track(d, min(short * 101 // 100 + M6, budget))
                        ctr["rtrSaved"] = ctr.get("rtrSaved", 0) + 1
                    except Exception as e:
                        ctr["rtr_save_err"] = repr(e)[:200]
                bal = d.buck.functions.balanceOf(self.proxy.address).call()
                if bal < required:
                    ctr["rtrGateWait"] = ctr.get("rtrGateWait", 0) + 1
                    return

        if amount >= M6:
            try:
                pre = d.buck.functions.signedBalanceOf(
                    self.proxy.address).call()
                self._proxy_exec(d, d.buck.address,
                                 d.buck.encode_abi("mint(uint256)", args=[amount]))
                post = d.buck.functions.signedBalanceOf(
                    self.proxy.address).call()
                self.premium_paid += max(0, pre - post)
            except Exception as e:
                self.throttled += 1
                ctr["rtrThrottled"] = ctr.get("rtrThrottled", 0) + 1
                why = repr(e)[:120]
                ctr.setdefault("rtrWhy", {})
                ctr["rtrWhy"][why] = ctr["rtrWhy"].get(why, 0) + 1
                return

        before = d.chain.balance_of(d.usdc, self.proxy.address)
        try:
            sold = self._sell_capped(d, d.pool_ub, buck)
        except Exception as e:
            ctr["rtr_sell_err"] = repr(e)[:200]
            return
        if sold <= 0:
            return
        got = d.chain.balance_of(d.usdc, self.proxy.address) - before
        self.trade_loss += sold - got
        paid = self._pay_bank(d, min(got, self.mortgage))
        self.mortgage -= paid
        if fixed:
            self.switch_paid += self._pay_bank(d, fixed)
        if penalty > 0:
            self.penalty_paid += self._pay_bank(d, penalty)
        first = not self.joined
        self.joined = True
        self.refis += 1
        self.deploys += 1
        ctr["rtrRefis" if first else "rtrTopUps"] = (
            ctr.get("rtrRefis" if first else "rtrTopUps", 0) + 1)
        ctr["rtrBuckSold"] = ctr.get("rtrBuckSold", 0) + sold // M6
        ctr["rtrUsdRetired"] = ctr.get("rtrUsdRetired", 0) + paid // M6
        self.note(d, "refinance" if first else "top-up",
                  renewal=self._at_renewal(), k=round(k / 1e18, 4),
                  sold=sold // M6, retired=paid // M6, **v.why())


# -- reading a vector ---------------------------------------------------------- #

def summarize(path) -> dict:
    """The retirees in one vector: who joined when, what they retired, what
    it cost them, and where each stands against its counterfactual.

        python -m alberta_buck.sim.household_agents VECTOR.json
    """
    import json
    from pathlib import Path

    d = json.loads(Path(path).read_text())
    frames = d["frames"]
    last, joined_day = {}, {}
    for f in frames:
        for st in f.get("octl") or []:
            if "joined" not in st:
                continue                      # a debtor, not a retiree
            i = st["idx"]
            last[i] = (f.get("day"), st)
            if st["joined"] and i not in joined_day:
                joined_day[i] = f.get("day")
    acts = {}
    for f in frames:
        for aid, entry in (f.get("ag") or {}).items():
            if not aid.startswith(ExternalDebtRetireeAgent.__name__):
                continue
            for a in (entry or {}).get("acts", []) if isinstance(entry, dict) else []:
                if a.get("kind") in ("refinance", "top-up"):
                    acts.setdefault(aid, []).append((f.get("day"), a))
    ctr = {k: v for k, v in frames[-1].items() if k.startswith("rtr_")}
    rows = []
    for i, (day, st) in sorted(last.items()):
        rows.append({
            "idx": i, "joined_day": joined_day.get(i), "refis": st["refis"],
            "mortgage": st["mortgage"] // M6, "drawn": st["drawn"] // M6,
            "deposit": st["deposit"] // M6, "limit": st["limit"] // M6,
            "ext_premium": st["ext_premium"] // M6,
            "hypo_premium": st["hypo_premium"] // M6,
            "one_time": (st["switch_paid"] + st["penalty_paid"]) // M6,
            "trade_loss": st["trade_loss"] // M6, "jub": st["jub"] // M6,
            "adv": (st["nw"] - st["hypo"]) // M6, "apr": st["apr"]})
    return {"vector": str(path), "days": frames[-1].get("day"),
            "retirees": len(rows),
            "joined": sum(1 for r in rows if r["joined_day"] is not None),
            "counters": ctr, "rows": rows,
            "decisions": {k: [(day, a.get("kind"), (a.get("why") or {}).get("usd"))
                              for day, a in v] for k, v in acts.items()}}


def main(argv=None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(prog="alberta_buck.sim.household_agents")
    ap.add_argument("vector")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    s = summarize(a.vector)
    if a.json:
        print(json.dumps(s, indent=1))
        return 0
    print(f"{s['vector']}: day {s['days']}, {s['joined']} of {s['retirees']} retirees joined")
    print("counters: " + ", ".join(f"{k}={v}" for k, v in sorted(s["counters"].items())
                                    if not isinstance(v, (dict, str))))
    cols = ("idx", "joined_day", "refis", "apr", "mortgage", "drawn", "limit",
            "deposit", "ext_premium", "hypo_premium", "one_time", "trade_loss",
            "jub", "adv")
    print(" ".join(f"{c:>12}" for c in cols))
    for r in s["rows"]:
        print(" ".join(f"{r[c] if r[c] is not None else '-':>12}"
                       if not isinstance(r[c], float) else f"{r[c]:>12.4f}"
                       for c in cols))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
