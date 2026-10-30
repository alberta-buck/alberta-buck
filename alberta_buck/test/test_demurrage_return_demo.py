"""The 50-year return comparison must remain closed and explicitly funded."""

import math

import pytest

from alberta_buck.sim.jubilee_return_demo import compare_fund_returns


def test_no_return_and_funded_two_percent_return_have_same_gross_demurrage():
    rows = compare_fund_returns()
    assert [row.years for row in rows] == [0, 10, 20, 25, 30, 40, 50]
    for row in rows:
        assert row.supply == 100
        assert row.gross_demurrage == pytest.approx(2 * row.years)
        assert row.no_return_holder + row.no_return_fund == pytest.approx(100)
        assert row.return_case_holder + row.return_case_fund == pytest.approx(100)
        assert row.return_case_fund == pytest.approx(2 * row.years, abs=1e-7)
    final = rows[-1]
    assert final.no_return_holder == pytest.approx(100 * math.exp(-1))
    assert final.no_return_fund == pytest.approx(100 * (1 - math.exp(-1)))
    assert final.no_return_fund_nominal == pytest.approx(100)
    assert final.return_case_holder == pytest.approx(0, abs=1e-7)
    assert final.return_case_fund == pytest.approx(100, abs=1e-7)
    assert final.return_transferred == pytest.approx(50, abs=0.01)


@pytest.mark.parametrize("principal, steps", [(0, 365), (100, 0), (-1, 1)])
def test_invalid_simulation_parameters_rejected(principal, steps):
    with pytest.raises(ValueError):
        compare_fund_returns(principal, steps)
