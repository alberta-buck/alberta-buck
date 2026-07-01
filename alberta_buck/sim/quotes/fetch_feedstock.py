"""Fetch real US retail / producer price feedstock from FRED (keyless CSV).

FRED serves every series as a headerless-auth CSV at

    https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>

with two columns: ``observation_date,<SERIES>``.  This module pulls the monthly
series that back the three US-real basket tokens plus the M2 / CPI references,
and *vendors* the raw pulls under ``data/us/`` so downstream loads (ingest.py)
and the Cantillon lag check run reproducibly offline.

    CNST (construction PPI, monthly index):
        steel     WPU101       avg hourly earnings feed for the composite
        lumber    WPU081
        cement    WPU1322
        gravel    WPU1321      (crushed stone / gravel)
    LABR (US wage, monthly $/hr) -- longest history, for the M2 lag test:
        AHETPI                 avg hourly earnings, prod/nonsupervisory, 1964+
    NRGC (energy retail, monthly):
        gasoline     APU000074714
        electricity  APU000072610   ($/kWh)
        natural gas  APU000072620   (utility piped gas, $/therm)
    references:
        M2SL     (money supply)   CPIAUCSL (all-items CPI)

Run ``python -m alberta_buck.sim.quotes.fetch_feedstock`` to (re)download every
series and rewrite the vendored CSVs.  Add ``--lag`` for the M2 Cantillon check.
Pure stdlib (urllib / csv / datetime).
"""

from __future__ import annotations

import csv
import math
import sys
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
US_DIR = HERE / "data" / "us"

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"

# Vendored name  ->  FRED series id.  Chosen from the primaries in the brief;
# every one verified non-empty and current (see module docstring for alts).
SERIES: dict[str, str] = {
    # -- CNST construction PPI components (monthly index) --
    "ppi_steel": "WPU101",       # iron & steel PPI            (1926+)
    "ppi_lumber": "WPU081",      # lumber PPI                  (1926+)
    "ppi_cement": "WPU1322",     # concrete ingredients PPI    (1971+)
    "ppi_gravel": "WPU1321",     # construction sand/gravel/stone PPI (1947+)
    # -- LABR US wage ($/hr) -- longest history for the lag test --
    "wage_ahetpi": "AHETPI",     # avg hourly earnings, prod/nonsup (1964+)
    # -- NRGC energy retail (monthly) --
    "energy_gasoline": "APU000074714",     # gasoline, $/gal          (1976+)
    "energy_electricity": "APU000072610",  # electricity, $/kWh    (1978-11+)
    "energy_natgas": "APU000072620",       # utility gas, $/therm  (1978-11+)
    # -- references (M2 lag check + normalization sanity) --
    "m2": "M2SL",                # M2 money stock, $B          (1959+)
    "cpi": "CPIAUCSL",           # CPI-U all items             (1947+)
}


def fetch_fred(series_id: str) -> list[tuple[date, float]]:
    """GET the fredgraph CSV for ``series_id`` -> sorted monthly (date, value).

    Skips '.' missing-value rows.  Raises on HTTP error or empty response.
    """
    url = FRED_CSV.format(series_id)
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 (trusted host)
        text = resp.read().decode("utf-8")
    reader = csv.reader(text.splitlines())
    header = next(reader, None)
    if not header or header[0].strip().lower() != "observation_date":
        raise ValueError(f"{series_id}: unexpected header {header!r}")
    out: list[tuple[date, float]] = []
    for row in reader:
        if len(row) < 2 or not row[0].strip():
            continue
        raw = row[1].strip()
        if raw in ("", "."):          # FRED missing-value marker
            continue
        y, m, d = row[0].strip().split("-")
        out.append((date(int(y), int(m), int(d)), float(raw)))
    if not out:
        raise ValueError(f"{series_id}: no observations returned")
    out.sort(key=lambda r: r[0])
    return out


def _write_csv(path: Path, series: list[tuple[date, float]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "value"])
        for d, v in series:
            w.writerow([d.isoformat(), f"{v:g}"])


def main() -> None:
    US_DIR.mkdir(parents=True, exist_ok=True)
    print(f"fetching {len(SERIES)} FRED series -> {US_DIR}")
    for name, sid in SERIES.items():
        series = fetch_fred(sid)
        _write_csv(US_DIR / f"{name}.csv", series)
        print(f"  {name:20} {sid:15} {len(series):5} rows  "
              f"{series[0][0]} .. {series[-1][0]}")


# --------------------------------------------------------------- Cantillon lag
#
# YoY-growth Pearson correlation, lag-swept over [-36, +36] months.  A positive
# peak lag L means the wage/construction series LAGS M2 by L months (M2 growth
# at t-L best explains price growth at t) -- the Cantillon transmission delay.

def _shift_months(d: date, k: int) -> date:
    m = d.month - 1 + k
    return date(d.year + m // 12, m % 12 + 1, d.day)


def _yoy(series: list[tuple[date, float]]) -> dict[date, float]:
    """{month: year-over-year growth} for a monthly (date, value) series."""
    lut = {d: v for d, v in series}
    out: dict[date, float] = {}
    for d, v in series:
        prev = _shift_months(d, -12)
        if prev in lut and lut[prev] > 0:
            out[d] = v / lut[prev] - 1.0
    return out


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    return cov / math.sqrt(vx * vy)


def lag_scan(price_yoy: dict[date, float], m2_yoy: dict[date, float],
             lags: range = range(-36, 37)) -> dict[int, tuple[int, float]]:
    """For each lag L, correlate price_yoy(t) with m2_yoy(t-L).

    Returns {L: (overlap_months, corr)}.
    """
    out: dict[int, tuple[int, float]] = {}
    for L in lags:
        xs: list[float] = []
        ys: list[float] = []
        for d, pv in price_yoy.items():
            md = _shift_months(d, -L)
            if md in m2_yoy:
                xs.append(pv)
                ys.append(m2_yoy[md])
        out[L] = (len(xs), _pearson(xs, ys) if len(xs) >= 24 else 0.0)
    return out


def m2_lag_report() -> None:
    """Print the AHETPI-vs-M2 and CNST-vs-M2 YoY lag-correlation summary."""
    from alberta_buck.sim.quotes import ingest

    m2 = _yoy(ingest._load_us_csv("m2"))
    checks = {
        "LABR (AHETPI wage)": ingest.load_labour_us(),
        "CNST (construction)": ingest.load_construction_us(),
        "NRGC (energy)": ingest.load_energy_us(),
        "CPI (all items)": ingest._load_us_csv("cpi"),
    }
    print("\nCantillon lag check -- YoY-growth Pearson corr vs M2SL, "
          "lag L in [-36,+36] mo (L>0 => series lags M2)")
    print(f"{'series':24} {'overlap':>8} {'corr@0':>8} "
          f"{'peakLag':>8} {'peakCorr':>9}")
    for label, series in checks.items():
        py = _yoy(series)
        scan = lag_scan(py, m2)
        overlap0, corr0 = scan[0]
        peakL, (n_peak, peak) = max(scan.items(), key=lambda kv: kv[1][1])
        print(f"{label:24} {overlap0:8} {corr0:8.3f} "
              f"{peakL:8d} {peak:9.3f}")


if __name__ == "__main__":
    if "--lag" in sys.argv:
        m2_lag_report()
    else:
        main()
