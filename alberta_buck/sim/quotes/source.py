"""QuoteSource: native-unit metrics -> target unit -> deterministic hourly quotes.

Pipeline per token:
  metrics.Metric (native USD/CAD)  ->  units.Converter (to target Unit)  ->
  monthly anchors in the target unit  ->  reusable Brownian bridge (walks.py)  ->
  hourly path that lands exactly on each monthly anchor.

Default unit is USD (the sim prices everything in nominal USD); CAD / CAD_NI /
USD_NI are available for analysis.  The hourly walk:
  * is pinned to both monthly endpoints (bridge vanishes at the joins);
  * wiggles by an amount = that token's local month-to-month volatility (in the
    chosen unit), so calm metrics stay calm and choppy ones stay choppy;
  * draws texture from a tier (smooth/medium/jagged) by that volatility and a
    shape by a stable hash of (token, year, month) -- reproducible, varied.

`at(token, ts)` is the continuous accessor; `as_prices(...)` is the sim's
`Prices`-compatible adapter.  Pure stdlib.
"""

from __future__ import annotations

import bisect
import math
from datetime import date, datetime, timedelta

from alberta_buck.sim.quotes import ingest, walks
from alberta_buck.sim.quotes.metrics import TOKENS, load_metrics
from alberta_buck.sim.quotes.units import Converter, Unit

DEFAULT_AMP = 1.0
DEFAULT_VOL_WINDOW = 12
VOL_FLOOR = 1e-4
BASE_DATE = date(1972, 1, 1)   # NI "constant dollars" base


def _stable_hash(s: str) -> int:
    """FNV-1a 32-bit -- reproducible across runs (unlike Python's hash())."""
    h = 0x811C9DC5
    for ch in s.encode():
        h = ((h ^ ch) * 0x01000193) & 0xFFFFFFFF
    return h


def _rolling_log_vol(values: list[float], window: int) -> list[float]:
    rets = [0.0] + [math.log(values[i] / values[i - 1]) for i in range(1, len(values))]
    out = []
    for i in range(len(values)):
        lo = max(1, i - window + 1)
        win = rets[lo:i + 1]
        if len(win) >= 2:
            m = sum(win) / len(win)
            var = sum((r - m) ** 2 for r in win) / (len(win) - 1)
            out.append(max(VOL_FLOOR, math.sqrt(var)))
        else:
            out.append(VOL_FLOOR)
    return out


