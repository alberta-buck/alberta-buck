"""The work wheel's chassis (alberta_buck/sim/work_wheel.py) and the closed
form its consistency arbitrage is sized by (alberta_buck/sim/wheel_tasks.py);
doc/BASKET-WHEEL.org."""
import pytest

from alberta_buck.sim.wheel_tasks import apply, compose, leg, optimum
from alberta_buck.sim.work_wheel import (Clock, ChainProfile, RewardReserve,
                                         TaskResult, WheelTask, WorkWheel)


class Fake(WheelTask):
    """n slots; slot i is due while pending[i] > 0; a run decrements it."""
    def __init__(self, kind, n, value=0.0, share=0.0, fund_frac=0.0, gas=1000):
        self.kind, self.n, self.v = kind, n, value
        self.share, self.fund_frac, self.gas = share, fund_frac, gas
        self.pending = [0] * n
        self.ran = []

    def slots(self, d):
        return self.n

    def due(self, d, i, clk):
        return self.pending[i] > 0

    def run(self, d, i, clk):
        self.pending[i] -= 1
        self.ran.append(i)
        return TaskResult(work=1, value=self.v, gas=self.gas)

    def estimate(self, i):
        return self.v


def wheel(*tasks, **kw):
    w = WorkWheel(list(tasks), ChainProfile(gwei=1.0), **kw)
    w.bind(None)
    return w


C0 = Clock(0, 0)
C1 = Clock(0, 1)


# -- the chassis ------------------------------------------------------------------- #

def test_round_robin_and_budget():
    a = Fake("a", 3)
    a.pending = [1, 1, 1]
    w = wheel(a)
    rc = w.tick(None, C0, max_work=2)
    assert a.ran == [0, 1] and rc.work == 2 and not rc.idle
    rc = w.tick(None, C0, max_work=2)
    assert a.ran == [0, 1, 2] and rc.work == 1


def test_idle_block_is_one_read_until_rearmed():
    a = Fake("a", 2)
    w = wheel(a)
    rc = w.tick(None, C0)
    assert rc.idle and w.idle_block == C0.block
    a.pending = [1, 0]
    assert w.tick(None, C0).idle          # same block: the memo, not a scan
    assert a.ran == []
    w.mark_dirty()                        # a basket-touching trade landed
    assert w.tick(None, C0).work == 1
    assert w.tick(None, C1).idle          # next block: scanned, nothing due


def test_kinds_compose_into_one_table():
    a, b = Fake("a", 2), Fake("b", 1)
    a.pending, b.pending = [0, 1], [1]
    w = wheel(a, b)
    assert len(w._table) == 3
    assert w.pending(None, C0) == 2
    w.tick(None, C0, max_work=5)
    assert a.ran == [1] and b.ran == [0]


def test_value_is_shared_funded_and_retained():
    arb = Fake("arb", 1, value=100.0, share=0.10, fund_frac=0.05)
    arb.pending = [1]
    w = wheel(arb, reserve=RewardReserve(kappa=0.5))
    rc = w.tick(None, C0)
    # the caller: 10% of the value, then kappa of the reserve the run funded
    assert rc.pay == pytest.approx(10.0 + 0.5 * 5.0)
    assert w.retained == pytest.approx(85.0)
    assert w.reserve.balance == pytest.approx(2.5)
    assert w.ledger["arb"].paid == pytest.approx(10.0)


def test_reserve_builds_when_under_called_and_pays_less_when_over_called():
    """The design owner's gas offset: kappa of the balance per working tick."""
    up = Fake("upkeep", 1)
    w = wheel(up, reserve=RewardReserve(kappa=0.1, cap=1e9))
    # under-called: yield accrues, nobody works -> the pile and the next pay grow
    for _ in range(10):
        w.reserve.fund(10.0)
    assert w.reserve.balance == pytest.approx(100.0)
    up.pending = [1]
    assert w.tick(None, C0).pay == pytest.approx(10.0)
    # over-called: several working ticks between fundings -> each pays less
    pays = []
    for t in range(4):
        up.pending = [1]
        pays.append(w.tick(None, Clock(1, t)).pay)
    assert all(p2 < p1 for p1, p2 in zip(pays, pays[1:]))
    # an idle tick is paid nothing
    assert w.tick(None, Clock(2, 0)).pay == 0


