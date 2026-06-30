"""CLI:  python -m alberta_buck.sim --scenario routing --days 120"""

from __future__ import annotations

import argparse
import sys

from alberta_buck.sim.anvil import Anvil
from alberta_buck.sim.loop import run
from alberta_buck.sim.scenario import SCENARIOS, build_historical


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alberta_buck.sim")
    ap.add_argument("--scenario", default="routing",
                    choices=sorted(SCENARIOS) + ["historical"])
    ap.add_argument("--basket", default="legacy", choices=["legacy", "prorata"],
                    help="basket implementation: BuckBasket (legacy) or BuckBasketProRata")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--ticks-per-day", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default=None, help="output JSON path")
    ap.add_argument("--port", type=int, default=None, help="anvil port")
    # historical-scenario window (real macro data)
    ap.add_argument("--start", default=None, help="historical: ISO start date")
    ap.add_argument("--end", default=None, help="historical: ISO end date")
    ap.add_argument("--years", type=float, default=5.0,
                    help="historical: window length if --start omitted (default 5)")
    a = ap.parse_args(argv)

    if a.scenario == "historical":
        sc = build_historical(start=a.start, end=a.end, years=a.years,
                              ticks_per_day=a.ticks_per_day or 1,
                              seed=a.seed if a.seed is not None else 0xA1BC)
    else:
        sc = SCENARIOS[a.scenario]
    if a.days is not None:
        sc.days = min(a.days, sc.prices.days)
    if a.ticks_per_day is not None:
        sc.ticks_per_day = a.ticks_per_day
    if a.seed is not None:
        sc.seed = a.seed

    with Anvil(port=a.port) as anvil:
        summary = run(sc, anvil, out_path=a.out, basket_impl=a.basket)
    ok = summary["cycle_trades"] > 0 and summary["all_eoa_verified"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
