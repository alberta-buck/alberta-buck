"""Compare production with strict maintenance on three tapes from common monthly anchors."""

import argparse
from bisect import bisect_right
from datetime import date, timedelta
import fcntl
import importlib.util
from pathlib import Path
import sys

from alberta_buck.sim.gen_historical import BINDINGS, USDC
from alberta_buck.sim.prices import PRICE_SCALE
from alberta_buck.sim.quotes import QuoteSource, Unit, ingest
from alberta_buck.sim.quotes.metrics import US_TOKENS


ROOT = Path(__file__).resolve().parents[1]
FIRST_MONTH = date(2020, 9, 1)
AVAILABILITY_LAG = timedelta(days=61)
DAYS = 730
SEED = 41404
TREATMENTS = {
    "pinned-bridge": "Repository Brownian bridge using the next, not-yet-available monthly anchor",
    "straight-bridge": "Linear interpolation using the same next monthly anchor",
    "carry-forward": "Latest available anchor held until the next simulated release",
}


def day_price(value):
    return round(value * USDC) // PRICE_SCALE


def build_tapes(days=DAYS):
    if days < 1:
        raise ValueError("days must be positive")
    sources = {"us": QuoteSource(Unit.USD, tokens=US_TOKENS), "xau": QuoteSource(Unit.USD)}
    bindings = [(token, metric, feed) for token, _, metric, feed in BINDINGS if feed != "btc"]
    anchors = [dict((ts.date(), price) for ts, price in sources[feed].anchors(metric))
               for _, metric, feed in bindings]
    common = sorted(set.intersection(*(set(series) for series in anchors)))
    first_day = FIRST_MONTH + AVAILABILITY_LAG
    closing_day = first_day + timedelta(days=days - 1)
    terminal_month = closing_day - AVAILABILITY_LAG
    first = bisect_right(common, FIRST_MONTH) - 1
    next_after_end = bisect_right(common, terminal_month)
    if first < 0 or common[first] != FIRST_MONTH or next_after_end == len(common):
        raise ValueError("missing an opening or terminal interpolation anchor")
    months = common[first:next_after_end + 1]
    if any(d.day != 1 for d in months) or any(
            b.year * 12 + b.month != a.year * 12 + a.month + 1 for a, b in zip(months, months[1:])):
        raise ValueError("daily treatments require consecutive common calendar months")
    available = [month + AVAILABILITY_LAG for month in months]

    tapes = {treatment: [] for treatment in TREATMENTS}
    for (_, metric, feed), values in zip(bindings, anchors, strict=True):
        pinned, straight, held = [], [], []
        for offset in range(days):
            day = first_day + timedelta(days=offset)
            left = bisect_right(available, day) - 1
            a, b = months[left:left + 2]
            fraction = (day - available[left]).days / (b - a).days
            held.append(day_price(values[a]))
            straight.append(day_price(values[a] + (values[b] - values[a]) * fraction))
            pinned.append(day_price(sources[feed].at(metric, day - AVAILABILITY_LAG)))
        for key, tape in zip(TREATMENTS, (pinned, straight, held), strict=True):
            tapes[key].append(tape)

    monthly = [dict(day=(release - first_day).days, observation_month=month.isoformat(),
                    assumed_available=release.isoformat(), prices=[day_price(v[month]) for v in anchors])
               for month, release in zip(months, available, strict=True)]
    for entry in monthly[:-1]:
        if any([column[entry["day"]] for column in tape] != entry["prices"] for tape in tapes.values()):
            raise ValueError("a treatment disagrees with its common monthly anchors")
    return [token for token, _, _ in bindings], tapes, {
        "first_date": first_day.isoformat(),
        "last_date": closing_day.isoformat(),
        "availability_lag_calendar_days": AVAILABILITY_LAG.days,
        "availability_convention": "Fixed lag from observation label. Vendored values without first-release vintages.",
        "price_units": "simulator-scaled USD micro per TOKEN",
        "treatments": TREATMENTS,
        "common_anchors": monthly[:-1],
        "last_common_anchor_day": monthly[-2]["day"],
        "first_anchor_beyond_horizon": monthly[-1],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--days", type=int, default=DAYS, help="Daily observations (default: 730)")
    parser.add_argument("--treatment", action="append", choices=TREATMENTS,
                        help="Tape to run; repeatable (default: all three)")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=14400, help="Seconds per cell")
    parser.add_argument("--plan", action="store_true", help="Write the tapes and manifest only")
    args = parser.parse_args()
    if args.jobs < 1 or args.timeout < 1 or args.days < 1:
        parser.error("use at least one day, one job and one second per cell")
    args.output = args.output.resolve()
    args.experiment = ROOT / "alberta_buck/sim/experiments/economy.toml"
    args.preset = "monthly-anchors"

    spec = importlib.util.spec_from_file_location("signal_grid", ROOT / "scripts/run-basket-signal-grid.py")
    grid = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grid)
    tokens, tapes, calendar = build_tapes(args.days)
    treatments = tuple(dict.fromkeys(args.treatment or TREATMENTS))
    grid.selections = lambda _args: ((treatment, 0., SEED, False) for treatment in treatments)
    base_arms = grid.arms
    grid.arms = lambda cross: (arm for arm in base_arms(cross) if arm[0] in grid.CONTROLS)

    def close_prices(scenario, treatment, days, seed, noise):
        if (tokens, args.days, SEED, 0.) != ([token[0] for token in scenario.tokens], days, seed, noise):
            raise ValueError("anchor tapes require the five production TOKENs, fixed horizon and seed, and no added noise")
        return tapes[treatment]

    grid.close_prices = close_prices
    runtime = grid.runtime_inputs()
    files = [ingest.GOLD_CSV, *(ingest.US_DIR / (name + ".csv") for name in (
        "wage_ahetpi", *ingest.CNST_COMPONENTS, *ingest.FOOD_COMPONENTS, *ingest.NRGC_COMPONENTS))]
    runtime["anchor_sources"] = {str(path.relative_to(ROOT)): grid.digest(path.read_bytes()) for path in files}
    runtime["anchor_companion_sha256"] = grid.digest(Path(__file__).read_bytes())
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error(f"another campaign is using {args.output}")
        for folder in ("cells", "prices"):
            (args.output / folder).mkdir(exist_ok=True)
        try:
            manifest = grid.plan(args, runtime)
            manifest.pop("historical_sources")
            manifest.pop("history_reversion_days")
            manifest["price_calendar"] = calendar
            return grid.execute(args, manifest)
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    sys.exit(main())
