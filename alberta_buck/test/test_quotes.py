"""Tests for the historical quote source (alberta_buck.sim.quotes).

Properties under test:
  * monthly anchors are hit EXACTLY by the hourly path (Brownian-bridge pinning);
  * the source is fully deterministic (seed-fixed walks, stable hashing);
  * intra-month variability mirrors each token's real volatility ordering;
  * unit conversion (USD/CAD/CAD_NI/USD_NI) is correct and self-consistent;
  * gold (XAU) loads as a USD-native token;
  * the Prices adapter is self-consistent (the future sim seam).
"""

import bisect
from datetime import date, datetime, timedelta

import pytest

from alberta_buck.sim.quotes import QuoteSource, TOKENS, Unit
from alberta_buck.sim.quotes import ingest, walks


# ---------------------------------------------------------------- walk library

def test_walk_library_dozen_normalized_bridges():
    lib = walks.library()
    shapes = [s for tier in lib.tiers for s in tier]
    assert len(shapes) == 12
    for sh in shapes:
        assert sh.samples[0] == 0.0 and sh.samples[-1] == 0.0   # pinned ends
        assert sh.at(0.0) == 0.0 and sh.at(1.0) == 0.0
        rms = (sum(x * x for x in sh.samples) / len(sh.samples)) ** 0.5
        assert abs(rms - 1.0) < 1e-9                            # unit RMS


def test_walk_library_is_singleton_and_seed_fixed():
    a = walks.library()
    b = walks.WalkLibrary()
    assert a.tiers[0][0].samples[:8] == b.tiers[0][0].samples[:8]


# ----------------------------------------------------------------- anchors

@pytest.fixture(scope="module")
def qs():
    return QuoteSource()          # USD


def test_token_set_and_span(qs):
    assert TOKENS == ["NRGY", "BULN", "FOOD", "XAU", "LABR"]
    assert (qs.start.year, qs.start.month) == (1972, 1)
    assert qs.end.year == 2025
    for token in ("NRGY", "BULN", "FOOD"):
        assert abs(qs.anchors(token)[0][1] - 100.0) < 1e-9     # USD index, 100=1972
    assert qs.anchors("XAU")[0][1] > 10.0                       # gold $/oz absolute
    assert qs.anchors("LABR")[0][1] > 1.0                       # wage $/hr absolute


def test_hourly_path_hits_every_monthly_anchor(qs):
    for token in TOKENS:
        for ts, value in qs.anchors(token):
            assert abs(qs.at(token, ts) - value) <= 1e-6 * max(1.0, abs(value)), (token, ts)


