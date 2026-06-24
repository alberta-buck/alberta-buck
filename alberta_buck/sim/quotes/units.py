"""Currency / inflation unit conversion for raw metrics.

Every raw metric (BCPI sub-index, gold, wages, ...) is carried in its *native*
unit -- a currency (USD or CAD), nominal.  A `Converter` turns a value from its
native unit into any target unit, using three rails:

  * FX        USD_CAD (CAD per USD), to move between currencies;
  * US CPI    (CPI-USD), to neutralize USD inflation;
  * Canadian CPI (STATIC_INFLATIONCALC), to neutralize CAD inflation.

Supported target units:

  USD     nominal US dollars                 (the sim default)
  CAD     nominal Canadian dollars
  USD_NI  inflation-neutralized USD          (USD x USCPI(base)/USCPI(t))
  CAD_NI  inflation-neutralized CAD          (CAD x CACPI(base)/CACPI(t))

Each currency is neutralized by its OWN CPI, so USD_NI and CAD_NI track US vs
Canadian inflation independently.  At the base date every unit collapses to its
nominal currency value.  Conversions are linear, so a single
`ratio(native_ccy, target, t)` characterizes each one.  Rails are looked up with
carry-forward (latest value at or before t), tolerating month gaps.  Pure stdlib.
"""

from __future__ import annotations

import bisect
import enum
from datetime import date


class Unit(enum.Enum):
    USD = "USD"
    CAD = "CAD"
    USD_NI = "USD_NI"
    CAD_NI = "CAD_NI"

    @property
    def currency(self) -> str:
        return "CAD" if self in (Unit.CAD, Unit.CAD_NI) else "USD"

    @property
    def neutralized(self) -> bool:
        return self in (Unit.USD_NI, Unit.CAD_NI)


class _Rail:
    """A monthly series with carry-forward lookup (latest value <= t)."""

    def __init__(self, series: dict[date, float]):
        self.keys = sorted(series)
        self.series = series

    def at(self, t: date) -> float:
        i = bisect.bisect_right(self.keys, t) - 1
        return self.series[self.keys[max(0, i)]]

    @property
    def lo(self) -> date:
        return self.keys[0]

    @property
    def hi(self) -> date:
        return self.keys[-1]


class Converter:
    """Convert native-currency nominal values into any `Unit`.

    fx:    {month: CAD per USD}      (USD_CAD)
    cacpi: {month: Canadian CPI}
    uscpi: {month: US CPI}
    base:  the date whose dollars NI units are expressed in.
    """

    def __init__(self, fx: dict[date, float], cacpi: dict[date, float],
                 uscpi: dict[date, float], base: date):
        self.fx = _Rail(fx)
        self.cacpi = _Rail(cacpi)
        self.uscpi = _Rail(uscpi)
        self.base = base
        # Valid range = where all rails exist (NI / cross-currency need them).
        self.dmin = max(self.fx.lo, self.cacpi.lo, self.uscpi.lo)
        self.dmax = min(self.fx.hi, self.cacpi.hi, self.uscpi.hi)
        if not (self.dmin <= base <= self.dmax):
            raise ValueError(f"base {base} outside convertible range "
                             f"{self.dmin}..{self.dmax}")
        self.cacpi_base = self.cacpi.at(base)
        self.uscpi_base = self.uscpi.at(base)

    def convert(self, value: float, native_ccy: str, target: Unit,
                t: date) -> float:
        # 1) currency
        v = value
        if native_ccy != target.currency:
            fx = self.fx.at(t)
            v = value * fx if native_ccy == "USD" else value / fx
        # 2) inflation neutralization, by the target currency's own CPI
        if target.neutralized:
            if target.currency == "USD":
                v *= self.uscpi_base / self.uscpi.at(t)
            else:
                v *= self.cacpi_base / self.cacpi.at(t)
        return v

    def ratio(self, native_ccy: str, target: Unit, t: date) -> float:
        """Multiplicative factor native-nominal -> target unit at month t."""
        return self.convert(1.0, native_ccy, target, t)
