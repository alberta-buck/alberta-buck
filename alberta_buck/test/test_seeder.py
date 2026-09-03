"""Unit tests for the treasury seeder's pure logic (WAVE3.org WP-8, L1).

The orientation checklist of alberta-buck-operations.org, pinned for BOTH
token orderings of the BUCK/USDC pool: the range from current toward ideal
must be funded with BUCK when BUCK/USD must rise and with USDC when it
must fall, whichever token is token0.
"""

import math

import pytest

from alberta_buck.sim.seeder_agent import (
    E6, E18, Q96, bp_to_ticks, corrective_side, depth_usd6, ideal_buck_usd,
    seeder_range, single_sided_liquidity, sqrt_at_tick, sqrt_price_to_usd6,
    tick_to_usd6, usd6_to_tick,
)

SPACING = 10          # the BUCK/USDC pool's 0.05% tier


# -- ideal ----------------------------------------------------------------- #

def _basket_amount(weight_18: int, p0_usd6: int) -> int:
    """What BuckBasketProRata.addBasketToken stores: weightUnit x 1e18 /
    initialPriceInBuck, with the init price as deploy.py passes it -- the
    6-dec p0 (raw BUCK per whole token; 1 BUCK == 1 USDC at t0)."""
    return weight_18 * E18 // p0_usd6


def test_ideal_is_the_bundle_at_par():
    # Two tokens at weights 60% / 40%, init prices $1.20 and $0.20 per
    # whole token: 0.5 whole of the first, 2 whole of the second.
    p0 = [1_200_000, 200_000]
    amounts = [_basket_amount(6 * 10 ** 17, p0[0]),
               _basket_amount(4 * 10 ** 17, p0[1])]
    assert amounts[0] == 5 * 10 ** 29 and amounts[1] == 2 * 10 ** 30
    # At p0 the bundle is exactly one BUCK, whatever the tokens' decimals.
    assert ideal_buck_usd(amounts, p0) == 1_000_000
    assert ideal_buck_usd(amounts, p0, decimals=[18, 8]) == 1_000_000
    # Prices move: the ideal moves with them, linearly.
    assert ideal_buck_usd(amounts, [1_320_000, 200_000]) == 1_060_000
    assert ideal_buck_usd(amounts, [1_200_000, 100_000]) == 800_000
    # The live deployment's scale: an 18-dec token at $0.29 with a 24%
    # weight is ~8.2e29 -- the number the units were verified against.
    ba = _basket_amount(24 * 10 ** 16, 291_961)
    assert 8.2e29 < ba < 8.3e29
    # basketAmount is a floor, so the round trip lands a unit under.
    assert 239_999 <= ideal_buck_usd([ba], [291_961]) <= 240_000


def test_ideal_buck_dec_scales_the_raw_unit():
    p0 = [1_000_000]
    amounts = [_basket_amount(E18, p0[0])]
    assert ideal_buck_usd(amounts, p0, buck_dec=6) == 10 ** 6
    assert ideal_buck_usd(amounts, p0, buck_dec=18) == 10 ** 18


def test_ideal_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        ideal_buck_usd([E18], [E6, E6])
    with pytest.raises(ValueError):
        ideal_buck_usd([E18], [E6], decimals=[18, 18])


# -- ticks and prices ------------------------------------------------------ #

@pytest.mark.parametrize("b0", [True, False])
def test_tick_zero_is_par_in_both_orientations(b0):
    assert usd6_to_tick(E6, b0) == 0
    assert tick_to_usd6(0, b0) == E6
    assert sqrt_price_to_usd6(Q96, b0) == E6


