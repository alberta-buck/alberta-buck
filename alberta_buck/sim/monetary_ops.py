#!/usr/bin/env python3
"""Two-mode BuckBasket: commodity rebalancing AND monetary operations.

    python -m alberta_buck.sim.monetary_ops          # table
    python -m alberta_buck.sim.monetary_ops --plot   # + images/basket-operations.png

WHY THIS MODEL EXISTS

The `pairs` rebalance engine reads per-leg EMA ladders over log pool ticks and
trades the DIFFERENTIAL modes -- commodity against commodity.  It explicitly
throws the common mode away, on the grounds that "BUCK's own valuation against
the numeraire is the K-controller's job, and it cancels exactly in
cross-commodity log ratios".

But a tick is log(TOKEN priced in BUCK), so the MEAN of the legs is
log(basket priced in BUCK) -- basketValueInBuck itself.  And because the EMA
is linear,

    mean_i EMA_k(c_i)  ==  EMA_k(mean_i c_i)

so the common mode is already present in the ladder at every scale, for free,
with no extra state.  One filter bank, two outputs:

    c_i - mean(c)   ->  differential  ->  commodity rebalancing   (today)
    mean(c)         ->  common        ->  monetary operations     (this model)

The question this model answers is whether acting on the common mode is worth
doing: whether a basket that trades BUCK's own mispricing absorbs an attack
faster than K alone, whether it profits from doing so, and -- the part that
matters most -- whether it loses when the move is real rather than transient.

THE FOUR QUADRANTS

Direction (is BUCK cheap or dear) crossed with persistence (did the excursion
turn, or does it keep going):

                      BUCK CHEAP (bvib > 1)        BUCK DEAR (bvib < 1)
    REVERTING     Q1 absorb: TEMPORARY          Q3 supply: TEMPORARY
                  TOKEN-funded bids below       BUCK-funded offers above
                  price; buy the dump, sell     price; sell into the bid,
                  it back on recovery.          buy back on the return.
                  Supply unchanged net.         Supply unchanged net.

    PERSISTENT    Q2 retire: PERMANENT          Q4 issue: PERMANENT
                  burn acquired BUCK against    mint against basket TOKEN
                  outstanding.  Supply falls.   and sell.  Supply rises.

Q1/Q3 are repo: a concentrated range order performs an operation and unwinds
it automatically as the price mean-reverts, earning the spread while it waits.
Q2/Q4 are outright: they change the size of the balance sheet and are only
reached when the temporary operation has been consumed AND the price stayed
away -- which is a regime signal, not a tick signal.

THE COLLOCATION PROBLEM, AND WHY THE OBVIOUS GUARD BACKFIRES

basketValueInBuck is measured from the basket's own pools, so a basket that
trades on it moves its own sensor.  The obvious guard is bandwidth
separation: measure on a scale so slow that one bounded operation cannot
shift it.  That was the first design here, measuring on the 160-day EMA.

It made things worse, and the reason is worth stating because it is the
central result of this model.  On the inflation scenario the excursion peaks
at 1645bp spot but only 712bp on the 160d average -- 933bp of phase lag.
Operations timed off that signal arrive after the spot has already turned,
so their flow lands PRO-cyclically and the peak deviation grew from 1645bp
to 2116bp.  They still made money, because the deviation mean-reverts around
the entries either way.

So: profitability and stabilization are NOT the same objective, and the
claim that "the corrective trade is the profitable trade" holds only when
the signal is timely.  With lag they come apart, and a lagged operator is a
profitable destabilizer.

Measured across the ladder (inflation scenario, peak deviation vs baseline
1645bp, and the sign of the supply change an outright operation produces):

    measure on    peak dev   damping   basket P&L   attacker    supply
      10d           1004bp    -641bp    +623,186    -580,548    -566,628
      20d           1074bp    -570bp    +672,384    -622,660    -801,908
      40d           1185bp    -460bp    +694,414    -711,547    -623,610
      80d           1344bp    -301bp    +746,063    -985,063    +685,152  <- WRONG WAY

Two constraints fall out.  Stabilization and extraction TRADE OFF: acting
early damps the excursion and therefore forecloses the profit that
excursion would have paid.  And past roughly 80 days the outright
operations invert -- the lagged reading still says "dear" long after the
market has gone cheap, so an inflation attack is answered by ISSUING more.
That is not a tuning preference, it is a correctness bound.

20d is the knee, and the collocation risk at that scale is handled by the
size bound instead: an operation capped at 0.4%/day of depth cannot dominate
a 20-day average.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_JSON = REPO / "test" / "vectors" / "basket-operations.json"
OUT_PNG = REPO / "images" / "basket-operations.png"

LADDER = [5, 10, 20, 40, 80, 160, 320]      # same geometry as PairsRebalanceDirector
FAST, SLOW = 1, 5                            # ladder indices: 10d turn, 160d level

DAYS = 730
COST_BP = 30.0            # per leg, matching the rebalance study's default


# ── the market ─────────────────────────────────────────────────────────────

@dataclass
class Market:
    """A single-price abstraction of BUCK against the basket.

    `bvib` is the basket priced in BUCK, the controller's process variable and
    the common mode of the tick ladder.  Flow moves it with constant-product
    impact against `depth`; absent flow it relaxes toward `fundamental` at
    rate 1/tau, which is the reversion the whole design is a bet on.

    `fundamental` is what bvib SHOULD be.  Normally 1.0.  A regime scenario
    moves it, and that is the case where trading the deviation is wrong --
    the price is not mispriced, it has genuinely moved.
    """
    depth: float = 40_000_000.0     # BUCK-equivalent pool depth
    tau: float = 120.0              # reversion timescale, days
    sigma: float = 0.004            # daily idiosyncratic vol of bvib
    fundamental: float = 1.0
    bvib: float = 1.0
    supply: float = 20_000_000.0

    def step(self, flow_buck: float, rng: random.Random) -> None:
        """`flow_buck` > 0 = BUCK sold into the market (bvib rises, BUCK cheap)."""
        impact = flow_buck / self.depth
        drift = (math.log(self.fundamental) - math.log(self.bvib)) / self.tau
        self.bvib *= math.exp(impact + drift + rng.gauss(0.0, self.sigma))


# ── the ladder ─────────────────────────────────────────────────────────────

class Ladder:
    """EMAs of log(bvib) on the PairsRebalanceDirector window geometry.

    This is the common mode of the per-leg ladder.  Nothing here is new state
    in a real implementation -- it is mean_i(leg_i.m[k]), which the engine
    already computes.
    """

    def __init__(self, windows=LADDER):
        self.w = list(windows)
        self.m: list[float | None] = [None] * len(windows)
        self.prev: list[float] = [0.0] * len(windows)
        self.vel: list[float] = [0.0] * len(windows)
        self.n = 0

    def update(self, x: float) -> None:
        self.n += 1
        for k, w in enumerate(self.w):
            b = 2.0 / (w + 1.0)
            cur = x if self.m[k] is None else self.m[k] + (x - self.m[k]) * b
            self.vel[k] = cur - (self.m[k] if self.m[k] is not None else cur)
            self.m[k] = cur

    def ready(self, k: int) -> bool:
        return self.n >= self.w[k]


# ── the operator ───────────────────────────────────────────────────────────

@dataclass
class Operator:
    """The BuckBasket running monetary operations on the common mode."""
    deadband: float = 0.010          # 100bp: below this, do nothing
    leash: float = 0.020             # 200bp sustained -> outright
    temp_frac: float = 0.004         # per-day temporary size, fraction of depth
    perm_frac: float = 0.002         # per-day outright size, fraction of depth
    cost_bp: float = COST_BP
    enabled: bool = True
    meas: int = 2                    # 20d: the knee of the sweep (see module doc)
    turn: int = 1                    # ladder index used to detect the turn
    persist_days: int = 30           # consecutive days past the leash -> outright
    inv_escalate: float = 0.05       # inventory/depth that means "it is not coming back"
    inv_max: float = 0.10            # hard position limit
    # Cumulative bound on BALANCE-SHEET change, as a fraction of supply.
    # Closing a deviation of d needs a supply change of roughly d, so an
    # 8% revaluation wants ~1.6M against a 20M supply.  Without this bound
    # the inventory limit simply relocated the runaway: escalation fired
    # every day and burned 18.65M -- 93% of all BUCK -- to chase a gap that
    # 8% would have closed.  A central bank announces a taper size; it does
    # not run the desk until the number comes right.
    max_outright: float = 0.10
    _over: int = 0

    token: float = 0.0               # TOKEN inventory bought with BUCK (P&L unit)
    buck: float = 0.0                # BUCK inventory held from temporary ops
    burned: float = 0.0
    minted: float = 0.0
    cost_paid: float = 0.0
    q: list[int] = field(default_factory=lambda: [0, 0, 0, 0])

    def act(self, mkt: Market, lad: Ladder) -> float:
        """Return the BUCK flow this operation puts into the market.

        Positive = the basket SELLS BUCK (supplying).  Negative = the basket
        BUYS BUCK (absorbing).
        """
        if not self.enabled or not lad.ready(self.meas):
            return 0.0
        # Measure on the slow scale: one bounded operation cannot move a
        # 160-day average, so the sensor is not chasing the actuator.
        dev = lad.m[self.meas]
        # Persistence is a DURATION, not an instantaneous velocity.  A
        # 10-day velocity changes sign on noise, so "not turning" almost
        # never held and the outright quadrants were unreachable -- supply
        # never moved and half the mechanism was inert while looking healthy.
        self._over = self._over + 1 if abs(dev) > self.leash else 0
        if abs(dev) < self.deadband:
            return 0.0
        # Escalate on INVENTORY, not only on the price deviation.
        #
        # This is the correction the model forced.  Absorbing holds the
        # measured deviation down -- that is the entire point of absorbing --
        # so a persistence test built on that deviation is suppressed by the
        # very act it is supposed to police.  In the drift scenario the
        # basket bought until its BUCK inventory reached 49.9% of pool depth
        # and STILL never escalated, because its own bid kept the price
        # inside the leash.  It showed a profit throughout, on a mark of a
        # position it could not have unwound.
        #
        # Inventory is the one signal the operator's own action cannot
        # suppress, because it IS the operator's own action.  If a large
        # position has been absorbed and the price has not come back, the
        # move is real -- whatever the price says.
        inv = abs(self.buck) / mkt.depth
        persistent = self._over >= self.persist_days or inv > self.inv_escalate
        if inv > self.inv_max and dev > 0:
            return 0.0                    # hard position limit: stop buying

        size = mkt.depth * (self.perm_frac if persistent else self.temp_frac)
        fee = self.cost_bp / 1e4

        if dev > 0:
            # BUCK is CHEAP (basket costs more BUCK than it should).
            # Buy BUCK with TOKEN.  Flow into the market is negative.
            # bvib is BUCK PER BASKET, so TOKEN -> BUCK multiplies.  When
            # BUCK is cheap (bvib > 1) a basket buys MORE than one BUCK, and
            # that surplus is the whole seigniorage the operation harvests.
            spend_token = size
            got_buck = spend_token * mkt.bvib * (1.0 - fee)
            self.token -= spend_token
            self.cost_paid += spend_token * fee
            if persistent and (self.burned - self.minted) < self.max_outright * 20_000_000:
                # Q2 RETIRE: burn it.  Supply falls permanently.
                self.burned += got_buck
                mkt.supply -= got_buck
                self.q[1] += 1
            else:
                # Q1 ABSORB: hold it; it goes back out when bvib recovers.
                self.buck += got_buck
                self.q[0] += 1
            return -got_buck
        else:
            # BUCK is DEAR.  Sell BUCK for TOKEN.
            if persistent and (self.minted - self.burned) < self.max_outright * 20_000_000:
                # Q4 ISSUE: mint against basket TOKEN and sell into the bid.
                sell_buck = size * mkt.bvib
                self.minted += sell_buck
                mkt.supply += sell_buck
                self.q[3] += 1
            else:
                # Q3 SUPPLY: only from inventory previously absorbed.
                sell_buck = min(self.buck, size * mkt.bvib)
                if sell_buck <= 0:
                    return 0.0
                self.buck -= sell_buck
                self.q[2] += 1
            got_token = sell_buck / mkt.bvib * (1.0 - fee)
            self.token += got_token
            self.cost_paid += sell_buck / mkt.bvib * fee
            return sell_buck

    def mark(self, mkt: Market) -> float:
        """P&L in TOKEN, marking BUCK inventory and retired liability.

        A burned BUCK was a liability the basket issued at par and has now
        extinguished for what it paid -- so retiring below par is a gain of
        (par - cost), which is already in `token` as the TOKEN not spent.
        Held BUCK marks at the current price.
        """
        return self.token + self.buck / mkt.bvib + self.burned - self.minted


# ── the attacker ───────────────────────────────────────────────────────────

@dataclass
class Attacker:
    """A whale who pushes BUCK off parity and must eventually unwind.

    `size` BUCK sold (or bought) over `days` from `start`, then held for
    `hold` days, then unwound -- because an inflation attack is funded by
    drawing credit, and the credit must be retired by buying BUCK back.  The
    unwind is where the transfer happens.
    """
    start: int = 120
    days: int = 20
    hold: int = 60
    size: float = 6_000_000.0
    direction: int = +1              # +1 sell BUCK (inflate), -1 buy (squeeze)

    token: float = 0.0
    buck: float = 0.0

    def flow(self, day: int, mkt: Market) -> float:
        per = self.size / self.days
        if self.start <= day < self.start + self.days:
            f = per * self.direction
        elif (self.start + self.days + self.hold <= day
              < self.start + 2 * self.days + self.hold):
            f = -per * self.direction          # unwind
        else:
            return 0.0
        self.buck -= f
        self.token += f / mkt.bvib
        return f

    def mark(self, mkt: Market) -> float:
        return self.token + self.buck / mkt.bvib


# ── one run ────────────────────────────────────────────────────────────────

def run(scenario: str, ops: bool, seed: int = 7, **kw) -> dict:
    rng = random.Random(seed)
    mkt = Market()
    lad = Ladder()
    op = Operator(enabled=ops, **kw)
    atk = Attacker(direction=-1 if scenario == "squeeze" else +1)
    if scenario in ("quiet", "drift"):
        atk.size = 0.0
    regime = scenario in ("regime", "drift")

    series = {"day": [], "bvib": [], "supply": [], "opPnl": [], "atkPnl": []}
    dev_days = 0
    for day in range(DAYS):
        if regime and day == 200:
            # A GENUINE revaluation: the basket really is worth more BUCK.
            # Trading this deviation is wrong, and the model must show it.
            mkt.fundamental = 1.08
        lad.update(math.log(mkt.bvib))
        f = atk.flow(day, mkt) + op.act(mkt, lad)
        mkt.step(f, rng)
        if abs(math.log(mkt.bvib)) > 0.02:
            dev_days += 1
        series["day"].append(day)
        series["bvib"].append(mkt.bvib)
        series["supply"].append(mkt.supply)
        series["opPnl"].append(op.mark(mkt))
        series["atkPnl"].append(atk.mark(mkt))

    peak = max(abs(math.log(b)) for b in series["bvib"])
    return {
        "scenario": scenario, "ops": ops,
        "peakDevBp": 1e4 * peak,
        "daysOff2pct": dev_days,
        "opPnl": op.mark(mkt),
        "atkPnl": atk.mark(mkt),
        "supplyEnd": mkt.supply,
        "burned": op.burned, "minted": op.minted, "costPaid": op.cost_paid,
        "quadrants": op.q,
        "series": series,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m alberta_buck.sim.monetary_ops")
    ap.add_argument("--plot", action="store_true")
    a = ap.parse_args(argv)

    rows = []
    # `drift` is the honest failure test: a GENUINE revaluation with no
    # attacker at all.  The basket must lose there, because it is trading
    # against a move that is not a mispricing.  A design that profits in
    # every scenario is not being tested, it is being flattered.
    for sc in ("inflate", "squeeze", "drift", "quiet"):
        rows.append((sc, run(sc, False), run(sc, True)))

    print(f"{'scenario':10s} {'ops':>4} {'peak dev':>9} {'days>2%':>8} "
          f"{'basket P&L':>12} {'attacker P&L':>13} {'supply':>12} "
          f"{'Q1/Q2/Q3/Q4':>16}")
    for sc, off, on in rows:
        for tag, r in (("off", off), ("ON", on)):
            q = "/".join(str(x) for x in r["quadrants"])
            print(f"{sc if tag=='off' else '':10s} {tag:>4} "
                  f"{r['peakDevBp']:>8.0f}b {r['daysOff2pct']:>8d} "
                  f"{r['opPnl']:>12,.0f} {r['atkPnl']:>13,.0f} "
                  f"{r['supplyEnd']:>12,.0f} {q:>16}")
    print()
    for sc, off, on in rows:
        d = on["peakDevBp"] - off["peakDevBp"]
        t = on["atkPnl"] - off["atkPnl"]
        print(f"  {sc:10s} peak deviation {d:+7.0f}bp   "
              f"attacker {t:+12,.0f}   basket {on['opPnl']:+12,.0f}")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(
        {sc: {"off": {k: v for k, v in off.items() if k != "series"},
              "on": {k: v for k, v in on.items() if k != "series"},
              "series": on["series"], "seriesOff": off["series"]}
         for sc, off, on in rows}))
    print(f"\nWrote {OUT_JSON.relative_to(REPO)}")

    if a.plot:
        _plot(rows)
    return 0


def _plot(rows) -> None:
    import os
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/alberta-buck-mpl")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(len(rows), 3, figsize=(15, 3.2 * len(rows)),
                             sharex=True)
    for r, (sc, off, on) in enumerate(rows):
        d = on["series"]["day"]
        ax = axes[r][0]
        ax.axhline(1.0, color="#20242c", lw=0.8, alpha=0.4, ls="-.")
        ax.plot(d, off["series"]["bvib"], color="#b3541e", lw=1.2,
                label="no operations")
        ax.plot(d, on["series"]["bvib"], color="#0b6e4f", lw=1.4,
                label="operations on")
        ax.set_ylabel(f"{sc}\nbasketValueInBuck")
        ax.grid(alpha=0.25)
        if r == 0:
            ax.set_title("The deviation")
            ax.legend(fontsize=8)
        ax = axes[r][1]
        ax.axhline(0, color="#20242c", lw=0.8, alpha=0.4)
        ax.plot(d, on["series"]["atkPnl"], color="#e34948", lw=1.4,
                label="attacker")
        ax.plot(d, on["series"]["opPnl"], color="#0b6e4f", lw=1.4,
                label="basket")
        ax.grid(alpha=0.25)
        if r == 0:
            ax.set_title("Who pays")
            ax.legend(fontsize=8)
        ax = axes[r][2]
        ax.plot(d, off["series"]["supply"], color="#b3541e", lw=1.2)
        ax.plot(d, on["series"]["supply"], color="#0b6e4f", lw=1.4)
        ax.grid(alpha=0.25)
        if r == 0:
            ax.set_title("BUCK supply")
    axes[-1][0].set_xlabel("day")
    fig.tight_layout()
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PNG, dpi=130)
    print(f"Wrote {OUT_PNG.relative_to(REPO)}")


if __name__ == "__main__":
    raise SystemExit(main())
