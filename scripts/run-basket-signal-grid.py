"""Run and resume matched real-EVM basket signal captures."""

import argparse
import csv
import fcntl
import hashlib
import importlib.metadata
import itertools
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time

from alberta_buck.sim import experiment
from alberta_buck.sim.gen_prices import gen_three_phase
from alberta_buck.sim.prices import PRICE_SCALE

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "scripts/capture-basket-signals.py"
REGIMES = ("three-phase", "mixed", "shock", "history", "revert")
SEEDS = (41404, 41405)
ONSETS = {"standard": (.0025, .02), "early": (.0025, .005)}
SPEEDS = {"fast": 1, "slow": 3}  # full-strength half-life in days
LAWS = ("velocity", "excursion", "both")
CONTROLS = {
    "production": "BasketUpkeepSignalTrace",
    "maintenance": "BasketUpkeepSignalTraceMaintenance",
}
CANDIDATE = "BasketUpkeepSignalSizing"
CODE_SIZE_LIMIT = 65536  # Local EVM limit for the instrumented research facets.
HISTORY_REVERSION_DAYS = 30


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def replace_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temp.replace(path)


def runtime_inputs():
    paths = [Path(__file__), CAPTURE, ROOT / "foundry.toml"]
    paths.extend(ROOT.glob("alberta_buck/**/*.py"))
    paths.extend(ROOT.glob("alberta_buck/sim/artifacts/*.json"))
    paths.extend(ROOT.glob("core/python/**/*.py"))
    paths.extend(ROOT.glob("core/python/**/*.so"))
    paths.extend(ROOT.glob("src/**/*.sol"))
    paths.extend(ROOT.glob("out/*.sol/*.json"))
    paths.extend(ROOT.glob("core/contracts/python/buck_contracts/*.json"))
    entries = [(str(path.relative_to(ROOT)), digest(path.read_bytes())) for path in sorted(set(paths))]
    return {
        "code_and_artifacts": digest(json_bytes(entries)),
        "python": sys.version.split()[0],
        "packages": {name: importlib.metadata.version(name) for name in ("pyrevm", "web3")},
        "sim_environment": {k: v for k, v in os.environ.items() if k.startswith("SIM_")},
    }


def reverting_history(source, days):
    """Replay observed log increments with an exponential pull to the first quote."""
    persistence = math.exp(-1 / HISTORY_REVERSION_DAYS)
    base = [source[0]]
    excursion = 0.0
    for before, after in zip(source, source[1:days]):
        excursion = persistence * excursion + math.log(after / before)
        base.append(source[0] * math.exp(excursion))
    return base


def close_prices(scenario, regime, days, seed, sigma):
    """Daily prices in the simulator's scaled micro-dollar units."""
    sources = scenario.prices.series
    prices = []
    for index, (token, source) in enumerate(zip(scenario.tokens, sources)):
        first = source[0]
        if regime == "history":
            base = source[:days]
        elif regime == "revert":
            base = reverting_history(source, days)
        elif regime == "shock":
            # Two separate relative price breaks on a flat tape: LABR loses
            # 20% permanently, and CNST gains 25% for the middle third.
            earlier, later = days // 3, 2 * days // 3
            base = [first * (.8 if index == 0 and day >= earlier
                             else 1.25 if index == 1 and earlier <= day < later else 1)
                    for day in range(days)]
        else:
            base = gen_three_phase(first, index, len(sources), days)
            if regime == "mixed":
                trend = (.20, -.15, .08, -.08, .12)[index % 5]
                phase = 2 * math.pi * index / len(sources)
                base = [value * math.exp(trend * day / 30 + .035 * (
                    math.sin(2 * math.pi * day / 11 + phase) - math.sin(phase)))
                        for day, value in enumerate(base)]
        # Match the increments across regimes and amplitudes for each seed.
        rng = random.Random(f"basket-signal-noise-v1/{seed}/{token[0]}")
        walk = 0.0
        row = []
        for day, value in enumerate(base):
            if day:
                walk += rng.gauss(0.0, 1.0)
            row.append(round(value * math.exp(sigma * walk)))
        if min(row) <= 0:
            raise ValueError(f"nonpositive reference price for {token[0]} in {regime}")
        prices.append(row)
    return prices


