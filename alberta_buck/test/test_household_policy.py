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


def test_population_arrivals_and_knobs_are_reproducible_and_separate():
    days                        = hp.arrivals(41404, 0, 1827)
    assert days[:10] == [0] * 10
    assert days == sorted(days) and max(days) > 1500
    assert days == hp.arrivals(41404, 0, 1827)
    assert days != hp.arrivals(41405, 0, 1827)
    profiles                    = [hp.profile(41404, f"0/{i}") for i in range(40)]
    assert len({p["retire_disc"] for p in profiles}) == 40
    assert len({p["phase"] for p in profiles}) > 8
    p                           = hp.profile(41404, "0/0")
    extra                       = hp.profile(41404, "0/0", {"new_knob": [1, 2]})
    assert {k: extra[k] for k in p} == p
    with pytest.raises(ValueError, match="max_members"):
        hp.arrivals(41404, 0, 1827, initial=10, arrival_rate=100, max_members=12)


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
console.log(JSON.stringify({
 profiles: Array.from({length: 8}, (_, i) => hp.profile(f.seed, `0/${i}`)),
 arrivals: hp.arrivals(f.seed, 0, 1827),
 decisions: f.issuance.map(c => hp.issuance({...f.state, ...c.state}, hp.profile(f.seed, '0', {...f.settings, ...c.settings})))
}));
"""
    js                          = json.loads(subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                                                           check=True, text=True, capture_output=True).stdout)
    assert js["arrivals"] == hp.arrivals(FIXTURE["seed"], 0, 1827)
    for i, p in enumerate(js["profiles"]):
        assert p == pytest.approx(hp.profile(FIXTURE["seed"], f"0/{i}"))
    for case, (jkind, jv) in zip(FIXTURE["issuance"], js["decisions"]):
        kind, verdict           = hp.issuance(FIXTURE["state"] | case["state"], hp.profile(
            FIXTURE["seed"], "0", FIXTURE["settings"] | case["settings"]))
        pv                      = asdict(verdict)
        assert kind == jkind
        assert jv.pop("parts") == pytest.approx(pv.pop("parts"), rel=1e-12, abs=1e-8)
        assert jv == pytest.approx(pv, rel=1e-12, abs=1e-8)
