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
import fcntl
import json
import os
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

    # WINDOW-STAMPED outputs: every distinct window gets its own file set
    # (hist-<sym>-<end>-<Nd>.csv) and manifest, so scenarios with different
    # horizons (1y historical, 2y realistic, 3y growth, 5y canonical) never
    # clobber each other or the COMMITTED base-name CSVs (hist-<sym>.csv --
    # the canonical 5-year snapshot the rebalance-policy model reads; those
    # are only ever rewritten explicitly by a human running this module).
    tag = f"{e.isoformat()}-{len(dates)}d"
    want_files = [fname.replace(".csv", f"-{tag}.csv")
                  for _, fname, _, _ in BINDINGS]
    manifest = out_dir / f"hist-manifest-{tag}.json"
    out_dir.mkdir(parents=True, exist_ok=True)

    # WAVE3.org decision 21: several runs starting together (the matrix's
    # xargs fan-out, the catalogue's and star's thread pools) generate the
    # SAME window.  Generate-or-reuse is therefore serialized under an
    # advisory lock, and every file is written atomically (temp + rename)
    # with the manifest LAST, so a reader that finds the manifest finds
    # complete files, and a reader arriving mid-generation waits at the
    # lock instead of reading a half-written CSV (three of fourteen matrix
    # arms did exactly that on 2026-09-10).
    lock_path = out_dir / f"hist-manifest-{tag}.lock"
    with lock_path.open("a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            if manifest.exists():
                try:
                    have = json.loads(manifest.read_text())
                    if (have.get("start") == s.isoformat()
                            and have.get("end") == e.isoformat()
                            and have.get("files") == want_files
                            and all((out_dir / f).exists() for f in want_files)):
                        return want_files, have["n_days"], s, e
                except Exception:
                    pass

            sources = {"us": qs_us, "xau": qs_xau}

            def price(metric, src, d):
                if src == "btc":
                    return btc_at(d)
                return sources[src].at(metric, d)

            files = []
            for (sym, base, metric, src), fname in zip(BINDINGS, want_files):
                tmp = out_dir / f"{fname}.tmp-{os.getpid()}"
                with tmp.open("w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["day", "close_usd_micro"])
                    for k, d in enumerate(dates):
                        w.writerow([k, round(price(metric, src, d) * USDC)])
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, out_dir / fname)
                files.append(fname)
                first = price(metric, src, dates[0])
                last = price(metric, src, dates[-1])
                print(f"  {fname}: {len(dates)} days  {first:,.2f} -> {last:,.2f}")
            _atomic_write_text(manifest, json.dumps({
                "start": s.isoformat(), "end": e.isoformat(),
                "files": files, "n_days": len(dates)}))
            return files, len(dates), s, e
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _atomic_write_text(path, text: str) -> None:
    """Write `text` to `path` through a same-directory temp file and a
    rename, so no reader ever sees a partial file (decision 21)."""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="alberta_buck.sim.gen_historical")
    ap.add_argument("--start", default=None, help="ISO date (default: end - years)")
    ap.add_argument("--end", default=None, help="ISO date (default: last data month)")
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--base", action="store_true",
                    help="ALSO rewrite the committed base-name CSVs "
                         "(hist-<sym>.csv) to this window -- the canonical "
                         "snapshot; do this deliberately and commit it")
    a = ap.parse_args(argv)
    files, n, s, e = gen(a.start, a.end, a.years)
    print(f"wrote {len(files)} CSVs, {n} days, {s} .. {e}")
    if a.base:
        import shutil
        from alberta_buck.sim.prices import CSV_DIR as _dir
        for (sym, base, _m, _s), fname in zip(BINDINGS, files):
            shutil.copyfile(_dir / fname, _dir / base)
            print(f"  canonical: {fname} -> {base}")


if __name__ == "__main__":
    main()
