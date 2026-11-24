"""Run matched upkeep after a quiet aged cash/fund bootstrap and two flat-price closes.

Default pilot is four pairs: age 0/365 days by directed Jubilee investment
0/2M BUCK, with 500k BUCK across four idle Ordinary holders in every cell.
Use a fresh OUTPUT distinct from any stock campaign; --plan writes inputs
only. --measured-days 2 --age-days 365 --endowment 2000000 --jobs 1 runs
one matched pair for four closes including warmup. Ages are extra simulated chain time
before day zero. The ordinary 2% annual continuous fee applies throughout.
"""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, timedelta
import fcntl
import json
from pathlib import Path
import re
import subprocess
import sys

from alberta_buck.sim import experiment
from alberta_buck.sim.basket_machine import settings as machine_settings
from alberta_buck.sim.prices import CSV_DIR, Prices

from basket_aged_opening import aged_opening
from basket_jubilee_endowment import apply_opening_endowment


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WARMUP = 2
ARMS = ("active", "maintenance")
CELL_KEYS = (
    "regime", "noise", "seed", "endowment_buck", "investor_rate", "exit_fraction",
    "exit_day", "warmup_days", "days", "price_digest", "opening_cash_buck",
)


def stock_module():
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("basket_stock_grid", HERE / "run-basket-stock-grid.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stock = stock_module()
grid = stock.script("run-basket-signal-grid")
digest, write_json = stock.digest, stock.write_json


def read_json(path):
    return json.loads(path.read_bytes())


def fresh_manifest(args):
    exp = experiment.load(args.experiment, ["scenario.prices=history"])
    if exp.interventions:
        raise ValueError("the aged pilot requires an experiment without scheduled interventions")
    scenario = experiment.build(exp)
    if scenario.day_step != 1 or scenario.rng_mode != "keyed" or scenario.prices.days < args.measured_days:
        raise ValueError("the experiment must cover the requested full history with keyed daily steps")

    tag = re.search(r"-(\d{4}-\d{2}-\d{2}-\d+d)\.csv$", scenario.csv_files[0])
    if tag is None:
        raise ValueError("expected a dated historical input window")
    source = read_json(CSV_DIR / f"hist-manifest-{tag[1]}.json")
    if any(name not in source["files"] for name in scenario.csv_files):
        raise ValueError("TOKEN price filenames disagree with the historical window manifest")
    start = date.fromisoformat(source["start"])
    end = start + timedelta(days=args.measured_days - 1)
    design = {
        "measured_days": args.measured_days, "prelude_closes": WARMUP,
        "reference_first_date": start.isoformat(), "reference_last_date": end.isoformat(),
        "price_source_end": source["end"], "price_source_files": scenario.csv_files,
        "passive_buck": args.passive_buck, "passive_holders": args.passive_holders,
        "annual_continuous_fee": 0.02,
        "aging_basis": "inactive on-chain cash/fund bootstrap before the first household or market day",
    }
    # Pin the complete source window in the scenario. The separately frozen
    # forcing tape includes two flat genesis closes then all measured dates.
    exp.scenario.update(start=source["start"], end=source["end"], days=args.measured_days + WARMUP)
    text = experiment.to_toml(exp.cfg)
    source_hash = digest(text.encode())
    frozen = args.output / f"experiment-{source_hash[:16]}.toml"
    runtime = grid.runtime_inputs()
    runtime.update(experiment_sha256=source_hash, aged_design=design,
                   study_scripts={name: digest((HERE / name).read_bytes()) for name in (
                       "run-basket-aged-study.py", "basket_aged_opening.py",
                       "basket_jubilee_endowment.py", "run-basket-patient-holder.py",
                       "capture-basket-ownership.py", "run-basket-stock-grid.py")})
    tokens = [t[0] for t in scenario.tokens]
    prices = [[row[0]] * WARMUP + row[:args.measured_days] for row in scenario.prices.series]
    price_hash = digest(grid.json_bytes([tokens, prices]))

    cells = []
    for age in dict.fromkeys(args.age_days):
        for endowment in dict.fromkeys(args.endowment):
            key = dict(regime=f"history-age{age}", noise=0., seed=args.seed, endowment_buck=endowment,
                       investor_rate=6., exit_fraction=0., exit_day=WARMUP + args.measured_days // 2,
                       warmup_days=WARMUP, days=args.measured_days + WARMUP, price_digest=price_hash,
                       opening_cash_buck=0)
            fingerprint = digest(grid.json_bytes([key, runtime]))
            ident = f"stock-aged-a{age}-j{endowment}-{fingerprint[:10]}"
            cells.append(dict(**key, id=ident, age_days=age, fingerprint=fingerprint, status="planned"))
    manifest = dict(schema=1, effective_machine=machine_settings(exp.cfg.get("machine")),
                    initial_agent_counts=scenario.agents, experiment=str(frozen),
                    base_experiment=str(args.experiment), run_root=str(args.runs), design="pilot",
                    tokens=tokens, runtime=runtime, inflation=0., price_treatments={}, aged_study=design,
                    endowment_source="directed SimLP BUCK after measured fee accrual and fund investment",
                    cells=cells)
    return manifest, frozen, text, prices


def stamp(deployment):
    block = deployment.w3.eth.get_block("latest")
    return dict(block=int(block["number"]), timestamp=int(block["timestamp"]))


def run_cell(folder, ident):
    manifest = read_json(folder / "manifest.json")
    design = manifest["aged_study"]
    cell = next(c for c in manifest["cells"] if c["id"] == ident)
    key = {name: cell[name] for name in CELL_KEYS}
    if (digest(grid.json_bytes([key, manifest["runtime"]])) != cell["fingerprint"]
            or cell["regime"] != f"history-age{cell['age_days']}"
            or design != manifest["runtime"]["aged_design"]):
        raise ValueError("aged bootstrap inputs differ from the planned fingerprint")
    frozen = Path(manifest["experiment"])
    if digest(frozen.read_bytes()) != manifest["runtime"]["experiment_sha256"]:
        raise ValueError("frozen experiment checksum differs")
    if any(Path(name).name != name for name in cell["files"]):
        raise ValueError("invalid frozen tape filename")
    files = [str(folder / "prices" / name) for name in cell["files"]]
    if digest(grid.json_bytes([manifest["tokens"], Prices(files).series])) != cell["price_digest"]:
        raise ValueError("opening tape checksum differs before replay")
    interventions = [
        dict(day=WARMUP, action="set_population", investor_rate=cell["investor_rate"]),
        dict(day=cell["exit_day"], action="set_population", exit_now=0.),
    ]
    settings = [f"scenario.seed={cell['seed']}", "interventions=" + repr(interventions)]
    closes = {arm: {} for arm in ARMS}
    metadata = {**design, "age_days": cell["age_days"]}
    runs = Path(manifest["run_root"])
    deployment = switch_clock = None

    def on_start(day, d, agents, counters):
        nonlocal deployment, switch_clock
        if day == 0:
            deployment = d
            if len(audits) == 2 and audits[0] != audits[1]:
                raise ValueError("matched arms had different aged bootstrap ledgers")
            arm = ARMS[len(audits) - 1]
            run_id = cell["run_ids"][arm]
            path = runs / run_id
            record = read_json(path / "manifest.json")
            if record["id"] != run_id or record["status"] != "running":
                raise ValueError("aged opening must be recorded in its active run directory")
            write_json(path / "opening.json", dict(schema=1, id=run_id, arm=arm, fingerprint=cell["fingerprint"],
                                                   aged_study=metadata, bootstrap_audit=audits[-1],
                                                   timeline_epoch=d.household_epoch))
        if day == WARMUP:
            switch_clock = stamp(d)

    def on_frame(arm, frame):
        day = frame["day"]
        if day in (WARMUP - 1, WARMUP, cell["days"] - 1):
            closes[arm][str(day)] = stamp(deployment)
        if day == WARMUP:
            closes[arm]["timeline_epoch"] = deployment.household_epoch
            closes[arm]["pre_switch"] = switch_clock

    def direct(d):
        return apply_opening_endowment(d, cell["endowment_buck"])

    patient = stock.script("run-basket-patient-holder")
    if tuple(patient.FACETS) != ARMS:
        raise ValueError("patient runner arm order changed")
    with aged_opening(cell["age_days"], design["passive_buck"], design["passive_holders"],
                      seed=cell["seed"], after_age=direct) as audits:
        pair = patient.run_pair(experiment_path=frozen, days=cell["days"], warmup_days=WARMUP,
                                settings=settings, price_files=files, on_day_start=on_start, on_frame=on_frame,
                                record_root=runs, run_id=cell["run_prefix"])
    if len(audits) != 2 or audits[0] != audits[1]:
        raise ValueError("matched arms had different aged bootstrap ledgers")
    for arm in ARMS:
        rows = pair["arms"][arm]["daily"]
        tape = [list(column) for column in zip(*(row["prices"] for row in rows), strict=True)]
        if digest(grid.json_bytes([manifest["tokens"], tape])) != cell["price_digest"]:
            raise ValueError("recorded tape differs from the planned input")
        clock = closes[arm]
        if (set(clock) != {str(WARMUP - 1), str(WARMUP), str(cell["days"] - 1), "timeline_epoch", "pre_switch"}
                or clock["pre_switch"]["block"] != pair["arms"][arm]["opening"]["block"]):
            raise ValueError("missing exact opening or measured close clock samples")
        times = [clock[str(day)]["timestamp"] for day in (WARMUP - 1, WARMUP, cell["days"] - 1)]
        if not clock["timeline_epoch"] < times[0] <= clock["pre_switch"]["timestamp"] < times[1] < times[2]:
            raise ValueError("recorded sample clock is outside the treatment timeline")
        path = runs / cell["run_ids"][arm] / "manifest.json"
        record = read_json(path)
        if record["status"] != "complete" or record["frames"] != cell["days"]:
            raise ValueError("dashboard record incomplete")
        record["settings"].update(stock_study=key, aged_study={**metadata, "close_chain_clock": closes[arm]})
        write_json(path, record)
    pair.update(fingerprint=cell["fingerprint"], run_ids=cell["run_ids"],
                aged_study={**metadata, "bootstrap_audit": audits[0], "close_chain_clock": closes})
    write_json(folder / "cells" / (ident + ".json"), pair)


def completed(cell, directory, runs):
    saved = directory / "cells" / (cell["id"] + ".json")
    ids = cell.get("run_ids", {})
    if not (cell["status"] == "complete" and set(ids) == set(ARMS) and saved.is_file()
            and digest(saved.read_bytes()) == cell.get("sha256")):
        return False
    for run_id in ids.values():
        folder = runs / run_id
        if not ((folder / "frames.jsonl").is_file() and (folder / "manifest.json").is_file()):
            return False
        record = read_json(folder / "manifest.json")
        if record["id"] != run_id or record["status"] != "complete" or record["frames"] != cell["days"]:
            return False
    return True


def execute(args):
    folder = args.output
    file = folder / "manifest.json"
    prior = read_json(file) if file.is_file() else None
    if prior is not None and "aged_study" not in prior:
        raise ValueError("output belongs to another campaign")
    manifest, frozen, text, prices = fresh_manifest(args)
    cells = manifest["cells"]
    if prior is not None:
        latest = {cell["fingerprint"]: cell for cell in prior["cells"]}
        if (set(latest) != {c["fingerprint"] for c in cells}
                or prior["run_root"] != manifest["run_root"]):
            raise ValueError("campaign inputs changed; use a new output directory")
        for cell in cells:
            known = latest[cell["fingerprint"]]
            if completed(known, folder, args.runs):
                cell.update(known)

    if frozen.is_file():
        if digest(frozen.read_bytes()) != manifest["runtime"]["experiment_sha256"]:
            raise ValueError("saved economic configuration differs from plan")
    else:
        frozen.write_text(text)
    prices_dir = folder / "prices"
    prices_dir.mkdir(exist_ok=True)
    _, files = grid.price_files(prices_dir, manifest["tokens"], prices)
    out = folder / "cells"
    out.mkdir(exist_ok=True)
    for cell in cells:
        cell["files"] = [p.name for p in files]
    write_json(file, manifest)
    if args.plan:
        print(f"{len(cells)} matched pairs planned")
        return 0

    def launch(cell):
        with (out / (cell["id"] + ".log")).open("a") as log:
            print(f"Starting {cell['run_prefix']}", file=log, flush=True)
            return subprocess.run([sys.executable, str(Path(__file__).resolve()), str(folder),
                                   "--cell", cell["id"]], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT).returncode

    pending = iter(cell for cell in cells if cell["status"] != "complete")
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        active = {}
        while True:
            while len(active) < args.jobs:
                cell = next(pending, None)
                if cell is None:
                    break
                prefix, attempt = cell["id"], 1
                while any((args.runs / f"{prefix}-{arm}").exists() for arm in ARMS):
                    attempt += 1
                    prefix = f"{cell['id']}-try{attempt}"
                cell.update(status="running", run_prefix=prefix, run_ids={arm: f"{prefix}-{arm}" for arm in ARMS})
                write_json(file, manifest)
                active[pool.submit(launch, cell)] = cell
            if not active:
                break
            for task in wait(active, return_when=FIRST_COMPLETED).done:
                cell = active.pop(task)
                status = task.result()
                cell["status"] = "failed" if status else "complete"
                if not status:
                    cell["sha256"] = digest((out / (cell["id"] + ".json")).read_bytes())
                write_json(file, manifest)
                print(cell["status"], cell["id"], flush=True)
    return int(any(c["status"] != "complete" for c in cells))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=Path)
    parser.add_argument("--runs", type=Path, default=ROOT / "build/runs")
    parser.add_argument("--experiment", type=Path, default=ROOT / "alberta_buck/sim/experiments/economy.toml")
    parser.add_argument("--measured-days", type=int, default=1825)
    parser.add_argument("--age-days", type=int, action="append", choices=(0, 365))
    parser.add_argument("--endowment", type=int, action="append", help="whole BUCK directed to Jubilee from SimLP")
    parser.add_argument("--passive-buck", type=int, default=500_000, help="total quiet cash drawn from existing SimLP BUCK")
    parser.add_argument("--passive-holders", type=int, default=4)
    parser.add_argument("--seed", type=int, default=41404)
    parser.add_argument("--jobs", type=int, choices=range(1, 5), default=1, help="parallel pairs, sequential arms")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--cell", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.cell:
        run_cell(args.output, args.cell)
        return 0
    args.age_days = args.age_days or [0, 365]
    args.endowment = args.endowment or [0, 2_000_000]
    if not 2 <= args.measured_days <= 1825:
        parser.error("--measured-days must be between 2 and 1825")
    if any(value < 0 for value in args.endowment) or args.passive_buck <= 0 or args.passive_holders < 2:
        parser.error("endowment must be nonnegative, with positive passive cash and at least two holders")
    if args.passive_buck % args.passive_holders:
        parser.error("passive BUCK must split into equal whole amounts")
    if len(set(args.age_days)) * len(set(args.endowment)) > 4:
        parser.error("the pilot supports at most four matched pairs")
    args.experiment, args.runs = args.experiment.resolve(), args.runs.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / ".stock-mirror").exists():
        parser.error("choose a campaign output distinct from the stock mirror")
    with (args.output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return execute(args)


if __name__ == "__main__":
    sys.exit(main())
