"""The star driver: one axis at a time from a baseline (WAVE3.org, "The
star method"; WP-12).

    python -m alberta_buck.sim.star STAR.toml
        [--outdir build/sim/star] [--jobs 4] [--days N] [--force]
        [--axes a,b] [--arms x,y] [--report-only] [--dry-run]

A STAR.toml names a baseline (dotted `--set` overrides applied to every
cell), the ARMS (experiment TOMLs -- the physics and the injection -- with
their own overrides), and the AXES: controllable knobs, each with a
baseline value and a lo / hi value.  For each arm the driver runs the
baseline cell and, per axis, the lo and hi cells with every other axis
held at its baseline value: 1 + 2A cells per arm.  An axis is either a
`--set` key (`key = "deploy.kp_frac"`) or an environment variable
(`env = "SIM_SHADOW_LAMBDA"`) for knobs that live outside the TOML.

    name = "k-integral-c0"
    objective = "G7 + G8 PASS; x4 AUC and rail days no worse than base"
    [baseline]
    "scenario.agents.ExcursionArbAgent" = 8
    [[arm]]
    name = "none"
    toml = "catalogue-none.toml"
    [[arm]]
    name = "squeeze-x4"
    toml = "catalogue-squeeze.toml"
    sets = { "agents.WhaleRaidAgent.budget_m" = [32, 48] }
    [[axis]]
    name = "kp_frac"
    key = "deploy.kp_frac"
    base = 0.02
    lo = 0.0
    hi = 0.05

Each cell is a `python -m alberta_buck.sim --experiment ... --backend
pyrevm` subprocess; vectors and logs land in <outdir>/<name>/; an
existing complete vector is reused unless --force.  The report (also
written by --report-only) is one SENSITIVITY TABLE per arm: the
baseline row, then per axis the lo and hi rows with each metric's delta
against the baseline -- verdict, tail bvib and K, rail share, the
injection's peak / AUC / recovery, defender realized and whale P&L, the
basket side (depositor realized, NAV in baskets), the K law (monthly
R^2, calm), and the organic-equivalence numbers max / RMS |K - K_base|
over the run (gate G7).  plot_star renders the panes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alberta_buck.sim import eqmetrics
from alberta_buck.sim.catalogue import complete

REPO = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO / "alberta_buck" / "sim" / "experiments"
STARS = EXPERIMENTS / "stars"
E18 = 10 ** 18


# ---------------------------------------------------------------------------
# spec
# ---------------------------------------------------------------------------

def load_star(path: str | Path) -> dict:
    p = Path(path)
    for cand in (p, p.with_suffix(".toml"), STARS / p.name,
                 STARS / (p.name + ".toml")):
        if cand.exists():
            p = cand
            break
    else:
        raise SystemExit(f"no such star spec: {path} (looked under {STARS})")
    spec = tomllib.loads(p.read_text())
    spec.setdefault("name", p.stem)
    spec.setdefault("baseline", {})
    spec.setdefault("arm", [])
    spec.setdefault("axis", [])
    spec["_path"] = str(p)
    for ax in spec["axis"]:
        if "key" not in ax and "env" not in ax:
            raise SystemExit(f"axis {ax.get('name')!r} needs `key` or `env`")
        for k in ("base", "lo", "hi"):
            if k not in ax:
                raise SystemExit(f"axis {ax.get('name')!r} needs `{k}`")
    return spec


def _fmt_set(key: str, val) -> str:
    """A --set KEY=VAL token; lists/bools/numbers via their Python repr,
    which experiment.py parses with ast.literal_eval."""
    if isinstance(val, str):
        return f"{key}={val}"
    return f"{key}={val!r}"


def cells(spec: dict, outdir: Path, arms=None, axes=None, days=None) -> list[dict]:
    """The star's cells: per arm, the baseline plus lo / hi per axis."""
    out = []
    want_arms = set(arms) if arms else None
    want_axes = set(axes) if axes else None
    for arm in spec["arm"]:
        if want_arms and arm["name"] not in want_arms:
            continue
        toml = EXPERIMENTS / arm["toml"]
        if not toml.exists():
            toml = Path(arm["toml"])
        if not toml.exists():
            raise SystemExit(f"no such arm toml: {arm['toml']}")
        base_sets = dict(spec["baseline"])
        base_sets.update(arm.get("sets") or {})
        base_env = {}
        for ax in spec["axis"]:
            # Pin every applicable axis at its baseline so "base" is
            # exactly the centre; an axis restricted to other arms leaves
            # this arm's own settings alone.
            if ax.get("arms") and arm["name"] not in ax["arms"]:
                continue
            if "key" in ax:
                base_sets[ax["key"]] = ax["base"]
            else:
                base_env[ax["env"]] = str(ax["base"])

        def cell(label, axis, level, sets, env):
            return {"arm": arm["name"], "axis": axis, "level": level,
                    "label": label, "toml": str(toml),
                    "sets": [_fmt_set(k, v) for k, v in sets.items()],
                    "env": dict(env), "days": days,
                    "out": str(outdir / f"{label}.json"),
                    "log": str(outdir / f"{label}.log")}

        out.append(cell(f"{arm['name']}-base", None, "base", base_sets,
                        base_env))
        for ax in spec["axis"]:
            if want_axes and ax["name"] not in want_axes:
                continue
            # An axis may name the arms it applies to (a whale-budget axis
            # is meaningless on the organic control).
            if ax.get("arms") and arm["name"] not in ax["arms"]:
                continue
            for level in ("lo", "hi"):
                sets, env = dict(base_sets), dict(base_env)
                if "key" in ax:
                    sets[ax["key"]] = ax[level]
                else:
                    env[ax["env"]] = str(ax[level])
                out.append(cell(f"{arm['name']}-{ax['name']}-{level}",
                                ax["name"], level, sets, env))
    return out


