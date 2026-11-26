"""Launch and resume the supported dashboard simulations."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from alberta_buck.sim import compare, experiment, runrecord

ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS = Path(__file__).with_name("experiments")
LIVE_RECIPES = ("economy", "economy-lifecycle")
RECIPES = {"economy": 2, "economy-lifecycle": 3, "basket-stock": 8}


def stock(runs, directory, name, days, seed):
    def invoke(script, *args):
        subprocess.run([sys.executable, str(ROOT / "scripts" / script), str(directory),
                        "--runs", str(runs), *args], cwd=ROOT, check=True)

    invoke("run-basket-stock-grid.py", "--config", str(ROOT / "alberta_buck/sim/studies/basket-stock-smoke.toml"),
           "--days", str(days), "--warmup-days", "2", "--seed", str(seed), "--regime", "history",
           "--noise", "0", "--endowment", "0", "--investor-rate", "6", "--exit-fraction", "0")
    invoke("summarize-basket-stock-grid.py", "--comparison", name, "--output", str(directory / "summary.md"))


def economy(runs, directory, name, days, seed):
    from alberta_buck.sim.savings_parity import main as replay

    engines = ("pyrevm", "webevm")
    ids = [f"{name}-{engine}" for engine in engines]
    evidence = directory / "parity.json"
    if not (evidence.is_file() and all(compare.complete(runs, run_id) for run_id in ids)):
        status = replay(["--days", str(days), "--seed", str(seed), "--output", str(evidence),
                         "--records", str(runs), "--record-prefix", name])
        if status:
            print("The records retain runtime differences reported by the parity check.")
    data = json.loads(evidence.read_text())
    if data["inputs"]["days"] != days or data["inputs"]["sets"]["scenario.seed"] != seed:
        raise ValueError("replay evidence has different inputs")
    manifests = [json.loads((runs / run_id / "manifest.json").read_text()) for run_id in ids]
    digest = data["inputs"]["price_digest"]
    for manifest, engine in zip(manifests, engines, strict=True):
        if (manifest["status"] != "complete" or manifest["frames"] != days or manifest["engine"] != engine
                or (manifest.get("source") or {}).get("price_digest") != digest):
            raise ValueError(f"unverified economy record: {manifest['id']}")
    compare.write_index(runs, {
        "schema": "buck-comparison/1", "name": name, "experiment": "economy", "days": days,
        "question": "Where do the Python and WebEVM economy runs diverge?",
        "axes": [{"name": "runtime", "reference": "pyrevm", "values": [{"label": e, "set": {}} for e in engines]}],
        "runs": [{"id": m["id"], "values": {"runtime": m["engine"]}, "revision": m["revision"]}
                 for m in manifests],
        "price_digest": digest, "seed": seed,
    })


def lifecycle(runs, name, days, seed):
    from alberta_buck.sim.loop import run
    from alberta_buck.sim.pyrevm_backend import PyrevmAnvil

    run_id = f"{name}-pyrevm"
    scenario = experiment.build(experiment.load(EXPERIMENTS / "economy-lifecycle.toml",
                                [f"scenario.days={days}", f"scenario.seed={seed}"]))
    tape = [[scenario.prices.ref(i, day) for day in range(scenario.days)]
            for i in range(len(scenario.tokens))]
    digest = hashlib.sha256(json.dumps([scenario.tokens, tape]).encode()).hexdigest()
    if runrecord.verified_complete(runs, run_id, scenario, digest, "pyrevm"):
        return
    recorder = runrecord.RunRecorder(runs, "Economy lifecycle", run_id=run_id)

    def record_source(_deployment):
        recorder.manifest["source"] = {"driver": "loop.run", "price_digest": digest}

    try:
        with PyrevmAnvil() as backend:
            run(scenario, backend, verbose=False, recorder=recorder, on_boot=record_source)
    except BaseException as exc:
        if recorder.manifest:
            recorder.close("failed", error=exc)
        raise
    recorder.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipe", nargs="?", choices=RECIPES)
    parser.add_argument("--list", action="store_true", help="list supported recipes")
    parser.add_argument("--runs", type=Path, default=runrecord.default_root())
    parser.add_argument("--campaigns", type=Path, default=Path("build/sim/workbench"))
    parser.add_argument("--id", help="campaign and dashboard comparison name")
    parser.add_argument("--days", type=int)
    parser.add_argument("--seed", type=int, default=41404)
    args = parser.parse_args(argv)
    if args.list:
        print("economy             Paired pyrevm and WebEVM replay\n"
              "economy-lifecycle   Python lifecycle\n"
              "basket-stock        Matched historical active and maintenance pair")
        return 0
    if args.recipe is None:
        parser.error("select a recipe or use --list")
    days = RECIPES[args.recipe] if args.days is None else args.days
    if not (4 if args.recipe == "basket-stock" else 1) <= days <= 30:
        parser.error("--days must be 1–30, or 4–30 for basket-stock")
    if not 1 <= args.seed <= 2**53 - 1:
        parser.error("--seed must be a positive JavaScript-safe integer")
    name = args.id or f"{args.recipe}-{days}d-{args.seed}"
    if not re.fullmatch(r"[A-Za-z0-9-]+", name):
        parser.error("--id must contain only letters, digits and hyphens")
    runs = args.runs.resolve()
    directory = args.campaigns.resolve() / name
    directory.mkdir(parents=True, exist_ok=True)
    identity = dict(recipe=args.recipe, seed=args.seed, days=days, runs=str(runs))
    request = directory / "request.json"
    with (directory / ".workbench.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if request.exists() and json.loads(request.read_text()) != identity:
            parser.error("campaign inputs changed; choose a new --id")
        if not request.exists():
            pending = request.with_suffix(".tmp")
            pending.write_text(json.dumps(identity, indent=2) + "\n")
            pending.replace(request)
        if args.recipe == "economy":
            economy(runs, directory, name, days, args.seed)
        elif args.recipe == "basket-stock":
            stock(runs, directory, name, days, args.seed)
        else:
            lifecycle(runs, name, days, args.seed)
    print(f"Dashboard run directory: {runs}")
    if args.recipe != "economy-lifecycle":
        print(f"Compare: ?tab=compare&comparison={name}")
    else:
        print(f"Run: {name}-pyrevm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