def price_files(folder, tokens, prices):
    price_digest = digest(json_bytes([tokens, prices]))
    paths = []
    for index, row in enumerate(prices):
        path = folder / f"{price_digest[:16]}-{index}.csv"
        temp = path.with_suffix(".tmp")
        with temp.open("w", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(["day", "close_usd_micro"])
            writer.writerows((day, value * PRICE_SCALE) for day, value in enumerate(row))
        temp.replace(path)
        paths.append(path)
    return price_digest, paths


def arms(cross):
    for arm, upkeep in CONTROLS.items():
        yield arm, "standard", None, ("production" if arm == "production" else "off"), upkeep
    onsets, speeds = (ONSETS, SPEEDS) if cross else (("standard",), ("fast",))
    for onset, speed, law in itertools.product(onsets, speeds, LAWS):
        yield "candidate", onset, speed, law, CANDIDATE


def selections(args):
    regimes = args.regime or (("three-phase",) if args.preset == "smoke" else REGIMES)
    noise = args.noise_sigma or ((.01,) if args.preset == "smoke" else (0., .01))
    seeds = args.seed or (SEEDS[:1] if args.preset == "smoke" else SEEDS)
    for regime, sigma, seed in itertools.product(dict.fromkeys(regimes), dict.fromkeys(noise),
                                                dict.fromkeys(seeds)):
        if args.preset == "first" and not args.seed and seed == SEEDS[1] and (
                sigma == 0 or regime not in ("three-phase", "history")):
            continue
        cross = args.preset == "full" or (
            args.preset == "first" and regime == "three-phase" and sigma > 0 and seed == seeds[0])
        yield regime, sigma, seed, cross


def plan(args, runtime):
    exp = experiment.load(args.experiment)
    exp.scenario["prices"] = "history"
    scenario = experiment.build(exp)
    if scenario.day_step != 1 or scenario.prices.days < args.days:
        raise ValueError("experiment must have daily observations and cover the requested horizon")
    tokens = [token[0] for token in scenario.tokens]
    cells = []
    source_digest = digest(args.experiment.read_bytes())
    for regime, sigma, seed, cross in selections(args):
        noise_label = repr(float(sigma)) if sigma else "0"
        group = f"{regime}--noise-{noise_label}--seed-{seed}"
        prices = close_prices(scenario, regime, args.days, seed, sigma)
        price_digest, files = price_files(args.output / "prices", tokens, prices)
        daily_prices = [list(row) for row in zip(*prices)]
        for arm, onset, speed, law, upkeep in arms(cross):
            ident = f"{group}--{arm}"
            settings = ["scenario.prices=history", f"scenario.seed={seed}", f"machine.upkeep={upkeep}"]
            slack, patience = ONSETS[onset]
            settings.extend((f"machine.slack={slack}", f"machine.patience={patience}"))
            half_life = SPEEDS.get(speed)
            rate = math.log(2) / half_life if half_life else None
            if arm == "candidate":
                ident += f"-{onset}-{speed}-{law}"
                settings.extend((f"machine.signal_rate={rate}", f"machine.signal_mode={law}",
                                 "machine.signal_velocity=0.005", "machine.signal_excursion=0.02"))
            fingerprint = digest(json_bytes([ident, args.days, settings, price_digest,
                                             source_digest, runtime, CODE_SIZE_LIMIT]))
            path = f"cells/{ident}-{fingerprint[:16]}"
            cells.append(dict(id=ident, group=group, regime=regime, noise_sigma=sigma, seed=seed,
                              arm=arm, onset=onset, slack=slack, patience=patience, speed=speed,
                              response=law, rate_per_day=rate, half_life_days=half_life,
                              status="planned", output=path + ".json", log=path + ".log",
                              fingerprint=fingerprint, price_digest=price_digest, settings=settings,
                              price_files=[str(path.relative_to(args.output)) for path in files],
                              expected_prices=daily_prices))
    return dict(schema_version=1, backend="pyrevm", code_size_limit=CODE_SIZE_LIMIT,
                preset=args.preset, days=args.days, tokens=tokens,
                identity_cache="isolated per capture",
                experiment=os.path.relpath(args.experiment, ROOT), experiment_sha256=source_digest,
                history_reversion_days=HISTORY_REVERSION_DAYS,
                historical_sources=scenario.csv_files, runtime=runtime, cells=cells)


def valid_capture(path, args, cell, tokens):
    try:
        data = json.loads(path.read_text())
        rows = data["frames"]
        expected = experiment.load(args.experiment, cell["settings"]).resolved()
        expected["scenario"]["days"] = args.days
        actual = data["config"]
        expected.pop("source")
        actual.pop("source")
        if data["tokens"] != tokens or actual != expected or len(rows) != args.days:
            return False
        ticks = expected["scenario"]["ticks_per_day"]
        n = len(tokens)
        marks = set()
        decisions = 0
        pairs_ready = False
        for day, (row, prices) in enumerate(zip(rows, cell["expected_prices"])):
            if row["day"] != day or row["prices"] != prices or len(row["pairs"]) != n * (n - 1) // 2:
                return False
            pairs_ready |= any(pair["ready"] for pair in row["pairs"])
            decisions += len(row["decisions"])
            if len(row["decisions"]) % n:
                return False
            for event in itertools.chain(row["trades"], row["decisions"]):
                mark = event["tx"], event["index"]
                if (not 0 <= event["tick"] < ticks or not 0 <= event["token"] < n
                        or not mark[0] or mark[1] < 0 or mark in marks):
                    return False
                marks.add(mark)
        return decisions > 0 or not pairs_ready
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def save(args, manifest):
    counts = {status: sum(cell["status"] == status for cell in manifest["cells"])
              for status in ("planned", "running", "complete", "failed")}
    public = [{key: value for key, value in cell.items() if key != "expected_prices"}
              for cell in manifest["cells"]]
    replace_json(args.output / "manifest.json", {**manifest, "counts": counts, "cells": public})


def start(args, cell):
    dest = args.output / cell["output"]
    partial = dest.with_suffix(".partial.json")
    partial.unlink(missing_ok=True)
    command = [sys.executable, str(CAPTURE), str(partial), "--experiment", str(args.experiment),
               "--days", str(args.days), "--code-size-limit", str(CODE_SIZE_LIMIT)]
    command.extend(arg for setting in cell["settings"] for arg in ("--set", setting))
    command.extend(arg for path in cell["price_files"] for arg in ("--price-file", str(args.output / path)))
    cache = tempfile.TemporaryDirectory(prefix="basket-signal-identity-")
    try:
        command.extend(("--identity-cache", str(Path(cache.name) / "registry.json")))
        with (args.output / cell["log"]).open("w") as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       env={**os.environ, "ALBERTA_BUCK_REPO": str(ROOT)})
    except BaseException:
        cache.cleanup()
        raise
    return process, partial, time.monotonic(), cache