def cell_complete(path: Path, days: int | None) -> bool:
    """A cell is complete when its vector reaches the experiment horizon
    (catalogue.complete) or, for a truncated (--days N) cell, day N-1."""
    if not path.exists():
        return False
    try:
        d = json.loads(path.read_text())
    except Exception:
        return False
    if not days:
        # A truncated cell records its own horizon in the resolved config.
        days = ((d.get("meta") or {}).get("experiment") or {}) \
            .get("scenario", {}).get("days") or 0
    if not days:
        return complete(path)
    fr = d.get("frames") or []
    return bool(fr) and int(fr[-1].get("day", 0)) >= int(days) - 1


def _run_one(spec: dict) -> dict:
    out = Path(spec["out"])
    if out.exists() and not spec.get("force") and \
            cell_complete(out, spec.get("days")):
        return {"label": spec["label"], "skipped": True}
    cmd = [sys.executable, "-m", "alberta_buck.sim",
           "--experiment", spec["toml"], "--backend", "pyrevm",
           "--director", "pairs", "--out", spec["out"]]
    if spec.get("days"):
        cmd += ["--days", str(spec["days"])]
    for kv in spec["sets"]:
        cmd += ["--set", kv]
    env = dict(os.environ)
    env.update(spec.get("env") or {})
    with Path(spec["log"]).open("w") as lf:
        rc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT,
                            cwd=REPO, env=env).returncode
    return {"label": spec["label"], "exit": rc, "ok": out.exists()}


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _k_series(path: Path) -> dict:
    d = json.loads(path.read_text())
    return {int(f.get("day", i)): f.get("buckK", 0) / E18
            for i, f in enumerate(d.get("frames") or [])}


def k_equivalence(a: dict, b: dict) -> dict | None:
    """max and RMS of |K_a(day) - K_b(day)| over the days both carry --
    the organic-equivalence numbers of gate G7."""
    days = sorted(set(a) & set(b))
    if not days:
        return None
    diffs = [abs(a[t] - b[t]) for t in days]
    return {"max": max(diffs),
            "rms": math.sqrt(sum(x * x for x in diffs) / len(diffs)),
            "n": len(diffs)}


def _metrics(st: dict) -> dict:
    """The sensitivity table's columns from an eqmetrics stats dict."""
    ok, _ = eqmetrics.accept(st)
    xs = [x for x in (st.get("excursions") or [])
          if x.get("src") in ("raid", "iv")]
    x = xs[0] if xs else {}
    b = (st.get("basket") or {}).get("end") or {}
    kfc = st.get("kfc") or {}
    law = kfc.get("law") or {}
    h30 = (kfc.get("h") or {}).get(30) or (kfc.get("h") or {}).get("30") or {}
    mk = (st.get("markout") or {}).get("basket") or {}
    return {
        "verdict": "PASS" if ok else "FAIL",
        "bv": st.get("bv_tail_mean"), "K": st.get("k_tail_mean"),
        "rail": st.get("k_tail_rail_frac"),
        "peak": (x.get("peak_dev") * 100) if x.get("peak_dev") is not None else None,
        "auc": x.get("auc_pct_days"), "recov": x.get("recovery_days"),
        "exc_real": x.get("d_exc_real_m"), "whale": x.get("d_raid_pnl_m"),
        "dm_real": b.get("dm_real_m"), "nav_bsk": b.get("nav_bsk_m"),
        "ki_r2": law.get("ki_r2"), "p30": h30.get("mae_persist"),
        "netlp": mk.get("netlp_m"),
    }


COLS = [("verdict", "s", ""), ("bv", ".4f", ""), ("K", ".3f", ""),
        ("rail", ".0%", ""), ("peak", "+.1f", "%"), ("auc", ".0f", ""),
        ("recov", "d", ""), ("exc_real", "+.2f", ""), ("whale", "+.2f", ""),
        ("dm_real", "+.2f", ""), ("nav_bsk", ".1f", ""), ("netlp", "+.3f", ""),
        ("ki_r2", ".2f", ""), ("p30", ".4f", ""),
        ("dK_max", ".3f", ""), ("dK_rms", ".3f", "")]


def _fmt(v, spec, none="--"):
    if v is None:
        return none
    try:
        return format(v, spec)
    except (TypeError, ValueError):
        return str(v)


