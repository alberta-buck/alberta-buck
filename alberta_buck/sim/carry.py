"""The organic agents' decision kernel: compare CARRY RATES, choose from a menu.

doc/ORGANIC-SCALE.org, "The stand before the controller", item 1: every
organic agent is a budget-constrained household or business that decides
by comparing carry rates -- demurrage on held BUCK, the basket yield, the
external debt rate, the cost of BUCK credit, the discount or premium, the
funding factor.  This module is that comparison.  It is pure (no chain
reads, plain floats in USD), so it is tested apart from any world, and
every agent that decides with it can put the breakdown in its `why` note.

T15 builds the first leg: REFINANCE external (USD) debt into BUCK credit.

The credit arithmetic is Buck.sol's.  Drawing needs ACTIVATED coverage:
the credit limit is K x the activated coverage's present value, and each
unit activated pays the insurance pool e = POOL_ROI_INV x premium (ten
years' premium) out of the same signed balance.  So a unit of coverage
adds K - e of spendable, drawing B BUCK beyond the headroom already
activated needs C = (B - headroom) / (K - e) of new coverage, and the
deposit is e C -- about 1.4x the draw's worth of premium years at K 0.75.
A draw inside existing headroom (a top-up after K rises) needs none.

Insurance, compared fairly (master fe3083f, the debtor ledger's doctrine).
Both paths insure the same asset at the same rate:

  * the external path pays the premium as it falls due -- a COST;
  * the BUCK path pays no premium.  The deposit is an OUTLAY, not a cost:
    the pool invests it to earn the premiums and returns it when the
    insurance is dropped.  What it costs is the opportunity of the capital
    it ties up (the external interest that credit would otherwise have
    retired), plus the pool's accrued age the refund carries back to the
    member (demurrage, Buck._carryingTransfer).  The coverage it buys
    earns the Jubilee relief.

So joining the BUCK system SAVES the external premium outright, and the
insurance leg costs only the deposit's opportunity: with the whole face
activated the BUCK path's insurance is the cheaper one below an
opportunity rate of 1 / POOL_ROI_INV = 10%, whatever the premium.  (The
external premium is on the whole face; the deposit only on the coverage
activated -- master's doctrine.  Whether BuckCredit then insures the
unactivated face is a question for the design owner, recorded in
doc/reports/wp-t15.org.)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# Buck.sol POOL_ROI_INV: a unit of coverage deposits this many years of its
# annual premium with the insurance pool ("10% assumed annual ROI").
POOL_ROI_INV = 10
# alberta-buck-demurrage.org: a flat 2%/yr on every BUCK held.  A Carrying
# insurance pool keeps its deposits' age and a refund hands it back.
DEMURRAGE = 0.02
# BuckCredit.jubileeRelief: ~2%/yr of the activated coverage, capped at it.
RELIEF = 0.02


def deposit_rate(premium_bp: float) -> float:
    """e: the pool principal per unit of coverage activated (Buck.sol's
    effRate = premiumRate x POOL_ROI_INV, as a fraction)."""
    return premium_bp * POOL_ROI_INV / 10_000.0


def usdc_out(buck_in: float, r_usdc: float, r_buck: float, fee: float) -> float:
    """USDC a constant-product sale of `buck_in` BUCK returns (the closed form
    BuckCreditDebtorAgent._atomic_plan quotes the active reserves with)."""
    b = buck_in * (1.0 - fee)
    return r_usdc * b / (r_buck + b) if r_buck + b > 0 else 0.0


def buck_in(usdc: float, r_usdc: float, r_buck: float, fee: float) -> float:
    """BUCK that must be sold to net `usdc` (the inverse of usdc_out)."""
    if usdc >= r_usdc:
        return math.inf
    return usdc * r_buck / ((r_usdc - usdc) * (1.0 - fee))


@dataclass(frozen=True)
class RefiTerms:
    """One household's refinance question, in USD.

    `payback_y` is the octl debtors' theta: the years of carry the household
    lets repay a one-time cost.  With every term but the interest zeroed the
    rule below is their theta law, gate_disc <= theta * apr, stated on the
    USD retired rather than the BUCK sold."""
    debt: float                 # external debt outstanding
    rate: float                 # its annual rate
    face: float                 # insured value: the external premium's base
    premium_bp: float           # the insurance rate, both paths
    joined: bool                # already insured through BuckCredit
    payback_y: float            # theta
    k: float = 1.0              # BUCK_K now
    headroom: float = 0.0       # spendable already activated (limit - drawn)
    unactivated: float = 0.0    # face not yet activated, at present value
    fixed_cost: float = 0.0     # switching: legal, appraisal, discharge
    penalty_months: float = 0.0  # prepayment penalty, months of interest (0 at renewal)
    risk: float = 0.0           # /yr the household charges for owing basket-indexed BUCK
    opp_rate: float | None = None  # the deposit's opportunity; None = `rate`
    k_margin: float = 0.99      # K may move at the mint (Buck.mint runs compute())

    @property
    def spend_per_cover(self) -> float:
        """Spendable a unit of new coverage adds: K less its deposit."""
        return self.k * self.k_margin - deposit_rate(self.premium_bp)

    @property
    def capacity(self) -> float:
        """The most BUCK the household can draw now."""
        return self.headroom + self.unactivated * max(0.0, self.spend_per_cover)

    def coverage_for(self, buck: float) -> float:
        """New coverage to activate so `buck` can be drawn."""
        need = max(0.0, buck - self.headroom)
        if need == 0:
            return 0.0
        spc = self.spend_per_cover
        return need / spc if spc > 0 else math.inf


@dataclass
class RefiVerdict:
    go: bool
    usd: float                  # external debt retired
    buck: float                 # BUCK drawn and sold for it: the liability, at par
    coverage: float             # new coverage activated for it
    deposit: float              # the insurance deposit that coverage makes
    carry: float                # USD/yr the BUCK path gains
    one_time: float             # USD paid once
    value: float                # payback_y * carry - one_time
    parts: dict = field(default_factory=dict)

    def why(self) -> dict:
        """The breakdown, rounded to dollars, for an agent's `why` note."""
        out = {k: round(v) for k, v in self.parts.items()}
        out.update(usd=round(self.usd), buck=round(self.buck),
                   coverage=round(self.coverage), deposit=round(self.deposit),
                   carry=round(self.carry), one_time=round(self.one_time),
                   value=round(self.value))
        return out


