"""CLI:  python -m alberta_buck.sim --scenario routing --days 120"""

from __future__ import annotations

import argparse
import sys

from alberta_buck.sim.anvil import Anvil
from alberta_buck.sim.loop import run
from alberta_buck.sim.scenario import SCENARIOS


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alberta_buck.sim")
    ap.add_argument("--scenario", default="routing", choices=sorted(SCENARIOS))
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--ticks-per-day", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default=None, help="output JSON path")
    ap.add_argument("--port", type=int, default=None, help="anvil port")
    a = ap.parse_args(argv)

    sc = SCENARIOS[a.scenario]
    if a.days is not None:
        sc.days = min(a.days, sc.prices.days)
    if a.ticks_per_day is not None:
        sc.ticks_per_day = a.ticks_per_day
    if a.seed is not None:
        sc.seed = a.seed

    with Anvil(port=a.port) as anvil:
        summary = run(sc, anvil, out_path=a.out)
    ok = summary["cycle_trades"] > 0 and summary["all_eoa_verified"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
