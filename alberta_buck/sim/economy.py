"""The household economy's grid (doc/review/household-economy-plan.org, "The baseline economy").

    python -m alberta_buck.sim.economy plan   --campaign NAME [--cells centre|grid|all]
    python -m alberta_buck.sim.economy run    --campaign NAME [--cells ...] [--jobs 8] [--days N]
    python -m alberta_buck.sim.economy report --campaign NAME

Cells scale the supply roles' arrival rates (debtors and savers) and the investors' rate by 1/3, 1
or 3 around economy.toml's centre, on the historical or the reverting price path, for two seeds.
"all" adds the centre with no price response (gain 0) and with rising and falling growth.

`run` freezes the committed tree into build/sim/economy/NAME/snapshot (with the compiled contracts,
generated price files and identity cache it needs) and runs each cell from there, so later edits
cannot reach a running campaign. A cell's vector is named only when its run completed; the loop
writes its PDF beside it. `report` reads completed vectors only, judges each against the plan's
acceptance tests and writes index.html and summary.json.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import datetime
from html import escape
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import tomllib

EXPERIMENT                      = "alberta_buck/sim/experiments/economy.toml"
FACTORS                         = {"lo": 1 / 3, "mid": 1., "hi": 3.}
PRICES                          = ("history", "revert")
SEEDS                           = (41404, 45517)
VARIANTS                        = {"open": {"gain": 0}, "rising": {"growth": "rising"}, "falling": {"growth": "falling"}}
POP                             = "agents.HouseholdPopulationAgent"
MEASURED_FROM                   = 365       # acceptance reads the economy after its first year


@dataclass(frozen=True)
class Cell:
    supply: str
    demand: str
    prices: str
    seed: int
    variant: str = ""

    @property
    def name(self) -> str:
        return f"s{self.supply}-d{self.demand}-{self.prices}-{self.seed}" + (f"-{self.variant}" if self.variant else "")

    def sets(self, centre: dict) -> list[str]:
        out                     = [f"scenario.prices={self.prices}", f"scenario.seed={self.seed}"]
        for role, factor in (("debtor", self.supply), ("saver", self.supply), ("investor", self.demand)):
            out.append(f"{POP}.{role}_rate={centre[f'{role}_rate'] * FACTORS[factor]!r}")
        for key, value in VARIANTS.get(self.variant, {}).items():
            out.append(f"{POP}.{key}={value}")
        return out


def cells(which: str) -> list[Cell]:
    centre                      = [Cell("mid", "mid", p, SEEDS[0]) for p in PRICES]
    if which == "centre":
        return centre
    grid                        = [Cell(s, dm, p, seed) for seed in SEEDS for p in PRICES for s in FACTORS for dm in FACTORS]
    if which == "grid":
        return grid
    every                       = grid + [Cell("mid", "mid", p, SEEDS[0], v) for v in VARIANTS for p in PRICES]
    if which == "all":
        return every
    if which == "rest":         # everything but the centre, when the centre runs as its own campaign
        return [c for c in every if c not in centre]
    raise ValueError(f"unknown cells {which!r}: centre, grid, rest or all")


def centre_rates(root: Path) -> dict:
    return tomllib.loads((root / EXPERIMENT).read_text())["agents"]["HouseholdPopulationAgent"]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git(root: Path, *args) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, text=True, capture_output=True).stdout


def freeze(root: Path, base: Path) -> Path:
    """The committed tree plus the untracked build products a run reads; reused if already made."""
    snapshot                    = base / "snapshot"
    manifest                    = base / "source-manifest.json"
    head                        = _git(root, "rev-parse", "HEAD").strip()
    if snapshot.exists():
        frozen                  = json.loads(manifest.read_text())["commit"]
        if frozen != head:
            print(f"note: campaign frozen at {frozen[:12]}, HEAD is {head[:12]}; running the frozen sources",
                  flush=True)
        return snapshot
    partial                     = base / "snapshot.partial"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    archive                     = subprocess.Popen(["git", "archive", "--format=tar", "HEAD"], cwd=root,
                                                   stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(partial)], stdin=archive.stdout, check=True)
    if archive.wait():
        raise RuntimeError("git archive failed")
    shutil.copytree((root / "out").resolve(), partial / "out")
    for csv in (root / "alberta_buck/sim/prices").glob("*-*d.csv"):
        shutil.copy2(csv, partial / "alberta_buck/sim/prices" / csv.name)
    cache                       = root / "test/vectors/identity-cache.json"
    if cache.exists():
        shutil.copy2(cache, partial / "test/vectors" / cache.name)
    manifest.write_text(json.dumps(dict(commit=head, frozen=datetime.datetime.now().isoformat(timespec="seconds"),
                                        uncommitted=_git(root, "status", "--porcelain").splitlines()), indent=1))
    partial.rename(snapshot)
    return snapshot


def adopt(runs: Path) -> list[str]:
    """Name every checkpoint whose world completed (an older runner judged by the exit status)."""
    named                       = []
    for partial in runs.glob("*.partial"):
        try:
            completed           = json.loads(partial.read_bytes())["meta"]["run"]["completed"]
        except (ValueError, KeyError):
            continue            # still being written, or not a vector
        if completed:
            partial.replace(partial.with_suffix(".json"))
            named.append(partial.stem)
    return named


def environment(snapshot: Path) -> dict:
    kernel                      = Path(importlib.util.find_spec("buck_kernel").origin).parents[1]
    path                        = [snapshot, snapshot / "core/python", snapshot / "core/contracts/python", kernel]
    return dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, path)), ALBERTA_BUCK_REPO=str(snapshot),
                SIM_CREDIT_FRAMES="1")


def run(args) -> int:
    root                        = repo_root()
    base                        = root / "build/sim/economy" / args.campaign
    runs                        = base / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    snapshot                    = freeze(root, base)
    env                         = environment(snapshot)
    centre                      = centre_rates(snapshot)
    adopt(runs)
    todo                        = [c for c in cells(args.cells) if not (runs / f"{c.name}.json").exists()
                                   and (args.seed is None or c.seed == args.seed)]
    status_path                 = base / "status.json"
    status                      = json.loads(status_path.read_text()) if status_path.exists() else {}
    lock                        = threading.Lock()

    def record(name, **fields):
        with lock:
            status[name]        = status.get(name, {}) | fields
            status_path.write_text(json.dumps(status, indent=1))

    def command(cell, out, days=None):
        cmd                     = [sys.executable, "-m", "alberta_buck.sim", "--backend", "pyrevm",
                                   "--experiment", EXPERIMENT]
        for s in cell.sets(centre):
            cmd                += ["--set", s]
        if days:
            cmd                += ["--days", str(days)]
        return cmd + (["--out", str(out)] if out else [])

    # Warm each seed's identity cache once, alone, before runs fan out and race to write it.
    for seed in sorted({c.seed for c in todo}):
        cell                    = next(c for c in todo if c.seed == seed)
        print(f"preflight seed {seed}", flush=True)
        with (runs / f"preflight-{seed}.log").open("w") as log:
            subprocess.run(command(cell, None, 2), cwd=snapshot, env=env, stdout=log, stderr=subprocess.STDOUT)

    def one(cell):
        out                     = runs / f"{cell.name}.json"
        partial                 = out.with_suffix(".partial")
        started                 = time.time()
        record(cell.name, status="running", started=datetime.datetime.now().isoformat(timespec="seconds"))
        print(f"start {cell.name}", flush=True)
        with out.with_suffix(".log").open("w") as log:
            done                = subprocess.run(command(cell, partial, args.days), cwd=snapshot, env=env,
                                                 stdout=log, stderr=subprocess.STDOUT)
        # The vector says whether the world completed; the process may still fail in its closing summary.
        ok                      = partial.exists() and json.loads(partial.read_bytes())["meta"]["run"]["completed"]
        if ok:
            partial.replace(out)
        record(cell.name, status="complete" if ok else "failed", returncode=done.returncode,
               seconds=round(time.time() - started))
        print(f"{'done' if ok else 'FAILED'} {cell.name} ({round(time.time() - started)} s)", flush=True)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        list(pool.map(one, todo))
    print("=== economy run done", flush=True)
    return 0


# -- acceptance ------------------------------------------------------------------------------------ #

def _k(frame) -> float:
    k                           = frame["buckK"]
    return k / 1e18 if k > 10 else k


def judge(vector: Path) -> dict:
    """The plan's acceptance tests on one completed vector, with the numbers that explain them."""
    data                        = json.loads(vector.read_bytes())
    frames                      = data["frames"]
    deploy                      = data.get("meta", {}).get("experiment", {}).get("deploy", {})
    kmin, kmax                  = float(deploy.get("kmin", 0.)), float(deploy.get("kmax", .95))
    window                      = [f for f in frames if f["day"] >= MEASURED_FROM] or frames
    err                         = sorted(abs(f["basketVal"] / 1e18 - 1) for f in window)
    ks                          = [_k(f) for f in window]
    interior                    = sum(kmin + .05 <= k <= kmax - .05 for k in ks) / len(ks)
    streak = longest            = 0
    for k in ks:
        streak                  = streak + 1 if (k <= kmin + .005 or k >= kmax - .005) else 0
        longest                 = max(longest, streak)
    last                        = frames[-1]
    members                     = [a for a in last.get("octl", []) if "member" in a]
    errors                      = sum(bool(a.get("error")) for a in members)
    drawn                       = {r: sum(a.get("drawn", 0) for a in members if a.get("role") == r) / 1e12
                                   for r in ("debtor", "saver")}
    pop                         = last.get("pop") or {}
    multipliers                 = [f["pop"]["multiplier"] for f in window if f.get("pop")]
    result                      = dict(
        vector=str(vector), days=last["day"] + 1,
        mean_err=sum(err) / len(err), p99_err=err[min(len(err) - 1, int(.99 * len(err)))],
        interior=interior, longest_rail=longest, k_end=_k(last), k_min=min(ks), k_max=max(ks),
        supply_start=frames[0].get("supply", 0) / 1e12, supply_end=last.get("supply", 0) / 1e12,
        drawn=drawn, investor_deposits=last.get("inv_invested", 0) / 1e12,
        arrived={r: pop.get(r, {}).get("arrived", 0) for r in ("debtor", "saver", "investor")},
        supply_multiplier=sum(m["debtor"] for m in multipliers) / len(multipliers) if multipliers else None,
        demand_multiplier=sum(m["investor"] for m in multipliers) / len(multipliers) if multipliers else None,
        errors=errors, refused=last.get("household_refused", 0))
    result["passed"]            = (result["mean_err"] < .01 and result["p99_err"] < .05 and interior >= .95
                                   and longest <= 30 and errors == 0)
    return result


