"""Load the vendored macro CSVs into raw, native-unit monthly series.

This layer does NO currency or inflation math -- it returns each metric exactly
in the unit the source publishes it in.  Composition into USD / CAD / *_NI is
the job of units.Converter (driven by QuoteSource).

  BCPI_MONTHLY    Bank of Canada commodity price index, USD-native, 1972-01=100.
                  M.ENER -> NRGY, M.MTLS -> BULN, M.AGRI -> FOOD.  (USD)
  AU-USD          Gold spot, USD per troy oz, monthly from 1833.            (USD)
  CAD_Wages_1997  StatCan all-industries average hourly wage.   (CAD; see labour.py)
  STATIC_INFLATIONCALC   Total Canadian CPI, monthly.            (inflation rail)
  USD_CAD_1972    USD/CAD exchange rate (DEXCAUS/100, CAD per USD).   (FX rail)

Pure stdlib (csv/datetime).
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
US_DIR = DATA_DIR / "us"          # vendored FRED pulls (fetch_feedstock.py)

BCPI_CSV = DATA_DIR / "BCPI_MONTHLY-sd-1972-01-01.csv"
CPI_CSV = DATA_DIR / "STATIC_INFLATIONCALC.csv"        # Canadian CPI
USCPI_CSV = DATA_DIR / "CPI-USD.csv"                    # US CPI
USD_CAD_CSV = DATA_DIR / "USD_CAD_1972.csv"
GOLD_CSV = DATA_DIR / "AU-USD.csv"
BTC_CSV = DATA_DIR / "BTC-USD.csv"

# Commodity token -> BCPI sub-index column (all USD-native indices, 100 at 1972).
COMMODITY_COLS = {
    "NRGY": "M.ENER",   # Energy
    "BULN": "M.MTLS",   # Metals and Minerals ("bullion")
    "FOOD": "M.AGRI",   # Agriculture
}


def _parse_date(s: str) -> date:
    y, m, d = s.strip().split("-")
    return date(int(y), int(m), int(d))


def _read_boc_observations(path: Path) -> tuple[list[str], list[tuple[date, dict]]]:
    """Read a Bank of Canada CSV (BOM, prose header, then OBSERVATIONS block)."""
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header: list[str] | None = None
        rows: list[tuple[date, dict]] = []
        for cells in reader:
            if not cells:
                if header is not None:
                    break
                continue
            if header is None:
                if cells[0].strip() == "date":
                    header = [c.strip() for c in cells]
                continue
            if not cells[0].strip():
                break
            d = _parse_date(cells[0])
            vals = {col: (float(raw) if raw.strip() else None)
                    for col, raw in zip(header[1:], cells[1:])}
            rows.append((d, vals))
    if header is None:
        raise ValueError(f"no 'date' header found in {path}")
    return header[1:], rows


def load_bcpi() -> dict[str, list[tuple[date, float]]]:
    """Raw USD BCPI sub-indices for NRGY/BULN/FOOD (index, 100 at 1972-01)."""
    _, rows = _read_boc_observations(BCPI_CSV)
    out: dict[str, list[tuple[date, float]]] = {}
    for token, col in COMMODITY_COLS.items():
        out[token] = [(d, v[col]) for d, v in rows if v[col] is not None]
    return out


def load_gold() -> list[tuple[date, float]]:
    """Gold spot, USD per troy oz, monthly (full history from 1833)."""
    out: list[tuple[date, float]] = []
    with GOLD_CSV.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            out.append((_parse_date(row["Date"]), float(row["Price"])))
    out.sort(key=lambda r: r[0])
    return out


def load_btc() -> list[tuple[date, float]]:
    """Bitcoin spot, USD, DAILY (from 2010-07).  Real high-frequency series."""
    out: list[tuple[date, float]] = []
    with BTC_CSV.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            out.append((_parse_date(row["Date"]), float(row["Price"])))
    out.sort(key=lambda r: r[0])
    return out


def load_cacpi() -> dict[date, float]:
    """Total Canadian CPI keyed by month (the CAD inflation rail)."""
    _, rows = _read_boc_observations(CPI_CSV)
    return {d: v["STATIC_INFLATIONCALC"] for d, v in rows
            if v["STATIC_INFLATIONCALC"]}


def load_uscpi() -> dict[date, float]:
    """US CPI index keyed by month (the USD inflation rail)."""
    out: dict[date, float] = {}
    with USCPI_CSV.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["Index"]:
                out[_parse_date(row["Date"])] = float(row["Index"])
    return out


def load_fx() -> dict[date, float]:
    """USD/CAD (CAD per USD) keyed by month; DEXCAUS index / 100 (the FX rail)."""
    out: dict[date, float] = {}
    with USD_CAD_CSV.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            out[_parse_date(row["observation_date"])] = float(row["DEXCAUS"]) / 100.0
    return out


# ------------------------------------------------------ US real feeds (FRED)
#
# Vendored by fetch_feedstock.py under data/us/<name>.csv (header: date,value).
# CNST and NRGC are equal-weight INDEX composites (each component normalized to
# 100 at the earliest month all its components share, then averaged); LABR_US is
# a single $/hr wage series used as-is.  All are USD-native, monthly.

# Composite component sets and the documented (equal) weights.
CNST_COMPONENTS = ["ppi_steel", "ppi_lumber", "ppi_cement", "ppi_gravel"]
NRGC_COMPONENTS = ["energy_gasoline", "energy_electricity", "energy_natgas"]
FOOD_COMPONENTS = ["food_beef", "food_bread", "food_bananas"]


def us_data_available() -> bool:
    """True when the vendored FRED pulls are present (else US feeds are skipped)."""
    return US_DIR.is_dir() and (US_DIR / "wage_ahetpi.csv").exists()


def _load_us_csv(name: str) -> list[tuple[date, float]]:
    """A vendored FRED series (header date,value) -> sorted monthly points."""
    out: list[tuple[date, float]] = []
    with (US_DIR / f"{name}.csv").open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("value") not in (None, "", "."):
                out.append((_parse_date(row["date"]), float(row["value"])))
    out.sort(key=lambda r: r[0])
    return out


def _equal_weight_index(
    components: list[list[tuple[date, float]]],
    base: date | None = None,
) -> tuple[list[tuple[date, float]], date]:
    """Equal-weight composite INDEX (=100 at `base`) over aligned components.

    Each component is normalized to 100 at the common base month, then the
    components are equal-weight averaged.  Only months present in EVERY
    component contribute; the base defaults to the earliest such shared month.
    """
    luts = [dict(c) for c in components]
    common = sorted(set(luts[0]).intersection(*(set(l) for l in luts[1:])))
    if not common:
        raise ValueError("components share no common month")
    if base is None:
        base = common[0]
    bases = [l[base] for l in luts]
    series = [
        (m, sum(100.0 * l[m] / b for l, b in zip(luts, bases)) / len(luts))
        for m in common
    ]
    return series, base


def load_construction_us() -> list[tuple[date, float]]:
    """CNST: equal-weight US construction-PPI index (100 = base, USD).

    Components (equal 1/4 weight): steel WPU101, lumber WPU081, cement WPU1322,
    gravel/stone WPU1321.  Base month = earliest shared (cement-limited, 1971-01).
    """
    comps = [_load_us_csv(n) for n in CNST_COMPONENTS]
    series, _ = _equal_weight_index(comps)
    return series


def load_labour_us() -> list[tuple[date, float]]:
    """LABR_US: US average hourly earnings, $/hr, monthly (AHETPI, 1964+, USD)."""
    return _load_us_csv("wage_ahetpi")


def load_energy_us() -> list[tuple[date, float]]:
    """NRGC: equal-weight US retail-energy index (100 = base, USD).

    Components (equal 1/3 weight): gasoline APU000074714 ($/gal), electricity
    APU000072610 ($/kWh), utility gas APU000072620 ($/therm).  Base month =
    earliest shared (1978-11, gas/electricity-limited).
    """
    comps = [_load_us_csv(n) for n in NRGC_COMPONENTS]
    series, _ = _equal_weight_index(comps)
    return series


def load_food_us() -> list[tuple[date, float]]:
    """FOOD: equal-weight US retail-food index (100 = base, USD).

    Components (equal 1/3 weight): ground beef APU0000703112 ($/lb), white bread
    APU0000702111 ($/lb), bananas APU0000711211 ($/lb).  Base month = earliest
    shared (1984-01, ground-beef-limited).
    """
    comps = [_load_us_csv(n) for n in FOOD_COMPONENTS]
    series, _ = _equal_weight_index(comps)
    return series


if __name__ == "__main__":
    bcpi = load_bcpi()
    for token, s in bcpi.items():
        print(f"{token}: {len(s)} mo  {s[0][0]}={s[0][1]:.1f} -> {s[-1][0]}={s[-1][1]:.1f}  (USD index)")
    g = load_gold()
    print(f"XAU : {len(g)} mo  {g[0][0]}=${g[0][1]:.2f} -> {g[-1][0]}=${g[-1][1]:.2f}  (USD/oz)")
    fx = load_fx()
    cpi = load_cacpi()
    print(f"FX  : {min(fx)}..{max(fx)}   CPI: {min(cpi)}..{max(cpi)}")
    if us_data_available():
        for tok, s in (("CNST", load_construction_us()),
                       ("LABR_US", load_labour_us()),
                       ("NRGC", load_energy_us()),
                       ("FOOD_US", load_food_us())):
            unit = "$/hr" if tok == "LABR_US" else "index"
            print(f"{tok:7}: {len(s)} mo  {s[0][0]}={s[0][1]:.2f} -> "
                  f"{s[-1][0]}={s[-1][1]:.2f}  (USD {unit})")