def test_segment_boundaries_are_continuous(qs):
    for token in TOKENS:
        anchors = qs.anchors(token)
        ts, value = anchors[len(anchors) // 2]
        assert abs(qs.at(token, ts - timedelta(seconds=1)) - value) < 1e-3 * max(1.0, abs(value))


def test_fully_deterministic_across_instances():
    a, b = QuoteSource(), QuoteSource()
    start = a.start + timedelta(days=365 * 20)
    assert list(a.hourly("NRGY", start, 500)) == list(b.hourly("NRGY", start, 500))


# ------------------------------------------------------------- volatility shape

def _hourly_log_vol(qs, token, start, hours):
    import math
    xs = list(qs.hourly(token, start, hours))
    rets = [math.log(xs[i] / xs[i - 1]) for i in range(1, len(xs)) if xs[i - 1] > 0]
    m = sum(rets) / len(rets)
    return (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5


def test_intra_month_variability_mirrors_source():
    import statistics
    # Use native CAD so labour's intrinsic calm shows (no imported FX noise).
    cad = QuoteSource(Unit.CAD)
    mv = {t: statistics.median(cad.models[t].vol) for t in TOKENS}
    assert mv["LABR"] == min(mv.values())            # labour: lowest source vol
    assert mv["NRGY"] > mv["LABR"] * 3               # energy much choppier
    assert mv["XAU"] > mv["LABR"] * 3                # gold much choppier
    # ...and that ordering survives into the realized hourly path: labour calmest.
    rv = {t: _hourly_log_vol(cad, t, datetime(2008, 1, 1), 24 * 360) for t in TOKENS}
    assert rv["LABR"] == min(rv.values())


def test_usd_pricing_imports_fx_volatility_into_labour():
    import statistics
    # Labour is CAD-native; priced in USD it inherits USD/CAD FX volatility,
    # so its month-to-month vol jumps relative to its near-flat native series.
    cad, usd = QuoteSource(Unit.CAD), QuoteSource(Unit.USD)
    assert (statistics.median(usd.models["LABR"].vol)
            > 3 * statistics.median(cad.models["LABR"].vol))


# ------------------------------------------------------------- unit conversion

def test_currency_conversion_matches_fx():
    usd, cad = QuoteSource(Unit.USD), QuoteSource(Unit.CAD)
    fx = ingest.load_fx()
    ts, _ = cad.anchors("LABR")[len(cad.anchors("LABR")) // 2]   # a LABR anchor
    rate = fx[date(ts.year, ts.month, 1)]                        # CAD per USD
    assert cad.at("LABR", ts) == pytest.approx(usd.at("LABR", ts) * rate, rel=1e-9)


def test_neutralization_identity_at_base():
    # At the 1972-01 base, NI units collapse to their nominal currency value.
    usd, usd_ni = QuoteSource(Unit.USD), QuoteSource(Unit.USD_NI)
    cad, cad_ni = QuoteSource(Unit.CAD), QuoteSource(Unit.CAD_NI)
    base = usd.anchors("NRGY")[0][0]
    assert usd_ni.at("NRGY", base) == pytest.approx(usd.at("NRGY", base), rel=1e-9)
    assert cad_ni.at("NRGY", base) == pytest.approx(cad.at("NRGY", base), rel=1e-9)


def test_neutralization_strips_inflation_trend():
    cad, cad_ni = QuoteSource(Unit.CAD), QuoteSource(Unit.CAD_NI)
    end = cad.end
    nom_growth = cad.at("NRGY", end) / cad.at("NRGY", cad.start)
    real_growth = cad_ni.at("NRGY", end) / cad_ni.at("NRGY", cad_ni.start)
    assert nom_growth > real_growth * 3              # inflation removed


def test_usd_ni_uses_us_cpi():
    # USD_NI deflates nominal USD by the US CPI rail, base-relative.
    usd, usd_ni = QuoteSource(Unit.USD), QuoteSource(Unit.USD_NI)
    uscpi = ingest.load_uscpi()
    keys = sorted(uscpi)

    def cpi_at(d):
        i = bisect.bisect_right(keys, date(d.year, d.month, 1)) - 1
        return uscpi[keys[max(0, i)]]

    base = usd.anchors("NRGY")[0][0]
    ts = usd.anchors("NRGY")[400][0]
    expected = usd.at("NRGY", ts) * cpi_at(base) / cpi_at(ts)
    assert usd_ni.at("NRGY", ts) == pytest.approx(expected, rel=1e-9)


def test_usd_ni_distinct_from_cad_ni():
    # US and Canadian inflation differ, so the two NI units diverge off-base.
    usd_ni, cad_ni = QuoteSource(Unit.USD_NI), QuoteSource(Unit.CAD_NI)
    ts = usd_ni.anchors("NRGY")[400][0]
    a, b = usd_ni.at("NRGY", ts), cad_ni.at("NRGY", ts)
    assert abs(a - b) > 1e-3 * b


# ------------------------------------------------------------------ gold/labour

def test_gold_token_loaded(qs):
    xau = qs.anchors("XAU")
    vals = [v for _, v in xau]
    assert min(vals) < 100.0 and max(vals) > 1000.0   # spans the gold bull run


def test_labour_gross_windfall_is_kept():
    # Inflation-neutralized gross labour rises (the genuine post-1997 windfall),
    # but modestly -- it is not a runaway like gold.
    ni = QuoteSource(Unit.CAD_NI)
    start = ni.at("LABR", ni.start)
    end = ni.at("LABR", ni.end)
    assert 1.1 < end / start < 2.0


# --------------------------------------------------------------- Prices adapter

def test_prices_adapter_self_consistent(qs):
    p = qs.as_prices(hours_per_step=1)
    assert p.tokens == TOKENS
    assert p.days > 24 * 365 * 50
    for i in range(len(TOKENS)):
        assert p.day0(i) == p.ref(i, 0)
        assert isinstance(p.ref(i, 1000), int) and p.ref(i, 1000) > 0
