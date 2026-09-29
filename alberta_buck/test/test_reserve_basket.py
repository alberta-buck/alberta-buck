"""The BUCK basis for the equity BuckBasket (alberta_buck/sim/reserve_basket.py):
one test per property doc/BASKET-EQUITY.org section 12 promises."""
import math

import pytest

from alberta_buck.sim.basket_harvest import run
from alberta_buck.sim.equity_basket import BUCK, ME, Pool
from alberta_buck.sim.reserve_basket import (BandDirector, External, ReserveBasket,
                                             TurnDirector, arbitrage,
                                             reserve_wheel_tasks)
from alberta_buck.sim.work_wheel import Clock, WorkWheel

K = 0.75


def world(prices=(1.0, 2.0, 3.0), fee=0.003, cost=0.001, seed=1e5, arb_band=None,
          director=None, **kw):
    """Pools seeded by an outside LP, the outside market at the same prices,
    the basket and its wheel (a block is a day)."""
    pools, ref = {}, {}
    for n, p in enumerate(prices):
        pool = Pool(p, fee)
        pool.add("seed", seed / p, seed)
        pools[f"T{n}"] = pool
        ref[f"T{n}"] = p
    ext = External(ref, cost)
    b = ReserveBasket(pools, ext=ext, K=K, director=director, **kw)
    w = WorkWheel(reserve_wheel_tasks(arb_band))
    w.bind(b)
    return b, w, ext


class Ticker:
    def __init__(self):
        self.block = 0

    def __call__(self, b, w, ext=None, n=60, arb=True):
        """n blocks: the wheel's tick, the outside arbitrage, a block passing."""
        for _ in range(n):
            w.mark_dirty()
            w.tick(b, Clock(self.block, 0), max_work=len(w._table))
            for t, p in b.pools.items():
                if ext is not None and arb:
                    arbitrage(p, ext, t, p.fee)
                p.mark()
            self.block += 1


def placed(seed=1e6, **kw):
    """A basket holding a placed deposit of 1e6 BUCK (the outside LP `seed`
    a side in each pool)."""
    b, w, ext = world(seed=seed, **kw)
    tick = Ticker()
    rid = b.deposit(BUCK, 1e6)
    tick(b, w, ext)
    return b, w, ext, tick, rid


def snapshot(b):
    return {t: (p.liq.get(ME, 0.0), p.sqrtP, p.L) for t, p in b.pools.items()}


# -- the exit, in BUCK --------------------------------------------------------------- #

def test_the_wheel_places_a_deposit_and_keeps_the_reserve():
    b, *_ = placed()
    R = b.reserve_target()
    assert R * (1 - b.rband) <= b.idle_buck <= R * (1 + b.rband)
    assert b.debt == pytest.approx(K * 1e6, rel=2e-3)
    assert b.equity() == pytest.approx(1e6, rel=5e-3)
    assert max(abs(x - 1 / 3) for x in b.weights().values()) < 0.02


def test_a_reserve_exit_pays_buck_and_touches_no_pool():
    b, w, ext, tick, _ = placed()
    small = b.deposit(BUCK, 1e4)
    tick(b, w, ext)
    before = snapshot(b)
    shares = b.receipts[small].shares
    f = shares / b.S
    value, debt = f * b.equity(low=True), b.debt
    paid = b.redeem(small)
    assert snapshot(b) == before                               # no pool touched
    assert paid == pytest.approx(value * (1 - b.charge(BUCK)), rel=1e-9)
    assert b.debt == pytest.approx(debt * (1 - f), rel=1e-12)  # its debt share burned
    assert b.pro_rata_exits == 0


def test_a_reserve_exit_leaves_the_others_price_whole():
    b, w, ext, tick, _ = placed()
    small = b.deposit(BUCK, 2e4)
    tick(b, w, ext)
    pi = b.price()
    b.redeem(small)
    assert b.price() >= pi * (1 - 1e-12)


def test_a_round_trip_takes_back_only_its_own():
    b, w, ext, tick, _ = placed()
    rid = b.deposit(BUCK, 1e4)
    paid = b.redeem(rid)                  # before the wheel has placed anything
    assert paid <= 1e4
    assert paid == pytest.approx(1e4 * (1 - b.charge(BUCK)) ** 2, rel=1e-3)


