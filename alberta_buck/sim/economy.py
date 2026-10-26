"""The household economy's grid (doc/review/household-economy-plan.org, "The baseline economy").

    python -m alberta_buck.sim.economy plan   --campaign NAME [--cells centre|grid|all]
    python -m alberta_buck.sim.economy run    --campaign NAME [--cells ...] [--jobs 8] [--days N]
    python -m alberta_buck.sim.economy report --campaign NAME

Cells scale the supply roles' arrival rates and the pace of adopting BUCK working balances by
1/3, 1 or 3, on the historical or reverting price path, for two seeds. Demand changes adoption
speed, not the eventual balance held. `policies` compares the four surplus policies at the centre.
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
from hashlib import sha256
from html import escape
import importlib.util
import json
import os
from pathlib import Path
import re
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
VARIANTS                        = {"open": {"gain": 0}, "rising": {"growth": "rising"}, "falling": {"growth": "falling"},
                                   "no-paydown": {"paydown_share": 0},
                                   **{f"policy-{p}": {"surplus_policy": p}
                                      for p in ("adaptive", "basket", "retire", "discount")}}
POP                             = "agents.HouseholdPopulationAgent"
MEASURED_FROM                   = 365       # acceptance reads the economy after its first year
STRESSES                        = {}
for strength, large in (("low", False), ("high", True)):
    for name, change in {
        "demand": dict(action="arm_shock", side="buy", usd=2_000_000 if large else 500_000, days=30),
        "supply": dict(action="arm_shock", side="sell", usd=2_000_000 if large else 500_000, days=30),
        "commodity": dict(action="price_shock", token="NRGC", mult=.5 if large else .8),
        "mortgage-rate": dict(action="set_population", apr_shift=.04 if large else .02),
        "recession": dict(action="set_population", income_scale=.5 if large else .75, days=180),
        "adoption-stop": dict(action="set_population", rate_scale=0 if large else .5),
        "adoption-surge": dict(action="set_population", rate_scale=3 if large else 1.5),
        "investor-exit": dict(action="set_population", exit_now=.75 if large else .25),
    }.items():
        STRESSES[f"stress-{name}-{strength}"] = [dict(day=730, **change)]


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
        for role in ("debtor", "saver"):
            out.append(f"{POP}.{role}_rate={centre[f'{role}_rate'] * FACTORS[self.supply]!r}")
        # Demand: how fast each household comes to pay in BUCKs, and so to hold them.
        out.append(f"{POP}.adopt_years={centre.get('adopt_years', 1.) / FACTORS[self.demand]!r}")
        for key, value in VARIANTS.get(self.variant, {}).items():
            out.append(f"{POP}.{key}={value}")
        if self.variant in STRESSES:
            changes             = STRESSES[self.variant]
            out.append(f"interventions={changes!r}")
            if any(c["action"] == "arm_shock" for c in changes):
                out.append("scenario.agents.ShockAgent=1")
        return out


def cells(which: str) -> list[Cell]:
    centre                      = [Cell("mid", "mid", p, SEEDS[0]) for p in PRICES]
    if which == "centre":
        return centre
    grid                        = [Cell(s, dm, p, seed) for seed in SEEDS for p in PRICES for s in FACTORS for dm in FACTORS]
    if which == "grid":
        return grid
    every                       = grid + [Cell("mid", "mid", p, SEEDS[0], v) for v in ("open", "rising", "falling")
                                          for p in PRICES]
    if which == "probe":        # the centre on both seeds and paths, and the two corners that failed before
        return ([Cell("mid", "mid", p, s) for s in SEEDS for p in PRICES]
                + [Cell("hi", "mid", "history", SEEDS[0]), Cell("lo", "hi", "history", SEEDS[0])])
    if which == "paydown":      # the discounted paydown on and off, on the centre and two corners
        probe                   = ([Cell("mid", "mid", p, SEEDS[0]) for p in PRICES]
                                   + [Cell("hi", "mid", "history", SEEDS[0]), Cell("lo", "hi", "history", SEEDS[0])])
        return probe + [Cell(c.supply, c.demand, c.prices, c.seed, "no-paydown") for c in probe]
    if which == "policies":
        return [Cell("mid", "mid", p, s, "" if policy == "adaptive" else f"policy-{policy}") for s in SEEDS for p in PRICES
                for policy in ("adaptive", "basket", "retire", "discount")]
    if which == "stress":
        return [Cell("mid", "mid", "history", s, v) for s in SEEDS for v in STRESSES]
    if which == "all":
        return every
    if which == "rest":         # everything but the centre, when the centre runs as its own campaign
        return [c for c in every if c not in centre]
    raise ValueError(f"unknown cells {which!r}: centre, probe, policies, stress, paydown, grid, rest or all")


def selected(which, seed=None):
    if which == "centre" and seed is not None:
        return [Cell("mid", "mid", p, seed) for p in PRICES]
    chosen                      = [c for c in cells(which) if seed is None or c.seed == seed]
    if not chosen:
        raise ValueError(f"no {which} cells for seed {seed}")
    return chosen


def campaign_path(root, name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ValueError("campaign must be a name containing letters, numbers, dots, underscores or hyphens")
    return root / "build/sim/economy" / name


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
        saved                   = json.loads(manifest.read_text())
        frozen                  = saved["commit"]
        if not saved.get("files"):
            raise ValueError("legacy snapshot has no content hashes; use a new campaign name (report still works)")
        for name, digest in saved["files"].items():
            if sha256((snapshot / name).read_bytes()).hexdigest() != digest:
                raise ValueError(f"frozen campaign source changed: {name}")
        if frozen != head:
            print(f"note: campaign frozen at {frozen[:12]}, HEAD is {head[:12]}; running the frozen sources",
                  flush=True)
        return snapshot
    if _git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("commit tracked changes before a long campaign; economy-smoke tests working-tree edits")
    partial                     = base / "snapshot.partial"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    archive                     = subprocess.Popen(["git", "archive", "--format=tar", "HEAD"], cwd=root,
                                                   stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(partial)], stdin=archive.stdout, check=True)
    if archive.wait():
        raise RuntimeError("git archive failed")
    archive.stdout.close()
    shutil.copytree((root / "out").resolve(), partial / "out")
    # Development kernels are generated, not tracked. Keep them with the frozen Python sources.
    for pattern in ("*.so", "*.pyd", "*.dylib"):
        for native in (root / "core/python/buck_core").glob(pattern):
            shutil.copy2(native, partial / "core/python/buck_core" / native.name)
    for csv in (root / "alberta_buck/sim/prices").glob("*-*d.csv"):
        shutil.copy2(csv, partial / "alberta_buck/sim/prices" / csv.name)
    cache                       = root / "test/vectors/identity-cache.json"
    if cache.exists():
        shutil.copy2(cache, partial / "test/vectors" / cache.name)
    files                       = {str(p.relative_to(partial)): sha256(p.read_bytes()).hexdigest()
                                   for p in sorted(partial.rglob("*")) if p.is_file()
                                   and p.name != "identity-cache.json"}
    manifest.write_text(json.dumps(dict(commit=head, frozen=datetime.datetime.now().isoformat(timespec="seconds"),
                                        files=files), indent=1))
    partial.rename(snapshot)
    return snapshot


def adopt(runs: Path) -> list[str]:
    """Name every checkpoint whose world completed (an older runner judged by the exit status)."""
    named                       = []
    status_path                 = runs.parent / "status.json"
    status                      = json.loads(status_path.read_text()) if status_path.exists() else {}
    for partial in runs.glob("*.partial"):
        recorded                = status.get(partial.stem, {})
        if recorded.get("status") == "running" or recorded.get("returncode", 0) != 0:
            continue
        try:
            completed           = json.loads(partial.read_bytes())["meta"]["run"]["completed"]
        except (ValueError, KeyError):
            continue            # still being written, or not a vector
        if completed:
            ensure_pdf(partial)
            partial.replace(partial.with_suffix(".json"))
            named.append(partial.stem)
    return named


def environment(snapshot: Path) -> dict:
    path                        = [snapshot, snapshot / "core/python", snapshot / "core/contracts/python"]
    kernel                      = importlib.util.find_spec("buck_kernel")
    if kernel is not None:
        path.append(Path(kernel.origin).parents[1])
    return dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, path)), ALBERTA_BUCK_REPO=str(snapshot),
                SIM_CREDIT_FRAMES="1")


def run(args) -> int:
    root                        = repo_root()
    base                        = campaign_path(root, args.campaign)
    runs                        = base / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    snapshot                    = freeze(root, base)
    horizon                     = args.days or 1827
    settings_path               = base / "run-settings.json"
    if settings_path.exists() and json.loads(settings_path.read_text())["days"] != horizon:
        raise ValueError("campaign horizon changed; use a new campaign name")
    settings_path.write_text(json.dumps(dict(days=horizon), indent=1) + "\n")
    env                         = environment(snapshot)
    centre                      = centre_rates(snapshot)
    adopt(runs)
    todo                        = [c for c in selected(args.cells, args.seed) if not (runs / f"{c.name}.json").exists()]
    status_path                 = base / "status.json"
    status                      = json.loads(status_path.read_text()) if status_path.exists() else {}
    lock                        = threading.Lock()

    def record(name, **fields):
        with lock:
            status[name]        = status.get(name, {}) | fields
            temporary           = status_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(status, indent=1))
            temporary.replace(status_path)

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
            subprocess.run(command(cell, None, 2), cwd=snapshot, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)

    def one(cell):
        out                     = runs / f"{cell.name}.json"
        partial                 = out.with_suffix(".partial")
        started                 = time.time()
        record(cell.name, status="running", started=datetime.datetime.now().isoformat(timespec="seconds"))
        print(f"start {cell.name}", flush=True)
        with out.with_suffix(".log").open("w") as log:
            done                = subprocess.run(command(cell, partial, args.days), cwd=snapshot, env=env,
                                                 stdout=log, stderr=subprocess.STDOUT)
        error                   = ""
        try:
            ok                  = done.returncode == 0 and json.loads(partial.read_bytes())["meta"]["run"]["completed"]
            if ok:
                ensure_pdf(partial)
        except Exception as exc:
            ok, error           = False, repr(exc)
        if ok:
            partial.replace(out)
        record(cell.name, status="complete" if ok else "failed", returncode=done.returncode,
               seconds=round(time.time() - started), error=error)
        print(f"{'done' if ok else 'FAILED'} {cell.name} ({round(time.time() - started)} s)", flush=True)
        return bool(ok)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        outcomes                = list(pool.map(one, todo))
    report(args)
    return int(not all(outcomes))


def ensure_pdf(vector):
    """A matching PDF is part of a completed result; regenerate it from saved data when needed."""
    from alberta_buck.sim.report import pdf_path, render
    target                      = pdf_path(vector)
    digest                      = sha256(vector.read_bytes()).hexdigest().encode()
    if not target.exists() or digest not in target.read_bytes():
        render(vector)
    if digest not in target.read_bytes():
        raise ValueError(f"PDF does not match {vector}")
    return target


# -- acceptance ------------------------------------------------------------------------------------ #

def _k(frame) -> float:
    k                           = frame["buckK"]
    return k / 1e18 if k > 10 else k


def judge(vector: Path) -> dict:
    """The plan's acceptance tests on one completed vector, with the numbers that explain them."""
    data                        = json.loads(vector.read_bytes())
    frames                      = data["frames"]
    if not data.get("meta", {}).get("run", {}).get("completed") or not frames:
        raise ValueError(f"{vector}: not a completed run")
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
    # A later successful decision must not erase an earlier recorded household error.
    errors                      = sum(bool(a.get("error")) for f in frames for a in f.get("octl", []))
    drawn                       = {r: sum(a.get("drawn", 0) for a in members if a.get("role") == r) / 1e12
                                   for r in ("debtor", "saver")}
    pop                         = last.get("pop") or {}
    multipliers                 = [f["pop"]["multiplier"] for f in window if f.get("pop")]
    result                      = dict(
        vector=str(vector), days=last["day"] + 1,
        full_horizon=[f["day"] for f in frames] == list(range(1827)),
        mean_err=sum(err) / len(err), p99_err=err[min(len(err) - 1, int(.99 * len(err)))],
        interior=interior, longest_rail=longest, k_end=_k(last), k_min=min(ks), k_max=max(ks),
        supply_start=frames[0].get("supply", 0) / 1e12, supply_end=last.get("supply", 0) / 1e12,
        drawn=drawn, investor_deposits=last.get("inv_invested", 0) / 1e12,
        arrived={r: pop.get(r, {}).get("arrived", 0) for r in ("debtor", "saver", "investor")},
        supply_multiplier=sum(m["debtor"] for m in multipliers) / len(multipliers) if multipliers else None,
        demand_multiplier=sum(m["investor"] for m in multipliers) / len(multipliers) if multipliers else None,
        errors=errors, refused=last.get("household_refused", 0),
        divested=last.get("hh_divested", 0), divest_bought=last.get("hh_divest_bought", 0) / 1e12,
        paid_down=last.get("hh_paid_down", 0) / 1e12,
        divest_short=last.get("hh_divest_short", 0),
        k_factor=sum(f["pop"].get("k_factor", 1) for f in window if f.get("pop")) / max(1, len(multipliers)))
    result["passed"]            = (result["full_horizon"] and result["mean_err"] < .01 and result["p99_err"] < .05
                                   and interior >= .95 and longest <= 30 and errors == 0 and result["divest_short"] == 0)
    return result


