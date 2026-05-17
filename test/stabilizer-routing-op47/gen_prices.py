#!/usr/bin/env python3
"""Generate three daily close-price CSV vectors for the routing simulation.

PAXG  (gold, ~troy oz USD)   18-dec token in the sim
cbBTC (bitcoin, USD)          8-dec token in the sim
AOIL  ("Alberta Oil", USD/bbl) 18-dec synthetic token in the sim

Deterministic geometric-Brownian-motion-ish random walks (fixed seed) so the
Forge fixture and the plot test always see the same series.  One row per day:

    day,close_usd

`close_usd` is an integer number of micro-dollars (1e6 == $1.00), matching the
6-dec USDC accounting used on-chain so the fixture parses a plain uint with no
float handling.
"""

import csv
import math
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent

DAYS = 365  # one-year daily horizon (bounded; the multi-year run is the
            # documented Python-driver extension, out of scope here)

# (filename, start price USD, annual drift, annual vol)
SERIES = [
    ("paxg.csv",  2000.0, 0.06, 0.16),  # gold
    ("cbbtc.csv", 60000.0, 0.20, 0.55),  # bitcoin
    ("aoil.csv",  70.0, -0.02, 0.35),  # oil
]

USDC = 1_000_000  # 1e6 micro-dollars == $1.00


def gen(start: float, mu: float, sigma: float, rng: random.Random):
    dt = 1.0 / 365.0
    p = start
    out = [p]
    for _ in range(1, DAYS):
        z = rng.gauss(0.0, 1.0)
        p *= math.exp((mu - 0.5 * sigma * sigma) * dt + sigma * math.sqrt(dt) * z)
        out.append(p)
    return out


def main():
    rng = random.Random(0x42)  # fixed seed -- reproducible series
    for fname, start, mu, sigma in SERIES:
        prices = gen(start, mu, sigma, rng)
        path = HERE / fname
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["day", "close_usd_micro"])
            for day, px in enumerate(prices):
                w.writerow([day, round(px * USDC)])
        print(f"{fname}: {DAYS} rows, "
              f"start ${prices[0]:,.2f} end ${prices[-1]:,.2f} "
              f"min ${min(prices):,.2f} max ${max(prices):,.2f}")


if __name__ == "__main__":
    main()
