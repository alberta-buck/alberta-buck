#!/usr/bin/env python3
"""Generate synthetic daily reference prices for PAXG, cbBTC, AOIL.

Produces 3 CSV files in prices/ with columns: date, price_usd.
Prices follow geometric Brownian motion with plausible drift and vol.
"""

import csv
import math
import os
import random
from datetime import date, timedelta

OUT = os.path.join(os.path.dirname(__file__), "prices")

CONFIG = {
    "PAXG":  {"start": 2600.0,  "drift": 0.08, "vol": 0.16, "seed": 42},
    "cbBTC": {"start": 65000.0, "drift": 0.15, "vol": 0.55, "seed": 43},
    "AOIL":  {"start": 78.0,   "drift": 0.02, "vol": 0.35, "seed": 44},
}

START_DATE = date(2024, 1, 1)
DAYS = 730  # 2 years


def generate(name: str, cfg: dict) -> None:
    rng = random.Random(cfg["seed"])
    prices = []
    p = cfg["start"]
    dt = 1.0 / 365.25

    for i in range(DAYS):
        d = START_DATE + timedelta(days=i)
        # Sample with some intra-day noise
        daily = p * math.exp(
            (cfg["drift"] - 0.5 * cfg["vol"] ** 2) * dt
            + cfg["vol"] * rng.gauss(0, math.sqrt(dt))
        )
        prices.append((d.isoformat(), round(daily, 2)))
        p = daily

    path = os.path.join(OUT, f"{name}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "price_usd"])
        w.writerows(prices)
    print(f"  {name}.csv: {len(prices)} rows, {prices[0][1]:.2f} -> {prices[-1][1]:.2f}")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    print("Generating price CSVs:")
    for name, cfg in CONFIG.items():
        generate(name, cfg)
    print("Done.")
