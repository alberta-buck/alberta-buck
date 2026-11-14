"""Shared examples, economic boundaries, and Python/JavaScript conformance."""
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from alberta_buck.sim import carry, household_policy as hp
from alberta_buck.sim.net_household import observe_refinance_forecasts
from buck_core import GlobalReliefRateQuote

ROOT                            = Path(__file__).resolve().parents[2]
FIXTURE                         = json.loads((ROOT / "test/fixtures/household-policy.json").read_text())
INSURANCE                       = json.loads((ROOT / "test/fixtures/household-insurance.json").read_text())


@pytest.mark.parametrize("case", FIXTURE["closing"], ids=lambda c: c["name"])
def test_closing_cost_tracks_confirmed_payment_separately_from_activation(case):
    p = hp.profile(FIXTURE["seed"], "closing", FIXTURE["settings"] | {"switch_k": 2})
    state = FIXTURE["state"] | {key: case[key] for key in ("joined", "closing_paid")}
    assert hp.terms(state, p).fixed_cost == case["fixed"]


@pytest.mark.parametrize("case", FIXTURE["issuance"], ids=lambda c: c["name"])
def test_issuance_cases(case):
    p                           = hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"] | case["settings"])
    kind, v                     = hp.issuance(FIXTURE["state"] | case["state"], p)
    assert (kind, v.go) == (case["kind"], case["go"])
    if v.go:
        assert v.value > 0 and v.carry > 0


@pytest.mark.parametrize("case", FIXTURE["net_refinance"], ids=lambda c: c["name"])
def test_net_refinance_uses_explicit_forecasts_and_existing_wallet(case):
    p = hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"] | {"apr": .03, "theta": 3})
    t = hp.terms(FIXTURE["state"] | case["state"], p)
    v = carry.refinance(t, case["usd"], case["buck"])
    assert v.go == case["go"]
    expected_new = max(0, case["buck"] - t.headroom)
    assert v.parts["relief"] == pytest.approx((expected_new + v.deposit) * t.expected_relief_rate)
    assert v.parts["deposit_write_down"] == pytest.approx(-v.deposit * (1 - t.premium_refund_fraction))
    assert v.parts["deposit_age"] == pytest.approx(v.deposit * t.premium_refund_fraction * t.expected_refund_growth)
    assert v.parts["wallet_carry"] == pytest.approx(-min(case["buck"], t.headroom) * t.expected_wallet_growth)


@pytest.mark.parametrize("case", INSURANCE["coverage"], ids=lambda c: c["name"])
def test_external_premium_depends_on_actual_coverage(case):
    premium, face, activated, due = (int(case[k]) for k in ("premium", "face", "activated", "due"))
    assert hp.external_premium_due(premium, face, activated) == due
    # Removing the active coverage restores the whole external premium.
    assert hp.external_premium_due(premium, face, 0) == premium


@pytest.mark.parametrize("case", INSURANCE["refinance"], ids=lambda c: c["name"])
def test_net_refinance_scores_only_incremental_insurance(case):
    t = replace(quoted_terms({"joined": case["joined"]}), fixed_cost=2000)
    args = dict(usd=4900, wallet_used=0 if case["new_spendable"] else 5000,
                new_spendable=case["new_spendable"], present_coverage=case["coverage"], premium=case["premium"])
    baseline = carry.refinance_quoted(t, **args)
    v = carry.refinance_quoted(t, **args, external_premium_saving=case["saving"])
    assert baseline.parts["premium"] == 0
    assert v.parts["premium"] == case["saving"]
    assert v.carry - baseline.carry == pytest.approx(case["saving"])
    assert v.value - baseline.value == pytest.approx(t.payback_y * case["saving"])
    assert v.parts["fixed"] == (-2000 if case["new_spendable"] else 0)
    assert carry.refinance(t, 4900, 5000).parts["premium"] == 0


def test_a_fixed_premium_cost_makes_small_loans_proportionally_dearer():
    """premium_bp + premium_fixed_usd / insured value: $100 a year adds 50 bp to a $20k car, 2 to a $500k home."""
    base = FIXTURE["settings"] | {"premium_bp": [50., 50.], "premium_fixed_usd": 100.}
    car = hp.profile(FIXTURE["seed"], "0", base | {"home_k": [20., 20.]})
    home = hp.profile(FIXTURE["seed"], "0", base | {"home_k": [500., 500.]})
    assert (car["premium_bp"], home["premium_bp"]) == (100, 52)
    assert hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"])["premium_bp"] == \
        hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"] | {"premium_fixed_usd": 0.})["premium_bp"]
    with pytest.raises(ValueError, match="premium_fixed_usd"):
        hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"] | {"premium_fixed_usd": -1.})


def test_external_premium_rejects_impossible_inputs():
    for args in ((1, 0, 0), (1, 10, 11), (-1, 10, 0), (1, 10, -1), (True, 10, 0),
                 (1, 10.5, 0), (2**256, 10, 0)):
        with pytest.raises(ValueError, match="external insurance"):
            hp.external_premium_due(*args)
    for saving in (-1, float("nan"), True, 10001):
        with pytest.raises(ValueError, match="incremental external"):
            carry.refinance_quoted(quoted_terms(), 4900, 0, 5000, 10000, 1000, external_premium_saving=saving)
    with pytest.raises(ValueError, match="incremental external"):
        carry.refinance_quoted(quoted_terms(), 4900, 5000, 0, 0, 0, external_premium_saving=1)
    with pytest.raises(ValueError, match="incremental external"):
        carry.refinance_quoted(quoted_terms(), 4900, 0, 5000, 0, 0, external_premium_saving=1)