def test_tick_direction_flips_with_orientation():
    # BUCK dearer ($1.05): a HIGHER pool price when BUCK is token0 (USDC per
    # BUCK), a LOWER one when BUCK is token1 (BUCK per USDC).
    assert usd6_to_tick(1_050_000, True) > 0
    assert usd6_to_tick(1_050_000, False) < 0
    assert usd6_to_tick(950_000, True) < 0
    assert usd6_to_tick(950_000, False) > 0
    # ... and by (almost exactly) the same number of ticks.
    up, dn = usd6_to_tick(1_050_000, True), usd6_to_tick(1_050_000, False)
    assert abs(up + dn) <= 1


@pytest.mark.parametrize("b0", [True, False])
@pytest.mark.parametrize("usd6", [900_000, 990_000, 1_000_000, 1_010_000,
                                  1_100_000])
def test_tick_round_trip(b0, usd6):
    t = usd6_to_tick(usd6, b0)
    # A tick is a floor, so the recovered price sits within one tick below.
    lo = tick_to_usd6(t, b0)
    hi = tick_to_usd6(t + 1, b0)
    assert min(lo, hi) <= usd6 <= max(lo, hi)


def test_bp_to_ticks_is_a_log():
    assert bp_to_ticks(100) == 99           # ln(1.01)/ln(1.0001)
    assert bp_to_ticks(900) == 861          # ln(1.09)/ln(1.0001) = 861.8
    assert bp_to_ticks(1) == 1


# -- the range: orientation, alignment, bounds ----------------------------- #

def _check_geometry(rng_, cur, ideal, spacing):
    lo, hi, _ = rng_
    assert lo % spacing == 0 and hi % spacing == 0
    assert hi - lo >= spacing
    if ideal > cur:
        assert lo > cur                     # strictly above: token0 only
        assert hi <= ideal                  # never beyond ideal
    else:
        assert hi <= cur                    # at/below the tick: token1 only
        assert lo >= ideal


@pytest.mark.parametrize("cur", [-7, -1, 0, 3, 9, 10, 123])
@pytest.mark.parametrize("gap", [25, 40, 100, 350])
def test_buck_must_rise_funded_with_buck_both_orientations(cur, gap):
    """ideal above current in USD terms -> BUCK funds, in either ordering."""
    # BUCK token0: USD rising == tick rising.
    r0 = seeder_range(cur, cur + gap, SPACING, buck_is_token0=True)
    assert r0 is not None and r0[2] == "buck"
    _check_geometry(r0, cur, cur + gap, SPACING)
    assert r0[0] > cur                                  # above: token0 = BUCK
    # BUCK token1: the same USD move is a FALLING pool price.
    r1 = seeder_range(cur, cur - gap, SPACING, buck_is_token0=False)
    assert r1 is not None and r1[2] == "buck"
    _check_geometry(r1, cur, cur - gap, SPACING)
    assert r1[1] <= cur                                 # below: token1 = BUCK


@pytest.mark.parametrize("cur", [-7, -1, 0, 3, 9, 10, 123])
@pytest.mark.parametrize("gap", [25, 40, 100, 350])
def test_buck_must_fall_funded_with_usdc_both_orientations(cur, gap):
    """ideal below current in USD terms -> USDC funds, in either ordering."""
    r0 = seeder_range(cur, cur - gap, SPACING, buck_is_token0=True)
    assert r0 is not None and r0[2] == "usdc"
    _check_geometry(r0, cur, cur - gap, SPACING)
    assert r0[1] <= cur                                 # below: token1 = USDC
    r1 = seeder_range(cur, cur + gap, SPACING, buck_is_token0=False)
    assert r1 is not None and r1[2] == "usdc"
    _check_geometry(r1, cur, cur + gap, SPACING)
    assert r1[0] > cur                                  # above: token0 = USDC


