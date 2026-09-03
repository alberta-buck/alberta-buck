"""Unit tests for the facility's pure decision rule (WAVE3.org WP-6, L1).

facility_decision(signal, x, y, drawn, limit, max_frac, open_position,
headroom=None) -> ("issue" | "retire" | "hold", size).  bvib < 1 is BUCK
dear (issue side); bvib > 1 is BUCK cheap (retire side).
"""

import pytest

from alberta_buck.sim.facility_agent import (
    HOLD, ISSUE, RETIRE, facility_decision,
)

M = 1_000_000 * 10 ** 6          # $1M in 6-dec


def test_issue_on_premium_below_cap():
    # BUCK dear (0.95 < 1 - 0.03), nothing drawn: issue up to max_frac*limit.
    act, size = facility_decision(0.95, 0.03, 0.03, 0, 4 * M, 0.5, False)
    assert act == ISSUE
    assert size == 2 * M


def test_issue_size_is_room_to_cap():
    act, size = facility_decision(0.95, 0.03, 0.03, 1 * M, 4 * M, 0.5, False)
    assert act == ISSUE
    assert size == 1 * M                     # 0.5 * 4M - 1M


def test_issue_capped_by_headroom():
    act, size = facility_decision(0.95, 0.03, 0.03, 0, 4 * M, 0.5, False,
                                  headroom=300_000 * 10 ** 6)
    assert act == ISSUE
    assert size == 300_000 * 10 ** 6


def test_hold_at_cap_even_on_premium():
    act, size = facility_decision(0.90, 0.03, 0.03, 2 * M, 4 * M, 0.5, False)
    assert (act, size) == (HOLD, 0)
    # ... and above it (K fell and the cap shrank under the draw).
    act, size = facility_decision(0.90, 0.03, 0.03, 3 * M, 4 * M, 0.5, False)
    assert (act, size) == (HOLD, 0)


def test_hold_when_headroom_is_zero():
    act, size = facility_decision(0.95, 0.03, 0.03, 0, 4 * M, 0.5, False,
                                  headroom=0)
    assert (act, size) == (HOLD, 0)


def test_retire_on_discount_when_drawn():
    act, size = facility_decision(1.05, 0.03, 0.03, 1_500_000 * 10 ** 6,
                                  4 * M, 0.5, True)
    assert act == RETIRE
    assert size == 1_500_000 * 10 ** 6       # the whole draw


def test_retire_with_open_position_but_nothing_drawn():
    # A receipt still open (e.g. the line was repaid from held BUCK):
    # retire fires with size 0 -- redeem / close only.
    act, size = facility_decision(1.05, 0.03, 0.03, 0, 4 * M, 0.5, True)
    assert (act, size) == (RETIRE, 0)


def test_hold_on_discount_with_nothing_to_retire():
    act, size = facility_decision(1.05, 0.03, 0.03, 0, 4 * M, 0.5, False)
    assert (act, size) == (HOLD, 0)


@pytest.mark.parametrize("signal", [0.97, 0.98, 1.0, 1.02, 1.03])
def test_deadband_holds_both_ways(signal):
    # Inside [1 - x, 1 + y] nothing fires, drawn or not, receipt or not.
    for drawn in (0, 1 * M):
        for open_pos in (False, True):
            act, size = facility_decision(signal, 0.03, 0.03, drawn, 4 * M,
                                          0.5, open_pos)
            assert (act, size) == (HOLD, 0)


def test_thresholds_are_strict():
    x = y = 0.03
    assert facility_decision(1 - x, x, y, 0, 4 * M, 0.5, False)[0] == HOLD
    assert facility_decision(1 - x - 1e-9, x, y, 0, 4 * M, 0.5, False)[0] == ISSUE
    assert facility_decision(1 + y, x, y, 1 * M, 4 * M, 0.5, False)[0] == HOLD
    assert facility_decision(1 + y + 1e-9, x, y, 1 * M, 4 * M, 0.5, False)[0] == RETIRE


def test_asymmetric_thresholds():
    # x tight, y wide: a 2% premium issues, a 2% discount holds.
    assert facility_decision(0.98, 0.01, 0.05, 0, 4 * M, 0.5, False)[0] == ISSUE
    assert facility_decision(1.02, 0.01, 0.05, 1 * M, 4 * M, 0.5, False)[0] == HOLD
    assert facility_decision(1.06, 0.01, 0.05, 1 * M, 4 * M, 0.5, False)[0] == RETIRE


def test_max_frac_scales_the_cap():
    act, size = facility_decision(0.95, 0.03, 0.03, 0, 4 * M, 1.0, False)
    assert (act, size) == (ISSUE, 4 * M)
    act, size = facility_decision(0.95, 0.03, 0.03, 0, 4 * M, 0.0, False)
    assert (act, size) == (HOLD, 0)


def test_negative_drawn_is_clamped():
    # A positive signed balance (held BUCK) is not a negative draw.
    act, size = facility_decision(1.05, 0.03, 0.03, -5 * M, 4 * M, 0.5, False)
    assert (act, size) == (HOLD, 0)
    act, size = facility_decision(0.95, 0.03, 0.03, -5 * M, 4 * M, 0.5, False)
    assert (act, size) == (ISSUE, 2 * M)


def test_sizes_are_ints():
    act, size = facility_decision(0.95, 0.03, 0.03, 0, 4 * M + 1, 0.5, False)
    assert isinstance(size, int)
