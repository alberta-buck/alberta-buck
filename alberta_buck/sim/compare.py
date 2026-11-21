"""Comparisons: runs identical in everything but the settings under study.

    python -m alberta_buck.sim.compare plan   SPEC
    python -m alberta_buck.sim.compare run    SPEC [--jobs 8] [--days N] [--runs DIR] [--here] [--only AXIS=VALUE ...]
    python -m alberta_buck.sim.compare report SPEC [--runs DIR] [--identical]

A comparison asks what one setting changes, everything else the same. Its spec (a name under
alberta_buck/sim/comparisons/, or a path) names an experiment, the settings every run shares, and one
or more axes; an axis names its values, and each value the settings that make it (the keys --set
takes). Every combination of the axes' values is one run. They all share the experiment, the shared
settings, the seed and the price path, and the keyed random streams (scenario.rng = "keyed") draw the
same arrivals and decisions in each until a setting that differs first acts: two runs that differ on
one axis differ only by what that axis changed.

    question        = "What does the integrator's one-year leak change?"
    experiment      = "economy"                  # alberta_buck/sim/experiments/economy.toml
    days            = 365
    [set]                                        # every run's settings
    "scenario.seed" = 41404
    [[axis]]
    name            = "leak"
    note            = "BuckKControllerDirect.setLeak: 0 is no leak"
    [axis.values]                                # the first value is the reference
    year            = {}                         # the experiment's own setting
    none            = { "deploy.leak_days" = 0.0 }

Each run is an ordinary run record in the runs directory ($SIM_RUNS or build/runs) named
<name>.<axis>-<value>[.<axis>-<value>...], so the dashboard opens it like any other. The index
<runs>/<name>.comparison.json names the axes and which run is which; =report= adds each run's
headline figures and, for every pair of runs that differ on one axis only, the differences and the
first day they diverge. The dashboard's Compare tab reads the index.

=run= freezes the committed tree under build/sim/compare/<name>/ (as the economy campaigns do,
alberta_buck.sim.economy) and runs every run not yet complete from there, in parallel; --here runs
the working tree instead, for quick checks. A run whose record is complete is skipped.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import datetime
import itertools
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import tomllib

from alberta_buck.sim import runrecord

HERE                            = Path(__file__).resolve().parent
SPECS                           = HERE / "comparisons"
EXPERIMENTS                     = HERE / "experiments"
SCHEMA                          = "buck-comparison/1"
LABEL                           = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
# The observables two runs are compared on, day by day: where they first differ.
WATCHED                         = (("control", "k"), ("control", "bvib"), ("market", "buck_usd"), ("supply", "total"))
RAW                             = 1e6


def repo_root() -> Path:
    return HERE.parents[1]


# --- the spec ----------------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Axis:
    name: str
    note: str
    values: tuple                   # ((label, {key: value}), ...): the first is the reference


@dataclass(frozen=True)
class Run:
    comparison: str
    values: tuple                   # ((axis, label), ...) in the axes' order
    settings: tuple                 # ((key, value), ...): the shared settings, then this run's own

    @property
    def id(self) -> str:
        return ".".join([self.comparison] + [f"{axis}-{label}" for axis, label in self.values])

    def sets(self) -> list[str]:
        """--set arguments: experiment._apply_set parses each value as a Python literal."""
        return [f"{key}={value!r}" for key, value in self.settings]


@dataclass
class Spec:
    name: str
    path: Path
    question: str
    experiment: Path
    days: int | None
    shared: dict
    axes: list

    def runs(self, only: dict | None = None) -> list[Run]:
        out                     = []
        for combo in itertools.product(*[axis.values for axis in self.axes]):
            values              = tuple((axis.name, label) for axis, (label, _) in zip(self.axes, combo))
            if only and any(dict(values).get(a) != v for a, v in only.items()):
                continue
            settings            = dict(self.shared)
            for _, own in combo:
                settings.update(own)
            out.append(Run(self.name, values, tuple(settings.items())))
        return out

    def reference(self, axis: str) -> str:
        return next(a for a in self.axes if a.name == axis).values[0][0]


def _label(kind: str, text) -> str:
    if not isinstance(text, str) or not LABEL.match(text):
        raise ValueError(f"{kind} {text!r}: letters, digits, '-' and '_' only (it names the run)")
    return text


def load_spec(arg: str | Path) -> Spec:
    path                        = Path(arg)
    if not path.exists():
        path                    = SPECS / f"{arg}.toml"
    data                        = tomllib.loads(path.read_text())
    name                        = _label("comparison", data.get("name", path.stem))
    experiment                  = Path(str(data.get("experiment", "")))
    if not experiment.suffix:
        experiment              = EXPERIMENTS / f"{experiment}.toml"
    elif not experiment.is_absolute():
        experiment              = repo_root() / experiment
    if not experiment.exists():
        raise ValueError(f"{path}: no experiment {data.get('experiment')!r}")
    shared                      = dict(data.get("set", {}))
    axes, seen                  = [], set()
    for raw in data.get("axis", []):
        axis                    = _label("axis", raw.get("name"))
        if axis in seen:
            raise ValueError(f"{path}: axis {axis!r} twice")
        seen.add(axis)
        values                  = raw.get("values") or {}
        if not values:
            raise ValueError(f"{path}: axis {axis!r} has no values")
        for label, sets in values.items():
            _label(f"axis {axis} value", label)
            if not isinstance(sets, dict):
                raise ValueError(f"{path}: {axis}.{label} must be a table of settings (key = value)")
        axes.append(Axis(axis, str(raw.get("note", "")), tuple((label, dict(sets)) for label, sets in values.items())))
    if not axes:
        raise ValueError(f"{path}: a comparison needs at least one [[axis]]")
    days                        = data.get("days")
    return Spec(name, path, str(data.get("question", "")), experiment, int(days) if days else None, shared, axes)


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root()))
    except ValueError:
        return str(path)


def index(spec: Spec, days) -> dict:
    """What the dashboard needs to place every run of the comparison."""
    return {
        "schema": SCHEMA, "name": spec.name, "question": spec.question, "spec": _rel(spec.path),
        "experiment": _rel(spec.experiment), "days": days, "set": spec.shared,
        "axes": [{"name": a.name, "note": a.note, "reference": a.values[0][0],
                  "values": [{"label": label, "set": sets} for label, sets in a.values]} for a in spec.axes],
        "runs": [{"id": r.id, "values": dict(r.values)} for r in spec.runs()],
    }


def index_path(runs: Path, name: str) -> Path:
    return runs / f"{name}.comparison.json"


def write_index(runs: Path, body: dict) -> Path:
    runs.mkdir(parents=True, exist_ok=True)
    body                        = dict(body, updated=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
    target                      = index_path(runs, body["name"])
    temporary                   = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(body, indent=1) + "\n")
    temporary.replace(target)
    return target


def comparisons(runs: Path) -> list[dict]:
    """Every comparison index under the runs directory, newest first."""
    out                         = []
    for p in Path(runs).glob("*.comparison.json"):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError) as e:
            out.append(dict(name=p.name.removesuffix(".comparison.json"), error=str(e)[:200]))
    return sorted(out, key=lambda c: c.get("updated") or "", reverse=True)


# --- the figures -------------------------------------------------------------------------------- #
def _get(frame, *path):
    for key in path:
        frame                   = frame.get(key) if isinstance(frame, dict) else None
    return frame


def _num(v):
    return None if v is None else float(v)


def headline(frames: list[dict]) -> dict:
    """One run's figures: where K, BVIB and the BUCK's price ended and how they moved, the supply and
    its sources at the end, the households that arrived and left, and the refusals and faults."""
    if not frames:
        return {"days": 0}
    last                        = frames[-1]
    ks                          = [x for f in frames if (x := _num(_get(f, "control", "k"))) is not None]
    bv                          = [x for f in frames if (x := _num(_get(f, "control", "bvib"))) is not None]
    roles                       = _get(last, "population", "roles") or {}
    money                       = lambda *p: None if _get(last, *p) is None else float(_get(last, *p)) / RAW
    mean                        = lambda xs: sum(xs) / len(xs) if xs else None

    def period_return(field):
        opening = _num(_get(frames[0], "basket", field))
        closing = _num(_get(last, "basket", field))
        return closing / opening - 1 if opening and closing is not None else None

    share_return = period_return("share_price")
    hold_return = period_return("hold_index")
    return {
        "share_price_end": _num(_get(last, "basket", "share_price")),
        "share_return": share_return, "hold_return": hold_return,
        "wallet_buck_end": money("basket", "idle_buck"),
        "days": len(frames),
        "k_end": ks[-1] if ks else None, "k_mean": mean(ks), "k_min": min(ks, default=None), "k_max": max(ks, default=None),
        "bvib_end": bv[-1] if bv else None, "bvib_gap_mean": mean([abs(x - 1) for x in bv]),
        "bvib_nominal_end": _num(_get(last, "control", "bvib_nominal")),
        "bvib_above": mean([1.0 if x > 1 else 0.0 for x in bv]),
        "buck_usd_end": _num(_get(last, "market", "buck_usd")),
        "supply_end": money("supply", "total"), "basket_lien_end": money("supply", "basket_lien"),
        "household_principal_end": money("supply", "household_principal"),
        "arrived": {r: v.get("arrived") for r, v in roles.items()},
        "exited": {r: v.get("exited") for r, v in roles.items()},
        "refused_days": sum(int(_get(f, "health", "errors") or 0) for f in frames),
        "faults": sum(int(_get(f, "health", "faults") or 0) for f in frames),
    }


def divergence(a: list[dict], b: list[dict]) -> dict:
    """The first day two runs' frames differ: on the watched observables (K, BVIB, BUCK/USD, supply)
    and anywhere in the frame. None for both over every common day: the runs are identical."""
    days                        = {f["day"]: f for f in b}
    out                         = {"watched": None, "any": None, "days": 0}
    for f in sorted(a, key=lambda x: x["day"]):
        g                       = days.get(f["day"])
        if g is None:
            break
        out["days"]            += 1
        if out["any"] is None and f != g:
            out["any"]          = f["day"]
        if out["watched"] is None and any(_get(f, *p) != _get(g, *p) for p in WATCHED):
            out["watched"]      = f["day"]
        if out["any"] is not None and out["watched"] is not None:
            break
    return out


def delta(run: dict, reference: dict) -> dict:
    """The run's figures less the reference run's, where both are numbers."""
    out                         = {}
    for key, value in run.items():
        other                   = reference.get(key)
        if isinstance(value, dict) and isinstance(other, dict):
            out[key]            = {k: (v - other[k]) for k, v in value.items()
                                   if isinstance(v, (int, float)) and isinstance(other.get(k), (int, float))}
        elif isinstance(value, (int, float)) and isinstance(other, (int, float)):
            out[key]            = value - other
    return out


def pairs(body: dict) -> list[tuple[str, dict, dict]]:
    """Every pair of runs that differ on exactly one axis, with the run on the axis's reference value
    second: (axis, run, reference run)."""
    runs                        = body["runs"]
    by_values                   = {tuple(sorted(r["values"].items())): r for r in runs}
    out                         = []
    for axis in body["axes"]:
        ref                     = axis["reference"]
        for r in runs:
            if r["values"][axis["name"]] == ref:
                continue
            partner             = by_values.get(tuple(sorted({**r["values"], axis["name"]: ref}.items())))
            if partner is not None:
                out.append((axis["name"], r, partner))
    return out


def summarize(body: dict, runs_root: Path) -> dict:
    """Read every complete run's frames; the headline figures, and each one-axis pair's differences."""
    frames, status              = {}, {}
    for r in body["runs"]:
        directory               = runs_root / r["id"]
        try:
            status[r["id"]]     = json.loads((directory / "manifest.json").read_text()).get("status")
        except (OSError, ValueError):
            status[r["id"]]     = "missing"
        frames[r["id"]]         = runrecord.read_lines(directory / runrecord.FILES["frames"])[0]
    figures                     = {rid: dict(headline(fr), status=status[rid]) for rid, fr in frames.items()}
    compared                    = []
    for axis, run, ref in pairs(body):
        a, b                    = frames[run["id"]], frames[ref["id"]]
        compared.append({"axis": axis, "value": run["values"][axis], "reference": ref["values"][axis],
                         "run": run["id"], "against": ref["id"],
                         "held": {k: v for k, v in run["values"].items() if k != axis},
                         "diverges": divergence(a, b) if a and b else None,
                         "delta": delta(figures[run["id"]], figures[ref["id"]]) if a and b else None})
    return {"figures": figures, "pairs": compared}


# --- running ------------------------------------------------------------------------------------ #
def base_dir(name: str) -> Path:
    return repo_root() / "build/sim/compare" / name


def runs_dir(arg) -> Path:
    return Path(arg).resolve() if arg else runrecord.default_root().resolve()


def complete(runs_root: Path, run_id: str) -> bool:
    try:
        return json.loads((runs_root / run_id / "manifest.json").read_text()).get("status") == "complete"
    except (OSError, ValueError):
        return False


def command(spec: Spec, run: Run, root: Path, runs_root: Path, out: Path | None, days: int | None) -> list[str]:
    experiment                  = root / spec.experiment.resolve().relative_to(repo_root())
    cmd                         = [sys.executable, "-m", "alberta_buck.sim", "--backend", "pyrevm",
                                   "--experiment", str(experiment)]
    for s in run.sets():
        cmd                    += ["--set", s]
    if days:
        cmd                    += ["--days", str(days)]
    if out is None:
        return cmd + ["--no-record"]
    return cmd + ["--record", str(runs_root), "--run-id", run.id, "--out", str(out)]


def seed_of(spec: Spec, run: Run):
    from alberta_buck.sim.experiment import load
    return load(spec.experiment, sets=run.sets()).scenario.get("seed")


def run(args) -> int:
    from alberta_buck.sim.economy import environment, freeze
    spec                        = load_spec(args.spec)
    days                        = args.days or spec.days
    runs_root                   = runs_dir(args.runs)
    base                        = base_dir(spec.name)
    (base / "runs").mkdir(parents=True, exist_ok=True)
    existing                    = index_path(runs_root, spec.name)
    if existing.exists():
        saved                   = json.loads(existing.read_text())
        if saved.get("days") != days:
            raise ValueError(f"{spec.name} ran for {saved.get('days')} days, not {days}: use a new comparison name")
    body                        = index(spec, days)
    write_index(runs_root, body)
    if args.here:
        root, env               = repo_root(), dict(os.environ)
    else:
        root                    = freeze(repo_root(), base)
        env                     = environment(root)
    only                        = dict(x.split("=", 1) for x in args.only)
    todo                        = [r for r in spec.runs(only) if not complete(runs_root, r.id)]
    print(f"{spec.name}: {len(spec.runs())} runs, {len(todo)} to run ({days or 'full'} days; records in {runs_root})",
          flush=True)
    if args.jobs > 1 and len(todo) > 1:
        # Warm each seed's identity cache once, alone, before runs fan out and race to write it.
        for seed in sorted({seed_of(spec, r) for r in todo}, key=str):
            first               = next(r for r in todo if seed_of(spec, r) == seed)
            print(f"preflight seed {seed}", flush=True)
            with (base / "runs" / f"preflight-{seed}.log").open("w") as log:
                subprocess.run(command(spec, first, root, runs_root, None, 2), cwd=root, env=env, stdout=log,
                               stderr=subprocess.STDOUT, check=True)

    def one(r: Run) -> bool:
        started                 = time.time()
        print(f"start {r.id}", flush=True)
        with (base / "runs" / f"{r.id}.log").open("w") as log:
            done                = subprocess.run(command(spec, r, root, runs_root, base / "runs" / f"{r.id}.json", days),
                                                 cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        ok                      = done.returncode == 0 and complete(runs_root, r.id)
        print(f"{'done' if ok else 'FAILED'} {r.id} ({round(time.time() - started)} s)", flush=True)
        return ok

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        outcomes                = list(pool.map(one, todo))
    return report(args) or int(not all(outcomes))


# --- reporting ---------------------------------------------------------------------------------- #
def _fmt(v, digits=4, sign=False) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:+.{digits}f}" if sign else f"{v:.{digits}f}"
    return f"{v:+d}" if sign and isinstance(v, int) else str(v)


def report(args) -> int:
    spec                        = load_spec(args.spec)
    runs_root                   = runs_dir(args.runs)
    path                        = index_path(runs_root, spec.name)
    if not path.exists():
        raise ValueError(f"{spec.name} has not run (no {path})")
    body                        = json.loads(path.read_text())
    body["summary"]             = summarize(body, runs_root)
    write_index(runs_root, body)
    figures                     = body["summary"]["figures"]
    print(f"\n{spec.name}: {spec.question}\n")
    print(f"{'run':52} {'status':9} {'days':>5} {'K end':>7} {'K mean':>7} {'BVIB end':>9} {'|BVIB-1|':>9} "
          f"{'BUCK/USD':>9} {'supply M':>9} {'share ROI%':>10} {'hold ROI%':>10}")
    for rid, f in figures.items():
        print(f"{rid:52} {f.get('status') or '-':9} {f['days']:>5} {_fmt(f.get('k_end')):>7} {_fmt(f.get('k_mean')):>7} "
              f"{_fmt(f.get('bvib_end')):>9} {_fmt(f.get('bvib_gap_mean')):>9} {_fmt(f.get('buck_usd_end')):>9} "
              f"{_fmt(None if f.get('supply_end') is None else f['supply_end'] / 1e6, 2):>9} "
              f"{_fmt(None if f.get('share_return') is None else 100 * f['share_return'], 3):>10} "
              f"{_fmt(None if f.get('hold_return') is None else 100 * f['hold_return'], 3):>10}")
    for axis in body["axes"]:
        every                   = [p for p in body["summary"]["pairs"] if p["axis"] == axis["name"]]
        rows                    = [p for p in every if p["diverges"]]
        if not rows:
            continue
        waiting                 = len(every) - len(rows)
        print(f"\n{axis['name']}: against {axis['reference']}  {axis['note']}"
              + (f"  ({waiting} pairs not yet run)" if waiting else ""))
        print(f"  {'value':14} {'held':44} {'diverges':>9} {'dK end':>10} {'dK mean':>10} {'d|BVIB-1|':>10} "
              f"{'dBUCK/USD':>10} {'dsupply M':>10}")
        for p in rows:
            held                = ",".join(f"{k}={v}" for k, v in p["held"].items()) or "-"
            dv, d               = p["diverges"], p["delta"] or {}
            day                 = "-" if dv is None else ("never" if dv["any"] is None else f"day {dv['watched'] if dv['watched'] is not None else dv['any']}")
            print(f"  {p['value']:14} {held[:44]:44} {day:>9} {_fmt(d.get('k_end'), 6, sign=True):>10} "
                  f"{_fmt(d.get('k_mean'), 6, sign=True):>10} {_fmt(d.get('bvib_gap_mean'), 6, sign=True):>10} "
                  f"{_fmt(d.get('buck_usd_end'), 6, sign=True):>10} "
                  f"{_fmt(None if d.get('supply_end') is None else d['supply_end'] / 1e6, 2, sign=True):>10}")
    print(f"\nindex: {path}")
    if getattr(args, "identical", False):
        # The null comparison's verdict: every run complete, and no pair ever differs.
        bad                     = [rid for rid, f in figures.items() if f.get("status") != "complete"]
        bad                    += [p["run"] for p in body["summary"]["pairs"] if not p["diverges"] or p["diverges"]["any"] is not None]
        if bad:
            print(f"NOT IDENTICAL: {', '.join(bad)}")
            return 1
        print("identical: every pair is the same, frame for frame")
    return 0


def plan(args) -> int:
    spec                        = load_spec(args.spec)
    print(f"{spec.name}: {spec.question}\nexperiment {_rel(spec.experiment)}, {args.days or spec.days or 'full'} days")
    for axis in spec.axes:
        print(f"  axis {axis.name} (reference {axis.values[0][0]}): {', '.join(label for label, _ in axis.values)}"
              + (f"  -- {axis.note}" if axis.note else ""))
    for r in spec.runs(dict(x.split("=", 1) for x in args.only)):
        print(f"{r.id}\n    " + " ".join(f"--set {s}" for s in r.sets()))
    return 0


def main(argv=None) -> int:
    ap                          = argparse.ArgumentParser(prog="python -m alberta_buck.sim.compare",
                                                          description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["plan", "run", "report"])
    ap.add_argument("spec", help="a comparison name (alberta_buck/sim/comparisons/<name>.toml) or a path")
    ap.add_argument("--runs", default=None, help="runs directory (default $SIM_RUNS or build/runs)")
    ap.add_argument("--days", type=int, default=None, help="override the spec's horizon (a new comparison name)")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--here", action="store_true", help="run the working tree, not a frozen snapshot")
    ap.add_argument("--only", action="append", default=[], metavar="AXIS=VALUE", help="only the runs with this value")
    ap.add_argument("--identical", action="store_true",
                    help="fail unless every pair is the same frame for frame (the null comparison's check)")
    args                        = ap.parse_args(argv)
    return {"plan": plan, "run": run, "report": report}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