def test_an_exit_beyond_the_reserve_goes_pro_rata_and_bears_its_conversion():
    b, w, ext, tick, first = placed()
    big = b.deposit(BUCK, 5e5)
    tick(b, w, ext)
    assert b.idle_buck < (1 + K) * b.value_of(big)             # the reserve cannot cover it
    before = snapshot(b)
    pi = b.price()
    b.redeem(big)
    assert b.pro_rata_exits == 1
    assert snapshot(b) != before                               # positions were withdrawn
    assert b.price() >= pi * (1 - 1e-9)                        # the others whole


def test_the_wheel_refills_a_short_reserve():
    b, w, ext, tick, _ = placed()
    rids = [b.deposit(BUCK, 1e4) for _ in range(8)]
    tick(b, w, ext)
    for r in rids:
        b.redeem(r)
    assert b.idle_buck < b.reserve_target() * (1 - b.rband)    # drained below its floor
    tick(b, w, ext)
    assert b.idle_buck >= b.reserve_target() * (1 - b.rband) - 1e-6


def test_the_flow_reserve_holds_z_sigma_at_its_floor():
    b, *_ = world(reserve=0.0, flow_z=2.0, flow_days=10.0)
    for n in range(400):                                       # net flows of +-1000 a day
        b.flow = 1000.0 if n % 2 else -1000.0
        b.close_day()
    floor = b.reserve_target() * (1 - b.rband)
    assert floor == pytest.approx(2.0 * 1000.0 * (1 + K), rel=1e-6)


def test_the_treasury_leaves_by_the_same_door_in_buck():
    b, w, ext, tick, _ = placed()
    b.treasury, b.S = 1e3, b.S + 1e3          # as if a cut had been paid (price diluted alike)
    paid = b.redeem_treasury()
    assert isinstance(paid, float) and paid > 0 and b.treasury == 0


# -- routing and the arbitrage ------------------------------------------------------- #

def test_trades_take_the_better_of_the_pool_and_the_outside_market():
    b, w, ext, tick, _ = placed()
    p = b.pools["T0"]
    b.idle["T0"] += 1e5                               # a big sale: the pool would slip
    s0 = p.sqrtP
    b.op_sell_tok("T0", 1e5)
    assert p.sqrtP == s0                              # sold outside
    ext.cost = 0.01                                   # the outside market dear:
    b.idle["T0"] += 10.0                              # a crumb goes to the pool
    b.op_sell_tok("T0", 10.0)
    assert p.sqrtP < s0


def test_the_arb_brings_the_pool_back_and_keeps_the_gap():
    b, w, ext, tick, _ = placed(arb_band=0.002)
    R0, c0 = b.idle_buck, b.captured
    ext.price["T0"] *= 1.02
    tick(b, w, ext, n=1, arb=False)
    got = b.captured - c0
    assert got > 0 and b.idle_buck == pytest.approx(R0 + got, rel=1e-9)
    p = b.pools["T0"]
    edge = p.fee + ext.cost                           # an outsider's band edge
    assert abs(p.price / ext.price["T0"] - 1) <= edge + 1e-4
    assert arbitrage(p, ext, "T0", p.fee) == (0.0, 0.0)  # nothing left for outsiders


def test_inside_the_band_there_is_nothing_to_take():
    b, w, ext, tick, _ = placed(seed=1e5, arb_band=0.001)   # the basket owns ~3/4 of each pool
    ext.price["T1"] *= 1.0035                         # inside the pool fee + the outside cost
    s0, c0 = b.pools["T1"].sqrtP, b.captured
    tick(b, w, ext, n=1, arb=False)
    assert b.pools["T1"].sqrtP == s0 and b.captured == c0


def test_inside_the_band_a_trade_is_not_arbitrage():
    """Why the wheel takes no narrower band than an outsider: on its own share
    of the pool the basket trades with itself, so at the outside price the
    trade gains only the outside LP's share of the gap and pays the outside
    cost on all of it -- a loss here, a bet on reversion at best."""
    from alberta_buck.sim.basket_harvest import nav
    b, w, ext, tick, _ = placed(seed=1e5)
    ext.price["T1"] *= 1.0035
    p = b.pools["T1"]
    v0 = nav(b, ext.price)
    fee = p.fee * (1 - b._pool_share("T1"))           # "its own fee comes back to it"
    spent, got = arbitrage(p, ext, "T1", fee, budget=b.idle_buck)
    b.idle_buck += got - spent
    assert spent > 0 and nav(b, ext.price) < v0


def _copy(p: Pool) -> Pool:
    q = Pool(p.price, p.fee)
    q.sqrtP, q.L = p.sqrtP, p.L
    q.liq = dict(p.liq)
    q.owed = {k: list(v) for k, v in p.owed.items()}
    return q


