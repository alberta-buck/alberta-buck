"""Load the daily close-price CSV vectors (gen_prices.py schema).

CSV rows: `day,close_usd_micro` where close_usd_micro = USD * 1e6.

BUCK is 6-dec with a uint80 balance cap; full BTC/gold magnitudes overflow
the basket-minted BUCK quantity, and arbitrage is scale-invariant, so every
monetary magnitude is divided by PRICE_SCALE.  Units stay "USDC micro per
whole token"; only the scale shrinks.
"""

from __future__ import annotations

import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
CSV_DIR = HERE / "prices"

PRICE_SCALE = 100


class Prices:
    def __init__(self, files: list[str]):
        self.series: list[list[int]] = []
        for fn in files:
            rows = []
            with (CSV_DIR / fn).open() as f:
                r = csv.reader(f)
                next(r)  # header
                for row in r:
                    rows.append(int(row[1]) // PRICE_SCALE)
            self.series.append(rows)
        self.days = min(len(s) for s in self.series)

    def ref(self, token_idx: int, day: int) -> int:
        """Reference price: USDC micro (6-dec) per 1 whole token, scaled."""
        return self.series[token_idx][day]

    def day0(self, token_idx: int) -> int:
        return self.series[token_idx][0]
