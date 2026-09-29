"""Where an equity BuckBasket's reversion harvest goes (doc/BASKET-EQUITY.org
section 12).

The same markets and the same outside arbitrageurs, against five baskets:

  token    EquityBasket as prototyped: TOKEN payouts, no reserve, the band
           Rebalance trading through its own pools, no arbitrage of its own
  reserve  ReserveBasket: the BUCK basis, a 5% reserve, still no arbitrage
  arb      + the wheel's arbitrage, the reserve as its float
  tilt     + a director leaning the targets against each TOKEN's excursion
           from its anchor (a 5000-day EMA), trading at once
  turn     + the same lean, gated by the multi-EMA turn detector (quorum 4
           of 6): never trimming a leg still running up, never buying one
           still running down; the proceeds park in the reserve between

and two benchmarks, from each basket's own holdings once it has settled:

  hold     those holdings, never traded
  cmix     their value weights, rebalanced every step at the outside prices
           and at no cost: the whole rebalancing premium there is

The worlds (each TOKEN independent, daily vol about 1.5%):

  fast     OU log price, half-life 10 days: reversion lives in the noise
  cycle    slow damped cycles (a year's period, 15% swings) with momentum,
           plus fast OU noise: reversion lives in the swings
  walk     a random walk: nothing reverts (the control)

An outside arbitrageur re-pins every pool to the edge of its no-arbitrage
band (pool fee plus the outside cost) every step, after the wheel's tick.

    python -m alberta_buck.sim.basket_harvest [--days 730] [--per-day 4] [--seeds 4] [--flows]
"""
from __future__ import annotations

import argparse
import math
import random
import statistics

from alberta_buck.sim.equity_basket import (ME, EquityBasket, Pool, Receipt,
                                            equity_wheel_tasks)
from alberta_buck.sim.reserve_basket import (External, ReserveBasket, TurnDirector,
                                             arbitrage, reserve_wheel_tasks)
from alberta_buck.sim.work_wheel import Clock, WorkWheel

ARMS = ("token", "reserve", "arb", "tilt", "turn")
WORLDS = ("fast", "cycle", "walk")


def paths(world: str, names, per_day: int, seed: int, sigma: float = 0.015):
    """An endless stream of {TOKEN: reference price}, one per step."""
    rng = random.Random(seed)
    dt = 1.0 / per_day
    base = {t: 1.0 + i for i, t in enumerate(names)}
    th_fast = math.log(2) / 10.0
    w = 2 * math.pi / 365.0
    zeta, amp = 0.25, 0.15
    s_cyc = amp * math.sqrt(4 * zeta * w ** 3)
    fast = {t: 0.0 for t in names}
    x = {t: rng.gauss(0.0, amp) for t in names}
    v = {t: rng.gauss(0.0, amp * w) for t in names}
    while True:
        out = {}
        for t in names:
            if world == "walk":
                fast[t] += sigma * math.sqrt(dt) * rng.gauss(0, 1)
                out[t] = base[t] * math.exp(fast[t])
                continue
            s_f = sigma if world == "fast" else 0.01
            fast[t] += -th_fast * fast[t] * dt + s_f * math.sqrt(dt) * rng.gauss(0, 1)
            if world == "cycle":
                v[t] += (-w * w * x[t] - 2 * zeta * w * v[t]) * dt \
                    + s_cyc * math.sqrt(dt) * rng.gauss(0, 1)
                x[t] += v[t] * dt
                out[t] = base[t] * math.exp(x[t] + fast[t])
            else:
                out[t] = base[t] * math.exp(fast[t])
        yield out


def holdings(b) -> tuple[dict[str, float], float]:
    """(TOKEN amounts, net BUCK) the basket holds, positions at their pools'
    reserves, owed fees included, the debt subtracted."""
    tok, buck = {}, b.idle_buck - b.debt
    for t, p in b.pools.items():
        L = p.liq.get(ME, 0.0)
        ot, ob = p.owed.get(ME, [0.0, 0.0])
        tok[t] = b.idle[t] + ot + L / p.sqrtP
        buck += ob + L * p.sqrtP
    return tok, buck