@pytest.mark.parametrize("rate", [None, 0, 20_000_000_000_000_000])
def test_default_forecasts_distinguish_no_age_from_an_observed_rate(rate):
    observed = GlobalReliefRateQuote(7, 0 if rate is None else 100 * 31_557_600,
        0 if rate is None else rate * 100 * 10**36 // 10**18, 0, 0, rate)
    d = SimpleNamespace(net_credit_ops=SimpleNamespace(quote_global_relief_rate=lambda: observed))
    forecast = observe_refinance_forecasts(d)
    assert forecast["observation"]["annual_rate_wad"] == rate
    assert forecast["values"]["expected_relief_rate"] == (0 if rate is None else rate / 10**18)
    assert forecast["values"]["expected_wallet_growth"] == forecast["values"]["expected_refund_growth"]
    assert -.02 < forecast["values"]["expected_wallet_growth"] < 0
    assert forecast["basis"] == ("no-claim-age/zero-relief/idle-cash" if rate is None else "marked-fund/idle-cash")


def test_default_forecast_refuses_a_missing_mark_instead_of_assuming_a_rate():
    def unavailable():
        raise ValueError("guarded fund mark unavailable")
    d = SimpleNamespace(net_credit_ops=SimpleNamespace(quote_global_relief_rate=unavailable))
    with pytest.raises(ValueError, match="guarded fund mark"):
        observe_refinance_forecasts(d)


def test_net_refinance_requires_available_explicit_forecasts():
    p = hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"])
    with pytest.raises(ValueError, match="explicit"):
        hp.terms(FIXTURE["state"] | {"accounting": "net"}, p)
    with pytest.raises(ValueError, match="refund fraction"):
        hp.terms(FIXTURE["state"] | FIXTURE["net_refinance"][0]["state"] | {"premium_refund_fraction": 1.1}, p)
    missing_capacity = FIXTURE["state"] | FIXTURE["net_refinance"][0]["state"]
    del missing_capacity["activated_capacity"]
    with pytest.raises(ValueError, match="activated credit capacity"):
        hp.terms(missing_capacity, p)
    missing_limit = FIXTURE["state"] | FIXTURE["net_refinance"][0]["state"]
    del missing_limit["issuance_limit"]
    with pytest.raises(ValueError, match="issuance limit"):
        hp.terms(missing_limit, p)


@pytest.mark.parametrize("field,value,error", [("expected_relief_rate", -.01, "relief"),
                                                ("expected_refund_growth", -1.01, "growth"),
                                                ("expected_wallet_growth", -1.01, "wallet"),
                                                ("premium_refund_fraction", float("nan"), "fraction"),
                                                ("activated_capacity", -1, "activated credit"),
                                                ("activated_capacity", float("nan"), "activated credit"),
                                                ("issuance_limit", -1, "issuance limit"),
                                                ("issuance_limit", float("nan"), "issuance limit"),
                                                ("expected_relief_rate", True, "relief"),
                                                ("premium_refund_fraction", True, "fraction"),
                                                ("expected_refund_growth", True, "growth"),
                                                ("expected_wallet_growth", True, "wallet"),
                                                ("activated_capacity", True, "activated credit"),
                                                ("issuance_limit", True, "issuance limit"),
                                                ("expected_relief_rate", "0.02", "relief"),
                                                ("expected_wallet_growth", 10**400, "wallet")])
def test_net_refinance_rejects_invalid_forecasts(field, value, error):
    p = hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"])
    with pytest.raises(ValueError, match=error):
        hp.terms(FIXTURE["state"] | FIXTURE["net_refinance"][0]["state"] | {field: value}, p)


def test_net_activated_capacity_is_new_issuance_without_a_second_premium():
    p = hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"] | {"apr": .03, "theta": 3})
    state = FIXTURE["state"] | FIXTURE["net_refinance"][-1]["state"]
    t = hp.terms(state, p)
    assert t.capacity == 100_000 and t.coverage_for(100_000) == 0
    v = carry.refinance(t, 99_000, 100_000)
    assert v.go and v.deposit == 0 and v.parts["relief"] == pytest.approx(2_000)
    kind, decision = hp.issuance(state, p)
    assert kind == "refinance" and decision.go and decision.buck > 0
    assert decision.coverage == decision.deposit == 0
    mixed = hp.terms(state | {"spendable": 20_000, "activated_capacity": 30_000, "unactivated": 1_000_000}, p)
    v = carry.refinance(mixed, 74_000, 75_000)
    assert v.coverage == pytest.approx(25_000 / mixed.spend_per_cover)
    assert v.parts["relief"] == pytest.approx((55_000 + v.deposit) * mixed.expected_relief_rate)
    assert v.parts["wallet_carry"] == pytest.approx(-20_000 * mixed.expected_wallet_growth)


def quoted_terms(case=None):
    p = hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"] | {"apr": .03, "theta": 3})
    state = FIXTURE["state"] | FIXTURE["net_refinance"][-1]["state"] | {
        "spendable": 100_000, "expected_wallet_growth": .1} | (case or {})
    return hp.terms(state, p)


@pytest.mark.parametrize("case", FIXTURE["quoted_refinance"], ids=lambda c: c["name"])
def test_quoted_refinance_prices_selected_wallet_issue_and_reserve(case):
    t = quoted_terms(case.get("state"))
    v = carry.refinance_quoted(t, case["usd"], case["wallet"], case["new_spendable"], case["coverage"],
                                case["premium"], reserve_cost=case.get("reserve_cost", 0),
                                reserve_carry=case.get("reserve_carry", 0),
                                premium_refund_fraction=case.get("premium_refund_fraction"),
                                expected_refund_growth=case.get("expected_refund_growth"))
    assert v.go == case["go"]
    assert v.parts["relief"] == pytest.approx((case["new_spendable"] + case["premium"]) * t.expected_relief_rate)
    assert v.parts["wallet_carry"] == pytest.approx(-case["wallet"] * t.expected_wallet_growth)
    assert v.parts["reserve_cost"] == -case.get("reserve_cost", 0)
    assert v.parts["reserve_carry"] == -case.get("reserve_carry", 0)
    fraction = case.get("premium_refund_fraction", t.premium_refund_fraction)
    growth = case.get("expected_refund_growth", t.expected_refund_growth)
    assert v.parts["deposit_write_down"] == pytest.approx(-case["premium"] * (1 - fraction))
    assert v.parts["deposit_age"] == pytest.approx(case["premium"] * fraction * growth)


@pytest.mark.parametrize("case", FIXTURE["staged_refinance"], ids=lambda c: c["name"])
def test_staged_refinance_retains_all_startup_costs_and_current_credit_bounds(case):
    t = replace(quoted_terms(), fixed_cost=case["fixed"], rate=.06)
    programme = dict(usd=99_000, wallet_used=0, new_spendable=100_000, present_coverage=150_000, premium=10_000)
    tranche = {key: value * case.get("scale", .01) for key, value in programme.items()}
    marginal, whole = carry.staged_refinance(t, tranche, programme)
    assert marginal.go is case["go"]
    assert carry.refinance_quoted(t, **tranche).go is case.get("standalone", False)
    if marginal.go:
        assert -marginal.parts["fixed"] == case["allocated"]
        assert marginal.value * 100 == pytest.approx(whole.value)
        assert -whole.parts["fixed"] == case["fixed"]
    with pytest.raises(ValueError, match="shared issuance limit"):
        carry.staged_refinance(replace(t, issuance_limit=5000), tranche, programme)


def test_quoted_refinance_can_retain_a_valuable_wallet():
    t = quoted_terms()
    old = carry.refinance(t, 99_000, 100_000)
    retain = carry.refinance_quoted(t, 99_000, 0, 100_000, 0, 0)
    assert not old.go and retain.go and retain.value > old.value


def quoted_candidate(case):
    return dict(usd=case["usd"], wallet_used=case["wallet"], new_spendable=case["new_spendable"],
                present_coverage=case["coverage"], premium=case["premium"],
                reserve_cost=case.get("reserve_cost", 0), reserve_carry=case.get("reserve_carry", 0),
                premium_refund_fraction=case.get("premium_refund_fraction"),
                expected_refund_growth=case.get("expected_refund_growth"))


def test_best_quoted_refinance_selects_from_a_feasible_menu_and_breaks_ties():
    candidates = [quoted_candidate(FIXTURE["quoted_refinance"][i]) for i in (0, 1, 2, 5)]
    index, choice = carry.best_quoted_refinance(quoted_terms(), candidates)
    assert index == 1 and choice.go and choice.buck == 100_000
    index, none = carry.best_quoted_refinance(quoted_terms(), candidates[:1])
    assert index is None and not none.go

    t = quoted_terms({"expected_wallet_growth": -.02})
    issue, wallet = candidates[1], candidates[0]
    index, choice = carry.best_quoted_refinance(t, [issue, wallet])
    assert index == 1 and choice.go  # exact tie: existing money adds no debt
    oversized = issue | {"present_coverage": 500}
    index, choice = carry.best_quoted_refinance(t, [oversized, issue])
    assert index == 1 and choice.coverage == 0  # don't activate unused coverage
    pooled = quoted_candidate(FIXTURE["quoted_refinance"][2])
    index, choice = carry.best_quoted_refinance(t, [pooled | {"premium_refund_fraction": .5},
                                                     pooled | {"premium_refund_fraction": 1.}])
    assert index == 1 and choice.go  # compare each pool's own observed refund fraction
    with pytest.raises(ValueError, match="wallet funds"):
        carry.best_quoted_refinance(t, [issue | {"wallet_used": 100_001}])


@pytest.mark.parametrize("overrides,error", [
    ({"usd": 400_001}, "external debt"), ({"wallet_used": 100_001}, "wallet funds"),
    ({"new_spendable": -1}, "invalid"), ({"premium": float("nan")}, "invalid"),
    ({"wallet_used": "100"}, "invalid"), ({"reserve_cost": True}, "invalid"),
    ({"new_spendable": 0, "present_coverage": 1}, "without issuance"),
    ({"reserve_cost": 1}, "without a premium"),
    ({"premium": 1}, "exceeds added coverage"),
    ({"premium_refund_fraction": 1.1}, "refund fraction"),
    ({"expected_refund_growth": -1.1}, "refund growth"),
    ({"premium_refund_fraction": float("nan")}, "refund forecast"),
    ({"new_spendable": 100_000_000_001}, "shared issuance limit"),
])
def test_quoted_refinance_rejects_inconsistent_inputs(overrides, error):
    args = dict(usd=99_000, wallet_used=0, new_spendable=100_000, present_coverage=0, premium=0)
    with pytest.raises(ValueError, match=error):
        carry.refinance_quoted(quoted_terms(), **(args | overrides))


def test_net_shared_limit_includes_the_new_premium_principal():
    p = hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"] | {"apr": .03, "theta": 3})
    state = FIXTURE["state"] | FIXTURE["net_refinance"][-1]["state"] | {
        "spendable": 20_000, "activated_capacity": 30_000, "unactivated": 1_000_000, "issuance_limit": 40_000}
    t = hp.terms(state, p)
    e = carry.deposit_rate(t.premium_bp)
    expected = 20_000 + 30_000 + 10_000 * t.spend_per_cover / (t.spend_per_cover + e)
    assert t.capacity == pytest.approx(expected)
    assert math.isinf(t.coverage_for(60_000))  # the new premium would exceed the shared room
    wallet_only = hp.terms(state | {"issuance_limit": 0}, p)
    assert wallet_only.capacity == 20_000 and wallet_only.coverage_for(20_000) == 0
    assert math.isinf(wallet_only.coverage_for(20_001))


@pytest.mark.parametrize("case", FIXTURE["surplus"])
def test_surplus_cases(case):
    assert hp.surplus_action(*case[:-1]) == case[-1]


def bvib_on(day, series="bvib"):
    """The fixture's scripted basket price in BUCKs (or K): each entry holds from its day on."""
    return [v for start, v in FIXTURE["population"][series] if start <= day][-1]


def arrivals(gain, settings=None, seed=None):
    """Every arrival day by role over the fixture's horizon, for one gain."""
    f                           = FIXTURE["population"]
    p                           = hp.population(f["settings"] | {"gain": gain} | (settings or {}))
    seed                        = FIXTURE["seed"] if seed is None else seed
    state                       = hp.arrival_state(seed, f["name"])
    days                        = {r: [] for r in hp.ROLES}
    for day in range(f["days"]):
        for role, k in hp.arrivals_step(state, seed, f["name"], day, f["days"], bvib_on(day), bvib_on(day, "k"), p):
            assert k == len(days[role])
            days[role].append(day)
    return days


def test_arrivals_are_reproducible_keyed_and_drawn_by_the_price_gap():
    base, drawn                 = arrivals(0.), arrivals(50.)
    assert base == arrivals(0.) and base != arrivals(0., seed=FIXTURE["seed"] + 1)
    for role, rate in (("debtor", 6), ("saver", 6), ("investor", 12)):
        assert .5 * rate * 2 < len(base[role]) < 1.5 * rate * 2, "about the base pace over two years"
    within                      = lambda days, lo, hi: sum(lo <= d < hi for d in days)
    # Dear from day 100 (the 30-day average lags): debtors and savers come faster, investors do not.
    for role in ("debtor", "saver"):
        assert within(drawn[role], 130, 300) > within(base[role], 130, 300)
    assert within(drawn["investor"], 130, 300) == within(base["investor"], 130, 300)
    # Cheap from day 300: investors come faster.
    assert within(drawn["investor"], 330, 500) > within(base["investor"], 330, 500)
    # The same people arrive, only sooner: the k-th arrival is never later with the response.
    for role in hp.ROLES:
        assert all(a <= b for a, b in zip(drawn[role], base[role]))
    # K at 0.9 from day 600: debtors and savers come (1 - 0.75) / (1 - 0.9) = 2.5 times as fast.
    for role in ("debtor", "saver"):
        assert within(base[role], 600, 730) > 1.5 * 6 * 130 / 365.25
    assert hp.k_factor(.75, hp.population({})) == 1 and hp.k_factor(.9, hp.population({})) == pytest.approx(2.5)
    assert hp.k_factor(None, hp.population({})) == 1


def test_population_settings_read_the_older_keys_and_validate():
    p                           = hp.population({})
    assert (p["initial_debtors"], p["initial_savers"], p["debtor_rate"], p["saver_rate"]) == (5, 5, 3., 3.)
    p                           = hp.population({"initial": 24, "arrival_rate": 12, "saver_share": .25})
    assert (p["initial_debtors"], p["initial_savers"], p["debtor_rate"], p["saver_rate"]) == (18, 6, 9., 3.)
    p                           = hp.population({"saver_share": 0, "saver_rate": 2})
    assert (p["initial_debtors"], p["initial_savers"], p["saver_rate"]) == (10, 0, 2.)
    for bad in ({"saver_share": 2}, {"initial_debtors": 1.5}, {"gain_cap": .5}, {"growth": "sideways"},
                {"max_members": 3}, {"investor_rate": -1}):
        with pytest.raises(ValueError):
            hp.population(bad)
    assert [hp.growth_factor(g, 0, 100) for g in hp.GROWTH] == [1., .25, 1.75]
    assert [hp.growth_factor(g, 50, 100) for g in hp.GROWTH] == [1., 1., 1.]


@pytest.mark.parametrize("case", FIXTURE["investor"])
def test_investor_buys_unless_the_buck_is_dear(case):
    assert hp.investor_action(*case[:-1]) == case[-1]


def test_profiles_are_keyed_and_settings_separate():
    profiles                    = [hp.profile(41404, f"0/debtor/{i}") for i in range(40)]
    assert len({p["retire_disc"] for p in profiles}) == 40
    assert len({p["phase"] for p in profiles}) > 8
    p                           = hp.profile(41404, "0/debtor/0")
    extra                       = hp.profile(41404, "0/debtor/0", {"new_knob": [1, 2]})
    assert {k: extra[k] for k in p} == p
    investor                    = hp.profile(41404, "0/investor/0", {"role": "investor"})
    assert 10 <= investor["invest_cash_k"] <= 100 and 3 <= investor["hold_years"] <= 15
    with pytest.raises(ValueError, match="hold_years"):
        hp.profile(41404, "0/investor/0", {"role": "investor", "hold_years": 0})


def test_one_facility_per_asset_and_bounded_asset_portfolios():
    for role in hp.ROLES:
        p = hp.profile(41404, "assets", {"role": role})
        assert p["asset_count"] == (0 if role == "investor" else 1)
    single = hp.profile(41404, "assets")
    portfolio = hp.profile(41404, "assets", {"asset_count": 4})
    assert {k: v for k, v in single.items() if k != "asset_count"} == {k: v for k, v in portfolio.items() if k != "asset_count"}
    for count in (0, -1, 1.5, 33, True, float("nan"), "4"):
        with pytest.raises(ValueError, match="asset_count"):
            hp.profile(41404, "assets", {"asset_count": count})


def test_paydown_urgency_compounds_with_the_discount():
    p                           = hp.profile(41404, "0/debtor/0")
    assert hp.paydown(0, p) == 0 and hp.paydown(-.01, p) == 0
    assert hp.paydown(.005, p) == pytest.approx(.01) and hp.paydown(.01, p) == pytest.approx(.03)
    assert hp.paydown(.02, p) == pytest.approx(.15) and hp.paydown(.04, p) == 1
    assert hp.discount(1., 1., 0) == 0 and hp.discount(.97, 1., 0) == pytest.approx(.03)
    assert hp.discount(1.02, 1., 0) == 0


@pytest.mark.parametrize("case", FIXTURE["saving"])
def test_saving_halves_with_the_premium(case):
    spot, basket, fee, double, share = case
    assert hp.saving(hp.premium(spot, basket, fee), {"paydown_double": double}) == pytest.approx(share, rel=1e-6)


def test_money_target_ramps_with_adoption_and_holds_more_when_cheap():
    p                           = hp.profile(41404, "0/debtor/0")
    spend                       = 120_000.                  # $10k a month
    assert hp.money_target(0, spend, 0, p) == pytest.approx(1_000)          # 10% of a month at arrival
    assert hp.money_target(365.25, spend, 0, p) == pytest.approx(10_000)    # all of it after a year
    assert hp.money_target(3650, spend, 0, p) == pytest.approx(10_000)
    slow                        = hp.profile(41404, "0/debtor/0", {"adopt_years": 3})
    assert hp.money_target(365.25, spend, 0, slow) == pytest.approx(4_000)
    assert hp.money_target(3650, spend, .01, p) == pytest.approx(30_000)    # 1% cheap: 1 + 200 x 0.01
    assert hp.money_target(3650, spend, .1, p) == pytest.approx(80_000)     # capped at 8x


def test_divestment_waits_average_ten_years_at_a_tenth_a_year():
    days                        = [hp.divest_day(41404, f"0/debtor/{i}", 100, .1) - 100 for i in range(2000)]
    assert 9 * 365 < sum(days) / len(days) < 11 * 365
    assert hp.divest_day(41404, "0/debtor/0", 100, .1) == hp.divest_day(41404, "0/debtor/0", 100, .1)
    assert hp.divest_day(41404, "0/debtor/0", 100, 0) is None


def test_premium_sale_keeps_even_last_unit_above_the_premium():
    ru, rb, fee                 = 12e6, 10e6, .003
    b                           = hp.premium_sale(1e6, ru, rb, fee, 1., .02, 5000)
    marginal                    = (ru / rb) * (1 - fee) / (1 + b * (1 - fee) / rb) ** 2
    assert b > 0 and marginal >= 1.02


def test_javascript_and_python_make_the_same_decisions_and_populations():
    script                      = """
import fs from 'node:fs';
import * as hp from './core/js/src/agents/household-policy.js';
const f = JSON.parse(fs.readFileSync('test/fixtures/household-policy.json'));
const at = (series, day) => f.population[series].filter(([start]) => start <= day).at(-1)[1];
const arrivals = (gain) => {
 const p = hp.population({...f.population.settings, gain});
 const state = hp.arrivalState(f.seed, f.population.name), days = {debtor: [], saver: [], investor: []};
 for (let day = 0; day < f.population.days; day++)
  for (const [role] of hp.arrivalsStep(state, f.seed, f.population.name, day, f.population.days,
                                       at("bvib", day), at("k", day), p))
   days[role].push(day);
 return days;
};
console.log(JSON.stringify({
 profiles: Array.from({length: 8}, (_, i) => hp.profile(f.seed, `0/debtor/${i}`)),
 investors: Array.from({length: 4}, (_, i) => hp.profile(f.seed, `0/investor/${i}`, {role: 'investor'})),
 arrivals: [arrivals(0), arrivals(50)],
 population: hp.population({initial: 24, arrival_rate: 12, saver_share: .25}),
 divest: Array.from({length: 8}, (_, i) => hp.divestDay(f.seed, `0/debtor/${i}`, 30, .1)),
 decisions: f.issuance.map(c => hp.issuance({...f.state, ...c.state}, hp.profile(f.seed, '0', {...f.settings, ...c.settings}))),
 wants: f.issuance.map(c => hp.investmentWant({...f.state, ...c.state}, hp.profile(f.seed, '0', {...f.settings, ...c.settings}))),
 quotedInvestment: f.quoted_refinance.filter(c => c.new_spendable > 0).map(c => hp.investmentQuoted(hp.terms(
     {...f.state, ...f.net_refinance.at(-1).state, mortgage: 0, spendable: 0, expected_wallet_growth: .1, ...(c.state ?? {})},
     hp.profile(f.seed, '0', {...f.settings, apr: .03, theta: 3}), true), c.usd, c.new_spendable, c.coverage, c.premium,
     {reserveCost: c.reserve_cost ?? 0, reserveCarry: c.reserve_carry ?? 0,
       premiumRefundFraction: c.premium_refund_fraction ?? null, expectedRefundGrowth: c.expected_refund_growth ?? null})),
 netRefinance: f.net_refinance.map(c => hp.refinance(hp.terms({...f.state, ...c.state},
     hp.profile(f.seed, '0', {...f.settings, apr: .03, theta: 3})), c.usd, c.buck)),
 quotedRefinance: f.quoted_refinance.map(c => hp.refinanceQuoted(hp.terms({...f.state, ...f.net_refinance.at(-1).state,
     spendable: 100000, expected_wallet_growth: .1, ...(c.state ?? {})},
     hp.profile(f.seed, '0', {...f.settings, apr: .03, theta: 3})), c.usd, c.wallet, c.new_spendable,
     c.coverage, c.premium, {reserveCost: c.reserve_cost ?? 0, reserveCarry: c.reserve_carry ?? 0,
       premiumRefundFraction: c.premium_refund_fraction ?? null, expectedRefundGrowth: c.expected_refund_growth ?? null})),
 quotedSelection: hp.bestQuotedRefinance(hp.terms({...f.state, ...f.net_refinance.at(-1).state,
     spendable: 100000, expected_wallet_growth: .1}, hp.profile(f.seed, '0', {...f.settings, apr: .03, theta: 3})),
     [0, 1, 2, 5].map(i => {const c = f.quoted_refinance[i]; return {usd: c.usd, wallet_used: c.wallet,
       new_spendable: c.new_spendable, present_coverage: c.coverage, premium: c.premium,
       reserve_cost: c.reserve_cost ?? 0, reserve_carry: c.reserve_carry ?? 0};}))
}));
"""
    js                          = json.loads(subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                                                           check=True, text=True, capture_output=True).stdout)
    assert js["arrivals"] == [arrivals(0.), arrivals(50.)]
    assert js["population"] == hp.population({"initial": 24, "arrival_rate": 12, "saver_share": .25})
    assert js["divest"] == [hp.divest_day(FIXTURE["seed"], f"0/debtor/{i}", 30, .1) for i in range(8)]
    for i, p in enumerate(js["profiles"]):
        assert p == pytest.approx(hp.profile(FIXTURE["seed"], f"0/debtor/{i}"))
    for i, p in enumerate(js["investors"]):
        assert p == pytest.approx(hp.profile(FIXTURE["seed"], f"0/investor/{i}", {"role": "investor"}))
    for case, (jkind, jv) in zip(FIXTURE["issuance"], js["decisions"]):
        kind, verdict           = hp.issuance(FIXTURE["state"] | case["state"], hp.profile(
            FIXTURE["seed"], "0", FIXTURE["settings"] | case["settings"]))
        pv                      = asdict(verdict)
        assert kind == jkind
        assert jv.pop("parts") == pytest.approx(pv.pop("parts"), rel=1e-12, abs=1e-8)
        assert jv == pytest.approx(pv, rel=1e-12, abs=1e-8)
    wants = [hp.investment_want(FIXTURE["state"] | case["state"], hp.profile(
        FIXTURE["seed"], "0", FIXTURE["settings"] | case["settings"])) for case in FIXTURE["issuance"]]
    assert [w[1] for w in wants] == [w[1] for w in js["wants"]] and {w[1] for w in wants} > {"hold"}
    assert [w[0] for w in js["wants"]] == pytest.approx([w[0] for w in wants], rel=1e-12, abs=1e-8)
    # The same quoted candidates, kept rather than sold: no external debt bounds them.
    quoted = [c for c in FIXTURE["quoted_refinance"] if c["new_spendable"] > 0]
    assert quoted and len(js["quotedInvestment"]) == len(quoted)
    for case, jv in zip(quoted, js["quotedInvestment"]):
        p = hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"] | {"apr": .03, "theta": 3})
        state = FIXTURE["state"] | FIXTURE["net_refinance"][-1]["state"] | dict(
            mortgage=0, spendable=0, expected_wallet_growth=.1) | case.get("state", {})
        t = hp.terms(state, p, investment=True)
        assert t.debt == 0 and t.rate == 0
        pv = asdict(carry.investment_quoted(t, case["usd"], case["new_spendable"], case["coverage"], case["premium"],
            reserve_cost=case.get("reserve_cost", 0), reserve_carry=case.get("reserve_carry", 0),
            premium_refund_fraction=case.get("premium_refund_fraction"),
            expected_refund_growth=case.get("expected_refund_growth")))
        assert pv["parts"]["interest"] == pv["parts"]["penalty"] == pv["parts"]["wallet_carry"] == 0
        assert jv.pop("parts") == pytest.approx(pv.pop("parts"), rel=1e-12, abs=1e-8)
        assert jv == pytest.approx(pv, rel=1e-12, abs=1e-8)
    with pytest.raises(ValueError, match="investment"):
        carry.investment_quoted(quoted_terms(), 1., 1., 1., 0.)  # refinance terms save interest; not an investment
    for case, jv in zip(FIXTURE["net_refinance"], js["netRefinance"]):
        p = hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"] | {"apr": .03, "theta": 3})
        pv = asdict(carry.refinance(hp.terms(FIXTURE["state"] | case["state"], p), case["usd"], case["buck"]))
        assert jv.pop("parts") == pytest.approx(pv.pop("parts"), rel=1e-12, abs=1e-8)
        assert jv == pytest.approx(pv, rel=1e-12, abs=1e-8)
    for case, jv in zip(FIXTURE["quoted_refinance"], js["quotedRefinance"]):
        pv = asdict(carry.refinance_quoted(quoted_terms(case.get("state")), case["usd"], case["wallet"],
            case["new_spendable"], case["coverage"], case["premium"], reserve_cost=case.get("reserve_cost", 0),
            reserve_carry=case.get("reserve_carry", 0), premium_refund_fraction=case.get("premium_refund_fraction"),
            expected_refund_growth=case.get("expected_refund_growth")))
        assert jv.pop("parts") == pytest.approx(pv.pop("parts"), rel=1e-12, abs=1e-8)
        assert jv == pytest.approx(pv, rel=1e-12, abs=1e-8)
    index, verdict = carry.best_quoted_refinance(quoted_terms(),
        [quoted_candidate(FIXTURE["quoted_refinance"][i]) for i in (0, 1, 2, 5)])
    assert js["quotedSelection"]["index"] == index
    pv = asdict(verdict)
    jv = js["quotedSelection"]["verdict"]
    assert jv.pop("parts") == pytest.approx(pv.pop("parts"), rel=1e-12, abs=1e-8)
    assert jv == pytest.approx(pv, rel=1e-12, abs=1e-8)
