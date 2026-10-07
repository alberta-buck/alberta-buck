"""Controlled 120-day gate experiment; no production policy is changed.

Each isolated worker uses a test adapter forwarding the same sensor. Only
running(i, side) differs. All actors, target lean, bands and execution caps
are held fixed, including the quorum's existing effect on response size.
"""
from concurrent.futures import ProcessPoolExecutor
import argparse
import copy
import json
import multiprocessing
from pathlib import Path
from unittest.mock import patch

from .cli import atomic_json, fingerprint
from .events import execution_counts
from .model import digest, load_config

NAMES = ("original", "band-only", "fast-velocity")


def run_case(config, mode, code, out):
    from alberta_buck.sim.chain import load_artifact
    from . import engine
    cfg = copy.deepcopy(config)
    profile = next(p for p in cfg["profiles"] if p["name"] == "three-phase")
    cfg["feed"]["walk_amplification"] = 0
    adapter = load_artifact("BasketGateComparison")
    key = digest(dict(config=cfg, mode=mode, implementation=code, adapter=adapter))
    path = Path(out) / f"{NAMES[mode]}-{key[:16]}.json"
    if path.exists():
        result = json.loads(path.read_text())
        if result.get("cache_key") == key:
            return dict(mode=NAMES[mode], file=path.name, counts=execution_counts(result["frames"]), summary=result["summary"])

    class GateWorld(engine.World):
        def __init__(self, cfg, stage):
            super().__init__(cfg, stage)
            gate = self.chain.deploy("BasketGateComparison", self.signals.address, mode)
            self.chain.send(self.d.basket.functions.setResponse(gate.address,
                (cfg["basket"]["leash_bp"], cfg["monitor"]["interval"], round(cfg["basket"]["tilt"] * 1e18))), sender=self.d.gov)

    # One world per worker; restore the constructor on every exit. This
    # experiment does not add a policy switch to the production simulator.
    with patch.object(engine, "World", GateWorld):
        result = engine.simulate(cfg, profile, "basket")
    result.update(cache_key=key, implementation=code, experiment="gate-comparison", gate=NAMES[mode],
                  adapter_sha256=digest(adapter))
    atomic_json(path, result)
    return dict(mode=NAMES[mode], file=path.name, counts=execution_counts(result["frames"]), summary=result["summary"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="doc/control-lab.toml")
    ap.add_argument("--out", default="doc/control-lab/gate-comparison")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--days", type=int)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.days is not None:
        cfg["days"] = args.days
    code = {**fingerprint(), "experiment_sha256": digest(Path(__file__).read_text())}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(max_workers=args.jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = [pool.submit(run_case, cfg, mode, code, out) for mode in range(3)]
        entries = [future.result() for future in futures]
    atomic_json(out / "manifest.json", dict(implementation=code, entries=entries))
    for entry in entries:
        print(json.dumps(entry), flush=True)


if __name__ == "__main__":
    main()
