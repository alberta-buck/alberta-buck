#!/usr/bin/env python3
"""Generate daily close-price CSV vectors for the routing simulation.

PAXG  (gold, USD/troy oz)   18-dec token
cbBTC (bitcoin, USD)         8-dec token
AOIL  ("Alberta Oil", USD/bbl) 18-dec synthetic token

Deterministic geometric-Brownian-motion walks (fixed seed).
One row per day: day,close_usd_micro
close_usd_micro is integer micro-dollars (1e6 == $1.00).
"""

import csv, math, random
from pathlib import Path

HERE = Path(__file__).resolve().parent

DAYS = 730  # 2-year daily horizon

SERIES = [
    # (filename, start USD, annual drift, annual vol, seed)
    ("paxg.csv",   2600.0,  0.08, 0.16, 42),
    ("cbbtc.csv", 65000.0,  0.15, 0.55, 43),
    ("aoil.csv",     78.0,  0.02, 0.35, 44),
]

USDC = 1_000_000  # 1e6 micro-dollars == $1.00


def gen(start: float, mu: float, sigma: float, seed: int):
    rng = random.Random(seed)
    dt = 1.0 / 365.25
    p = start
    out = [p]
    for _ in range(1, DAYS):
        z = rng.gauss(0.0, 1.0)
        p *= math.exp((mu - 0.5 * sigma * sigma) * dt + sigma * math.sqrt(dt) * z)
        out.append(p)
    return out


def main():
    for fname, start, mu, sigma, seed in SERIES:
        prices = gen(start, mu, sigma, seed)
        path = HERE / fname
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["day", "close_usd_micro"])
            for day, px in enumerate(prices):
                w.writerow([day, round(px * USDC)])
        print(f"  {fname}: {DAYS} rows "
              f"${prices[0]:,.2f} -> ${prices[-1]:,.2f} "
              f"[${min(prices):,.2f} .. ${max(prices):,.2f}]")


if __name__ == "__main__":
    print(f"Generating {DAYS}-day price CSVs:")
    main()
    print("Done.")
