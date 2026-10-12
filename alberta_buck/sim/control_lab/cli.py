"""What every campaign shares: atomic output, the implementation fingerprint, one worker."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from .model import dependencies, digest


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
        "gauge.py", "pyrevm_backend.py", "control_lab/engine.py", "control_lab/model.py",
        "control_lab/events.py", "control_lab/block_pairs.py", "control_lab/historical.py",
        "control_lab/commodities.py", "control_lab/cli.py")]
    paths += [root / "alberta_buck/sim/artifacts/UniversalRouter.json", root / "foundry.toml"]
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    for name in ("Buck", "BuckCredit", "BuckKControllerDirect", "IdentityRegistryHarness",
                 "SimLP", "MockERC20", "UniswapV3Factory", "UniswapV3Pool", "WETH9",
                 "BuckBasketEquity", "BuckBasketEquityWheel", "BuckBasketUniswapV3",
                 "BasketDesk", "BasketWheel", "BasketMachineWheel",
                 "BasketReference", "OuterBandDeskPolicy",
                 "BasketVenueFacet", "PairSignals", "PairRebalancePolicy", "BasketUpkeepFacet"):
        h.update(digest(load_artifact(name)).encode())
    versions = {n: version(n) for n in ("pyrevm", "web3")}
    h.update(digest(versions).encode())
    return {"sha256": h.hexdigest(), "libraries": versions}


def worker(cfg, profile, stage, key, code, path):
    from .block_pairs import simulate_observed
    result = simulate_observed(cfg, profile, stage)
    result.update(cache_key=key, implementation=code, sensor_details=True,
                  dependencies=dependencies(cfg, profile, stage))
    atomic_json(path, result)
    return {"profile": profile["name"], "stage": stage, "cache_key": key,
            "file": Path(path).name, "summary": result["summary"]}