def execute(args, manifest):
    tokens = manifest["tokens"]
    manifest_path = args.output / "manifest.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"cells": []}
    prior_hashes = {cell["fingerprint"]: cell["output_sha256"] for cell in previous["cells"]
                    if "output_sha256" in cell}
    for cell in manifest["cells"]:
        dest = args.output / cell["output"]
        known = prior_hashes.get(cell["fingerprint"])
        if known:
            cell["output_sha256"] = known
        if valid_capture(dest, args, cell, tokens):
            observed = digest(dest.read_bytes())
            if known is None or known == observed:
                cell.update(status="complete", output_sha256=observed)
    save(args, manifest)
    pending = [cell for cell in manifest["cells"] if cell["status"] != "complete"]
    print(f"{len(manifest['cells'])} cells, {len(pending)} incomplete, {args.days} days/cell", flush=True)
    if args.plan:
        return 0
    running = {}
    try:
        while pending or running:
            while pending and len(running) < args.jobs:
                cell = pending.pop(0)
                try:
                    running[cell["id"]] = (cell, *start(args, cell))
                except OSError as error:
                    cell.update(status="failed", error=str(error))
                else:
                    cell["status"] = "running"
                print(f"{cell['status']} {cell['id']}", flush=True)
                save(args, manifest)
            for ident, (cell, process, partial, started, cache) in list(running.items()):
                status = process.poll()
                timed_out = status is None and time.monotonic() - started > args.timeout
                if status is None and not timed_out:
                    continue
                if timed_out:
                    process.kill()
                    process.wait()
                cache.cleanup()
                if status == 0 and valid_capture(partial, args, cell, tokens):
                    dest = args.output / cell["output"]
                    partial.replace(dest)
                    cell.update(status="complete", output_sha256=digest(dest.read_bytes()))
                else:
                    message = ("timed out" if timed_out else f"process exited {status}" if status
                               else "capture missing or failed validation")
                    cell.update(status="failed", error=message)
                print(f"{cell['status']} {ident}", flush=True)
                del running[ident]
                save(args, manifest)
            if running:
                time.sleep(.25)
    finally:
        for cell, process, _, _, cache in running.values():
            if process.poll() is None:
                process.kill()
            process.wait()
            cache.cleanup()
            cell.update(status="failed", error="campaign interrupted")
        save(args, manifest)
    return int(any(cell["status"] != "complete" for cell in manifest["cells"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", choices=("smoke", "first", "full"), default="smoke")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--experiment", type=Path,
                        default=ROOT / "alberta_buck/sim/experiments/economy.toml")
    parser.add_argument("--days", type=int, help="Override the preset horizon (8 or 36 days)")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=3600, help="Seconds per cell")
    parser.add_argument("--regime", action="append", choices=REGIMES)
    parser.add_argument("--noise-sigma", action="append", type=float, help="Daily log-walk sigma, repeatable")
    parser.add_argument("--seed", action="append", type=int)
    parser.add_argument("--plan", action="store_true", help="Write price inputs and manifest without running the cells")
    args = parser.parse_args()
    if args.days is None:
        args.days = 8 if args.preset == "smoke" else 36
    if args.days < 3 or args.jobs < 1 or args.timeout < 1:
        parser.error("use at least 3 days, 1 job and 1 second per cell")
    if any(not math.isfinite(sigma) or sigma < 0 for sigma in args.noise_sigma or ()):
        parser.error("--noise-sigma must be finite and nonnegative")
    args.output = args.output.resolve()
    args.experiment = args.experiment.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error(f"another campaign is using {args.output}")
        for folder in ("cells", "prices"):
            (args.output / folder).mkdir(exist_ok=True)
        try:
            return execute(args, plan(args, runtime_inputs()))
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    sys.exit(main())
