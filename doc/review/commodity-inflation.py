"""Do the basket's commodities oscillate around USD inflation, each with its own delay?

A descriptive check on the vendored monthly series (alberta_buck/sim/quotes/data),
not a forecast. For each price P and the US CPI it reports:

- the drift of the real price log(P / CPI), in percent a year;
- how far the real price strays from its own trend line, and how long a
  departure lasts (AR(1) half-life, and the mean time between crossings);
- the delay at which its year-over-year change best matches CPI's and M2's;
- how much the departures of different commodities move together.

A second table repeats the measurements inside ten-year windows, each against
that window's own trend line. Those are the figures the control lab's
synthetic commodity profile is calibrated to: a 120-day run stands for a decade.

Run: PYTHONPATH=. python3 doc/review/commodity-inflation.py [--json out.json]
"""
import json
import math
import sys
from datetime import date

from alberta_buck.sim.quotes import ingest
from alberta_buck.sim.quotes.fetch_feedstock import _pearson, _yoy, lag_scan

START = date(1976, 1, 1)                # the first month every series has


def monthly(series):
    return {d.replace(day=1): v for d, v in series}


def load():
    bcpi = ingest.load_bcpi()
    series = {"Gold": ingest.load_gold(), "Energy (US retail)": ingest.load_energy_us(),
              "Food (US retail)": ingest.load_food_us(), "Construction (US PPI)": ingest.load_construction_us(),
              "Labour (US hourly)": ingest.load_labour_us(),
              "Energy (BCPI)": bcpi["NRGY"], "Metals (BCPI)": bcpi["BULN"], "Agriculture (BCPI)": bcpi["FOOD"]}
    return {k: monthly(v) for k, v in series.items()}, monthly(ingest._load_us_csv("cpi")), monthly(ingest._load_us_csv("m2"))


def fit_line(ys):
    n = len(ys)
    mx, my = (n - 1) / 2, sum(ys) / n
    slope = sum((i - mx) * (y - my) for i, y in enumerate(ys)) / sum((i - mx) ** 2 for i in range(n))
    return slope, my - slope * mx


def describe(prices, cpi, m2):
    months = sorted(d for d in prices if d >= START and d in cpi)
    real = [math.log(prices[d] / cpi[d]) for d in months]
    slope, intercept = fit_line(real)
    away = [r - (intercept + slope * i) for i, r in enumerate(real)]
    spread = math.sqrt(sum(x * x for x in away) / len(away))
    phi = _pearson(away[:-1], away[1:])
    crossings = sum(1 for a, b in zip(away, away[1:]) if (a < 0) != (b < 0))
    yoy = {d: v for d, v in _yoy(sorted(prices.items())).items() if d >= START}
    best = {}
    for name, driver in (("cpi", cpi), ("m2", m2)):
        scan = lag_scan(yoy, _yoy(sorted(driver.items())), lags=range(0, 49))
        lag, (_, corr) = max(scan.items(), key=lambda kv: kv[1][1])
        best[name] = dict(lag_months=lag, correlation=corr, at_zero=scan[0][1])
    return dict(first=str(months[0]), last=str(months[-1]), months=len(months),
                real_drift_pct_year=100 * 12 * slope, spread_log=spread,
                low_log=min(away), high_log=max(away),
                half_life_months=math.log(.5) / math.log(phi) if 0 < phi < 1 else None,
                months_between_crossings=len(away) / max(crossings, 1),
                monthly_volatility=math.sqrt(sum((b - a) ** 2 for a, b in zip(real, real[1:])) / (len(real) - 1)),
                lag=best, away=dict(zip(map(str, months), away)))


