#!/usr/bin/env python3
"""Generate daily close-price CSV vectors for the routing simulation.

PAXG  (gold, USD/troy oz)   18-dec token
cbBTC (bitcoin, USD)         8-dec token
AOIL  ("Alberta Oil", USD/bbl) 18-dec synthetic token

One row per day: day,close_usd_micro, in integer micro-dollars (1e6 == $1.00).
Deterministic: each series has a fixed seed.

TWO REGIMES
-----------

`trend` (the default, and what the committed CSVs hold) is geometric Brownian
motion with a positive annual drift: +8% gold, +15% bitcoin, +2% oil.  Over
the 730-day horizon that compounds into a market which mostly goes up.

That drift confounds every reversion measurement made against these files.
A basket policy is judged on the rebalancing premium, which is a claim about
harvesting oscillation; a demand agent is judged on buying cheap and selling
dear.  In a market that rises throughout, buy-and-hold beats both of them for
a reason that has nothing to do with either mechanism -- it simply had more
of a rising asset for longer.  The BuckBasket's own charter says the basket
admits only commodities that physics forces to revert, so a relentless uptrend
is not the regime the design is built for; it is the stress case.

`revert` is the honest test bed for reversion.  Each series is an
Ornstein-Uhlenbeck walk in log price -- so it wanders naturally, with the same
volatility as the trend regime and a pull back toward its own level -- and is
then pinned by a Brownian bridge so that it ENDS EXACTLY WHERE IT BEGAN.  Net
drift is zero by construction, over the whole window and by definition at the
endpoint.  Anything a policy or an agent earns here it earned from the
oscillation, because there is no trend left to earn from.

    python -m alberta_buck.sim.gen_prices                      # trend
    python -m alberta_buck.sim.gen_prices --regime revert      # revert, -rev.csv
"""

import argparse
import csv
import math
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "prices"

DAYS = 730  # 2-year daily horizon

SERIES = [
    # (stem, start USD, annual drift, annual vol, seed)
    ("paxg",   2600.0,  0.08, 0.16, 42),
    ("cbbtc", 65000.0,  0.15, 0.55, 43),
    ("aoil",     78.0,  0.02, 0.35, 44),
]

# Mean-reversion timescale for the `revert` regime, in days.  Four months is
# the idiosyncratic reversion floor the M2-lag study uses for basket
# constituents (see rebalance_policy.IDIO_FLOOR_MONTHS).
TAU_DAYS = 120.0

USDC = 1_000_000  # 1e6 micro-dollars == $1.00


def gen_trend(start: float, mu: float, sigma: float, seed: int, days: int):
    """Geometric Brownian motion with drift `mu`."""
    rng = random.Random(seed)
    dt = 1.0 / 365.25
    p = start
    out = [p]
    for _ in range(1, days):
        z = rng.gauss(0.0, 1.0)
        p *= math.exp((mu - 0.5 * sigma * sigma) * dt + sigma * math.sqrt(dt) * z)
        out.append(p)
    return out


def gen_revert(start: float, sigma: float, seed: int, days: int,
               tau_days: float = TAU_DAYS):
    """A natural-looking walk with zero net drift that ends where it began.

    Two mechanisms, and they do different jobs.  The Ornstein-Uhlenbeck pull
    (rate 1/tau) makes excursions come home DURING the window, which is what
    makes the path look like a commodity rather than a random walk that
    happens to be tied down at the end.  The Brownian bridge -- subtracting
    the straight line from the terminal value, B(t) = W(t) - (t/T)W(T) --
    then pins the endpoint exactly, so the series cannot smuggle in a drift
    that a long-horizon agent would collect for free.

    Volatility is the same `sigma` the trend regime uses, so the two regimes
    differ in drift alone and remain comparable.
    """
    rng = random.Random(seed)
    dt = 1.0 / 365.25
    step = sigma * math.sqrt(dt)
    theta = 1.0 / tau_days
    x, xs = 0.0, [0.0]
    for _ in range(1, days):
        x += -theta * x + rng.gauss(0.0, step)
        xs.append(x)
    T = days - 1
    xs = [xs[i] - xs[T] * i / T for i in range(days)]   # pin both ends
    return [start * math.exp(v) for v in xs]