INVEST                          = json.loads((ROOT / "test/fixtures/household-invest.json").read_text())


def invest_terms(**changes):
    """A mortgage-free household's investment terms at K = 0.75: no interest saved, no penalty."""
    state = dict(accounting="net", mortgage=0., face=1_000_000., joined=False, closing_paid=False, k=.75, spendable=0.,
                 drawn=0., unactivated=1_000_000., activated_capacity=0., issuance_limit=600_000., at_renewal=False,
                 ru=1_000_000., rb=1_000_000., fee=.003, basket_usd=1., premium_refund_fraction=1.,
                 **INVEST["forecasts"]) | changes
    p = hp.profile(FIXTURE["seed"], "0", INVEST["settings"])
    return state, p, hp.terms(state, p, investment=True)


def test_staged_investment_allocates_the_closing_cost_across_the_whole_line():
    spec = INVEST["programme"]
    _, p, t = invest_terms()
    assert t.fixed_cost == spec["fixed_cost"] == p["switch_k"] * 1000 and t.rate == t.penalty_months == 0
    alone, whole = carry.investment_quoted(t, **spec["tranche"]), carry.investment_quoted(t, **spec["whole"])
    assert not alone.go and alone.carry > 0 and whole.go, "one bite cannot repay the closing fee; the line can"
    marginal, programme = carry.staged_investment(t, spec["tranche"], spec["whole"])
    assert programme == whole and marginal.go
    allocated = spec["fixed_cost"] * spec["tranche"]["usd"] / spec["whole"]["usd"]
    assert marginal.parts["fixed"] == pytest.approx(-allocated) and marginal.carry == pytest.approx(alone.carry)
    assert marginal.value == pytest.approx(alone.value + spec["fixed_cost"] - allocated)
    # Nothing to allocate once the closing is paid; a bite larger than its line or an unworthy line is refused.
    assert carry.staged_investment(replace(t, fixed_cost=0.), spec["tranche"], spec["whole"])[0] is carry.NO
    assert carry.staged_investment(t, spec["whole"], spec["tranche"])[0] is carry.NO
    costly = replace(t, fixed_cost=100_000.)
    assert carry.staged_investment(costly, spec["tranche"], spec["whole"]) == (carry.NO, carry.investment_quoted(costly, **spec["whole"]))
    with pytest.raises(ValueError, match="investment"):
        carry.staged_investment(quoted_terms(), spec["tranche"], spec["whole"])  # refinance terms are not an investment


