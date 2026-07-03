"""Raw metric registry: named native-unit monthly series.

A `Metric` is the unit-tagged source data for one token, before any currency or
inflation conversion.  QuoteSource pulls these, converts each to the requested
target unit (units.Converter), and lays the hourly walk on top.

Adding a new metric is a one-liner here: load a native-unit series and tag its
currency.  Everything downstream (conversion, walks, the Prices adapter) then
works unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from alberta_buck.sim.quotes import ingest, labour

# Sim token order (also the Prices adapter's token_idx order).
TOKENS = ["NRGY", "BULN", "FOOD", "XAU", "LABR"]

# US-real feeds (FRED, native USD).  Registered alongside the defaults but kept
# OUT of the default TOKENS basket so the existing sim/adapter/tests are
# unchanged; select them explicitly via QuoteSource(tokens=US_TOKENS).
US_TOKENS = ["CNST", "LABR_US", "NRGC", "FOOD_US"]


@dataclass
class Metric:
    name: str
    currency: str                      # native nominal currency: "USD" or "CAD"
    anchors: list[tuple[date, float]]  # monthly (date, value) in the native unit


def load_metrics() -> dict[str, Metric]:
    """All raw metrics keyed by token name, in native (nominal) units."""
    out: dict[str, Metric] = {}
    for name, series in ingest.load_bcpi().items():   # NRGY/BULN/FOOD (USD index)
        out[name] = Metric(name, "USD", series)
    out["XAU"] = Metric("XAU", "USD", ingest.load_gold())       # USD/oz
    out["LABR"] = Metric("LABR", "CAD", labour.load_labour_cad())  # CAD/hr gross
    if ingest.us_data_available():                              # US-real feeds
        out["CNST"] = Metric("CNST", "USD", ingest.load_construction_us())  # index
        out["LABR_US"] = Metric("LABR_US", "USD", ingest.load_labour_us())  # $/hr
        out["NRGC"] = Metric("NRGC", "USD", ingest.load_energy_us())        # index
        out["FOOD_US"] = Metric("FOOD_US", "USD", ingest.load_food_us())    # index
    return out