def nav(b, ref) -> float:
    tok, buck = holdings(b)
    return buck + sum(tok[t] * ref[t] for t in tok)


def build(arm: str, ref, K=0.75, fee=0.003, cost=0.001, share=0.9, E0=1e6,
          reserve=0.05, arb_band=0.005, per_day=4, **turn):
    names = list(ref)
    pools = {}
    side = E0 * (1 + K) / len(names) / 2 * (1 - share) / share
    for t in names:
        p = Pool(ref[t], fee)
        p.add("outside", side / ref[t], side)
        pools[t] = p
    ext = External(ref, cost)
    if arm == "token":
        b = EquityBasket(pools, K=K)
        tasks = equity_wheel_tasks()
    else:
        director = (TurnDirector(**turn) if arm == "turn" else
                    TurnDirector(**{**turn, "quorum": 0}) if arm == "tilt" else None)
        b = ReserveBasket(pools, ext=ext, K=K, reserve=reserve, director=director)
        tasks = reserve_wheel_tasks(arb_band if arm != "reserve" else None,
                                    day=per_day)
    w = WorkWheel(tasks)
    w.bind(b)
    start(b, E0, reserve if arm != "token" else 0.0)
    return b, w, ext


def start(b, E0: float, reserve: float) -> None:
    """The basket as its wheel would leave a deposit of E0, placed at the
    reference prices at no cost: debt K x E0, `reserve` of the gross in BUCK,
    the rest in equal positions.  Every arm starts from the same place, so
    the measurement is the harvest alone (what placing costs is the entry
    charge's business, tested in test_equity_basket)."""
    k = b.k()
    gross = (1 + k) * E0
    b.idle_buck = reserve * gross
    per = gross * (1 - reserve) / len(b.pools)
    for p in b.pools.values():
        l = per / (2 * p.sqrtP)
        p.liq[ME] = p.liq.get(ME, 0.0) + l
        p.owed.setdefault(ME, [0.0, 0.0])
        p.L += l
    b.debt = b.minted = k * E0
    b.S = E0
    b.receipts[1] = Receipt(E0, E0)
    b._next = 2


def run(arm: str, world: str, days: int = 730, per_day: int = 4, seed: int = 1,
        **kw) -> dict:
    """One basket in one world.  Returns annual rates (fractions of the
    settled NAV per year): the basket's, hold's and cmix's, what the outside
    arbitrageurs took from its pools and what its own arbitrage captured."""
    names = ["T0", "T1", "T2"]
    gen = paths(world, names, per_day, seed)
    ref = next(gen)
    b, w, ext = build(arm, ref, per_day=per_day, **kw)
    block = 0
    tok0, buck0 = holdings(b)
    v0 = buck0 + sum(tok0[t] * ref[t] for t in names)
    wt = {t: tok0[t] * ref[t] / v0 for t in names}
    cm, cm_buck = dict(tok0), buck0
    outside = 0.0
    for _ in range(days * per_day):
        ref = next(gen)
        ext.price.update(ref)
        w.mark_dirty()
        w.tick(b, Clock(block, 0), max_work=len(w._table))
        for t, p in b.pools.items():
            spent, got = arbitrage(p, ext, t, p.fee)
            outside += got - spent
            p.mark()
        v = cm_buck + sum(cm[t] * ref[t] for t in names)
        cm = {t: wt[t] * v / ref[t] for t in names}
        cm_buck = v * (1 - sum(wt.values()))
        block += 1
    yrs = days / 365.0
    rate = lambda x: (x / v0) ** (1 / yrs) - 1
    return {
        "nav": rate(nav(b, ref)),
        "hold": rate(buck0 + sum(tok0[t] * ref[t] for t in names)),
        "cmix": rate(cm_buck + sum(cm[t] * ref[t] for t in names)),
        "outside": outside / v0 / yrs,
        "captured": getattr(b, "captured", 0.0) / v0 / yrs,
        "reserve": b.idle_buck / b.gross(),
        "leverage": b.debt / b.equity(),
    }


# -- flows: what deposits and exits cost, and who pays -------------------------------- #

