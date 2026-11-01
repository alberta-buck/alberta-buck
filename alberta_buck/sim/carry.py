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
    member (demurrage, Buck._carryingTransfer).

The legacy comparison assumes 2% annual Jubilee relief on the BUCK drawn
and premium principal, and 2% annual loss on the premium refund. For BuckNet,
those are forecasts rather than guaranteed outcomes: the marked fund and
insurer pool can change. A BuckNet wallet is already-issued money, so spending
it adds no new relief. Net-model callers supply explicit forecasts and an
estimated net refund fraction; the latter can reflect a pool's carried fees
and any disclosed strategy. These annualized estimates are decision inputs,
not actual settlement quotes or a substitute for the fund's liquidity cap.

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
# Buck.reliefOf: ~2%/yr of the lien (the BUCK issued), capped at it.
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


def discounted_buyback(cash: float, drawn: float, r_usdc: float, r_buck: float,
                       fee: float, basket_usd: float, discount: float) -> float:
    """BUCKs to buy while even the last unit, including fee and impact, retains the requested discount.

    Inputs and output use whole currency units. The caller may further cap cash by its impact budget.
    Basket parity can be above or below $1; the obligation is denominated in BUCK, not dollars.
    """
    if min(cash, drawn, r_usdc, r_buck, basket_usd) <= 0 or not 0 <= fee < 1:
        return 0.
    net                         = 1 - fee
    ceiling                     = basket_usd * (1 - discount)
    spot                        = r_usdc / r_buck
    if ceiling * net <= spot:
        return 0.
    budget                      = r_usdc * (math.sqrt(ceiling * net / spot) - 1) / net
    spend                       = min(cash, budget) * .999  # leave rounding room at the marginal-price bound
    return min(drawn, r_buck * (spend * net) / (r_usdc + spend * net))


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
    buck_value: float = 1.0     # basket-parity USD value of one BUCK, not a fixed dollar peg
    # Forecasts, not promises: net-mode callers must provide these explicitly.
    expected_relief_rate: float | None = None   # annual relief per newly issued BUCK
    premium_refund_fraction: float = 1.0        # estimated net value / nominal premium on entry
    expected_refund_growth: float | None = None  # annual change in that net refund claim
    wallet_already_issued: bool = False          # net-mode headroom is money already issued
    expected_wallet_growth: float = 0.0         # annual return on the wallet's alternative use
    activated_capacity: float = 0.0             # new spendable issuance without more coverage or premium
    issuance_limit: float | None = None         # common room for new principal, including premium

    def __post_init__(self):
        if (self.expected_relief_rate is not None and
                (not math.isfinite(self.expected_relief_rate) or self.expected_relief_rate < 0)):
            raise ValueError("invalid expected relief rate")
        if not math.isfinite(self.premium_refund_fraction) or not 0 <= self.premium_refund_fraction <= 1:
            raise ValueError("invalid premium refund fraction")
        if (self.expected_refund_growth is not None and
                (not math.isfinite(self.expected_refund_growth) or self.expected_refund_growth < -1)):
            raise ValueError("invalid expected refund growth")
        if not math.isfinite(self.expected_wallet_growth) or self.expected_wallet_growth < -1:
            raise ValueError("invalid expected wallet growth")
        if not math.isfinite(self.activated_capacity) or self.activated_capacity < 0:
            raise ValueError("invalid activated credit capacity")
        if self.issuance_limit is not None and (not math.isfinite(self.issuance_limit) or self.issuance_limit < 0):
            raise ValueError("invalid shared issuance limit")

    @property
    def spend_per_cover(self) -> float:
        """Spendable a unit of new coverage adds: K less its deposit."""
        return self.k * self.k_margin - deposit_rate(self.premium_bp)

    @property
    def capacity(self) -> float:
        """The most BUCK the household can draw now."""
        spend_per_cover = max(0.0, self.spend_per_cover)
        if self.issuance_limit is None:
            return self.headroom + self.activated_capacity + self.unactivated * spend_per_cover
        activated = min(self.activated_capacity, self.issuance_limit)
        total_per_cover = spend_per_cover + deposit_rate(self.premium_bp)
        extra_coverage = (min(self.unactivated, (self.issuance_limit - activated) / total_per_cover)
                          if spend_per_cover > 0 and total_per_cover > 0 else 0.)
        return self.headroom + activated + extra_coverage * spend_per_cover

    def coverage_for(self, buck: float) -> float:
        """New coverage to activate so `buck` can be drawn."""
        new_spendable = max(0.0, buck - self.headroom)
        need = max(0.0, new_spendable - self.activated_capacity)
        if need == 0:
            return 0.0 if self.issuance_limit is None or new_spendable <= self.issuance_limit else math.inf
        spc = self.spend_per_cover
        coverage = need / spc if spc > 0 else math.inf
        if self.issuance_limit is not None and new_spendable + coverage * deposit_rate(self.premium_bp) > self.issuance_limit:
            return math.inf
        return coverage


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
    newly_issued = max(0., buck - t.headroom) if t.wallet_already_issued else buck
    wallet_used = min(buck, t.headroom) if t.wallet_already_issued else 0.
    return _score_refinance(t, usd, buck, cov, dep, newly_issued, wallet_used)


