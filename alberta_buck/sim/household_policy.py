"""Portable household policy. Whole currency units; mirrored by core/js/src/agents/household-policy.js."""

from dataclasses import replace
import math

from alberta_buck.sim import carry

DEFAULTS                        = dict(
    home_k=[200., 1500.], ltv=[.05, .85], income_frac=[.20, .35], living_frac=[.55, .70],
    cash_k=[20., 60.], apr=[.045, .075], premium_bp=[25., 100.], theta=[.5, 3.],
    risk=[0., .01], switch_k=[1., 3.], penalty_months=3., term_months=60., renew_frac=[0., 1.],
    retire_disc=[.005, .03], issue_premium=[.005, .025], utilization=[.65, .85],
    buffer_k=10., max_impact_bp=[25., 100.], decision_days=[5., 20.], surplus_policy="adaptive",
    role="debtor", credit_invest=True,
    # Share of the K-scaled limit a household with no mortgage keeps drawn and invested while the BUCK
    # is not cheap (0: it borrows only at a premium).
    carry_utilization=0.,
    # The investor: opening savings and yearly saving ($k), and the years before it withdraws.
    invest_cash_k=[10., 100.], invest_income_k=[2., 20.], hold_years=[3., 15.],
    # A debtor or saver sells its insured asset at this hazard a year, buying back its whole lien
    # first; it repays BUCKs at no other time (0: never).
    divest_rate=.1)
POLICIES                        = ("adaptive", "basket", "retire", "discount")
ROLES                           = ("debtor", "saver", "investor")
SUPPLY                          = ("debtor", "saver")   # drawn while the BUCK is dear; investors while it is cheap
GROWTH                          = ("steady", "rising", "falling")
POPULATION                      = dict(
    initial_debtors=5, initial_savers=5, initial_investors=0, debtor_rate=3., saver_rate=3., investor_rate=0.,
    growth="steady", gain=50., gain_cap=4., signal_halflife_days=30., settle_days=7, max_members=2000,
    # Issuers arrive faster as K rises, by (1 - k_ref) / (1 - K): the BUCKs an insured dollar yields
    # grow without bound as K nears 1, so no rational household stays away.
    k_ref=.75)
# Older settings, still accepted when no role is named: a total split between savers and debtors.
LEGACY_POPULATION               = dict(initial=10, arrival_rate=6., saver_share=.5)


def uniform(seed, member, key):
    """Stateless 32-bit keyed draw; adding a knob cannot move any other draw."""
    h                           = 2166136261
    for byte in f"{seed}:{member}:{key}".encode("utf-8"):
        h                       = ((h ^ byte) * 16777619) & 0xffffffff
    h                          ^= h >> 16
    h                           = (h * 0x85ebca6b) & 0xffffffff
    h                          ^= h >> 13
    h                           = (h * 0xc2b2ae35) & 0xffffffff
    h                          ^= h >> 16
    return (h + .5) / 4294967296


def profile(seed, member, overrides=None):
    settings                    = DEFAULTS | (overrides or {})
    result                      = {}
    for key, value in settings.items():
        if isinstance(value, (tuple, list)):
            if len(value) != 2 or not all(math.isfinite(v) for v in value) or value[1] < value[0]:
                raise ValueError(f"invalid household range: {key}")
            lo, hi              = value
            if key == "home_k" and lo <= 0:
                raise ValueError("home_k must be positive")
            u                   = uniform(seed, member, key)
            value               = lo * (hi / lo) ** u if key == "home_k" else lo + (hi - lo) * u
        result[key]             = value
    for key in ("home_k", "ltv", "income_frac", "cash_k", "apr", "premium_bp", "theta", "risk", "switch_k",
                "penalty_months", "term_months", "renew_frac", "buffer_k", "max_impact_bp", "decision_days",
                "invest_cash_k", "invest_income_k", "hold_years", "divest_rate"):
        if not math.isfinite(result[key]) or result[key] < 0:
            raise ValueError(f"invalid household parameter: {key}")
    if result["home_k"] <= 0 or result["max_impact_bp"] > 10000 or result["renew_frac"] > 1:
        raise ValueError("invalid household size, impact or renewal fraction")
    if result["hold_years"] <= 0:
        raise ValueError("an investor's hold_years must be positive")
    if result["surplus_policy"] not in POLICIES:
        raise ValueError("unknown household surplus_policy")
    if result["role"] not in ROLES:
        raise ValueError("unknown household role")
    if not 0 <= result["retire_disc"] < 1 or result["issue_premium"] < 0:
        raise ValueError("invalid household price thresholds")
    if (not 0 < result["utilization"] <= 1 or not 0 <= result["living_frac"] <= 1
            or not 0 <= result["carry_utilization"] <= 1):
        raise ValueError("invalid household budget fractions")
    result["decision_days"]      = max(1, int(result["decision_days"]))
    result["phase"]              = int(uniform(seed, member, "phase") * result["decision_days"])
    result["term_months"]        = max(1, int(result["term_months"]))
    result["renew_in"]           = int(result["renew_frac"] * result["term_months"])
    result["premium_bp"]         = int(result["premium_bp"])
    return result