# -- the directors ------------------------------------------------------------------- #

def _first_trim_day(director, path):
    """Drive T0 along `path` (one price a day) with the others still; the day
    the wheel first trims T0 (its liquidity falls: nothing else lowers it)."""
    b, w, ext = world(seed=1e6, director=director)
    tick = Ticker()
    b.deposit(BUCK, 1e6)
    tick(b, w, ext, n=30)
    for day, price in enumerate(path):
        l0 = b.pools["T0"].liq[ME]
        ext.price["T0"] = price
        tick(b, w, ext, n=1)
        if b.pools["T0"].liq[ME] < l0 * (1 - 1e-9):
            return day
    return None


def _run_up_then_down(days_up=60, days_down=60, rate=0.01):
    up = [math.exp(rate * d) for d in range(1, days_up + 1)]
    top = up[-1]
    return up + [top * math.exp(-rate * d) for d in range(1, days_down + 1)]


def test_the_band_director_sells_into_a_run():
    day = _first_trim_day(BandDirector(), _run_up_then_down())
    assert day is not None and day < 60


def test_the_turn_director_lets_a_run_run_and_sells_the_turn():
    day = _first_trim_day(TurnDirector(), _run_up_then_down())
    assert day is not None and day >= 60


def test_the_turn_director_buys_no_falling_knife():
    b, w, ext = world(seed=1e6, director=TurnDirector())
    tick = Ticker()
    b.deposit(BUCK, 1e6)
    tick(b, w, ext, n=30)
    for d in range(1, 21):                         # T0 starts falling
        ext.price["T0"] = math.exp(-0.01 * d)
        tick(b, w, ext, n=1)
    b.idle_buck += 2e5                             # proceeds parked in the reserve
    l0 = b.pools["T0"].liq[ME]
    for d in range(21, 61):                        # and keeps falling
        ext.price["T0"] = math.exp(-0.01 * d)
        tick(b, w, ext, n=1)
    assert b.pools["T0"].liq[ME] < l0 * 1.005     # only its own fees compounded
    assert b.spare() > 1e5                         # the proceeds wait
    for d in range(1, 61):                         # the turn: T0 recovers
        ext.price["T0"] = math.exp(-0.6 + 0.01 * d)
        tick(b, w, ext, n=1)
    assert b.pools["T0"].liq[ME] > 1.1 * l0        # bought on the way back up
    assert b.spare() < 5e4                         # the parked proceeds placed


# -- the whole machine --------------------------------------------------------------- #

@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_whole_reserve_machine_keeps_its_books(seed):
    import random
    rng = random.Random(seed)
    b, w, ext = world(seed=1e6, arb_band=0.003, director=TurnDirector(), flow_z=2.0,
                      reserve=0.01)
    tick = Ticker()
    first = b.deposit(BUCK, 1e6)
    tick(b, w, ext, n=10)
    open_ = []
    x = {t: 0.0 for t in b.pools}
    base = {t: ext.price[t] for t in b.pools}
    for step in range(400):
        for t in b.pools:
            x[t] += -0.05 * x[t] + 0.015 * rng.gauss(0, 1)
            ext.price[t] = base[t] * math.exp(x[t])
        if rng.random() < 0.5:
            t = rng.choice([BUCK] + list(b.pools))
            amt = rng.uniform(1e3, 3e4)
            open_.append(b.deposit(t, amt if t == BUCK else amt / b.pools[t].price))
        if open_ and rng.random() < 0.4:
            b.redeem(open_.pop(rng.randrange(len(open_))))
        tick(b, w, ext, n=1)
        assert b.minted - b.burned == pytest.approx(b.debt, abs=1e-6)
        held = sum(r.shares for r in b.receipts.values()) + b.treasury
        assert held == pytest.approx(b.S, rel=1e-9)
        assert -1e-6 <= b.pending <= K * b.gross() + 1e-6
        assert b.idle_buck >= -1e-6 and min(b.idle.values()) >= -1e-9
    assert b.value_of(first) > 0


# -- the harvest (doc section 12.1): who gets the reversion ---------------------------- #

def test_the_wheel_takes_back_what_outside_arbitrage_took():
    kw = dict(days=240, per_day=1, seed=3)
    plain = run("reserve", "fast", **kw)
    arb = run("arb", "fast", **kw)
    assert plain["outside"] > 0.005               # outside arbitrage takes > 0.5%/yr
    assert arb["outside"] < 0.1 * plain["outside"]
    assert arb["nav"] > plain["nav"] + 0.005