def _quantiles(xs: list[float], n: int) -> list[float]:
    s = sorted(xs)
    return [s[min(len(s) - 1, (len(s) * k) // n)] for k in range(1, n)]


class _TokenModel:
    def __init__(self, name: str, anchors: list[tuple[date, float]],
                 lib: walks.WalkLibrary, amp: float, vol_window: int):
        self.name = name
        self.dates = [datetime(d.year, d.month, d.day) for d, _ in anchors]
        self.values = [v for _, v in anchors]
        self.lib = lib
        self.amp = amp
        self.vol = _rolling_log_vol(self.values, vol_window)
        self.tier_cuts = _quantiles(self.vol, lib.n_tiers)
        self._seg_cache: dict[int, tuple] = {}

    def _tier(self, vol: float) -> int:
        return bisect.bisect_right(self.tier_cuts, vol)

    def _segment(self, m: int) -> tuple:
        cached = self._seg_cache.get(m)
        if cached is not None:
            return cached
        a, b = self.values[m], self.values[m + 1]
        vol = max(self.vol[m], self.vol[m + 1])
        d = self.dates[m]
        shape = self.lib.shape(self._tier(vol), _stable_hash(f"{self.name}-{d.year}-{d.month}"))
        out = (a, b, self.amp * vol, shape)
        self._seg_cache[m] = out
        return out

    def at(self, ts: datetime) -> float:
        if ts <= self.dates[0]:
            return self.values[0]
        if ts >= self.dates[-1]:
            return self.values[-1]
        m = bisect.bisect_right(self.dates, ts) - 1
        a, b, amp, shape = self._segment(m)
        span = (self.dates[m + 1] - self.dates[m]).total_seconds()
        s = (ts - self.dates[m]).total_seconds() / span
        return (a + (b - a) * s) * (1.0 + amp * shape.at(s))


class QuoteSource:
    """Historical quotes for the basket tokens, in a chosen currency/inflation unit.

    unit:      Unit.USD (default) | CAD | CAD_NI | USD_NI.
    amp:       intra-month wiggle scale (RMS = amp * local monthly log-vol).
    base_date: NI "constant dollars of" date (default 1972-01).
    """

    def __init__(self, unit: Unit = Unit.USD, amp: float = DEFAULT_AMP,
                 vol_window: int = DEFAULT_VOL_WINDOW, base_date: date = BASE_DATE,
                 tokens: list[str] | None = None):
        self.unit = unit
        self.tokens = list(tokens) if tokens is not None else list(TOKENS)
        lib = walks.library()
        conv = Converter(ingest.load_fx(), ingest.load_cacpi(),
                         ingest.load_uscpi(), base_date)
        metrics = load_metrics()
        self.models: dict[str, _TokenModel] = {}
        for name in self.tokens:
            m = metrics[name]
            anchors = [(d, conv.convert(v, m.currency, unit, d))
                       for d, v in m.anchors if conv.dmin <= d <= conv.dmax]
            self.models[name] = _TokenModel(name, anchors, lib, amp, vol_window)
        self.start = max(m.dates[0] for m in self.models.values())
        self.end = min(m.dates[-1] for m in self.models.values())

    def anchors(self, token: str) -> list[tuple[datetime, float]]:
        m = self.models[token]
        return list(zip(m.dates, m.values))

    def at(self, token: str, ts: datetime | date) -> float:
        if isinstance(ts, date) and not isinstance(ts, datetime):
            ts = datetime(ts.year, ts.month, ts.day)
        return self.models[token].at(ts)

    def hourly(self, token: str, start: datetime, hours: int):
        for h in range(hours):
            yield self.at(token, start + timedelta(hours=h))

    def as_prices(self, start: datetime | None = None, hours_per_step: int = 1,
                  scale: int = 1_000_000):
        """A `Prices`-compatible view (ref/day0) for a future historical scenario.

        Values are integer-scaled quotes in this source's `unit` (value * scale);
        token index order is metrics.TOKENS.  Commodity tokens are USD indices
        (100 = 1972); XAU/LABR are absolute USD prices.
        """
        return _PricesAdapter(self, start or self.start, hours_per_step, scale)


class _PricesAdapter:
    def __init__(self, qs: QuoteSource, start: datetime, hours_per_step: int, scale: int):
        self.qs = qs
        self.start = start
        self.hours_per_step = hours_per_step
        self.scale = scale
        self.tokens = list(TOKENS)
        max_hours = int((qs.end - start).total_seconds() // 3600)
        self.days = max(0, max_hours // hours_per_step)

    def _ts(self, step: int) -> datetime:
        return self.start + timedelta(hours=step * self.hours_per_step)

    def ref(self, token_idx: int, step: int) -> int:
        return round(self.qs.at(self.tokens[token_idx], self._ts(step)) * self.scale)

    def day0(self, token_idx: int) -> int:
        return self.ref(token_idx, 0)


if __name__ == "__main__":
    for unit in (Unit.USD, Unit.CAD, Unit.CAD_NI, Unit.USD_NI):
        qs = QuoteSource(unit=unit)
        parts = []
        for token in TOKENS:
            a = qs.anchors(token)
            parts.append(f"{token} {a[0][1]:.1f}->{a[-1][1]:.1f}")
        print(f"[{unit.value:7}] {qs.start.date()}..{qs.end.date()}  " + "  ".join(parts))
