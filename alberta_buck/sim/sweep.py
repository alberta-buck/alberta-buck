"""Parallel experiment sweep: N runs -> eqmetrics summary table.

    python -m alberta_buck.sim.sweep EXP.toml [EXP2.toml ...]
        [--seeds 1,2,3]           # cross-product each experiment x seed
        [--set k=v ...]           # extra dotted overrides for EVERY run
        [--jobs 3]                # concurrent runs (each owns an anvil)
        [--outdir test/vectors/sweep]

Each run is a `python -m alberta_buck.sim --experiment ...` subprocess with
its own anvil on an ephemeral port; vectors + per-run logs land in --outdir.
When all runs finish, an eqmetrics acceptance table is printed and written
to <outdir>/summary.json.

All experiments in one sweep must share a data window (start/end/years):
the daily CSVs are generated once up front so parallel children never race
on the shared prices dir (they hit the gen_historical manifest cache).

A 5-year macro run is ~35 min of wall clock; budget jobs accordingly.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alberta_buck.sim import eqmetrics
from alberta_buck.sim import experiment as expmod

REPO = Path(__file__).resolve().parents[2]


def _window(exp) -> tuple:
    s = exp.scenario
    return (s.get("start", ""), s.get("end", ""), float(s.get("years", 0.0)))


def _run_one(spec: dict) -> dict:
    """Run one experiment subprocess; return its eqmetrics stats (or error)."""
    cmd = [sys.executable, "-m", "alberta_buck.sim",
           "--experiment", spec["toml"],
           "--out", spec["out"]]
    for kv in spec["sets"]:
        cmd += ["--set", kv]
    log = Path(spec["log"])
    with log.open("w") as lf:
        rc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT,
                            cwd=REPO).returncode
    if not Path(spec["out"]).exists():
        return {"path": spec["out"], "name": spec["label"], "frames": 0,
                "error": f"no vector (exit {rc}; see {log})"}
    st = eqmetrics.summarize(spec["out"])
    st["name"] = spec["label"]
    st["exit"] = rc
    return st


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alberta_buck.sim.sweep")
    ap.add_argument("experiments", nargs="+", help="experiment TOML files")
    ap.add_argument("--seeds", default="", help="comma-separated seed list; "
                    "cross-product with the experiments")
    ap.add_argument("--set", action="append", default=[], dest="sets",
                    metavar="KEY=VAL", help="extra override for every run")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--outdir", default="test/vectors/sweep")
    a = ap.parse_args(argv)

    outdir = Path(a.outdir)
    if not outdir.is_absolute():
        outdir = REPO / outdir
    outdir.mkdir(parents=True, exist_ok=True)

    # Load every experiment; enforce the single shared window; pre-generate
    # the CSVs once (children then hit the manifest cache).
    exps = []
    for path in a.experiments:
        exp = expmod.load(path, sets=a.sets)
        exps.append((path, exp))
    windows = {_window(e) for _, e in exps}
    if len(windows) > 1:
        ap.error(f"experiments span {len(windows)} data windows {windows}; "
                 "sweep one window at a time (parallel children share the "
                 "generated CSV dir)")
    s0 = exps[0][1].scenario
    from alberta_buck.sim.gen_historical import gen
    gen(start=s0.get("start") or None, end=s0.get("end") or None,
        years=float(s0.get("years") or 5.0))

    seeds = [s.strip() for s in a.seeds.split(",") if s.strip()]
    specs = []
    for path, exp in exps:
        for seed in (seeds or [None]):
            label = exp.name + (f"-s{seed}" if seed is not None else "")
            sets = list(a.sets)
            if seed is not None:
                sets.append(f"scenario.seed={seed}")
            specs.append({
                "toml": path,
                "label": label,
                "sets": sets,
                "out": str(outdir / f"eq-{label}.json"),
                "log": str(outdir / f"eq-{label}.log"),
            })

    print(f"[sweep] {len(specs)} runs x ~35min/5yr, {a.jobs} jobs -> {outdir}")
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        results = list(pool.map(_run_one, specs))

    # ---- summary table + JSON ---------------------------------------- #
    print(f"\n{'name':<18} {'seed':<7} {'days':>5} {'bv~':>8} {'bv sd':>7} "
          f"{'K~':>7} {'rail':>5} {'iss$M':>8} {'ret$M':>9} {'thr':>6}  verdict")
    n_pass = 0
    for st in results:
        if st.get("error"):
            print(f"{st['name']:<18} ERROR: {st['error']}")
            continue
        ok, _ = eqmetrics.accept(st)
        n_pass += bool(ok)
        print(eqmetrics.row(st))
    (outdir / "summary.json").write_text(json.dumps(results, indent=1))
    print(f"\n[sweep] {n_pass}/{len(results)} PASS; summary -> "
          f"{outdir / 'summary.json'}")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