def report(args) -> int:
    base                        = repo_root() / "build/sim/economy" / args.campaign
    adopt(base / "runs")
    vectors                     = sorted((base / "runs").glob("*.json"))
    results                     = [judge(v) for v in vectors]
    (base / "summary.json").write_text(json.dumps(results, indent=1))
    rows                        = []
    for r in results:
        v                       = Path(r["vector"])
        pdf                     = v.with_suffix(".pdf")
        link                    = (f'<a href="runs/{escape(pdf.name)}">{escape(v.stem)}</a>' if pdf.exists()
                                   else escape(v.stem))
        a, dr                   = r["arrived"], r["drawn"]
        rows.append(
            f'<tr class="{"pass" if r["passed"] else "fail"}"><td>{link}</td>'
            f'<td><a href="runs/{escape(v.name)}">JSON</a></td><td>{"pass" if r["passed"] else "FAIL"}</td>'
            f'<td>{100 * r["mean_err"]:.2f}%</td><td>{100 * r["p99_err"]:.2f}%</td>'
            f'<td>{100 * r["interior"]:.0f}%</td><td>{r["longest_rail"]}</td>'
            f'<td>{r["k_min"]:.3f} / {r["k_max"]:.3f} / {r["k_end"]:.3f}</td>'
            f'<td>{r["supply_start"]:.1f} &rarr; {r["supply_end"]:.1f}</td>'
            f'<td>{dr["debtor"]:.1f} / {dr["saver"]:.1f}</td><td>{r["investor_deposits"]:.1f}</td>'
            f'<td>{a["debtor"]} / {a["saver"]} / {a["investor"]}</td>'
            f'<td>{(r["supply_multiplier"] or 0):.2f} / {(r["demand_multiplier"] or 0):.2f}</td>'
            f'<td>{r["errors"]}</td><td>{r["refused"]}</td></tr>')
    passed                      = sum(r["passed"] for r in results)
    (base / "index.html").write_text(f'''<!doctype html><html lang="en"><meta charset="utf-8">
<title>Household economy: {escape(args.campaign)}</title>
<style>body{{font:14px system-ui;margin:2rem}}table{{border-collapse:collapse}}
td,th{{padding:.35rem .5rem;border-bottom:1px solid #ddd}}th{{text-align:left;position:sticky;top:0;background:white}}
td:nth-child(n+3){{text-align:right}}tr.fail td:nth-child(3){{color:#b00020;font-weight:600}}a{{color:#1857a4}}</style>
<h1>Household economy: {escape(args.campaign)}</h1>
<p>{len(results)} completed runs; {passed} pass. Measured from day {MEASURED_FROM}. A run passes when the basket's
price in BUCKs averages within 1% of one with its 99th percentile within 5%, K stays at least 0.05 inside its rails
on 95% of days and never sits at a rail for more than 30 days running, and no household records an error.
Cells are named s&lt;supply&gt;-d&lt;demand&gt;-&lt;prices&gt;-&lt;seed&gt;: supply scales the debtors' and
savers' arrival rates, demand the investors', by 1/3 (lo), 1 (mid) or 3 (hi).</p>
<table><thead><tr><th>Run (PDF)</th><th>Data</th><th>Result</th><th>Mean |bvib-1|</th><th>99th pct</th>
<th>K interior</th><th>Days at a rail</th><th>K min / max / end</th><th>Supply (M)</th>
<th>Drawn: debtors / savers (M)</th><th>Investor deposits (M)</th><th>Arrived d / s / i</th>
<th>Multiplier s / d</th><th>Errors</th><th>Refusals</th></tr></thead><tbody>
{chr(10).join(rows)}
</tbody></table></html>
''')
    print(f"{len(results)} runs, {passed} pass: {base / 'index.html'}")
    return 0


def plan(args) -> int:
    centre                      = centre_rates(repo_root())
    for c in cells(args.cells):
        print(f"{c.name:34s} {' '.join(c.sets(centre))}")
    return 0


def main(argv=None) -> int:
    ap                          = argparse.ArgumentParser(description=__doc__,
                                                          formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("plan", "run", "report"))
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--cells", default="all")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--days", type=int, help="a shorter horizon, for a smoke campaign")
    ap.add_argument("--seed", type=int, help="run only this seed's cells")
    args                        = ap.parse_args(argv)
    return {"plan": plan, "run": run, "report": report}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