def test_investment_programme_is_the_whole_carry_line_and_only_the_bite_at_a_premium():
    state, p, t = invest_terms()
    bite, why = hp.investment_want(state, p)
    line = state["face"] * state["k"] * p["carry_utilization"]
    assert why == "carry" and bite == pytest.approx(state["rb"] * p["max_impact_bp"] / 20_000 * .999)
    whole = hp.investment_programme(state, p)
    assert whole == pytest.approx(min(t.capacity, line) * .999) and whole > 40 * bite
    drawn = state | {"drawn": line - 1_000.}
    assert hp.investment_want(drawn, p) == (pytest.approx(999.), "carry")
    assert hp.investment_programme(drawn, p) == hp.investment_want(drawn, p)[0], "the last bite is the whole line"
    dear = state | {"basket_usd": .9}
    assert hp.investment_want(dear, p)[1] == "premium"
    assert hp.investment_programme(dear, p) == hp.investment_want(dear, p)[0], "a premium is a passing state"
    off = dict(p) | {"carry_utilization": 0.}
    assert hp.investment_want(state, off) == (0., "hold") and hp.investment_programme(state, off) == 0.


def test_investor_tolerance_is_a_keyed_bounded_normal_identical_in_both_languages():
    members = [f"0/investor/{i}" for i in range(400)]
    draws = [hp.profile(FIXTURE["seed"], m, {"role": "investor"})["bvib_tolerance"] for m in members]
    assert all(0. <= x <= .02 for x in draws)
    mean = sum(draws) / len(draws)
    sd = (sum((x - mean) ** 2 for x in draws) / len(draws)) ** .5
    assert abs(mean - .01) < .0006 and .004 < sd < .0055, (mean, sd)
    assert 0 < sum(x in (0., .02) for x in draws) < 30, "about 2.3% of members sit at each bound"
    # Keyed by member and name alone: another setting's override does not move it; a scalar pins it.
    assert hp.profile(FIXTURE["seed"], members[0], {"cash_k": 1.})["bvib_tolerance"] == draws[0]
    assert hp.profile(FIXTURE["seed"], members[0], {"bvib_tolerance": .015})["bvib_tolerance"] == .015
    assert hp.normal(1, "m", "k", .5, 0., 0., 1.) == .5
    for bad in ({"mean": .01, "sd": -1., "lo": 0., "hi": .02}, {"mean": .01, "sd": .005, "lo": 0.}, -.01, 1.):
        with pytest.raises(ValueError):
            hp.profile(FIXTURE["seed"], members[0], {"bvib_tolerance": bad})
    script = """
import * as hp from './core/js/src/agents/household-policy.js';
const [seed, members] = [%d, %s];
console.log(JSON.stringify(members.map(m => hp.profile(seed, m, {role: 'investor'}).bvib_tolerance)));
""" % (FIXTURE["seed"], json.dumps(members))
    js = json.loads(subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True, text=True,
                                   capture_output=True).stdout)
    assert js == draws, "the same doubles, not merely close"


