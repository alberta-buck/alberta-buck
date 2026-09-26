"""The decision kernel (alberta_buck/sim/carry.py): the carry comparison the
wave-4 households decide by, starting with T15's refinance leg."""
import math

import pytest

from alberta_buck.sim import carry


def terms(**kw):
    base = dict(debt=400_000.0, rate=0.055, face=900_000.0, premium_bp=35,
                joined=False, payback_y=2.0)
    base.update(kw)
    return carry.RefiTerms(**base)


def test_deposit_is_pool_roi_inv_years_of_premium():
    # Buck.sol: take = amount * BP / (BP - rate * 10); principal = take - amount
    amount = 100_000.0
    take = amount * 10_000 / (10_000 - 35 * carry.POOL_ROI_INV)
    assert carry.deposit_per_buck(35) * amount == pytest.approx(take - amount)
    assert carry.deposit_per_buck(0) == 0.0
    assert math.isinf(carry.deposit_per_buck(1_000))


def test_pool_quote_inverts():
    ru, rb, fee = 5e6, 5e6, 0.003
    b = carry.buck_in(250_000, ru, rb, fee)
    assert carry.usdc_out(b, ru, rb, fee) == pytest.approx(250_000)
    assert math.isinf(carry.buck_in(ru, ru, rb, fee))


def test_insurance_is_an_outlay_not_a_cost():
    """master fe3083f: the BUCK path's insurance deposit is returned when the
    insurance is dropped, so it is charged its opportunity (and the carried
    age, net of its own relief), never its face; joining stops the external
    premium, a real cost."""
    t = terms()
    v = carry.refinance(t, 400_000, 404_000)
    dep = v.deposit
    assert dep == pytest.approx(404_000 * carry.deposit_per_buck(35))
    assert v.parts["premium"] == pytest.approx(900_000 * 0.0035)
    assert v.parts["deposit_opp"] == pytest.approx(-dep * 0.055)
    assert v.parts["deposit_age"] == pytest.approx(-dep * carry.DEMURRAGE)
    # the insurance leg as a whole favours the BUCK path at a 5.5% opportunity
    ins = (v.parts["premium"] + v.parts["deposit_opp"]
           + v.parts["deposit_age"] + dep * carry.RELIEF)
    assert ins > 0
    # Drawing the whole face, the BUCK path's insurance is the cheaper one
    # below an opportunity of (1 - 10p) / POOL_ROI_INV (9.65% at 0.35%);
    # drawing less, the deposit is smaller and the edge wider.
    p = t.premium_bp / 1e4
    edge = (1 - carry.POOL_ROI_INV * p) / carry.POOL_ROI_INV

    def ins_at(opp, buck):
        w = carry.refinance(terms(opp_rate=opp), buck, buck)
        return w.parts["premium"] + w.parts["deposit_opp"]

    assert ins_at(edge - 1e-4, t.face) > 0 > ins_at(edge + 1e-4, t.face)
    assert ins_at(edge + 1e-4, t.face / 2) > 0


def test_a_marginal_refinance_goes_only_when_the_deposit_is_an_outlay():
    """The old accounting charged the deposit as a loss.  Pick a household
    whose one-time costs leave it just short under that accounting: under
    the fair one it refinances."""
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


def test_no_carry_no_go():
    v = carry.refinance(terms(rate=0.001, risk=0.05, premium_bp=0), 100_000,
                        100_000)
    assert v.carry < 0 and not v.go


def test_partial_refinance_answers_capacity():
    """K sets what a household can draw.  Below its debt it retires what it
    can and keeps the rest external; more capacity, more retired -- the
    flow that answers K."""
    t = terms(debt=600_000.0, payback_y=3.0, fixed_cost=2_000.0)
    ru = rb = 50e6
    fee = 0.003
    small = carry.best_refinance(t, 200_000, ru, rb, fee)
    large = carry.best_refinance(t, 400_000, ru, rb, fee)
    full = carry.best_refinance(t, 2_000_000, ru, rb, fee)
    assert small.go and large.go and full.go
    assert small.usd < large.usd < full.usd
    assert full.usd == pytest.approx(600_000)
    # the draw and its deposit fit the capacity
    assert small.buck + small.deposit <= 200_000


def test_depth_caps_the_sale():
    t = terms(debt=5_000_000.0, payback_y=3.0)
    v = carry.best_refinance(t, 1e9, 2e6, 2e6, 0.003)
    assert v.usd <= 0.5 * 2e6 + 1e-6


def test_why_is_rounded_dollars():
    v = carry.refinance(terms(), 400_000, 404_000)
    w = v.why()
    assert all(isinstance(x, int) for x in w.values())
    assert w["usd"] == 400_000 and w["buck"] == 404_000
