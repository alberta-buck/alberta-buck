"""Synthesize LABR: the gross cost of Canadian labour, 1972-present (CAD/hr).

LABR is the price to *obtain* the labour component of civilization -- what an
employer actually pays per hour, across an all-industries average of wage-earner
classes.  It is a GROSS, nominal CAD series; currency / inflation views are
applied later by units.Converter.  (Earlier drafts carried a "stagnation"
detrend; that modeled *net take-home*, a different quantity, and has been
removed.  The post-1997 real-wage "windfall" is genuine in gross terms -- labour
must be paid in line with true, end-user inflation -- and LABR keeps it.)

Data: StatCan all-industries average hourly wage, 1997-2024 (CAD_Wages_1997.csv).
For 1972-1996 there is no series, so wages are back-cast along CPI (b=1): under
the project thesis, pre-1995 CPI tracked true inflation and wages tracked CPI, so

    wage(t) = wage(1997) * CPI(t) / CPI(1997)        for t < 1997

which lands ~$3.80/hr in 1972 (about 2x the era's $1.90 federal minimum).  Actual
data is spliced on from 1997.  Annual points are interpolated log-linearly to
months.  Pure stdlib.
"""

from __future__ import annotations

import csv
import math
from datetime import date

from alberta_buck.sim.quotes.ingest import DATA_DIR, load_cacpi

WAGES_CSV = DATA_DIR / "CAD_Wages_1997.csv"

# Sanity reference only: ~2x the 1972 federal minimum of $1.90 (architecture doc).
WAGE_1972_REF = 3.80


def _load_annual_wages() -> dict[int, float]:
    """All-industries average hourly wage rate, by year (1997-2024)."""
    out: dict[int, float] = {}
    with WAGES_CSV.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            naics = row["North American Industry Classification System (NAICS)"]
            if naics != "Total employees, all industries":
                continue
            if row["Wages"] != "Average hourly wage rate" or not row["VALUE"]:
                continue
            out[int(row["REF_DATE"])] = float(row["VALUE"])
    return out


def _annual_cpi(cpi: dict[date, float]) -> dict[int, float]:
    acc: dict[int, list[float]] = {}
    for d, v in cpi.items():
        acc.setdefault(d.year, []).append(v)
    return {y: sum(vs) / len(vs) for y, vs in acc.items()}


def _interp_loglinear(anchors: list[tuple[float, float]], t: float) -> float:
    if t <= anchors[0][0]:
        return anchors[0][1]
    if t >= anchors[-1][0]:
        return anchors[-1][1]
    for (t0, v0), (t1, v1) in zip(anchors, anchors[1:]):
        if t0 <= t <= t1:
            f = (t - t0) / (t1 - t0)
            return math.exp(math.log(v0) * (1 - f) + math.log(v1) * f)
    return anchors[-1][1]


def load_labour_cad() -> list[tuple[date, float]]:
    """Monthly gross nominal wage, CAD/hr, 1972-01 .. last CPI month."""
    cpi = load_cacpi()
    cpi_months = sorted(d for d in cpi if d.year >= 1972)
    cpi_y = _annual_cpi(cpi)
    wages = _load_annual_wages()
    y_lo, y_hi = min(wages), max(wages)   # actual-data range (1997..2024)

    # Annual nominal wage: actual where we have it; outside that range, CPI-track
    # from the nearest actual year (back-cast before y_lo, forward after y_hi) so
    # the gross level -- including the post-1997 windfall -- carries through.
    nominal_year: dict[int, float] = {}
    for y in range(1972, cpi_months[-1].year + 1):
        if y not in cpi_y:
            continue
        if y in wages:
            nominal_year[y] = wages[y]
        elif y < y_lo:
            nominal_year[y] = wages[y_lo] * cpi_y[y] / cpi_y[y_lo]
        else:  # y > y_hi
            nominal_year[y] = wages[y_hi] * cpi_y[y] / cpi_y[y_hi]
    anchors = [(y + 0.5, nominal_year[y]) for y in sorted(nominal_year)]

    def _tyears(d: date) -> float:
        return d.year + (d.month - 0.5) / 12.0

    return [(d, _interp_loglinear(anchors, _tyears(d))) for d in cpi_months]


if __name__ == "__main__":
    s = load_labour_cad()
    print(f"LABR: {len(s)} months  {s[0][0]}=${s[0][1]:.2f}/hr -> "
          f"{s[-1][0]}=${s[-1][1]:.2f}/hr  (gross nominal CAD)")