@pytest.mark.parametrize("bvib,tolerance,action", [
    (10**18, 0., "invest"), (10**18 + 1, 0., "wait"), (101 * 10**16, .01, "invest"), (101 * 10**16 + 1, .01, "wait"),
    (98 * 10**16, 0., "invest"), (12 * 10**17, .02, "wait"), (0, .01, "invest")])
def test_investor_invests_within_its_bvib_tolerance_and_waits_above_it(bvib, tolerance, action):
    assert hp.net_investor_action(bvib, tolerance) == action
    script = "import * as hp from './core/js/src/agents/household-policy.js';" \
             f"console.log(hp.netInvestorAction({bvib}n, {tolerance!r}));"
    assert subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True, text=True,
                          capture_output=True).stdout.strip() == action
    for bad in ((-1, .01), (10**18, -.01), (10**18, 1.), (1.0, .01)):
        with pytest.raises(ValueError):
            hp.net_investor_action(*bad)


def test_javascript_investment_programme_and_staged_score_match_python():
    state, p, t = invest_terms()
    drawn = state | {"drawn": state["face"] * state["k"] * p["carry_utilization"] - 1_000.}
    dear = state | {"basket_usd": .9}
    script = """
import fs from 'node:fs';
import * as hp from './core/js/src/agents/household-policy.js';
const [state, p, spec] = JSON.parse(fs.readFileSync(0, 'utf8'));
const t = hp.terms(state, p, true);
console.log(JSON.stringify({
 programmes: [state, {...state, drawn: state.face * state.k * p.carry_utilization - 1000}, {...state, basket_usd: .9}]
   .map(s => [hp.investmentProgramme(s, p), ...hp.investmentWant(s, p)]),
 staged: hp.stagedInvestment(t, spec.tranche, spec.whole),
 paid: hp.stagedInvestment({...t, fixed_cost: 0}, spec.tranche, spec.whole).verdict.go,
}));
"""
    js = json.loads(subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True, text=True,
                                   capture_output=True, input=json.dumps([state, p, INVEST["programme"]])).stdout)
    for s, (programme, bite, why) in zip((state, drawn, dear), js["programmes"]):
        assert (programme, bite) == pytest.approx((hp.investment_programme(s, p), hp.investment_want(s, p)[0]), rel=1e-12)
        assert why == hp.investment_want(s, p)[1]
    marginal, whole = carry.staged_investment(t, INVEST["programme"]["tranche"], INVEST["programme"]["whole"])
    for jv, pv in ((js["staged"]["verdict"], asdict(marginal)), (js["staged"]["programme"], asdict(whole))):
        assert jv.pop("parts") == pytest.approx(pv.pop("parts"), rel=1e-12, abs=1e-8)
        assert jv == pytest.approx(pv, rel=1e-12, abs=1e-8)
    assert js["paid"] is False