@pytest.mark.parametrize("b0", [True, False])
def test_side_agrees_with_usd_prices_end_to_end(b0):
    """From USD prices through ticks to the funding side, both orderings."""
    for cur_usd, ideal_usd in [(980_000, 1_000_000), (1_000_000, 1_030_000),
                               (1_020_000, 1_000_000), (1_000_000, 960_000)]:
        ct, it = usd6_to_tick(cur_usd, b0), usd6_to_tick(ideal_usd, b0)
        rng_ = seeder_range(ct, it, SPACING, b0)
        assert rng_ is not None
        assert rng_[2] == corrective_side(ideal_usd, cur_usd)
        # The range lies between current and ideal in USD terms too.
        usd_lo = tick_to_usd6(rng_[0], b0)
        usd_hi = tick_to_usd6(rng_[1], b0)
        lo_usd, hi_usd = min(usd_lo, usd_hi), max(usd_lo, usd_hi)
        assert min(cur_usd, ideal_usd) <= lo_usd
        assert hi_usd <= max(cur_usd, ideal_usd)


def test_no_range_when_it_cannot_fit():
    assert seeder_range(0, 0, SPACING, True) is None
    assert seeder_range(0, 5, SPACING, True) is None        # < one spacing
    assert seeder_range(0, 19, SPACING, True) is None       # lo 10, hi 10
    assert seeder_range(0, 20, SPACING, True) == (10, 20, "buck")
    # Below is asymmetric: tickUpper may EQUAL the current tick (the pool
    # funds token1 only when slot0.tick >= tickUpper), so -19 already fits.
    assert seeder_range(0, -9, SPACING, True) is None       # hi 0, lo 0
    assert seeder_range(0, -19, SPACING, True) == (-10, 0, "usdc")
    assert seeder_range(0, -20, SPACING, True) == (-20, 0, "usdc")
    assert seeder_range(9, 20, SPACING, False) == (10, 20, "usdc")
    with pytest.raises(ValueError):
        seeder_range(0, 100, 0, True)


def test_corrective_side():
    assert corrective_side(1_010_000, 1_000_000) == "buck"
    assert corrective_side(990_000, 1_000_000) == "usdc"
    assert corrective_side(E6, E6) == ""


# -- sizing and depth ------------------------------------------------------ #

def _amount0(L, lo, hi):
    sa, sb = sqrt_at_tick(lo), sqrt_at_tick(hi)
    return L * (sb - sa) * Q96 // (sa * sb)


def _amount1(L, lo, hi):
    sa, sb = sqrt_at_tick(lo), sqrt_at_tick(hi)
    return L * (sb - sa) // Q96


@pytest.mark.parametrize("lo,hi", [(10, 20), (10, 100), (-300, -10), (50, 400)])
def test_single_sided_liquidity_consumes_at_most_the_budget(lo, hi):
    budget = 15_000_000 * E6
    La = single_sided_liquidity(lo, hi, budget, above=True)
    Lb = single_sided_liquidity(lo, hi, budget, above=False)
    assert La > 0 and Lb > 0
    assert 0.99 * budget <= _amount0(La, lo, hi) <= budget
    assert 0.99 * budget <= _amount1(Lb, lo, hi) <= budget
    # The narrower the range, the more L the same budget buys.
    assert single_sided_liquidity(lo, lo + SPACING, budget, True) > La or \
        hi == lo + SPACING


def test_single_sided_liquidity_degenerate():
    assert single_sided_liquidity(20, 10, E6, True) == 0
    assert single_sided_liquidity(10, 20, 0, True) == 0


def test_depth_is_L_at_par_either_orientation():
    L = 50_000_000 * E6
    assert depth_usd6(L, Q96, usdc_is_token0=True) == L
    assert depth_usd6(L, Q96, usdc_is_token0=False) == L
    # Off par the two orientations move in opposite directions, as the
    # USDC-side reserve of a full-range position does.
    sp = sqrt_at_tick(200)
    assert depth_usd6(L, sp, True) < L < depth_usd6(L, sp, False)
    assert depth_usd6(0, Q96, True) == 0


def test_sqrt_at_tick_matches_definition():
    assert sqrt_at_tick(0) == Q96
    assert math.isclose(sqrt_at_tick(2) / Q96, 1.0001, rel_tol=1e-12)