class CountingPool(Pool):
    """A pool that counts the operations performed on it (a gas proxy)."""
    ops = 0

    def add(self, *a):
        CountingPool.ops += 1
        return super().add(*a)

    def remove(self, *a):
        CountingPool.ops += 1
        return super().remove(*a)

    def collect(self, *a):
        CountingPool.ops += 1
        return super().collect(*a)

    def sell_tok(self, dx):
        CountingPool.ops += 1
        return super().sell_tok(dx)

    def sell_buck(self, dy):
        CountingPool.ops += 1
        return super().sell_buck(dy)


class RoutedEquityBasket(EquityBasket):
    """The TOKEN-payout basket given the reserve basket's routing (the better
    of its pool and the outside market) for every trade, exits' included: to
    separate what routing buys from what the reserve's netting buys."""

    ext = None
    _pool_share = ReserveBasket._pool_share
    _route_sell_tok = ReserveBasket._route_sell_tok
    _route_sell_buck = ReserveBasket._route_sell_buck
    op_sell_tok = ReserveBasket.op_sell_tok
    op_sell_buck = ReserveBasket.op_sell_buck

    def _settle_residue(self, pay, residue):
        if residue <= self.dust:
            return
        val = {t: pay[t] * self.pools[t].price for t in pay}
        tot = sum(val.values()) or 1.0
        for t in self.pools:
            pay[t] += self._route_sell_buck(t, residue * val[t] / tot)


FLOW_ARMS = ("token", "routed", "reserve", "flow")


def run_flows(arm: str, seed: int = 1, days: int = 365, per_day: int = 4,
              arrivals: float = 2.0, size: float = 1e4, hold_days: float = 60.0,
              K=0.75, fee=0.003, cost=0.001, share=0.9, E0=1e6) -> dict:
    """Depositors arrive (`arrivals` a day, sizes lognormal about `size`, 30%
    in BUCK) and each leaves with a hazard of 1/`hold_days` a day, in the
    fast world, beside a first holder of E0 who stays.  The wheel ticks every
    step, outside arbitrage re-pins the pools.  No arbitrage of the basket's
    own and no director: the flows alone."""
    rng = random.Random(seed * 7919)
    names = ["T0", "T1", "T2"]
    gen = paths("fast", names, per_day, seed)
    ref = next(gen)
    g = globals()                     # build() makes its pools from this module's Pool
    saved, g["Pool"] = g["Pool"], CountingPool
    try:
        if arm in ("token", "routed"):
            b, w, ext = build("token", ref, K=K, fee=fee, cost=cost, share=share, E0=E0,
                              per_day=per_day)
            if arm == "routed":
                b.__class__ = RoutedEquityBasket
                b.ext = ext
        else:
            b, w, ext = build("reserve", ref, K=K, fee=fee, cost=cost, share=share, E0=E0,
                              per_day=per_day, arb_band=None,
                              reserve=0.05 if arm == "reserve" else 0.01)
            if arm == "flow":
                b.flow_z = 2.0
    finally:
        g["Pool"] = saved
    price0 = b.price()
    open_: dict[int, float] = {}                 # rid -> the TOKEN's... value paid in
    exits = []
    exit_ops = 0
    wheel_ops0 = CountingPool.ops
    block = 0
    for day in range(days):
        for sub in range(per_day):
            ref = next(gen)
            ext.price.update(ref)
            if sub == 0:
                for _ in range(_poisson(rng, arrivals)):
                    amt = size * math.exp(rng.gauss(0, 0.8) - 0.32)
                    if rng.random() < 0.3:
                        rid = b.deposit("BUCK", amt)
                    else:
                        t = rng.choice(names)
                        rid = b.deposit(t, amt / b.pools[t].price)
                    open_[rid] = b.receipts[rid].shares * b.price()
                for rid in list(open_):
                    if rng.random() < 1.0 / hold_days:
                        marked = b.receipts[rid].shares * b.price()
                        n0 = CountingPool.ops
                        pay = b.redeem(rid)
                        exit_ops += CountingPool.ops - n0
                        got = pay if isinstance(pay, float) else \
                            sum(pay[t] * ref[t] for t in names)
                        exits.append(got / marked)
                        open_.pop(rid)
            w.mark_dirty()
            w.tick(b, Clock(block, 0), max_work=len(w._table))
            for t, p in b.pools.items():
                arbitrage(p, ext, t, p.fee)
                p.mark()
            block += 1
    stayer = b.receipts[1].shares * b.price()
    ops = CountingPool.ops - wheel_ops0
    return {
        "exits": len(exits),
        "ops_per_exit": exit_ops / max(len(exits), 1),
        "wheel_ops_per_day": (ops - exit_ops) / days,
        "exit_got": statistics.mean(exits) if exits else 0.0,
        "from_reserve": 1 - getattr(b, "pro_rata_exits", len(exits)) / max(len(exits), 1),
        "stayer": (b.price() / price0) ** (365.0 / days) - 1,
        "reserve": b.idle_buck / b.gross(),
    }