def test_reserve_cap_spills_to_depositors():
    r = RewardReserve(cap=50.0)
    r.fund(80.0)
    assert r.balance == 50.0 and r.spilled == 30.0


def test_quote_is_what_the_tick_pays():
    arb = Fake("arb", 2, value=40.0, share=0.25)
    arb.pending = [1, 1]
    w = wheel(arb, reserve=RewardReserve(kappa=0.0))
    pay, gas = w.quote(None, C0, max_work=2)
    assert pay == pytest.approx(20.0) and gas == 2 * arb.gas
    assert w.tick(None, C0, max_work=2).pay == pytest.approx(pay)


def test_chain_profile_prices_gas():
    assert ChainProfile(gwei=5.0, eth_usd=3000).usd(420_000) == pytest.approx(6.3)
    assert ChainProfile(gwei=0.01, eth_usd=3000).usd(420_000) == pytest.approx(0.0126)


# -- the closed-form cycle --------------------------------------------------------- #

def test_mobius_legs_compose():
    l1, l2, l3 = leg(1e6, 2e6, 0.003), leg(5e5, 7e5, 0.003), leg(3e6, 1.1e6, 0.0005)
    m = compose(compose(l1, l2), l3)
    for x in (1.0, 1e3, 5e4):
        assert apply(m, x) == pytest.approx(apply(l3, apply(l2, apply(l1, x))))


def test_optimum_is_the_peak():
    m = compose(compose(leg(1e6, 1e6, 0.003), leg(1e6, 1.02e6, 0.003)),
                leg(1e6, 1e6, 0.0005))
    x, p = optimum(m)
    assert x > 0 and p > 0
    for dx in (-0.01 * x, 0.01 * x):
        assert apply(m, x + dx) - (x + dx) < p
    assert optimum(compose(leg(1e6, 1e6, 0.003), leg(1e6, 1e6, 0.003))) == (0.0, 0.0)


def _triangle(gap, own_fee):
    """BUCK -> TOKEN (the basket's pool) -> USDC -> BUCK, deep pools, the
    USD venues pricing TOKEN `gap` above the basket's pool."""
    r = 1e9
    return compose(compose(leg(r, r, own_fee), leg(r, r * (1 + gap), 0.003)),
                   leg(r, r, 0.0005))


def test_the_fee_band_only_the_basket_can_take():
    """A 0.5% gap: inside 0.35-0.65%.  The basket, whose own-pool fee comes
    back to it, profits; a searcher paying all three fees does not."""
    basket = optimum(_triangle(0.005, own_fee=0.0))
    searcher = optimum(_triangle(0.005, own_fee=0.003))
    assert basket[1] > 0 and searcher == (0.0, 0.0)
    # below the basket's own cost nobody profits; above 0.65% both do
    assert optimum(_triangle(0.003, own_fee=0.0)) == (0.0, 0.0)
    assert optimum(_triangle(0.008, own_fee=0.003))[1] > 0


def test_the_scan_is_amortized_across_callers():
    """Examining a slot is the expensive part on chain (three slot0 reads for
    an arb).  With max_scan, several callers in a block share one scan, and
    only when every slot has been seen idle is the block memoized."""
    a = Fake("a", 4)
    w = wheel(a)
    rc = w.tick(None, C0, max_scan=2)
    assert rc.idle and w.idle_block is None          # half the wheel seen
    rc = w.tick(None, C0, max_scan=2)
    assert w.idle_block == C0.block                  # all four seen idle
    a.pending = [0, 0, 1, 0]
    assert w.tick(None, C0, max_scan=2).idle          # memoized until re-armed
    w.mark_dirty()
    rc = w.tick(None, C0, max_scan=4)
    assert rc.work == 1 and a.ran == [2]