def _score_refinance(t, usd, buck, cov, dep, newly_issued, wallet_used, reserve_costs=None):
    """Common valuation; feasibility and token-unit rounding belong to the caller."""
    value_buck                  = buck * t.buck_value
    value_dep                   = dep * t.buck_value
    refund_value                = value_dep * t.premium_refund_fraction
    relief_rate                 = RELIEF if t.expected_relief_rate is None else t.expected_relief_rate
    refund_growth               = -DEMURRAGE if t.expected_refund_growth is None else t.expected_refund_growth
    opp = t.rate if t.opp_rate is None else t.opp_rate
    parts = {
        # external interest no longer paid
        "interest": usd * t.rate,
        # the Jubilee relief the new lien earns: the BUCK drawn and the
        # deposit drawn with it
        "relief": (newly_issued * t.buck_value + value_dep) * relief_rate,
        # the external premium stops once the asset is insured through BUCK
        "premium": 0.0 if t.joined else t.face * t.premium_bp / 10_000.0,
        # the deposit: an outlay, whose cost is the opportunity it forgoes...
        "deposit_opp": -refund_value * opp,
        # ...and the expected change in the insurer's net refund value
        "deposit_age": refund_value * refund_growth,
        # The newly paid full-value premium can immediately share old carried
        # fees. This is an estimate of the claim's initial mark, not a burn.
        "deposit_write_down": -(value_dep - refund_value),
        # Spending existing money gives up its alternative return. A negative
        # return (for example idle wallet demurrage) is a cost avoided.
        "wallet_carry": -wallet_used * t.buck_value * t.expected_wallet_growth,
        # owing a basket-indexed unit against USD income
        "risk": -value_buck * t.risk,
        # execution: the liability, at par, beyond the USD it retired
        "execution": -(value_buck - usd),
        "fixed": -t.fixed_cost,
        "penalty": -usd * t.rate * t.penalty_months / 12.0,
    }
    carry = sum(parts[k] for k in ("interest", "relief", "premium",
                                   "deposit_opp", "deposit_age", "wallet_carry", "risk"))
    one_time = -(parts["execution"] + parts["fixed"] + parts["penalty"] + parts["deposit_write_down"])
    if reserve_costs is not None:
        one_time_cost, annual_cost = reserve_costs
        parts["reserve_cost"] = -one_time_cost
        parts["reserve_carry"] = -annual_cost
        one_time += one_time_cost
        carry -= annual_cost
    value = t.payback_y * carry - one_time
    return RefiVerdict(go=(usd > 0 and carry > 0 and value > 0), usd=usd,
                       buck=buck, coverage=cov, deposit=dep, carry=carry,
                       one_time=one_time, value=value, parts=parts)


def refinance_quoted(t: RefiTerms, usd: float, wallet_used: float, new_spendable: float,
                     present_coverage: float, premium: float, *, reserve_cost: float = 0.,
                     reserve_carry: float = 0.) -> RefiVerdict:
    """Value a proposed split between already-held money and one quoted issue.

    Amounts use whole units; ``usd`` is the external mortgage principal the
    household expects actually to retire after trading. ``present_coverage`` is the marked increment,
    not the face-unit argument to issue. The premium must come from the actual
    per-NFT quote. Reserve costs are the incremental, unrecovered acquisition
    and annual carrying costs in USD, excluding the still-owned reserve and
    anything already included in wallet opportunity. They can be negative if
    acquiring/holding the reserve has a net benefit.

    This is an economic forecast, not an issuance or trade check. The caller
    must first establish exact integer feasibility, reserve funding, a guarded
    market quote and an appropriate current block; repeat as necessary before
    transacting. No gross reserve purchase is treated as a loss of principal.
    """
    amounts = (usd, wallet_used, new_spendable, present_coverage, premium)
    if (not t.wallet_already_issued or t.expected_relief_rate is None or t.expected_refund_growth is None or
            any(not math.isfinite(v) or v < 0 for v in amounts) or
            not math.isfinite(reserve_cost) or not math.isfinite(reserve_carry)):
        raise ValueError("invalid quoted refinance inputs")
    if not math.isfinite(t.debt) or t.debt < 0 or usd > t.debt:
        raise ValueError("quoted refinance exceeds external debt")
    if not math.isfinite(t.headroom) or t.headroom < 0 or wallet_used > t.headroom:
        raise ValueError("quoted refinance exceeds wallet funds")
    if new_spendable == 0 and (present_coverage or premium or reserve_cost or reserve_carry):
        raise ValueError("quoted refinance has issuance costs without issuance")
    if premium == 0 and (reserve_cost or reserve_carry):
        raise ValueError("quoted refinance has reserve costs without a premium")
    if t.issuance_limit is None or new_spendable + premium > t.issuance_limit:
        raise ValueError("quoted refinance exceeds shared issuance limit")
    if premium > present_coverage:
        raise ValueError("quoted refinance premium exceeds added coverage")
    return _score_refinance(t, usd, wallet_used + new_spendable, present_coverage, premium,
                            new_spendable, wallet_used, (reserve_cost, reserve_carry))


def best_quoted_refinance(t: RefiTerms, candidates):
    """Choose the best worthwhile candidate from an explicitly supplied menu.

    Each mapping has the arguments of ``refinance_quoted``. Candidates must
    already have exact token and market feasibility; this finite menu does not
    search for an optimum or infer monotonicity. Exact ties prefer less added
    coverage, then less new issuance, avoiding unnecessary activation or debt.
    """
    best_index, best, best_key = None, NO, None
    for index, candidate in enumerate(candidates):
        verdict = refinance_quoted(t, **candidate)
        if not verdict.go:
            continue
        key = (verdict.value, -candidate["present_coverage"], -candidate["new_spendable"])
        if best_key is None or key > best_key:
            best_index, best, best_key = index, verdict, key
    return best_index, best


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