def _poisson(rng, lam: float) -> int:
    n, t = 0, rng.expovariate(1.0)
    while t < lam:
        n += 1
        t += rng.expovariate(1.0)
    return n


def flows_table(seeds=4, **kw) -> list[dict]:
    rows = []
    for arm in FLOW_ARMS:
        rs = [run_flows(arm, seed=s, **kw) for s in range(1, seeds + 1)]
        row = {"arm": arm}
        for k in rs[0]:
            row[k] = statistics.mean(r[k] for r in rs)
        rows.append(row)
    return rows


def table(worlds=WORLDS, arms=ARMS, seeds=4, **kw) -> list[dict]:
    rows = []
    for world in worlds:
        for arm in arms:
            rs = [run(arm, world, seed=s, **kw) for s in range(1, seeds + 1)]
            row = {"world": world, "arm": arm}
            for k in rs[0]:
                row[k] = statistics.mean(r[k] for r in rs)
            row["vs_hold_sd"] = statistics.pstdev(r["nav"] - r["hold"] for r in rs)
            rows.append(row)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--per-day", type=int, default=4)
    ap.add_argument("--seeds", type=int, default=4)
    ap.add_argument("--fee", type=float, default=0.003)
    ap.add_argument("--cost", type=float, default=0.001)
    ap.add_argument("--world", action="append", choices=WORLDS)
    ap.add_argument("--arm", action="append", choices=ARMS)
    ap.add_argument("--flows", action="store_true", help="also the flows experiment")
    a = ap.parse_args()
    rows = table(worlds=a.world or WORLDS, arms=a.arm or ARMS, seeds=a.seeds,
                 days=a.days, per_day=a.per_day, fee=a.fee, cost=a.cost)
    pct = lambda x: f"{100 * x:+6.2f}"
    print(f"{'world':6} {'arm':8} {'nav/y':>7} {'-hold':>7} {'(sd)':>6} {'-cmix':>7} "
          f"{'outside':>8} {'captured':>9} {'reserve':>8} {'D/E':>5}")
    for r in rows:
        print(f"{r['world']:6} {r['arm']:8} {pct(r['nav'])}% {pct(r['nav'] - r['hold'])}% "
              f"{100 * r['vs_hold_sd']:5.2f} {pct(r['nav'] - r['cmix'])}% "
              f"{pct(r['outside'])}%  {pct(r['captured'])}%  {100 * r['reserve']:6.2f}% "
              f"{r['leverage']:5.2f}")
    if a.flows:
        print()
        print(f"{'flows':8} {'exits':>6} {'ops/exit':>9} {'wheel ops/d':>12} {'exit got':>9} "
              f"{'reserve paid':>13} {'stayer/y':>9} {'reserve':>8}")
        for r in flows_table(seeds=a.seeds, per_day=a.per_day, fee=a.fee, cost=a.cost):
            print(f"{r['arm']:8} {r['exits']:6.0f} {r['ops_per_exit']:9.1f} "
                  f"{r['wheel_ops_per_day']:12.1f} {100 * r['exit_got']:8.2f}% "
                  f"{100 * r['from_reserve']:12.1f}% {pct(r['stayer'])}% "
                  f"{100 * r['reserve']:7.2f}%")


if __name__ == "__main__":
    main()