def median(values):
    return sorted(values)[len(values) // 2]


def decades(prices, cpi, window=120, step=12):
    """Per series: medians over rolling ten-year windows, and the median pairwise correlation."""
    real = {name: {d: math.log(p[d] / cpi[d]) for d in p if d >= START and d in cpi} for name, p in prices.items()}

    def departures(values):
        slope, intercept = fit_line(values)
        return slope, [v - (intercept + slope * i) for i, v in enumerate(values)]

    rows = {}
    for name, series in real.items():
        values = [series[d] for d in sorted(series)]
        found = dict(drift=[], spread=[], reach=[], crossing=[], volatility=[])
        for start in range(0, len(values) - window + 1, step):
            slope, away = departures(values[start:start + window])
            crossings = sum(1 for a, b in zip(away, away[1:]) if (a < 0) != (b < 0))
            found["drift"].append(abs(1200 * slope))
            found["spread"].append(math.sqrt(sum(x * x for x in away) / window))
            found["reach"].append(max(abs(x) for x in away))
            found["crossing"].append(window / max(crossings, 1))
            found["volatility"].append(math.sqrt(sum((b - a) ** 2 for a, b in zip(away, away[1:])) / (window - 1)))
        rows[name] = dict(windows=len(found["drift"]), **{k: median(v) for k, v in found.items()})
    together = {}
    for a in real:
        for b in real:
            months = sorted(set(real[a]) & set(real[b]))
            va, vb = [real[a][d] for d in months], [real[b][d] for d in months]
            together[a, b] = median([_pearson(departures(va[s:s + window])[1], departures(vb[s:s + window])[1])
                                     for s in range(0, len(months) - window + 1, step)])
    return rows, together


def main(argv):
    prices, cpi, m2 = load()
    rows = {name: describe(p, cpi, m2) for name, p in prices.items()}
    print(f"Real price = log(price / US CPI), monthly, from {START}. Departures are from each series' own trend line.\n")
    print(f"{'series':24s} {'span':>17s} {'real drift':>10s} {'spread':>7s} {'low':>7s} {'high':>7s} {'half-life':>9s} "
          f"{'crossing':>8s} {'vol/mo':>6s} | {'CPI lag':>7s} {'corr':>5s} {'@0':>5s} | {'M2 lag':>6s} {'corr':>5s}")
    for name, r in rows.items():
        half = f"{r['half_life_months']:.0f} mo" if r["half_life_months"] else "none"
        print(f"{name:24s} {r['first'][:7]}–{r['last'][:7]:>7s} {r['real_drift_pct_year']:+9.2f}% {100*r['spread_log']:6.1f}% "
              f"{100*r['low_log']:+6.0f}% {100*r['high_log']:+6.0f}% {half:>9s} {r['months_between_crossings']:5.0f} mo "
              f"{100*r['monthly_volatility']:5.1f}% | {r['lag']['cpi']['lag_months']:4d} mo {r['lag']['cpi']['correlation']:5.2f} "
              f"{r['lag']['cpi']['at_zero']:5.2f} | {r['lag']['m2']['lag_months']:3d} mo {r['lag']['m2']['correlation']:5.2f}")
    names = list(rows)
    print("\nCorrelation of departures from trend (common months):")
    print(" " * 24 + " ".join(f"{n.split()[0][:6]:>7s}" for n in names))
    matrix = {}
    for a in names:
        cells = []
        for b in names:
            common = sorted(set(rows[a]["away"]) & set(rows[b]["away"]))
            c = _pearson([rows[a]["away"][d] for d in common], [rows[b]["away"][d] for d in common])
            matrix[f"{a}|{b}"] = c
            cells.append(f"{c:7.2f}")
        print(f"{a:24s}" + " ".join(cells))
    ten, together = decades(prices, cpi)
    print("\nInside ten-year windows (medians; departures from each window's own trend line):\n")
    print(f"{'series':24s} {'windows':>7s} {'|real drift|':>12s} {'spread':>7s} {'furthest':>8s} {'crossing':>8s} {'vol/mo':>6s}")
    for name, r in ten.items():
        print(f"{name:24s} {r['windows']:7d} {r['drift']:10.2f}%/y {100*r['spread']:6.1f}% {100*r['reach']:7.1f}% "
              f"{r['crossing']:5.1f} mo {100*r['volatility']:5.2f}%")
    print("\nMedian correlation of those ten-year departures:")
    print(" " * 24 + " ".join(f"{n.split()[0][:6]:>7s}" for n in names))
    for a in names:
        print(f"{a:24s}" + " ".join(f"{together[a, b]:7.2f}" for b in names))
    years = sorted(d for d in cpi if d >= START and d.month == 1)
    rates = [100 * (cpi[b] / cpi[a] - 1) for a, b in zip(years, years[1:])]
    print(f"\nUS CPI, January to January from {START.year}: lowest {min(rates):.1f}%, median {median(rates):.1f}%, "
          f"highest {max(rates):.1f}%; {100 * ((cpi[years[-1]] / cpi[years[0]]) ** (1 / (len(years) - 1)) - 1):.2f}% a year overall.")
    if "--json" in argv:
        out = {n: {k: v for k, v in r.items() if k != "away"} for n, r in rows.items()}
        with open(argv[argv.index("--json") + 1], "w") as f:
            json.dump(dict(start=str(START), series=out, departure_correlation=matrix, ten_year_windows=ten,
                           ten_year_correlation={f"{a}|{b}": c for (a, b), c in together.items()}), f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