def _count(value, key):
    if not math.isfinite(value) or value != int(value) or value < 0:
        raise ValueError(f"{key} must be a whole number of households")
    return int(value)


def population(given):
    """The population's settings: those a world names, defaults for the rest.

    The older keys (initial, arrival_rate, saver_share) are read first when any is named: savers take
    saver_share of the total and debtors the rest. Keys naming a role then override them.
    """
    given                       = {k: v for k, v in given.items() if v is not None}
    result                      = dict(POPULATION)
    if any(k in given for k in LEGACY_POPULATION):
        old                     = LEGACY_POPULATION | {k: given[k] for k in LEGACY_POPULATION if k in given}
        share                   = float(old["saver_share"])
        if not 0 <= share <= 1:
            raise ValueError("saver_share must be between zero and one")
        initial                 = _count(float(old["initial"]), "initial")
        rate                    = float(old["arrival_rate"])
        savers                  = math.floor(initial * share + .5)
        result.update(initial_savers=savers, initial_debtors=initial - savers, saver_rate=rate * share,
                      debtor_rate=rate * (1 - share))
    result.update({k: given[k] for k in POPULATION if k in given})
    for role in ROLES:
        result[f"initial_{role}s"] = _count(float(result[f"initial_{role}s"]), f"initial_{role}s")
        result[f"{role}_rate"]  = float(result[f"{role}_rate"])
    for key in ("gain", "gain_cap", "signal_halflife_days", "k_ref"):
        result[key]             = float(result[key])
    if not 0 <= result["k_ref"] < 1:
        raise ValueError("k_ref must be in [0, 1)")
    result["settle_days"]       = _count(float(result["settle_days"]), "settle_days")
    result["max_members"]       = _count(float(result["max_members"]), "max_members")
    if result["growth"] not in GROWTH:
        raise ValueError(f"growth must be one of {GROWTH}")
    if (not all(math.isfinite(result[f"{r}_rate"]) and result[f"{r}_rate"] >= 0 for r in ROLES)
            or not math.isfinite(result["gain"]) or result["gain"] < 0
            or not math.isfinite(result["gain_cap"]) or result["gain_cap"] < 1
            or not math.isfinite(result["signal_halflife_days"]) or result["signal_halflife_days"] <= 0):
        raise ValueError("invalid household population rates, gain or averaging")
    if result["max_members"] < sum(result[f"initial_{r}s"] for r in ROLES):
        raise ValueError("max_members is smaller than the opening population")
    return result


def growth_factor(growth, day, days):
    """The base pace over the horizon: straight lines that each average one."""
    t                           = day / days if days > 0 else 0.
    return 1. if growth == "steady" else .25 + 1.5 * t if growth == "rising" else 1.75 - 1.5 * t


def _exponential(seed, population, role, k):
    return -math.log(1 - uniform(seed, population, f"{role}/arrival/{k}"))


def arrival_state(seed, population):
    """Before day 0: the averaged basket price at parity, and each role's first arrival threshold."""
    return dict(average=1., gap=0., k_factor=1., intensity={r: 0. for r in ROLES}, arrived={r: 0 for r in ROLES},
                multiplier={r: 1. for r in ROLES}, due={r: _exponential(seed, population, r, 0) for r in ROLES})


def k_factor(k, p):
    """How much faster issuers arrive at this K than at k_ref: (1 - k_ref) / (1 - K)."""
    if k is None or not math.isfinite(k):
        return 1.
    return (1 - p["k_ref"]) / max(1e-6, 1 - k)


