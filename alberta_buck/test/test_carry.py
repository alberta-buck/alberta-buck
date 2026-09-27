"""The decision kernel (alberta_buck/sim/carry.py): the carry comparison the
wave-4 households decide by, starting with T15's refinance leg."""
import math

import pytest

from alberta_buck.sim import carry


def terms(**kw):
    base = dict(debt=400_000.0, rate=0.055, face=900_000.0, premium_bp=35,
                joined=False, payback_y=2.0, k=0.75, headroom=0.0,
                unactivated=900_000.0)
    base.update(kw)
    return carry.RefiTerms(**base)


def test_coverage_is_buck_sol_arithmetic():
    """A unit of coverage adds K - e of spendable (the limit rises K, the
    deposit e is debited from the same balance); a draw inside existing
    headroom needs none."""
    t = terms(k_margin=1.0)
    e = carry.deposit_rate(35)
    assert e == pytest.approx(0.035)
    assert t.coverage_for(100_000) == pytest.approx(100_000 / (0.75 - e))
    assert t.capacity == pytest.approx(900_000 * (0.75 - e))
    h = terms(k_margin=1.0, headroom=60_000)
    assert h.coverage_for(50_000) == 0
    assert h.coverage_for(100_000) == pytest.approx(40_000 / (0.75 - e))
    assert math.isinf(terms(k=0.03, k_margin=1.0).coverage_for(1))


def test_pool_quote_inverts():
    ru, rb, fee = 5e6, 5e6, 0.003
    b = carry.buck_in(250_000, ru, rb, fee)
    assert carry.usdc_out(b, ru, rb, fee) == pytest.approx(250_000)
    assert math.isinf(carry.buck_in(ru, ru, rb, fee))


def test_insurance_is_an_outlay_not_a_cost():
    """master fe3083f: the BUCK path's deposit is returned when the insurance
    is dropped, so it is charged its opportunity and carried age, never its
    face; joining stops the external premium, a real cost."""
    t = terms(k_margin=1.0)
    v = carry.refinance(t, 400_000, 404_000)
    cov = 404_000 / (0.75 - 0.035)
    assert v.coverage == pytest.approx(cov)
    assert v.deposit == pytest.approx(cov * 0.035)
    assert v.parts["premium"] == pytest.approx(900_000 * 0.0035)
    assert v.parts["relief"] == pytest.approx(cov * carry.RELIEF)
    assert v.parts["deposit_opp"] == pytest.approx(-v.deposit * 0.055)
    assert v.parts["deposit_age"] == pytest.approx(-v.deposit * carry.DEMURRAGE)
    # the insurance leg as a whole favours the BUCK path at a 5.5% opportunity
    ins = sum(v.parts[k] for k in ("premium", "relief", "deposit_opp", "deposit_age"))
    assert ins > 0


def test_whole_face_insurance_breaks_even_at_one_over_pool_roi_inv():
    """With the whole face activated the deposit is 10 years' premium on the
    face: the external premium p F against the opportunity 10 p F x opp."""
    edge = 1.0 / carry.POOL_ROI_INV
    for bp in (25, 35, 50):
        def ins(opp):
            t = terms(premium_bp=bp, opp_rate=opp, k_margin=1.0)
            # the draw that activates exactly the whole face
            v = carry.refinance(t, 1.0, 900_000 * t.spend_per_cover)
            assert v.coverage == pytest.approx(900_000)
            return v.parts["premium"] + v.parts["deposit_opp"]
        assert ins(edge - 1e-4) > 0 > ins(edge + 1e-4)


def test_a_marginal_refinance_goes_only_when_the_deposit_is_an_outlay():
    """The old accounting charged the deposit as a loss.  A household whose
    one-time costs leave it short under that accounting refinances under
    the fair one."""
    t = terms(premium_bp=50, payback_y=0.6, fixed_cost=3_000.0,
              penalty_months=3.0, debt=150_000.0)
    v = carry.refinance(t, 150_000, 153_000)
    assert v.go
    as_cost = v.value - v.deposit      # the deposit written off at once
    assert as_cost < 0


def test_reduces_to_the_theta_law(monkeypatch):
    """Every term but the interest zeroed: go iff (B - A) < theta * A * apr."""
    monkeypatch.setattr(carry, "RELIEF", 0.0)
    monkeypatch.setattr(carry, "DEMURRAGE", 0.0)
    t = terms(premium_bp=0, payback_y=2.0, joined=True)
    usd = 100_000.0
    edge = usd + 2.0 * usd * 0.055          # B at which value == 0
    assert carry.refinance(t, usd, edge - 1).go
    assert not carry.refinance(t, usd, edge + 1).go


def test_penalty_only_off_renewal():
    at = carry.refinance(terms(penalty_months=0.0), 200_000, 201_000)
    off = carry.refinance(terms(penalty_months=3.0), 200_000, 201_000)
    assert at.parts["penalty"] == 0
    assert off.parts["penalty"] == pytest.approx(-200_000 * 0.055 * 3 / 12)
    assert off.value == pytest.approx(at.value + off.parts["penalty"])


def test_joined_household_saves_no_further_premium():
    v = carry.refinance(terms(joined=True), 100_000, 100_500)
    assert v.parts["premium"] == 0


def test_a_top_up_inside_headroom_makes_no_deposit():
    v = carry.refinance(terms(joined=True, headroom=200_000), 100_000, 100_500)
    assert v.coverage == 0 and v.deposit == 0 and v.parts["relief"] == 0


def test_no_carry_no_go():
    v = carry.refinance(terms(rate=0.001, risk=0.05, premium_bp=0), 100_000,
                        100_000)
    assert v.carry < 0 and not v.go


def test_partial_refinance_answers_k():
    """K sets what a household can draw.  Below its debt it retires what it
    can and keeps the rest external; a higher K, more retired -- the flow
    that answers K."""
    ru = rb = 50e6
    fee = 0.003
    got = [carry.best_refinance(terms(debt=700_000.0, payback_y=3.0,
                                      fixed_cost=2_000.0, k=k), ru, rb, fee)
           for k in (0.55, 0.65, 0.75)]
    assert all(v.go for v in got)
    assert got[0].usd < got[1].usd < got[2].usd
    assert got[2].usd < 700_000             # 900k x (0.7425 - 0.035) < 700k
    full = carry.best_refinance(terms(debt=700_000.0, payback_y=3.0, k=0.95),
                                ru, rb, fee)
    assert full.usd == pytest.approx(700_000)
    # the draw and its deposit fit the coverage available
    assert got[0].coverage <= 900_000 * (1 + 1e-9)


def test_depth_caps_the_sale():
    t = terms(debt=5_000_000.0, payback_y=3.0, face=10e6, unactivated=10e6)
    v = carry.best_refinance(t, 2e6, 2e6, 0.003)
    assert v.usd <= 0.5 * 2e6 + 1e-6


def test_why_is_rounded_dollars():
    v = carry.refinance(terms(), 400_000, 404_000)
    w = v.why()
    assert all(isinstance(x, int) for x in w.values())
    assert w["usd"] == 400_000 and w["buck"] == 404_000