def _cellfmt(key, spec, val, base):
    """value (delta vs base) for numeric columns; the value alone for the
    verdict and for the base row."""
    if key == "verdict" or base is None or val is None or base.get(key) is None:
        return _fmt(val, spec)
    try:
        d = val - base[key]
    except TypeError:
        return _fmt(val, spec)
    dspec = spec if spec[0] in "+" else ("+" + spec if spec[0] in ".d" else spec)
    return f"{_fmt(val, spec)} ({_fmt(d, dspec)})"


def report(spec: dict, specs: list[dict], outdir: Path, resp_days: int,
           band: float) -> dict:
    stats = {}
    for sp in specs:
        p = Path(sp["out"])
        if not cell_complete(p, sp.get("days")):
            stats[sp["label"]] = None
            continue
        st = eqmetrics.summarize(p, resp_days=resp_days, band=band)
        st["name"] = sp["label"]
        stats[sp["label"]] = st

    lines = []
    P = lines.append
    P(f"# star {spec['name']} -- {spec.get('notes', '')}")
    P("")
    P(f"objective: {spec.get('objective', '(none declared)')}")
    P("")
    arms = []
    for sp in specs:
        if sp["arm"] not in arms:
            arms.append(sp["arm"])
    table = {}
    for arm in arms:
        base_sp = next(s for s in specs if s["arm"] == arm and s["level"] == "base")
        base_st = stats.get(base_sp["label"])
        base_m = _metrics(base_st) if base_st else None
        base_k = _k_series(Path(base_sp["out"])) if base_st else {}
        P(f"## arm {arm}")
        P("")
        P("| axis | level | value | " + " | ".join(c for c, _, _ in COLS) + " |")
        P("|---|---|---|" + "---|" * len(COLS))
        rows = []
        for sp in [s for s in specs if s["arm"] == arm]:
            st = stats.get(sp["label"])
            if st is None:
                P(f"| {sp['axis'] or 'base'} | {sp['level']} | | (no vector) |")
                continue
            m = _metrics(st)
            if sp["level"] == "base":
                m["dK_max"] = m["dK_rms"] = 0.0
            else:
                ke = k_equivalence(_k_series(Path(sp["out"])), base_k)
                m["dK_max"] = ke["max"] if ke else None
                m["dK_rms"] = ke["rms"] if ke else None
            ax = next((a for a in spec["axis"] if a["name"] == sp["axis"]), None)
            value = "" if ax is None else _fmt(ax[sp["level"]], "")
            cells_txt = [_cellfmt(c, s, m.get(c),
                                  None if sp["level"] == "base" else base_m)
                         for c, s, _ in COLS]
            P(f"| {sp['axis'] or 'base'} | {sp['level']} | {value} | "
              + " | ".join(cells_txt) + " |")
            rows.append({"label": sp["label"], "axis": sp["axis"],
                         "level": sp["level"], "value": value, **m})
        P("")
        table[arm] = rows
    md = "\n".join(lines)
    (outdir / "summary.md").write_text(md)
    (outdir / "summary.json").write_text(json.dumps(
        {"star": {k: v for k, v in spec.items() if not k.startswith("_")},
         "cells": table}, indent=1))
    print(md)
    print(f"[star] summary -> {outdir / 'summary.md'} / summary.json")
    return table


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alberta_buck.sim.star")
    ap.add_argument("star", help="STAR.toml (or a name under experiments/stars/)")
    ap.add_argument("--outdir", default="build/sim/star")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--axes", default="", help="comma list; default all")
    ap.add_argument("--arms", default="", help="comma list; default all")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--report-only", action="store_true",
                    help="tables from the vectors; pass the same --days the "
                         "cells were run with so truncated cells count")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resp-days", type=int, default=eqmetrics.RESP_DAYS)
    ap.add_argument("--band", type=float, default=eqmetrics.EXC_BAND)
    a = ap.parse_args(argv)

    spec = load_star(a.star)
    outdir = Path(a.outdir)
    if not outdir.is_absolute():
        outdir = REPO / outdir
    outdir = outdir / spec["name"]
    outdir.mkdir(parents=True, exist_ok=True)
    axes = [s for s in a.axes.split(",") if s] or None
    arms = [s for s in a.arms.split(",") if s] or None
    specs = cells(spec, outdir, arms=arms, axes=axes, days=a.days)
    for sp in specs:
        sp["force"] = a.force

    if a.dry_run:
        for sp in specs:
            env = " ".join(f"{k}={v}" for k, v in sp["env"].items())
            print(f"{sp['label']:<28} {env} " + " ".join(sp["sets"]))
        print(f"[star] {len(specs)} cells")
        return 0

    if not a.report_only:
        from alberta_buck.sim.gen_historical import gen
        gen(years=2.0)
        todo = [sp for sp in specs
                if a.force or not cell_complete(Path(sp["out"]), sp.get("days"))]
        print(f"[star] {spec['name']}: {len(specs)} cells, {len(todo)} to run, "
              f"{a.jobs} jobs -> {outdir}", flush=True)
        with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
            for r in pool.map(_run_one, todo):
                print(f"[star] {r}", flush=True)

    report(spec, specs, outdir, a.resp_days, a.band)
    return 0


if __name__ == "__main__":
    sys.exit(main())
