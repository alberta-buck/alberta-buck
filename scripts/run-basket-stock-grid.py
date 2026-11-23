"""Compare patient basket capital and household flows on matched daily price tapes."""

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import fcntl
import hashlib
import importlib.util
import itertools
import json
import math
from pathlib import Path
import subprocess
import sys
import tomllib
from types import SimpleNamespace

from alberta_buck.sim import experiment
from alberta_buck.sim.gen_prices import gen_trend


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REGIMES = ("three-phase", "history", "revert", "inflating", "mixed", "shock",
           "shock-temporary", "shock-permanent", "commodity-trend")
COMMODITY_TREND = {"LABR": .05, "CNST": .10, "FOOD": -.08, "NRGC": -.20, "PAXG": .25, "cbBTC": .12}


def script(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temp.replace(path)


def stock_prices(grid, scenario, regime, days, seed, sigma, inflation):
    if regime == "inflating":
        prices = grid.close_prices(scenario, "revert", days, seed, sigma)
        growth = math.log1p(inflation)
        return [[round(price * math.exp(growth * day / 365.25)) for day, price in enumerate(series)]
                for series in prices]
    if regime in ("shock-temporary", "shock-permanent"):
        if scenario.tokens[0][0] != "LABR":
            raise ValueError("isolated shock regimes require LABR in the basket")
        # The signal grid's combined shock already has the permanent LABR leg.
        # Strip the CNST leg and optionally undo the LABR move at the same recovery date.
        combined = grid.close_prices(scenario, "shock", days, seed, 0)
        baseline = [combined[0], *([series[0]] * days for series in combined[1:])]
        if regime == "shock-temporary":
            recovery = 2 * days // 3
            baseline[0][recovery:] = [baseline[0][0]] * (days - recovery)
    elif regime == "commodity-trend":
        baseline = [gen_trend(series[0], COMMODITY_TREND[token[0]], 0., seed, days)
                    for token, series in zip(scenario.tokens, scenario.prices.series, strict=True)]
    else:
        return grid.close_prices(scenario, regime, days, seed, sigma)
    # Use the same per-TOKEN log-walk overlay and simulation rounding as all other grid tapes.
    tape = SimpleNamespace(tokens=scenario.tokens, prices=SimpleNamespace(series=baseline))
    return grid.close_prices(tape, "history", days, seed, sigma)


def plan(args):
    grid = script("run-basket-signal-grid")
    base = experiment.load(args.experiment)
    source = experiment._deep_merge(base.cfg, args.settings)
    for override in args.overrides:
        experiment._apply_set(source, override)
    text = experiment.to_toml(source)
    source_hash = digest(text.encode())
    frozen = args.output / f"experiment-{source_hash[:16]}.toml"
    if not frozen.exists():
        frozen.write_text(text)
    if digest(frozen.read_bytes()) != source_hash:
        raise ValueError("saved economic configuration differs from plan")
    scenario = experiment.build(experiment.load(frozen, ["scenario.prices=history"]))
    if scenario.prices.days < args.days or scenario.day_step != 1:
        raise ValueError("the experiment must cover the requested daily horizon")
    tokens = [t[0] for t in scenario.tokens]
    regimes = args.regime or list(REGIMES[:4])
    balances = args.endowment or [0, 2_000_000]
    if args.opening_cash < 0:
        raise ValueError("opening_cash must be nonnegative")
    rates = args.investor_rate or [6., 60.]
    exits = args.exit_fraction or [0., .25]
    noise = args.noise or [0., .01]
    seeds = args.seed or [41404]
    if any(v < 0 for v in balances) or any(not math.isfinite(v) or v < 0 for v in rates + noise):
        raise ValueError("endowment, rates and noise must be finite and nonnegative")
    if any(not 0 <= v <= 1 for v in exits):
        raise ValueError("exit fractions must be between 0 and 1")
    if not math.isfinite(args.inflation) or args.inflation <= -1:
        raise ValueError("annual inflation must be finite and greater than -100%")
    if not 1 <= args.warmup_days < args.days - 1:
        raise ValueError("allow at least one warmup day and two treatment days")
    exit_day = args.warmup_days + (args.days - args.warmup_days) // 2
    runtime = grid.runtime_inputs()
    runtime["study_scripts"] = {name: digest((HERE / name).read_bytes()) for name in (
        "run-basket-stock-grid.py", "run-basket-patient-holder.py", "capture-basket-ownership.py",
        "basket_jubilee_endowment.py", "run-basket-signal-grid.py")}
    runtime["experiment_sha256"] = source_hash
    prices_dir = args.output / "prices"
    prices_dir.mkdir(exist_ok=True)
    cells = []
    # The pilot crosses funded stock with quiet and brisk demand scenarios; the full
    # design crosses both household controls independently with both noise levels.
    for regime, seed in itertools.product(dict.fromkeys(regimes), dict.fromkeys(seeds)):
        sigmas = noise if args.design == "full" else [noise[-1] if regime == "three-phase" else noise[0]]
        flows = (list(itertools.product(rates, exits)) if args.design == "full" else
                 list(dict.fromkeys([(min(rates), min(exits)), (max(rates), max(exits))])))
        for sigma in dict.fromkeys(sigmas):
            prices = stock_prices(grid, scenario, regime, args.days, seed, sigma, args.inflation)
            price_hash, files = grid.price_files(prices_dir, tokens, prices)
            for balance, (rate, fraction) in itertools.product(dict.fromkeys(balances), dict.fromkeys(flows)):
                key = dict(regime=regime, noise=sigma, seed=seed, endowment_buck=balance,
                           investor_rate=rate, exit_fraction=fraction, exit_day=exit_day,
                           warmup_days=args.warmup_days, days=args.days, price_digest=price_hash, opening_cash_buck=args.opening_cash)
                fingerprint = digest(grid.json_bytes([key, runtime]))
                label = f"stock-{regime}-j{balance}-i{rate:g}-x{fraction:g}-n{sigma:g}-{fingerprint[:10]}"
                cells.append(dict(**key, id=label, files=[p.name for p in files],
                                  status="planned", fingerprint=fingerprint))
    from alberta_buck.sim.basket_machine import settings as machine_settings
    treatments = {}
    if "commodity-trend" in regimes:
        treatments["commodity-trend"] = dict(annual_log_drift={token: COMMODITY_TREND[token] for token in tokens})
    for regime in ("shock-temporary", "shock-permanent"):
        if regime in regimes:
            treatments[regime] = dict(token="LABR", change=-.20, onset_day=args.days // 3,
                                      recovery_day=2 * args.days // 3 if regime == "shock-temporary" else None)
    return dict(schema=1, effective_machine=machine_settings(source.get("machine")),
                initial_agent_counts=scenario.agents, experiment=str(frozen), base_experiment=str(args.experiment), study_config=str(args.config or ""), run_root=str(args.runs),
                design=args.design, tokens=tokens, runtime=runtime, inflation=args.inflation, price_treatments=treatments,
                endowment_source="existing bootstrap SimLP BUCK, invested by Jubilee",
                cells=cells)


def run_cell(folder, ident):
    manifest = json.loads((folder / "manifest.json").read_text())
    cell = next(c for c in manifest["cells"] if c["id"] == ident)
    patient = script("run-basket-patient-holder")
    endow = script("basket_jubilee_endowment").apply_opening_endowment
    audit = []

    def opening(day, deployment, agents, counters):
        if day == 0:
            audit.append(endow(deployment, cell["endowment_buck"], cash_buck=cell["opening_cash_buck"]))

    interventions = list(experiment.load(manifest["experiment"]).interventions) + [
        dict(day=cell["warmup_days"], action="set_population", investor_rate=cell["investor_rate"]),
        dict(day=cell["exit_day"], action="set_population", exit_now=cell["exit_fraction"])]
    settings = [f"scenario.seed={cell['seed']}", "scenario.prices=history",
                "interventions=" + repr(interventions)]
    files = [folder / "prices" / name for name in cell["files"]]
    runs = Path(manifest["run_root"])
    prefix = cell["run_prefix"]
    result = patient.run_pair(experiment_path=manifest["experiment"], days=cell["days"],
                              warmup_days=cell["warmup_days"], settings=settings,
                              price_files=files, on_day_start=opening,
                              record_root=runs, run_id=prefix)
    if len(audit) != 2 or audit[0] != audit[1]:
        raise ValueError("opening endowments differ between arms")
    for arm in result["arms"].values():
        series = [list(series) for series in zip(*(row["prices"] for row in arm["daily"]))]
        actual = digest(json.dumps([manifest["tokens"], series], separators=(",", ":")).encode())
        if actual != cell["price_digest"]:
            raise ValueError("captured daily TOKEN prices differ from input tape")
    run_ids = {arm: prefix + "-" + arm for arm in result["arms"]}
    for arm, run_id in run_ids.items():
        path = runs / run_id / "manifest.json"
        record = json.loads(path.read_text())
        if record["status"] != "complete" or record["frames"] != cell["days"]:
            raise ValueError(f"incomplete dashboard record for {run_id}")
        record.setdefault("settings", {})["stock_study"] = {
            key: cell[key] for key in ("regime", "seed", "noise", "endowment_buck", "investor_rate",
                                        "exit_fraction", "exit_day", "warmup_days", "price_digest", "opening_cash_buck")}
        write_json(path, record)
    result.update(fingerprint=cell["fingerprint"], endowment_audit=audit[0], run_ids=run_ids)
    write_json(folder / "cells" / (ident + ".json"), result)


def execute(args):
    m = plan(args)
    cells_dir = args.output / "cells"
    cells_dir.mkdir(exist_ok=True)
    manifest_file = args.output / "manifest.json"
    prev = json.loads(manifest_file.read_text()) if manifest_file.exists() else {}
    previous = {c["fingerprint"]: c for c in prev.get("cells", [])}
    if previous and {c["fingerprint"] for c in m["cells"]} != set(previous):
        raise ValueError("campaign settings or runtime changed; use another output directory")
    for c in m["cells"]:
        known = previous.get(c["fingerprint"], {})
        saved = cells_dir / (c["id"] + ".json")
        ids = known.get("run_ids", {})
        if (saved.exists() and known.get("sha256") == digest(saved.read_bytes())
                and set(ids) == {"active", "maintenance"}
                and all((args.runs / run_id / "frames.jsonl").exists() for run_id in ids.values())):
            c.update(status="complete", sha256=known["sha256"], run_ids=ids)
    write_json(manifest_file, m)
    if args.plan:
        print(f"{len(m['cells'])} matched pairs planned")
        return 0
    def launch(c):
        with (cells_dir / (c["id"] + ".log")).open("w") as log:
            r = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(args.output),
                                "--cell", c["id"]], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            return c, r.returncode
    pending = iter(c for c in m["cells"] if c["status"] != "complete")
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        active = {}
        while True:
            while len(active) < args.jobs:
                cell = next(pending, None)
                if cell is None:
                    break
                prefix = cell["id"]
                attempt = 1
                while any((args.runs / f"{prefix}-{arm}").exists() for arm in ("active", "maintenance")):
                    attempt += 1
                    prefix = f"{cell['id']}-try{attempt}"
                cell.update(status="running", run_prefix=prefix,
                            run_ids={arm: f"{prefix}-{arm}" for arm in ("active", "maintenance")})
                write_json(manifest_file, m)
                active[pool.submit(launch, cell)] = cell
            if not active:
                break
            for task in wait(active, return_when=FIRST_COMPLETED).done:
                del active[task]
                cell, status = task.result()
                cell["status"] = "failed" if status else "complete"
                if not status:
                    raw = (cells_dir / (cell["id"] + ".json")).read_bytes()
                    cell.update(sha256=digest(raw), run_ids=json.loads(raw)["run_ids"])
                write_json(manifest_file, m)
                print(cell["status"], cell["id"], flush=True)
    return int(any(c["status"] != "complete" for c in m["cells"]))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("output", type=Path)
    p.add_argument("--config", type=Path, help="TOML study axes and economic settings")
    p.add_argument("--set", dest="overrides", action="append", default=[],
                   help="override an economic experiment setting, e.g. machine.tilt=0")
    p.add_argument("--opening-cash", type=int, help="additional opening Jubilee cash in whole BUCK")
    p.add_argument("--runs", type=Path, default=ROOT / "build/runs", help="standard dashboard records")
    p.add_argument("--experiment", type=Path, default=None)
    p.add_argument("--design", choices=("pilot", "full"), default=None)
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--warmup-days", type=int, default=None)
    p.add_argument("--regime", action="append", choices=REGIMES)
    p.add_argument("--noise", action="append", type=float)
    p.add_argument("--inflation", type=float, default=None, help="annual common drift for the inflating regime")
    p.add_argument("--endowment", action="append", type=int, help="opening Jubilee investment, whole BUCK")
    p.add_argument("--investor-rate", action="append", type=float, help="base annual arrivals after warmup")
    p.add_argument("--exit-fraction", action="append", type=float, help="fraction of current investors scheduled to exit")
    p.add_argument("--seed", action="append", type=int)
    p.add_argument("--jobs", type=int, default=1, help="parallel pairs, arms run sequentially")
    p.add_argument("--plan", action="store_true")
    p.add_argument("--cell", help=argparse.SUPPRESS)
    args = p.parse_args()
    args.output = args.output.resolve()
    if args.cell:
        run_cell(args.output, args.cell)
        return 0
    args.settings = {}
    axes = {"experiment", "design", "days", "warmup_days", "opening_cash", "regime", "noise",
            "inflation", "endowment", "investor_rate", "exit_fraction", "seed"}
    if args.config:
        config = tomllib.loads(args.config.read_text())
        if set(config) - {"study", "settings"} or set(config.get("study", {})) - axes:
            p.error("unknown TOML table or study axis")
        for key, val in config.get("study", {}).items():
            if getattr(args, key) is None:
                if key == "experiment":
                    val = args.config.parent / val
                setattr(args, key, val)
        args.settings = config.get("settings", {})
    for key, default in dict(experiment=ROOT / "alberta_buck/sim/experiments/economy.toml", design="pilot",
                             days=36, warmup_days=7, inflation=.03, opening_cash=0).items():
        if getattr(args, key) is None:
            setattr(args, key, default)
    if args.design not in ("full", "pilot") or any(r not in REGIMES for r in args.regime or []):
        p.error("unknown study design or price regime")
    if args.jobs < 1:
        p.error("--jobs must be positive")
    args.runs = args.runs.resolve()
    args.experiment = args.experiment.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return execute(args)


if __name__ == "__main__":
    sys.exit(main())