def report(args) -> int:
    base                        = campaign_path(repo_root(), args.campaign)
    if not (base / "runs").is_dir():
        raise ValueError(f"no campaign runs at {base}")
    adopt(base / "runs")
    vectors                     = sorted((base / "runs").glob("*.json"))
    for vector in vectors:
        ensure_pdf(vector)
    results                     = [judge(v) for v in vectors]
    (base / "summary.json").write_text(json.dumps(results, indent=1))
    rows                        = []
    for r in results:
        v                       = Path(r["vector"])
        pdf                     = v.with_suffix(".pdf")
        link                    = f'<a href="runs/{escape(pdf.name)}">{escape(v.stem)}</a>'
        a, dr                   = r["arrived"], r["drawn"]
        verdict                 = "pass" if r["passed"] else "FAIL"
        if not r["full_horizon"]:
            verdict             = "SMOKE" if r["days"] < 1827 else "INCOMPLETE"
        rows.append(
            f'<tr class="{"pass" if r["passed"] else "fail"}"><td>{link}</td>'
            f'<td><a href="runs/{escape(v.name)}">JSON</a></td><td>{verdict}</td>'
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
<p><a href="source-manifest.json">Frozen source manifest</a> | <a href="status.json">Worker status</a></p>
<p>{len(results)} completed runs; {passed} pass. Measured from day {MEASURED_FROM}. A run passes when the basket's
price in BUCKs averages within 1% of one with its 99th percentile within 5%, K stays at least 0.05 inside its rails
on 95% of days and never sits at a rail for more than 30 days running, and no household records an error.
Cells are named s&lt;supply&gt;-d&lt;demand&gt;-&lt;prices&gt;-&lt;seed&gt;: supply scales the debtors' and
savers' arrival rates, demand the pace of adopting BUCK working balances, by 1/3 (lo), 1 (mid) or 3 (hi).
Demand changes how soon the balance is held, not its eventual size. SMOKE runs are shorter than five years
and do not establish equilibrium acceptance. Errors count recorded household observations throughout the run.</p>
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
    for c in selected(args.cells, args.seed):
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
    campaign_path(repo_root(), args.campaign)
    if args.jobs < 1 or (args.days is not None and args.days < 1):
        ap.error("jobs and days must be positive")
    return {"plan": plan, "run": run, "report": report}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
