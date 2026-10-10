"""Three commodity-like prices that wander around a common USD inflation trend.

The hypothesis under test: a basic commodity's price is bounded below by what
it costs to produce and above by what replaces it, so over years it follows
the currency's inflation, each with its own delay. A price here is

    base * exp( inflation(t) + lag_gap_i(t) + departure_i(t) )

- inflation(t): the log of a USD price level, piecewise-constant rates.
- lag_gap_i: TOKEN i follows that level through a first-order lag of
  `lag_months`. At a steady rate the gap is constant and folded into the base;
  when the rate changes a slow TOKEN falls behind (or stays ahead) and closes
  the gap over a few lags.
- departure_i: its own bounded oscillation, a sum of sinusoids with seeded
  phases and frequencies, the slow ones larger. `spread` is its standard
  deviation and `crossing_months` the mean time between crossings of its
  trend; `floor` and `ceiling` squash it softly, the floor the tighter. The
  three are made uncorrelated over the live window.

Every term is a closed form in time, so the path does not depend on the
sampling rate, and history before day zero is the same process continued
backwards. History is deflated: it is each TOKEN's price in day-zero dollars,
which is what a BUCK that kept its value would have paid.

Time is compressed: `months_per_day` months of market time pass in one lab
day. The shapes and their relative timing are meant to look like a decade of
monthly data; the hours-scale machinery sees them thirty times too fast.
doc/review/commodity-inflation.py measures the vendored series these
parameters are taken from.
"""
import math
import random

from .model import digest

VERSION = "commodities-v1"


def _inflation(schedule, months_per_day):
    """Segments (start month, log rate per month), from [[start_day, percent_per_year], ...]."""
    if not schedule or schedule[0][0] != 0:
        raise ValueError("the inflation schedule must begin at day 0")
    days = [float(d) for d, _ in schedule]
    if days != sorted(set(days)) or any(not math.isfinite(float(r)) or not -50 < float(r) < 100 for _, r in schedule):
        raise ValueError("inflation needs increasing start days and rates in percent a year")
    return [(d * months_per_day, math.log1p(float(r) / 100) / 12) for d, r in schedule]


def _level(segments, month):
    """Log price level, zero at day 0; the first rate also holds before it."""
    if month <= 0:
        return segments[0][1] * month
    total = 0.
    for (start, rate), following in zip(segments, [*segments[1:], (math.inf, 0.)]):
        total += rate * (min(month, following[0]) - start)
        if month <= following[0]:
            break
    return total


def _lag_gap(segments, lag, month):
    """How far a first-order follower sits from its steady-rate position."""
    if month <= 0 or lag <= 0:
        return 0.
    steady, gap = segments[0][1], 0.
    for (start, rate), following in zip(segments, [*segments[1:], (math.inf, 0.)]):
        rest = -lag * (rate - steady)
        elapsed = min(month, following[0]) - start
        gap = rest + (gap - rest) * math.exp(-elapsed / lag)
        if month <= following[0]:
            break
    return gap


def _oscillation(seed, index, token, components):
    """(angular frequency, phase, share of the spread) for each sinusoid.

    Frequencies are spread over a wide band and the slower ones carry more of
    the variance, as a wandering price does. They are then scaled so that the
    crossings of the trend average `crossing_months` apart: for a stationary
    oscillation that interval is pi over the variance-weighted rms frequency.
    """
    stream = random.Random(digest([seed, VERSION, index, components]))
    # One frequency in each slice of the band: two that nearly coincide would
    # beat for centuries and leave the spread permanently off its mark.
    low, width = math.log(.3), math.log(5. / .3) / components
    multiples = [math.exp(low + (k + stream.uniform(.1, .9)) * width) for k in range(components)]
    weights = [1 / math.sqrt(m) for m in multiples]
    power = sum(w * w for w in weights)
    rms = math.sqrt(sum(w * w * m * m for w, m in zip(weights, multiples)) / power)
    base = math.pi / token["crossing_months"] / rms
    return [(base * m, stream.uniform(0, 2 * math.pi), w / math.sqrt(power)) for m, w in zip(multiples, weights)]


def _shape(waves, month):
    """One oscillation at unit spread."""
    return math.sqrt(2) * sum(share * math.cos(w * month + phase) for w, phase, share in waves)


