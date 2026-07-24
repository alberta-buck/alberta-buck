"""CLI:  python -m alberta_buck.sim --scenario routing --days 120"""

from __future__ import annotations

import argparse
import sys

from alberta_buck.sim.anvil import Anvil
from alberta_buck.sim.loop import run
from alberta_buck.sim.scenario import (
    SCENARIOS, build_historical, build_equilibrium,
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alberta_buck.sim")
    ap.add_argument("--scenario", default="routing",
                    choices=sorted(SCENARIOS) + ["historical", "equilibrium"])
    ap.add_argument("--backend", default="pyrevm", choices=["anvil", "pyrevm"],
                    help="EVM backend: in-process pyrevm (default; ~1000x "
                         "faster, see pyrevm_backend) or the anvil subprocess "
                         "(RPC-faithful; needs anvil on PATH -- fork tests)")
    ap.add_argument("--director", default="pairs", choices=["vrate", "pairs"],
                    help="rebalance-director signal engine (prorata only)")
    ap.add_argument("--basket", default=None, choices=["legacy", "prorata"],
                    help="basket implementation: BuckBasketProRata (prorata, "
                         "default) or BuckBasket (legacy)")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--ticks-per-day", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--day-step", type=int, default=None,
                    help="calendar days advanced per iteration (coarse macro "
                         "mode for long horizons; default 1)")
    ap.add_argument("--out", default=None, help="output JSON path")
    ap.add_argument("--port", type=int, default=None, help="anvil port")
    # historical-scenario window (real macro data)
    ap.add_argument("--start", default=None, help="historical: ISO start date")
    ap.add_argument("--end", default=None, help="historical: ISO end date")
    ap.add_argument("--years", type=float, default=None,
                    help="historical/equilibrium: window length if --start "
                         "omitted (default 5 historical, 1.5 equilibrium)")
    # equilibrium experiment harness (initial conditions + interventions)
    ap.add_argument("--experiment", default=None, metavar="TOML",
                    help="equilibrium experiment file; implies --scenario "
                         "equilibrium (see alberta_buck/sim/experiments/)")
    ap.add_argument("--set", action="append", default=[], dest="sets",
                    metavar="KEY=VAL",
                    help="dotted override into the experiment config, e.g. "
                         "--set deploy.k0=0.8 --set scenario.seed=7 "
                         "(repeatable; usable without --experiment)")
    a = ap.parse_args(argv)

    basket_impl = a.basket or "prorata"
    seed0 = a.seed if a.seed is not None else 0xA1BC
    if a.experiment or a.sets:
        from alberta_buck.sim import experiment as expmod
        exp = expmod.load(a.experiment, sets=a.sets)
        # Explicit CLI flags override the experiment's [scenario] section.
        s = exp.scenario
        if a.years is not None:
            s["years"] = a.years
        if a.start:
            s["start"] = a.start
        if a.end:
            s["end"] = a.end
        if a.ticks_per_day is not None:
            s["ticks_per_day"] = a.ticks_per_day
        if a.seed is not None:
            s["seed"] = a.seed
        if a.day_step is not None:
            s["day_step"] = a.day_step
        if a.days is not None:
            s["days"] = a.days
        if a.basket is None:                        # no CLI flag: exp wins
            basket_impl = s.get("basket", "prorata")
        sc = expmod.build(exp)
        if a.out is None:
            a.out = f"test/vectors/eq-{exp.name}.json"
    elif a.scenario == "historical":
        sc = build_historical(start=a.start, end=a.end,
                              years=a.years if a.years is not None else 5.0,
                              ticks_per_day=a.ticks_per_day or 1, seed=seed0)
    elif a.scenario == "equilibrium":
        sc = build_equilibrium(start=a.start, end=a.end, years=a.years,
                               ticks_per_day=a.ticks_per_day or 48, seed=seed0)
    else:
        sc = SCENARIOS[a.scenario]
    if a.days is not None:
        sc.days = min(a.days, sc.prices.days)
    if a.ticks_per_day is not None:
        sc.ticks_per_day = a.ticks_per_day
    if a.seed is not None:
        sc.seed = a.seed
    if a.day_step is not None:
        sc.day_step = a.day_step

    if a.backend == "pyrevm":
        from alberta_buck.sim.pyrevm_backend import PyrevmAnvil as Backend
    else:
        Backend = Anvil
    with Backend(port=a.port) as anvil:
        summary = run(sc, anvil, out_path=a.out, basket_impl=basket_impl,
                      director_impl=a.director)
    ok = summary["cycle_trades"] > 0 and summary["all_eoa_verified"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