NO = RefiVerdict(False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def refinance(t: RefiTerms, usd: float, buck: float) -> RefiVerdict:
    """The value of retiring `usd` of external debt by selling `buck` BUCK
    drawn on BuckCredit.  Carry is per year; one-time costs are paid once;
    the household goes when the carry repays them within `payback_y`."""
    cov = t.coverage_for(buck)
    if not math.isfinite(cov):
        return NO
    dep = cov * deposit_rate(t.premium_bp)
    opp = t.rate if t.opp_rate is None else t.opp_rate
    parts = {
        # external interest no longer paid
        "interest": usd * t.rate,
        # the Jubilee relief the new coverage earns
        "relief": cov * RELIEF,
        # the external premium stops once the asset is insured through BUCK
        "premium": 0.0 if t.joined else t.face * t.premium_bp / 10_000.0,
        # the deposit: an outlay, whose cost is the opportunity it forgoes...
        "deposit_opp": -dep * opp,
        # ...and the pool's accrued age its refund carries back
        "deposit_age": -dep * DEMURRAGE,
        # owing a basket-indexed unit against USD income
        "risk": -buck * t.risk,
        # execution: the liability, at par, beyond the USD it retired
        "execution": -(buck - usd),
        "fixed": -t.fixed_cost,
        "penalty": -usd * t.rate * t.penalty_months / 12.0,
    }
    carry = sum(parts[k] for k in ("interest", "relief", "premium",
                                   "deposit_opp", "deposit_age", "risk"))
    one_time = -(parts["execution"] + parts["fixed"] + parts["penalty"])
    value = t.payback_y * carry - one_time
    return RefiVerdict(go=(usd > 0 and carry > 0 and value > 0), usd=usd,
                       buck=buck, coverage=cov, deposit=dep, carry=carry,
                       one_time=one_time, value=value, parts=parts)


# Sizes tried, as fractions of the most the household can retire.  The
# value is concave in size (carry is linear, slippage convex, fixed costs
# fixed), so a coarse grid finds the best size without a solver.
FRACTIONS = (1.0, 0.75, 0.5, 0.25, 0.1)


def best_refinance(t: RefiTerms, r_usdc: float, r_buck: float, fee: float,
                   margin: float = 1.005, depth_frac: float = 0.5) -> RefiVerdict:
    """The best refinance the household can make now.

    The sale is quoted on the pool's active reserves, sized with the same
    safety margin the atomic debtor uses, and never asks for more than
    `depth_frac` of the pool's USDC.  K enters twice: through the capacity
    (a household whose debt exceeds what K lets it draw retires what it can
    and keeps the rest external -- the partial refinance that makes the
    supply answer K as a flow, not a threshold) and through the coverage a
    BUCK drawn needs (a higher K, a smaller deposit)."""
    if t.debt <= 0 or t.capacity <= 0 or r_usdc <= 0 or r_buck <= 0:
        return NO
    usd_max = min(t.debt, usdc_out(t.capacity / margin, r_usdc, r_buck, fee),
                  depth_frac * r_usdc)
    best = NO
    for f in FRACTIONS:
        usd = f * usd_max
        b = buck_in(usd, r_usdc, r_buck, fee) * margin
        if not math.isfinite(b):
            continue
        v = refinance(t, usd, b)
        if v.go and v.value > best.value:
            best = v
    return best