def test_net_surplus_guards_actual_purchase_cost_and_credit_capital():
    import json
    from pathlib import Path
    from fractions import Fraction
    from buck_core import NetMarketQuote
    from alberta_buck.sim import household_policy as policy
    f = json.loads((Path(__file__).resolve().parents[2] / "test/fixtures/household-surplus.json").read_text())
    for c in f["cases"]:
        q = NetMarketQuote(**{k: int(v) for k, v in (f["market"] | c.get("market", {})).items()})
        args = {k: int(v) for k, v in (f["amounts"] | c.get("amounts", {})).items()}
        p = policy.profile(41404, "surplus-vector", f["settings"] | dict(surplus_policy=c["policy"]))
        r = policy.net_surplus_purchase(q, **args, p=p)
        assert r["action"] == c["action"], c["name"]
        if "spend" in c:
            assert r["spend_usdc"] == int(c["spend"]), c["name"]
        if "target" in c:
            assert r["target_principal"] == int(c["target"]), c["name"]
        if "spend_between" in c:
            assert int(c["spend_between"][0]) <= r["spend_usdc"] <= int(c["spend_between"][1])
        assert r["spend_usdc"] <= max(0, args["cash"] - args["cash_floor"])
        assert 0 <= r["target_principal"] <= args["issued"]
        if r["spend_usdc"]:
            assert r["expected_net"] == q.buy_expected(r["spend_usdc"]) >= r["minimum_net"] > 0
            if r["action"] == "retire" and c["policy"] != "retire":
                # Independent rational cash/mark comparison at the GUARDED
                # minimum, not the nominal pool output or its spot price.
                price = Fraction(r["spend_usdc"], r["minimum_net"])
                ceiling = Fraction(args["fair_num"], args["fair_den"]) * Fraction(99, 100)
                assert price < ceiling


