#!/usr/bin/env python3
"""Generate daily price CSVs for the HISTORICAL scenario from real macro data.

Writes one `day,close_usd_micro` CSV per token into the sim prices dir, sampling
the historical quote source (alberta_buck.sim.quotes) once per calendar day over
a chosen window:

  PAXG  <- gold (AU-USD), USD            monthly real, bridged to daily
  cbBTC <- bitcoin (BTC-USD), USD        REAL daily series (used directly)
  NRGC  <- energy (BCPI M.ENER), CAD     monthly real, bridged to daily
  LABR  <- labour (synth wage), CAD      monthly, bridged to daily

PAXG/cbBTC are USD; NRGC/LABR are CAD (the Canadian basket legs carry USD/CAD FX
dynamics).  The sim's numeraire is the pool quote (USDC): each token's CSV is its
price in its own unit, taken at face value as the pool price -- so "equal weights
by value at start" (deploy seeds every pool to a common quote-depth and the
basket targets equal shares) holds in that numeraire.

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

USDC = 1_000_000  # micro-dollars per $1 (matches gen_prices schema)

# (symbol, csv filename, quote metric, unit).  cbBTC is special: real daily BTC.
BINDINGS = [
    ("PAXG",  "hist-paxg.csv",  "XAU",  Unit.USD),
    ("cbBTC", "hist-cbbtc.csv", None,   None),     # real daily series
    ("NRGC",  "hist-nrgc.csv",  "NRGY", Unit.CAD),
    ("LABR",  "hist-labr.csv",  "LABR", Unit.CAD),
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
    """Write the four historical CSVs; return (filenames, n_days, start, end)."""
    qs = {Unit.USD: QuoteSource(Unit.USD), Unit.CAD: QuoteSource(Unit.CAD)}
    btc_at, btc_lo, btc_hi = _daily_carry(ingest.load_btc())

    data_end = min(qs[Unit.USD].end.date(), btc_hi)
    data_start = max(qs[Unit.USD].start.date(), btc_lo)
    s, e = resolve_window(start, end, years, data_start, data_end)
    dates = [s + timedelta(days=k) for k in range((e - s).days + 1)]

    def price(sym, metric, unit, d):
        if sym == "cbBTC":
            return btc_at(d)
        return qs[unit].at(metric, d)

    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for sym, fname, metric, unit in BINDINGS:
        with (out_dir / fname).open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["day", "close_usd_micro"])
            for k, d in enumerate(dates):
                w.writerow([k, round(price(sym, metric, unit, d) * USDC)])
        files.append(fname)
        first = price(*( (sym, metric, unit, dates[0]) ))
        last = price(sym, metric, unit, dates[-1])
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
