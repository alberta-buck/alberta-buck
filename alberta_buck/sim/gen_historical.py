#!/usr/bin/env python3
"""Generate daily price CSVs for the HISTORICAL / EQUILIBRIUM scenarios.

Writes one `day,close_usd_micro` CSV per token into the sim prices dir, sampling
the historical quote source (alberta_buck.sim.quotes) once per calendar day over
a chosen window.  The recomposed M2-laggard basket (real-economy anchors + a
small hard/crypto satellite), ALL native USD:

  LABR  <- US wage (AHETPI, $/hr)           US feed, monthly, bridged to daily
  CNST  <- US construction PPI index        US feed, monthly, bridged to daily
  FOOD  <- US retail-food index             US feed, monthly, bridged to daily
  NRGC  <- US retail-energy index           US feed, monthly, bridged to daily
  PAXG  <- gold (AU-USD, $/oz)              default source, monthly, bridged
  cbBTC <- bitcoin (BTC-USD, $)             REAL daily series (used directly)

Two quote sources feed the bindings by `source` tag:
  "us"  -> QuoteSource(tokens=US_TOKENS)  (CNST/LABR_US/NRGC/FOOD_US, native USD)
  "xau" -> default QuoteSource()          (gold, native USD)
  "btc" -> real daily BTC series          (ingest.load_btc)

The CAD legs are gone -- every token is USD-native now.  The sim's numeraire is
the pool quote (USDC): each token's CSV is its price in USD, taken at face value
as the pool price; explicit per-token weights (Scenario.tokens weightBp, threaded
into addBasketToken at deploy) set the basket composition.

Run:  python -m alberta_buck.sim.gen_historical --years 5
"""

from __future__ import annotations

import argparse
import bisect
import csv
from datetime import date, timedelta

from alberta_buck.sim.prices import CSV_DIR
from alberta_buck.sim.quotes import QuoteSource, Unit
from alberta_buck.sim.quotes import ingest
from alberta_buck.sim.quotes.metrics import US_TOKENS

USDC = 1_000_000  # micro-dollars per $1 (matches gen_prices schema)

# (symbol, csv filename, quote metric, source tag).  All native USD.
#   "us"  -> US real-economy QuoteSource(tokens=US_TOKENS)
#   "xau" -> default QuoteSource() (gold)
#   "btc" -> real daily BTC (metric ignored)
BINDINGS = [
    ("LABR",  "hist-labr.csv",  "LABR_US", "us"),
    ("CNST",  "hist-cnst.csv",  "CNST",    "us"),
    ("FOOD",  "hist-food.csv",  "FOOD_US", "us"),
    ("NRGC",  "hist-nrgc.csv",  "NRGC",    "us"),
    ("PAXG",  "hist-paxg.csv",  "XAU",     "xau"),
    ("cbBTC", "hist-cbbtc.csv", None,      "btc"),   # real daily series
]


def _daily_carry(series):
    """Carry-forward lookup over a (date, value) series for any calendar day."""
    keys = [d for d, _ in series]
    vals = [v for _, v in series]

    def at(d: date) -> float:
        return vals[max(0, bisect.bisect_right(keys, d) - 1)]

    return at, keys[0], keys[-1]


def resolve_window(start, end, years, data_start, data_end):
    e = date.fromisoformat(end) if end else data_end
    s = date.fromisoformat(start) if start else e - timedelta(days=round(years * 365.25))
    s = max(s, data_start)
    e = min(e, data_end)
    if s >= e:
        raise ValueError(f"empty window {s}..{e}")
    return s, e


def gen(start=None, end=None, years=5.0, out_dir=CSV_DIR):
    """Write the historical CSVs (one per BINDINGS token); return
    (filenames, n_days, start, end).

    Samples two USD QuoteSources -- the default (gold) and a US real-economy
    source (CNST/LABR/NRGC/FOOD) -- plus the real daily BTC series, once per
    calendar day over the shared window.
    """
    qs_xau = QuoteSource(Unit.USD)                       # default TOKENS (gold)
    qs_us = QuoteSource(Unit.USD, tokens=US_TOKENS)      # US real-economy feeds
    btc_at, btc_lo, btc_hi = _daily_carry(ingest.load_btc())

    data_end = min(qs_xau.end.date(), qs_us.end.date(), btc_hi)
    data_start = max(qs_xau.start.date(), qs_us.start.date(), btc_lo)
    s, e = resolve_window(start, end, years, data_start, data_end)
    dates = [s + timedelta(days=k) for k in range((e - s).days + 1)]

    sources = {"us": qs_us, "xau": qs_xau}

    def price(metric, src, d):
        if src == "btc":
            return btc_at(d)
        return sources[src].at(metric, d)

    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for sym, fname, metric, src in BINDINGS:
        with (out_dir / fname).open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["day", "close_usd_micro"])
            for k, d in enumerate(dates):
                w.writerow([k, round(price(metric, src, d) * USDC)])
        files.append(fname)
        first = price(metric, src, dates[0])
        last = price(metric, src, dates[-1])
        print(f"  {fname}: {len(dates)} days  {first:,.2f} -> {last:,.2f}")
    return files, len(dates), s, e


def main(argv=None):
    ap = argparse.ArgumentParser(prog="alberta_buck.sim.gen_historical")
    ap.add_argument("--start", default=None, help="ISO date (default: end - years)")
    ap.add_argument("--end", default=None, help="ISO date (default: last data month)")
    ap.add_argument("--years", type=float, default=5.0)
    a = ap.parse_args(argv)
    files, n, s, e = gen(a.start, a.end, a.years)
    print(f"wrote {len(files)} CSVs, {n} days, {s} .. {e}")


if __name__ == "__main__":
    main()