def test_each_household_keeps_one_keyed_day_of_the_month_for_its_payday():
    import json
    from pathlib import Path
    from alberta_buck.sim import household_policy as policy
    f = json.loads((Path(__file__).resolve().parents[2] / "test/fixtures/household-calendar.json").read_text())["paydays"]
    for arrive, pay_day, first in f["cases"]:
        assert policy.first_payday(arrive, pay_day) == first
        assert arrive < first <= arrive + 30 and first % 30 == pay_day
    for bad in ((-1, 0), (0, 30), (0, -1), (0, 1.0), (1.0, 0)):
        with pytest.raises(ValueError, match="pay day"):
            policy.first_payday(*bad)
    days = [policy.profile(41404, f"payday/{i}")["pay_day"] for i in range(200)]
    assert all(type(day) is int and 0 <= day < 30 for day in days)
    assert len(set(days)) == 30, "a cohort's paydays spread over the whole month"
    assert max(days.count(day) for day in range(30)) < 20
    assert days == [policy.profile(41404, f"payday/{i}")["pay_day"] for i in range(200)], "keyed, so reproducible"
    assert policy.profile(41404, "payday/0", {"pay_day": 7})["pay_day"] == 7
    assert policy.profile(41404, "payday/0", {"pay_day": 7}) | {"pay_day": days[0]} == policy.profile(41404, "payday/0"), \
        "pinning the payday moves no other draw"
    for bad in (30, -1, float("nan"), "7"):
        with pytest.raises(ValueError, match="pay_day"):
            policy.profile(41404, "payday/0", {"pay_day": bad})