def arrivals_step(state, seed, population, day, days, bvib, k, p):
    """One day of arrivals, updating `state` in place; returns [(role, k), ...] for today's arrivals.

    The gap is 1 - (the averaged basket price in BUCKs): positive while the BUCK is dear. Debtors and
    savers arrive faster while it is dear, by 1 + gain x gap (at most gain_cap), and faster as K
    rises, by (1 - k_ref) / (1 - K); investors arrive faster while it is cheap, by 1 + gain x -gap.
    A role's k-th arrival (from 0) comes when its cumulative intensity reaches the sum of k + 1 unit
    exponentials, so a world with gain 0 at K = k_ref has the same arrivals at the base pace.
    """
    settled                     = day >= p["settle_days"]
    if settled and bvib is not None and math.isfinite(bvib):
        state["average"]       += (1 - .5 ** (1 / p["signal_halflife_days"])) * (bvib - state["average"])
    gap                         = 1 - state["average"] if settled else 0.
    state["gap"]                = gap
    state["k_factor"]           = k_factor(k, p)
    pace                        = growth_factor(p["growth"], day, days)
    arrived                     = []
    for role in ROLES:
        side                    = gap if role in SUPPLY else -gap
        m                       = min(p["gain_cap"], 1 + p["gain"] * max(0., side))
        if role in SUPPLY:
            m                  *= state["k_factor"]
        state["multiplier"][role] = m
        state["intensity"][role] += p[f"{role}_rate"] * pace * m / 365.25
        while state["intensity"][role] >= state["due"][role]:
            arrived.append((role, state["arrived"][role]))
            state["arrived"][role] += 1
            state["due"][role] += _exponential(seed, population, role, state["arrived"][role])
    return arrived


def divest_day(seed, member, arrive_day, rate):
    """The day a debtor or saver sells its insured asset: an exponential wait at `rate` a year, keyed
    by the member alone. None when it never does."""
    if rate <= 0:
        return None
    return arrive_day + int(-math.log(1 - uniform(seed, member, "divest")) * 365.25 / rate)


def investor_action(spot, basket_usd, fee, premium):
    """An investor holds BUCKs as its money: it buys them, unless they are dear by more than its
    premium after the fee; then it converts what it holds into basket shares instead. (A BUCK
    deposited in the basket is spent by the basket on commodities, so only BUCKs held are demand.)"""
    return "convert" if spot * (1 - fee) > basket_usd * (1 + premium) else "buy"


def surplus_action(policy, spot, basket_usd, fee, discount, premium):
    if policy == "basket":
        return "invest"
    if policy == "retire":
        return "retire"
    cheap                       = spot / (1 - fee) < basket_usd * (1 - discount)
    if cheap:
        return "retire"
    if policy == "adaptive" and spot * (1 - fee) > basket_usd * (1 + premium):
        return "invest"
    return "hold"


def premium_sale(capacity, ru, rb, fee, basket_usd, premium, impact_bp):
    """Cap even the last BUCK sold to the requested net premium."""
    if min(capacity, ru, rb, basket_usd) <= 0:
        return 0.
    net                         = 1 - fee
    floor                       = basket_usd * (1 + premium)
    budget                      = rb * (math.sqrt(ru / rb * net / floor) - 1) / net
    return max(0., min(capacity, budget, rb * impact_bp / 20_000) * .999)


def terms(state, p, investment=False):
    return carry.RefiTerms(
        debt=state["mortgage"], rate=0. if investment else p["apr"], face=state["face"],
        premium_bp=p["premium_bp"], joined=state["joined"], payback_y=p["theta"], k=state["k"],
        headroom=state["spendable"], unactivated=state["unactivated"],
        fixed_cost=0. if state["joined"] else p["switch_k"] * 1000,
        penalty_months=0. if investment or state["at_renewal"] else p["penalty_months"],
        risk=p["risk"], opp_rate=p["apr"], buck_value=state["basket_usd"])


def issuance(state, p):
    """Choose mortgage refinancing first; otherwise a bounded premium-funded investment."""
    ru, rb, fee                 = state["ru"], state["rb"], state["fee"]
    t                           = terms(state, p)
    if state["mortgage"] > 1:
        # One decision cannot exceed the member's impact allowance.
        t                       = replace(t, debt=min(t.debt, carry.usdc_out(rb * p["max_impact_bp"] / 20_000,
                                                                           ru, rb, fee)))
        return "refinance", carry.best_refinance(t, ru, rb, fee)
    if not p["credit_invest"] or p["surplus_policy"] in ("retire", "discount"):
        return "hold", carry.NO
    t                           = terms(state, p, investment=True)
    limit                       = state["face"] * state["k"] * p["utilization"] - state["drawn"]
    buck                        = premium_sale(min(t.capacity, max(0., limit)), ru, rb, fee,
                                               state["basket_usd"], p["issue_premium"], p["max_impact_bp"])
    keep                        = state["face"] * state["k"] * p["carry_utilization"] - state["drawn"]
    cheap                       = ru / rb / (1 - fee) < state["basket_usd"] * (1 - p["retire_disc"])
    if buck <= 0 and keep > 0 and not cheap:
        # Carry: keep this share of the K-scaled limit drawn and invested while the BUCK is not cheap.
        buck                    = max(0., min(t.capacity, keep, rb * p["max_impact_bp"] / 20_000) * .999)
    v                           = carry.refinance(t, carry.usdc_out(buck, ru, rb, fee), buck)
    return "invest", v
