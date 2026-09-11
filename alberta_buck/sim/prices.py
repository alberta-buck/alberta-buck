"""Load the daily close-price CSV vectors (gen_prices.py schema).

CSV rows: `day,close_usd_micro` where close_usd_micro = USD * 1e6.

BUCK is 6-dec with a uint80 balance cap; full BTC/gold magnitudes overflow
the basket-minted BUCK quantity, and arbitrage is scale-invariant, so every
monetary magnitude is divided by PRICE_SCALE.  Units stay "USDC micro per
whole token"; only the scale shrinks.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CSV_DIR = HERE / "prices"

PRICE_SCALE = 100


class PricesIncomplete(RuntimeError):
    """A price CSV is partial or inconsistent (WAVE3.org decision 21): a
    generator was still writing it, or its length disagrees with the
    window's manifest.  Regenerate (gen_historical) and rerun."""


_WINDOW_TAG = re.compile(r"-(\d{4}-\d{2}-\d{2}-\d+d)\.csv$")


class Prices:
    def __init__(self, files: list[str]):
        self.series: list[list[int]] = []
        for fn in files:
            rows = []
            with (CSV_DIR / fn).open() as f:
                r = csv.reader(f)
                next(r)  # header
                for n, row in enumerate(r, start=2):
                    if len(row) < 2:
                        raise PricesIncomplete(
                            f"{fn}: short row at line {n} -- a generator was "
                            f"still writing this file; regenerate and rerun")
                    rows.append(int(row[1]) // PRICE_SCALE)
            self.series.append(rows)
        lengths = [len(s) for s in self.series]
        if len(set(lengths)) > 1:
            raise PricesIncomplete(
                "price series lengths disagree: "
                + ", ".join(f"{fn}={n}" for fn, n in zip(files, lengths))
                + " -- a partial file from a concurrent generation; "
                  "regenerate and rerun")
        # Window-stamped files carry a manifest with the intended length;
        # a complete file that is SHORTER than the manifest is the silent
        # failure (a 1093-frame vector from a 1827-day window).
        if files:
            m = _WINDOW_TAG.search(files[0])
            if m:
                manifest = CSV_DIR / f"hist-manifest-{m.group(1)}.json"
                if manifest.exists():
                    try:
                        want = int(json.loads(manifest.read_text()).get("n_days", 0))
                    except Exception:
                        want = 0
                    if want and lengths and lengths[0] != want:
                        raise PricesIncomplete(
                            f"{files[0]}: {lengths[0]} rows but the window's "
                            f"manifest says {want}; regenerate and rerun")
        self.days = min(lengths) if lengths else 0

    def ref(self, token_idx: int, day: int) -> int:
        """Reference price: USDC micro (6-dec) per 1 whole token, scaled."""
        return self.series[token_idx][day]

    def day0(self, token_idx: int) -> int:
        return self.series[token_idx][0]