def test_net_wallet_takes_one_capped_bite_toward_its_money_target():
    import json
    from pathlib import Path
    from buck_core import NetMarketQuote
    from alberta_buck.sim import household_policy as policy
    f = json.loads((Path(__file__).resolve().parents[2] / "test/fixtures/household-wallet.json").read_text())
    for c in f["cases"]:
        q = NetMarketQuote(**{k: int(v) for k, v in (f["market"] | c.get("market", {})).items()})
        args = {k: int(v) for k, v in (f["amounts"] | c.get("amounts", {})).items()}
        r = policy.net_wallet_adjustment(q, **args, p=f["settings"])
        assert (r["action"], r["reason"]) == (c["action"], c["reason"]), c["name"]
        assert r["spend_usdc"] == int(c.get("spend", 0)) and r["minimum_net"] == int(c.get("minimum_net", 0)), c["name"]
        assert r["sell_buck"] == int(c.get("sell", 0)) and r["minimum_usdc"] == int(c.get("minimum_usdc", 0)), c["name"]
        # Independent bounds: never the protected dollars, never past the
        # target, never beyond half the impact allowance, never under a unit.
        assert r["spend_usdc"] <= max(0, args["cash"] - args["cash_floor"])
        assert r["spend_usdc"] <= q.reserve_usdc * 5000 // 10**6 and r["sell_buck"] <= q.reserve_buck * 5000 // 10**6
        assert not (r["spend_usdc"] and r["sell_buck"])
        if r["action"] == "buy":
            assert args["held"] * 100 < args["target"] * 95 and 10**6 <= r["minimum_net"] <= r["expected_net"]
            assert r["expected_net"] == q.buy_expected(r["spend_usdc"]) <= args["target"] - args["held"] + 10**6
        elif r["action"] == "sell":
            assert args["held"] * 100 > args["target"] * 110 and r["sell_buck"] <= args["held"] - args["target"]
            assert 10**6 <= r["minimum_usdc"] <= r["expected_usdc"] == q.sell_expected(r["sell_buck"])
        else:
            assert not r["spend_usdc"] and not r["sell_buck"]
    for bad in (dict(cash=-1), dict(held=1.0), dict(target=2**256)):
        with pytest.raises(ValueError, match="invalid net wallet"):
            policy.net_wallet_adjustment(q, **(args | bad), p=f["settings"])
    # The target every runtime must size identically from one observation.
    t = f["targets"]
    for c in t["cases"]:
        ratio = None if c["ratio"] is None else int(c["ratio"])
        assert policy.net_wallet_target(ratio, c["days"], float(t["annual_spending"]), t["settings"]) == int(c["target"]), c["name"]
    with pytest.raises(ValueError, match="ratio"):
        policy.net_wallet_target(1.0, 30, 60000., t["settings"])


def test_the_lifecycle_works_retires_and_draws_down():
    """age 40 at arrival day 100: works until 65, draws down over 25 years, a month of what is left
    each payday, all of it in the last month; outside the lifecycle (age 0) nothing changes."""
    p = hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"] | {"age": 40.})
    assert (p["retire_age"], p["drawdown_years"], p["usd_fund_share"], p["usd_fund_return"]) == (65., 25., 0., .08)
    year = hp.YEAR_DAYS
    assert hp.age_on(p, 100, 100) == 40 and hp.age_on(p, 100, 100 + int(10 * year)) == pytest.approx(50, abs=.01)
    assert hp.stage(p, 100, 100) == "working" and hp.drawdown_share(p, 100, 100) == 0
    retired = 100 + int(25 * year) + 1
    assert hp.stage(p, 100, retired) == "retired"
    assert hp.drawdown_share(p, 100, retired) == pytest.approx(30 / (25 * year), rel=1e-3)
    assert hp.drawdown_share(p, 100, 100 + int(50 * year) - 10) == 1
    assert hp.stage(p, 100, 100 + int(50 * year) + 1) == "done"
    off = hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"])
    assert hp.age_on(off, 0, 1000) is None and hp.stage(off, 0, 1000) is None and hp.drawdown_share(off, 0, 1000) == 0
    assert hp.fund_growth(.08, hp.YEAR_DAYS) == pytest.approx(1.08) and hp.fund_growth(.08, 0) == 1
    for bad in ({"retire_age": 0.}, {"drawdown_years": 0.}, {"usd_fund_share": 1.5}, {"usd_fund_return": -1.},
                {"age": -1.}):
        with pytest.raises(ValueError):
            hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"] | bad)
