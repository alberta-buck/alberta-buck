"""CLI:  python -m alberta_buck.sim --scenario routing --days 120"""

from __future__ import annotations

import argparse
import os
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
    ap.add_argument("--controller", default=os.environ.get("SIM_CONTROLLER", "direct"),
                    choices=["direct", "shadow"],
                    help="BUCK_K controller: BuckKControllerDirect (direct, "
                         "default) or BuckKControllerShadow (shadow -- WP-3a: "
                         "reads the ops shell's observer, bvib + lambda * "
                         "netInventory / D, with Ki scheduled by the desk's "
                         "saturation; SIM_SHADOW_LAMBDA / SIM_SHADOW_GAMMA, "
                         "default 0, make it Direct's twin).  Env SIM_CONTROLLER "
                         "sets the default so catalogue/star cells can select it")
    ap.add_argument("--basket", default=None,
                    choices=["legacy", "prorata", "ops", "fence"],
                    help="basket implementation: BuckBasketProRata (prorata, "
                         "default), BuckBasketOps (ops -- prorata plus the "
                         "monetary-operations desk on the director's common "
                         "mode), BuckBasketFence (fence -- K-scaled issuance deployed as a concentrated band), or BuckBasket (legacy)")
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
    # WAVE3.org decision 21: build the scenario (generating its inputs --
    # the window-stamped historical CSVs -- under gen_historical's lock)
    # and exit, so a grid's fan-out starts from complete files.
    ap.add_argument("--prepare", action="store_true",
                    help="build the scenario and generate its inputs, then exit "
                         "without running (the grid targets call this per arm "
                         "before fanning out)")
    ap.add_argument("--set", action="append", default=[], dest="sets",
                    metavar="KEY=VAL",
                    help="dotted override into the experiment config, e.g. "
                         "--set deploy.k0=0.8 --set scenario.seed=7 "
                         "(repeatable; usable without --experiment)")
    # WP-13: the observer's aggregation mode (CARRY-CONVEXITY.org D7): S,
    # the shadow bvib in price units (sum lambda_i q_i / D), or V, the
    # cost-weighted position vector (sum w_i q_i / cap_i).  The position
    # loop's gains and the weights are env-only (SIM_SHADOW_KQ / _KQI /
    # _KQD, SIM_SHADOW_W_* / _LAMBDA_*, SIM_SHADOW_OFFSET_CAP_USD; deploy.py).
    ap.add_argument("--shadow-mode", default=os.environ.get("SIM_SHADOW_MODE", "s"),
                    choices=["s", "v"],
                    help="shadow controller's aggregation (WP-13): s = shadow "
                         "bvib (D4 units, default), v = position vector; env "
                         "SIM_SHADOW_MODE sets the default for catalogue/star cells")
    a = ap.parse_args(argv)
    os.environ["SIM_SHADOW_MODE"] = a.shadow_mode       # WP-13: deploy.py reads it

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

    if a.prepare:
        print(f"[prepare] {sc.name}: {sc.days} days x {sc.ticks_per_day} ticks; "
              f"inputs complete ({len(sc.csv_files)} price files)")
        return 0

    if a.backend == "pyrevm":
        from alberta_buck.sim.pyrevm_backend import PyrevmAnvil as Backend
    else:
        Backend = Anvil
    with Backend(port=a.port) as anvil:
        summary = run(sc, anvil, out_path=a.out, basket_impl=basket_impl,
                      director_impl=a.director, controller_impl=a.controller)
    ok = summary["cycle_trades"] > 0 and summary["all_eoa_verified"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
