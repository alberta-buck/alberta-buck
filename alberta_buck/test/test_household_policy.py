"""Shared examples, economic boundaries, and Python/JavaScript conformance."""
from dataclasses import asdict
import json
from pathlib import Path
import subprocess

import pytest

from alberta_buck.sim import household_policy as hp

ROOT                            = Path(__file__).resolve().parents[2]
FIXTURE                         = json.loads((ROOT / "test/fixtures/household-policy.json").read_text())


@pytest.mark.parametrize("case", FIXTURE["issuance"], ids=lambda c: c["name"])
def test_issuance_cases(case):
    p                           = hp.profile(FIXTURE["seed"], "0", FIXTURE["settings"] | case["settings"])
    kind, v                     = hp.issuance(FIXTURE["state"] | case["state"], p)
    assert (kind, v.go) == (case["kind"], case["go"])
    if v.go:
        assert v.value > 0 and v.carry > 0


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
 decisions: f.issuance.map(c => hp.issuance({...f.state, ...c.state}, hp.profile(f.seed, '0', {...f.settings, ...c.settings})))
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
