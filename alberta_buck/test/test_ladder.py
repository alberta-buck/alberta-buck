"""Unit tests for the weak-side ladder (WAVE3.org WP-2, L1)."""

import math

import pytest

from alberta_buck.sim.ladder import Ladder

EPS, DELTA, PHI, R0 = 0.03, 0.01, 0.25, 1_000_000.0


def _ladder(**kw):
    args = dict(eps=EPS, delta=DELTA, phi=PHI, R0=R0)
    args.update(kw)
    return Ladder(**args)


def test_schedule_construction_and_validation():
    L = _ladder()
    assert L.tranche == 0 and L.rho == 1.0 and L.R == R0
    assert L.bid() == pytest.approx(1 - EPS)
    assert L.tranche_size() == pytest.approx(PHI * R0)
    assert L.capacity() == pytest.approx(PHI * R0)
    # p scales eps (the single-token convenience path).
    assert _ladder(p=2.0).bid() == pytest.approx(1 - 2 * EPS)
    for bad in (dict(phi=0.0), dict(phi=1.5), dict(R0=0.0), dict(delta=-1e-3),
                dict(eps=-0.01), dict(p=0.0), dict(eps=0.6, p=2.0)):
        with pytest.raises(ValueError):
            _ladder(**bad)


def test_bid_steps_by_delta():
    L = _ladder()
    for k in range(6):
        assert L.bid(k) == pytest.approx(1 - EPS - k * DELTA)
    # Filling exactly one tranche advances the ladder by one step.
    for k in range(1, 5):
        buck, avg, done = L.fill(L.tranche_size())
        assert done == 1
        assert L.tranche == k
        assert L.bid() == pytest.approx(1 - EPS - k * DELTA)
        assert avg == pytest.approx(1 - EPS - (k - 1) * DELTA)


def test_geometric_depletion_never_zero_at_finite_fills():
    L = _ladder()
    for n in range(1, 13):
        L.fill(L.tranche_size())
        assert L.R == pytest.approx(R0 * (1 - PHI) ** n, rel=1e-9)
        assert L.rho > 0.0
        assert L.tranche == n
    # The schedule ends at a finite PRICE, with reserve still in the book.
    L2 = _ladder()
    buck, avg, done = L2.fill(1e6 * R0)
    assert L2.exhausted and L2.bid() <= 0.0
    assert L2.R > 0.0 and L2.rho > 0.0
    assert math.isinf(L2.edge_bvib())
    assert L2.capacity() == 0.0
    # Tranches completed == the number of positive bids on the schedule.
    n_pos = sum(1 for k in range(10_000) if 1 - EPS - k * DELTA > 1e-12)
    assert done == n_pos
    assert L2.spent == pytest.approx(R0 * (1 - (1 - PHI) ** n_pos), rel=1e-9)


def test_delta_zero_is_the_flat_edge():
    L = _ladder(delta=0.0)
    assert L.capacity() == pytest.approx(R0)          # all reserve at one price
    assert L.edge_bvib() == pytest.approx(1 / (1 - EPS))
    buck, avg, done = L.fill(0.9 * R0)
    assert avg == pytest.approx(1 - EPS)              # one price throughout
    assert buck == pytest.approx(0.9 * R0 / (1 - EPS))
    assert L.bid() == pytest.approx(1 - EPS)           # no step, however deep
    assert L.edge_bvib() == pytest.approx(1 / (1 - EPS))
    assert done >= 1 and L.rho == pytest.approx(0.1)
    assert L.capacity() == pytest.approx(L.R)


def test_fill_accounting_sums_over_tranches():
    L = _ladder()
    fills = [0.1 * R0, 0.05 * R0, 0.3 * R0, 0.02 * R0, 0.25 * R0]
    tot_spent = tot_buck = 0.0
    for a in fills:
        buck, avg, done = L.fill(a)
        tot_spent += a
        tot_buck += buck
        assert avg == pytest.approx(a / buck)
        assert L.spent == pytest.approx(tot_spent)
        assert L.bought == pytest.approx(tot_buck)
        assert L.R == pytest.approx(R0 - tot_spent)
    # Independent recomputation: walk the schedule tranche by tranche.
    R, k, buck = R0, 0, 0.0
    left = tot_spent
    while left > 1e-9:
        size = PHI * R
        take = min(left, size)
        buck += take / (1 - EPS - k * DELTA)
        left -= take
        if take >= size * (1 - 1e-12):
            R -= size
            k += 1
        else:
            R -= take
    assert tot_buck == pytest.approx(buck, rel=1e-9)
    assert L.tranche == k
    # A fill inside a tranche leaves its remainder; the next fill takes it.
    rem = L.tranche_remaining()
    assert 0.0 < rem < L.tranche_size()
    buck, avg, done = L.fill(rem)
    assert done == 1 and L.tranche == k + 1


def test_rho_monotone_and_reset():
    L = _ladder()
    last = L.rho
    for a in (0.01, 0.2, 0.0, 0.4, 0.05):
        L.fill(a * R0)
        assert L.rho <= last + 1e-15
        assert L.rho == pytest.approx(1 - L.spent / R0)
        last = L.rho
    assert 0.0 < L.rho < 1.0 and L.tranche > 0
    L.reset()
    assert L.rho == 1.0 and L.tranche == 0 and L.spent == 0.0
    assert L.bid() == pytest.approx(1 - EPS)
    assert L.fill(0.0) == (0.0, 0.0, 0)
    assert L.fill(-1.0) == (0.0, 0.0, 0)


def test_edge_bvib_is_the_inverse_bid_and_rises_with_the_tranche():
    L = _ladder()
    last = 0.0
    for k in range(8):
        assert L.edge_bvib() == pytest.approx(1 / (1 - EPS - k * DELTA))
        assert L.edge_bvib() > last
        last = L.edge_bvib()
        L.fill(L.tranche_size())
    # p = 2 widens the first edge to 1 / (1 - 2 eps).
    assert _ladder(p=2.0).edge_bvib() == pytest.approx(1 / (1 - 2 * EPS))


def test_cpmm_limit_on_a_fine_grid():
    # delta, phi -> 0 at fixed gamma = delta / phi: the marginal bid tends
    # to b(rho) = b0 + gamma ln(rho); with gamma = 2 b0 that is the
    # constant-product marginal bid b0 rho^2 to first order in the
    # depletion.  Check the ladder against both on a fine grid.
    b0 = 1 - EPS
    phi = 1e-3
    gamma = 2 * b0
    L = Ladder(eps=EPS, delta=gamma * phi, phi=phi, R0=R0)
    step = 1e-3 * R0
    last_bid = L.bid()
    while L.rho > 0.9:
        L.fill(step)
        rho = L.rho
        b = L.bid()
        assert b <= last_bid + 1e-12                   # monotone, and ...
        assert last_bid - b <= gamma * phi * 2 + 1e-12  # ... no cliff
        last_bid = b
        # Within one tranche step of the continuous curve (the bid is the
        # CURRENT tranche's, struck one step ago).
        assert b == pytest.approx(b0 + gamma * math.log(rho),
                                  abs=2 * gamma * phi + 1e-9)
        if rho >= 0.95:
            assert abs(b - b0 * rho ** 2) / b0 < 0.01