# The six-token reverting set: one seed per constituent, fixed so a window's
# reverting prices are the same on every machine (house rule: the vectors
# are byte-identical across hosts).
REVERT_SEED = 0x5AFE


def revert_like(files: list[str], out_dir: Path = OUT_DIR) -> list[str]:
    """The reverting twin of a price window (the savings demonstration's
    "Reverting" toggle, doc/CONVERGENCE.org 7a): for each series in `files`,
    a `gen_revert` walk from the same first price with that series' own
    realized volatility -- the same commodities and the same scale, their
    trend removed, each ending where it began.  Written beside the originals
    as rev-<name> (hist-labr-...csv -> rev-labr-...csv) on first use, under
    the window's lock and atomically, like gen_historical's (WAVE3.org
    decision 21); returns the reverting file names."""
    import fcntl
    import os

    want = [("rev-" + f[len("hist-"):]) if f.startswith("hist-") else f"rev-{f}" for f in files]
    lock = out_dir / (want[0] + ".lock")
    with lock.open("a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            if all((out_dir / f).exists() for f in want):
                return want
            for k, (src, dst) in enumerate(zip(files, want)):
                with (out_dir / src).open() as f:
                    rows = [int(r[1]) for r in list(csv.reader(f))[1:]]
                logs = [math.log(b / a) for a, b in zip(rows, rows[1:]) if a > 0 and b > 0]
                mean = sum(logs) / len(logs)
                sd = math.sqrt(sum((x - mean) ** 2 for x in logs) / (len(logs) - 1))
                sigma = sd * math.sqrt(365.25)            # annual, as gen_revert takes it
                series = gen_revert(float(rows[0]), sigma, REVERT_SEED + k, len(rows))
                tmp = out_dir / f"{dst}.tmp-{os.getpid()}"
                with tmp.open("w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["day", "close_usd_micro"])
                    for day, px in enumerate(series):
                        w.writerow([day, round(px)])
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, out_dir / dst)
            return want
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def write(path: Path, prices):
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "close_usd_micro"])
        for day, px in enumerate(prices):
            w.writerow([day, round(px * USDC)])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m alberta_buck.sim.gen_prices",
        description=__doc__.splitlines()[0])
    ap.add_argument("--regime", choices=("trend", "revert"), default="trend",
                    help="trend: GBM with the drifts above (default).  "
                         "revert: OU + Brownian bridge, zero net drift, ends "
                         "at its starting value.")
    ap.add_argument("--days", type=int, default=DAYS)
    ap.add_argument("--suffix", default=None,
                    help="filename suffix; defaults to '' for trend and "
                         "'-rev' for revert, so a revert run never "
                         "overwrites the committed trend CSVs")
    a = ap.parse_args(argv)
    suffix = a.suffix if a.suffix is not None else (
        "" if a.regime == "trend" else "-rev")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Generating {a.days}-day price CSVs [{a.regime}]:")
    for stem, start, mu, sigma, seed in SERIES:
        if a.regime == "trend":
            prices = gen_trend(start, mu, sigma, seed, a.days)
        else:
            prices = gen_revert(start, sigma, seed, a.days)
        path = OUT_DIR / f"{stem}{suffix}.csv"
        write(path, prices)
        drift = 100.0 * (prices[-1] / prices[0] - 1.0)
        print(f"  {path.name}: {a.days} rows "
              f"${prices[0]:,.2f} -> ${prices[-1]:,.2f} ({drift:+.2f}%) "
              f"[${min(prices):,.2f} .. ${max(prices):,.2f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