def _unmix(shapes):
    """Coefficients that leave the three oscillations uncorrelated over the samples given.

    Independent draws this slow still correlate by chance over ten or so
    swings. Each oscillation gives up its projection on the ones before it,
    so the test does not depend on a lucky seed. The same constants apply to
    history, which is therefore only nearly uncorrelated.
    """
    kept, means, coefficients = [], [], []
    for u in shapes:
        row = []
        for v, mean in zip(kept, means):
            centred = [x - mean for x in v]
            beta = sum(a * c for a, c in zip(u, centred)) / sum(c * c for c in centred)
            u = [a - beta * c for a, c in zip(u, centred)]
            row.append(beta)
        kept.append(u)
        means.append(sum(u) / len(u))
        coefficients.append(row)
    return coefficients, means


def _departures(tokens, waves, mix, month):
    coefficients, means = mix
    mixed = []
    for token, w, row in zip(tokens, waves, coefficients):
        u = _shape(w, month) - sum(beta * (v - mean) for beta, v, mean in zip(row, mixed, means))
        mixed.append(u)
    out = []
    for token, u in zip(tokens, mixed):
        x = token["spread"] * u
        bound = token.get("ceiling") if x > 0 else token.get("floor")
        out.append(bound * math.tanh(x / bound) if bound else x)
    return out


def commodity_inputs(cfg, profile):
    spec = profile["commodities"]
    tokens = spec["tokens"]
    if len(tokens) != 3:
        raise ValueError("the commodity profile describes exactly three TOKENs")
    scale = float(spec.get("months_per_day", 1.))
    components = int(spec.get("components", 10))
    noise = float(spec.get("noise", 0.))
    if not 0 < scale <= 12 or not 2 <= components <= 32 or not 0 <= noise < .05:
        raise ValueError("invalid commodity time compression, component count or noise")
    for t in tokens:
        if (not 0 <= t["lag_months"] <= 120 or not 0 <= t["spread"] < 1 or not t["crossing_months"] > 0
                or t.get("floor", -1) >= 0 or t.get("ceiling", 1) <= 0):
            raise ValueError("invalid commodity lag, spread, crossing time or bounds")
    segments = _inflation(spec["inflation"], scale)
    waves = [_oscillation(cfg["seed"], i, t, components) for i, t in enumerate(tokens)]
    base = cfg["feed"]["base"]
    # A fixed quarter-day grid over the live window, whatever the run's own sampling.
    mix = _unmix([[_shape(w, k / 4 * scale) for k in range(4 * cfg["days"] + 1)] for w in waves])

    def real(day):          # day-zero dollars: no price level
        month = day * scale
        return [b * math.exp(_lag_gap(segments, t["lag_months"], month) + x)
                for b, t, x in zip(base, tokens, _departures(tokens, waves, mix, month))]

    history = [dict(day=d, prices=[round(p, 6) for p in real(d)])
               for d in range(-cfg["bootstrap"]["history_days"], 0)]
    rows = []
    for step in range(cfg["days"] * cfg["ticks_per_day"] + 1):
        day = step / cfg["ticks_per_day"]
        level = math.exp(_level(segments, day * scale))
        prices = real(day)
        if noise:
            prices = [p * math.exp(noise * random.Random(digest([cfg["seed"], VERSION, "noise", i, step])).gauss(0., 1.))
                      for i, p in enumerate(prices)]
        rows.append(dict(day=day, target_usd=[round(p * level, 6) for p in prices],
                         inflation_usd=round(level, 9), real_usd=[round(p, 6) for p in prices]))
    return dict(rows=rows, history=history, provenance=dict(kind="commodities", version=VERSION,
        labels=[t.get("label", f"TOKEN {'ABC'[i]}") for i, t in enumerate(tokens)],
        months_per_day=scale, inflation_percent_per_year=[[d, r] for d, r in spec["inflation"]],
        tokens=tokens, waves=[[list(w) for w in ws] for ws in waves], unmixing=mix[0], noise=noise,
        ending_inflation_usd=rows[-1]["inflation_usd"],
        method="base * exp(inflation + first-order lag gap + bounded seeded oscillation), closed form in time. "
               "History is the same process before day 0, in day-zero dollars. Synthetic: calibrated to the "
               "departures of real prices from US CPI, not a replay of them."))
