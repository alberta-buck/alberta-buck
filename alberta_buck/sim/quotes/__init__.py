"""Historical commodity, gold & labour quote source for the basket sim.

Turns real Bank of Canada / StatCan / market series into deterministic hourly
price quotes for the simulated tokens, in a chosen currency/inflation unit:

    NRGY  <- BCPI Energy (M.ENER)              USD index, 100 = 1972
    BULN  <- BCPI Metals and Minerals (M.MTLS) USD index, 100 = 1972
    FOOD  <- BCPI Agriculture (M.AGRI)         USD index, 100 = 1972
    XAU   <- gold spot (AU-USD)                USD / troy oz
    LABR  <- synthesized Canadian labour cost  CAD / hr (gross), back-cast to 1972

Each metric is carried in its native unit; units.Converter expresses it in the
requested target Unit (USD default, or CAD / CAD_NI / USD_NI), and reusable
seeded Brownian bridges (walks.py) fill hourly samples between monthly anchors
with variability mirroring the source data.

    from alberta_buck.sim.quotes import QuoteSource, Unit
    qs = QuoteSource()                  # nominal USD (sim default)
    qs.at("XAU", datetime(1980, 1, 15))
    real = QuoteSource(unit=Unit.CAD_NI)   # inflation-neutralized CAD
    prices = qs.as_prices()                # Prices-compatible sim adapter
"""

from alberta_buck.sim.quotes.source import QuoteSource
from alberta_buck.sim.quotes.metrics import TOKENS
from alberta_buck.sim.quotes.units import Unit

__all__ = ["QuoteSource", "TOKENS", "Unit"]
