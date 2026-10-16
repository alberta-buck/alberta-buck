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
    role="debtor", credit_invest=True)
POLICIES                        = ("adaptive", "basket", "retire", "discount")


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
                "penalty_months", "term_months", "renew_frac", "buffer_k", "max_impact_bp", "decision_days"):
        if not math.isfinite(result[key]) or result[key] < 0:
            raise ValueError(f"invalid household parameter: {key}")
    if result["home_k"] <= 0 or result["max_impact_bp"] > 10000 or result["renew_frac"] > 1:
        raise ValueError("invalid household size, impact or renewal fraction")
    if result["surplus_policy"] not in POLICIES:
        raise ValueError("unknown household surplus_policy")
    if result["role"] not in ("debtor", "saver"):
        raise ValueError("unknown household role")
    if not 0 <= result["retire_disc"] < 1 or result["issue_premium"] < 0:
        raise ValueError("invalid household price thresholds")
    if not 0 < result["utilization"] <= 1 or not 0 <= result["living_frac"] <= 1:
        raise ValueError("invalid household budget fractions")
    result["decision_days"]      = max(1, int(result["decision_days"]))
    result["phase"]              = int(uniform(seed, member, "phase") * result["decision_days"])
    result["term_months"]        = max(1, int(result["term_months"]))
    result["renew_in"]           = int(result["renew_frac"] * result["term_months"])
    result["premium_bp"]         = int(result["premium_bp"])
    return result


def arrivals(seed, population, days, initial=10, arrival_rate=6., max_members=256):
    """A continuing Poisson arrival stream, in households/year, with an explicit resource bound."""
    if (not all(math.isfinite(v) for v in (days, initial, arrival_rate, max_members))
            or initial != int(initial) or max_members != int(max_members)
            or days <= 0 or initial < 0 or arrival_rate < 0 or max_members < initial):
        raise ValueError("invalid household population settings")
    result                      = [0.] * int(initial)
    day                         = 0.
    while arrival_rate > 0:
        day                    += -math.log(1 - uniform(seed, population, f"arrival/{len(result)}")) * 365.25 / arrival_rate
        if math.ceil(day) >= days:
            break
        if len(result) >= max_members:
            raise ValueError("household arrivals exceed max_members; raise the bound or lower arrival_rate")
        result.append(day)
    return [math.ceil(d) for d in result]


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
    v                           = carry.refinance(t, carry.usdc_out(buck, ru, rb, fee), buck)
    return "invest", v
