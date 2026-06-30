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
    return out
