"""Run independent stages in worker processes, or render saved JSON only."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import tempfile

from .model import STAGES, dependencies, digest, feed_rows, load_config, validate


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, separators=(",", ":"), allow_nan=False)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fingerprint():
    """Identify simulator sources AND compiled bytecode actually available.

    Rendering code/config/docs are deliberately excluded: replotting should
    not execute an EVM. Solidity edits need `forge build` before running.
    """
    from alberta_buck.sim.chain import load_artifact, repo_root
    from importlib.metadata import version
    root = repo_root()
    paths = list((root / "src").rglob("*.sol"))
    paths += list((root / "core/python/buck_core").rglob("*.py"))
    paths += [root / "alberta_buck/sim" / n for n in (
        "deploy.py", "chain.py", "identity.py", "experiment.py", "router.py",
        "gauge.py", "pyrevm_backend.py", "signal_history.py", "control_lab/engine.py", "control_lab/model.py",
        "control_lab/events.py", "control_lab/block_pairs.py", "control_lab/historical.py", "control_lab/cli.py")]
    paths += [root / "alberta_buck/sim/artifacts/UniversalRouter.json", root / "foundry.toml"]
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    for name in ("Buck", "BuckCredit", "BuckKControllerDirect", "IdentityRegistryHarness",
                 "SimLP", "MockERC20", "UniswapV3Factory", "UniswapV3Pool", "WETH9",
                 "BuckBasketEquity", "BuckBasketEquityWheel", "BuckBasketUniswapV3",
                 "EquityDesk", "BasketRebalanceDirector", "BasketWheel", "BasketMachineWheel",
                 "BuckBasketEquityResponse", "BasketSignals", "OuterBandMonetaryDirector",
                 "ValidatedBasketVenue", "BlockPairSignals", "PairRebalancePolicy", "PairBasketEquityResponse"):
        h.update(digest(load_artifact(name)).encode())
    versions = {n: version(n) for n in ("pyrevm", "web3")}
    h.update(digest(versions).encode())
    return {"sha256": h.hexdigest(), "libraries": versions}


def worker(cfg, profile, stage, key, code, path):
    from .block_pairs import simulate_observed
    result = simulate_observed(cfg, profile, stage,
                               basket_mode="pair" if STAGES[stage].get("basket") else None)
    result.update(cache_key=key, implementation=code, sensor_details=True,
                  dependencies=dependencies(cfg, profile, stage))
    atomic_json(path, result)
    return {"profile": profile["name"], "stage": stage, "cache_key": key,
            "file": Path(path).name, "summary": result["summary"]}


def run_matrix(cfg, profiles, stages, out, jobs, force=False):
    out = Path(out)
    code = fingerprint()
    wanted, entries, pending = [], {}, []
    for profile in profiles:
        feed_rows(cfg, profile)  # reject impossible prices before starting workers
        for stage in stages:
            identity = (profile["name"], stage)
            wanted.append(identity)
            key = digest({"schema": 1, "implementation": code,
                          "inputs": dependencies(cfg, profile, stage)})
            path = out / f"{profile['name']}-{stage}-{key[:16]}.json"
            if path.exists() and not force:
                result = json.loads(path.read_text())
                if result.get("cache_key") == key:
                    entries[identity] = dict(profile=profile["name"], stage=stage, cache_key=key,
                                             file=path.name, summary=result["summary"])
                    print(f"cached {profile['name']}/{stage}", flush=True)
                    continue
            pending.append((cfg, profile, stage, key, code, str(path)))
    if pending:
        # Spawn gives each revm instance and deployment environment its own process.
        with ProcessPoolExecutor(max_workers=jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
            futures = {pool.submit(worker, *args): (args[1]["name"], args[2]) for args in pending}
            for future in as_completed(futures):
                entry = future.result()  # failures never become cached successes
                identity = futures[future]
                entries[identity] = entry
                print(f"ran {identity[0]}/{identity[1]}: {entry['summary']['seconds']:.2f}s, "
                      f"{entry['summary']['transactions']} tx", flush=True)
    manifest = {"schema": 1, "requested_config": cfg, "implementation": code,
                "entries": [entries[k] for k in wanted]}
    atomic_json(out / "manifest.json", manifest)
    return out / "manifest.json"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="doc/control-lab.toml")
    ap.add_argument("--out", default="doc/control-lab/results")
    ap.add_argument("--profiles", default="three-phase,mixed", help="comma-separated profile names")
    ap.add_argument("--stages", default=",".join(STAGES), help="comma-separated stages")
    ap.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument("--days", type=int, help="short smoke run override")
    ap.add_argument("--force", action="store_true", help="recompute selected cached runs")
    ap.add_argument("--render-only", action="store_true", help="read manifest/JSON without importing the EVM backend")
    ap.add_argument("--no-render", action="store_true")
    args = ap.parse_args(argv)
    if args.jobs < 1:
        ap.error("--jobs must be positive")
    if not args.render_only:
        cfg = load_config(args.config)
        if args.days is not None:
            cfg["days"] = args.days
            validate(cfg)
        names = args.profiles.split(",")
        profiles = [p for name in names for p in cfg["profiles"] if p["name"] == name]
        if len(profiles) != len(names) or len(set(names)) != len(names):
            ap.error("--profiles must name distinct configured profiles")
        stages = args.stages.split(",")
        if any(s not in STAGES for s in stages) or len(set(stages)) != len(stages):
            ap.error("--stages must name distinct known stages: " + ",".join(STAGES))
        run_matrix(cfg, profiles, stages, args.out, args.jobs, args.force)
    if not args.no_render:
        from .render import render
        render(Path(args.out) / "manifest.json")
    return 0
