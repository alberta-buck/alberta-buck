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
    pair_report(spec, outdir)
    return 0


# ---------------------------------------------------------------------------
# WP-15: multi-level axes, named presets and compound levels (WAVE3.org "The
# controller-alternatives test plan": the design TAG axis of cycle 1), and
# the d7 metric columns of the sensitivity table.
#
# Beyond the WP-12 axis (one `key` or `env`, scalar base / lo / hi -> two
# cells), an axis may carry
#
#   values = [v1, v2, ...]     one cell per level BESIDE the baseline; an
#                              optional `base` is pinned in every other cell
#                              of the arm (a value equal to it makes no cell);
#   preset = "<table>"         with `values`: each v names an entry of
#                              [presets.<table>] whose assignments the cell
#                              receives -- a design tag expanding to several
#                              environment variables (SIM_CONTROLLER,
#                              SIM_SHADOW_MODE, the gains, lambdas, weights);
#   base / lo / hi as tables   a compound two-level axis: several knobs moved
#                              together (the capacity axis: leg_bp,
#                              reserve_frac and the SIM_OPS_* bounds).
#
# An assignment's NAME selects its channel: an upper-case name (SIM_*) is an
# environment variable, a dotted name a --set key.  The WP-12 axes are still
# produced by the WP-12 `cells` (existing specs keep 1 + 2A cells per arm);
# the WP-15 axes pin their base in every cell of the arm and append their
# own cells after it, labelled <arm>-<axis>-<level>, with the level as the
# `value` column.  `main` binds these names at call time, so the CLI, the
# dry run and --report-only use them unchanged.
# ---------------------------------------------------------------------------

_load_star_wp12 = load_star
_cells_wp12 = cells
_report_wp12 = report


def _is_env_name(name: str) -> bool:
    return name.isupper() or name.startswith("SIM_")


def _wp15_axis(ax: dict) -> bool:
    return ("values" in ax or "preset" in ax
            or any(isinstance(ax.get(k), dict) for k in ("base", "lo", "hi")))


def _expand(spec: dict, ax: dict, level) -> dict:
    """The assignments {name: value} one level of a WP-15 axis makes."""
    if "preset" in ax:
        table = (spec.get("presets") or {}).get(ax["preset"])
        if not isinstance(table, dict) or str(level) not in table:
            raise SystemExit(f"axis {ax.get('name')!r}: no preset "
                             f"{ax['preset']}.{level} in [presets]")
        return dict(table[str(level)])
    if isinstance(level, dict):
        return dict(level)
    if "key" in ax:
        return {ax["key"]: level}
    if "env" in ax:
        return {ax["env"]: level}
    raise SystemExit(f"axis {ax.get('name')!r} needs `key`, `env` or `preset`")


def _apply_cell(cell: dict, assign: dict) -> None:
    for name, val in assign.items():
        if _is_env_name(name):
            cell["env"][name] = str(val)
        else:
            cell["sets"] = [s for s in cell["sets"]
                            if s.split("=", 1)[0] != name]
            cell["sets"].append(_fmt_set(name, val))


def load_star(path: str | Path) -> dict:
    """The WP-12 loader, accepting the WP-15 axis forms (validated here;
    the WP-12 axes keep the WP-12 checks)."""
    p = Path(path)
    for cand in (p, p.with_suffix(".toml"), STARS / p.name,
                 STARS / (p.name + ".toml")):
        if cand.exists():
            p = cand
            break
    else:
        raise SystemExit(f"no such star spec: {path} (looked under {STARS})")
    return _finish_spec(tomllib.loads(p.read_text()), p)


def _finish_spec(spec: dict, p: Path) -> dict:
    """Defaults and validation of a star spec read from `p`."""
    spec.setdefault("name", p.stem)
    spec.setdefault("baseline", {})
    spec.setdefault("arm", [])
    spec.setdefault("axis", [])
    spec.setdefault("presets", {})
    spec["_path"] = str(p)
    for ax in spec["axis"]:
        if not _wp15_axis(ax):
            if "key" not in ax and "env" not in ax:
                raise SystemExit(f"axis {ax.get('name')!r} needs `key` or `env`")
            for k in ("base", "lo", "hi"):
                if k not in ax:
                    raise SystemExit(f"axis {ax.get('name')!r} needs `{k}`")
            continue
        if "values" in ax:
            if not isinstance(ax["values"], list) or not ax["values"]:
                raise SystemExit(f"axis {ax.get('name')!r}: `values` must be "
                                 "a non-empty list")
            for v in ax["values"]:
                _expand(spec, ax, v)
        else:
            for k in ("lo", "hi"):
                if not isinstance(ax.get(k), dict):
                    raise SystemExit(f"axis {ax.get('name')!r}: a compound "
                                     f"axis needs `{k}` as a table")
        if "base" in ax:
            _expand(spec, ax, ax["base"])
    return spec


def cells(spec: dict, outdir: Path, arms=None, axes=None, days=None) -> list[dict]:
    """The WP-12 cells (baseline + lo / hi per scalar axis) with every
    WP-15 axis's base pinned, then the WP-15 cells per arm: one per value
    (values axes) or lo / hi (compound axes)."""
    wp12 = {**spec, "axis": [ax for ax in spec["axis"] if not _wp15_axis(ax)]}
    out = _cells_wp12(wp12, outdir, arms=arms, axes=axes, days=days)
    # A [baseline] or arm entry with an environment variable's name (SIM_*)
    # is an environment variable, as it is in a preset: passed to the sim as
    # --set it would land in the experiment's config and be read by nothing.
    for c in out:
        for tok in [t for t in c["sets"] if _is_env_name(t.split("=", 1)[0])]:
            k, _, v = tok.partition("=")
            c["env"].setdefault(k, v)
            c["sets"].remove(tok)
    w15 = [ax for ax in spec["axis"] if _wp15_axis(ax)]
    if not w15:
        return out
    want_axes = set(axes) if axes else None
    by_arm: dict[str, list[dict]] = {}
    for c in out:
        by_arm.setdefault(c["arm"], []).append(c)
    for arm_name, arm_cells in by_arm.items():
        for ax in w15:
            if ax.get("arms") and arm_name not in ax["arms"]:
                continue
            if "base" in ax:
                assign = _expand(spec, ax, ax["base"])
                for c in arm_cells:
                    _apply_cell(c, assign)
    result = []
    for arm in spec["arm"]:
        name = arm["name"]
        if name not in by_arm:
            continue
        base = next(c for c in by_arm[name] if c["level"] == "base")
        result.extend(by_arm[name])
        for ax in w15:
            if want_axes and ax["name"] not in want_axes:
                continue
            if ax.get("arms") and name not in ax["arms"]:
                continue
            if "values" in ax:
                levels = [(str(v), v) for v in ax["values"]
                          if "base" not in ax or v != ax["base"]]
            else:
                levels = [("lo", ax["lo"]), ("hi", ax["hi"])]
            for lvl, val in levels:
                label = f"{name}-{ax['name']}-{lvl}"
                c = {**base, "axis": ax["name"], "level": lvl, "label": label,
                     "sets": list(base["sets"]), "env": dict(base["env"]),
                     "value": lvl,
                     "out": str(outdir / f"{label}.json"),
                     "log": str(outdir / f"{label}.log")}
                _apply_cell(c, _expand(spec, ax, val))
                result.append(c)
    return result


COLS_D7 = [("hab_t", "d", ""), ("hab_sc", "d", ""), ("carry", ".3g", ""),
           ("sat", ".0%", ""), ("stale", ".0%", ""), ("k_tv", ".3f", ""),
           ("k_dmax", ".4f", ""), ("bl_pnl", "+.3f", ""), ("bl_kexc", ".4f", ""),
           ("attr_pos", ".0%", "")]


def _metrics_d7(st: dict) -> dict:
    """The d7 panel's columns (eqmetrics.d7_panel): habituation time and
    sign changes, carry BUCK-days (ut + fac + desk), saturation and stale
    dwell, K's total variation and max |dK|/day, the loader's P&L and K
    excursion vs the twin, the position loop's share of |dK|."""
    p = st.get("d7") or {}
    h = p.get("habituation") or {}
    c = p.get("carry") or {}
    w = p.get("dwell") or {}
    k = p.get("k_economy") or {}
    b = p.get("book_loading") or {}
    a = p.get("attribution") or {}
    return {"hab_t": h.get("t_hab"), "hab_sc": h.get("sign_changes"),
            "carry": c.get("total"), "sat": w.get("sat_frac"),
            "stale": w.get("stale_frac"), "k_tv": k.get("tv"),
            "k_dmax": k.get("dk_max_per_day"), "bl_pnl": b.get("pnl_m"),
            "bl_kexc": b.get("k_exc_max"), "attr_pos": a.get("share_pos")}


def report(spec: dict, specs: list[dict], outdir: Path, resp_days: int,
           band: float) -> dict:
    """The WP-12 sensitivity table with the WP-15 levels' `value` column
    and the d7 columns; the none arm's cell of the same axis / level is the
    book-loading twin of every other arm's cell."""
    labels = {sp["label"]: sp for sp in specs}
    stats = {}
    for sp in specs:
        p = Path(sp["out"])
        if not cell_complete(p, sp.get("days")):
            stats[sp["label"]] = None
            continue
        twin = None
        if sp["arm"] != "none" and sp["label"].startswith(sp["arm"] + "-"):
            tl = "none-" + sp["label"][len(sp["arm"]) + 1:]
            if tl in labels:
                twin = labels[tl]["out"]
        st = eqmetrics.summarize(p, resp_days=resp_days, band=band, twin=twin)
        st["name"] = sp["label"]
        stats[sp["label"]] = st

    cols = COLS + COLS_D7
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
        base_m = {**_metrics(base_st), **_metrics_d7(base_st)} if base_st else None
        base_k = _k_series(Path(base_sp["out"])) if base_st else {}
        P(f"## arm {arm}")
        P("")
        P("| axis | level | value | " + " | ".join(c for c, _, _ in cols) + " |")
        P("|---|---|---|" + "---|" * len(cols))
        rows = []
        for sp in [s for s in specs if s["arm"] == arm]:
            st = stats.get(sp["label"])
            if st is None:
                P(f"| {sp['axis'] or 'base'} | {sp['level']} | | (no vector) |")
                continue
            m = {**_metrics(st), **_metrics_d7(st)}
            if sp["level"] == "base":
                m["dK_max"] = m["dK_rms"] = 0.0
            else:
                ke = k_equivalence(_k_series(Path(sp["out"])), base_k)
                m["dK_max"] = ke["max"] if ke else None
                m["dK_rms"] = ke["rms"] if ke else None
            if "value" in sp:
                value = str(sp["value"])
            else:
                ax = next((a for a in spec["axis"] if a["name"] == sp["axis"]), None)
                value = "" if ax is None else _fmt(ax.get(sp["level"], ""), "")
            cells_txt = [_cellfmt(c, s, m.get(c),
                                  None if sp["level"] == "base" else base_m)
                         for c, s, _ in cols]
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
# Twins (2026-09-29): a star that EXTENDS another -- the same arms, cast and
# seeds on a different design -- and the PAIRING report that sets each of its
# cells beside the parent's cell of the same label.  Written for the equity
# basket's twins of the E-series (doc/CONVERGENCE.org), whose parents run the
# pro-rata ops basket.
#
#   extends = "conv-e2"    the parent star.  The twin's [baseline] and its
#                          preset tables override the parent's key by key
#                          (a preset level the twin names replaces the
#                          parent's level); a twin [[axis]] replaces the
#                          parent's axis of the same name (others are
#                          appended); `drop_axes` removes axes; a twin
#                          [[arm]] list replaces the parent's arms.
#   pair = "conv-e2"       the star it is compared with (default: the one it
#                          extends).  After the twin's own summary, pair.md
#                          beside it: per arm and cell label, the twin's
#                          value of each metric and its delta against the
#                          paired star's cell (its summary.json under the
#                          same --outdir).
# ---------------------------------------------------------------------------

_load_star_wp15 = load_star


def _resolve_star(path) -> Path:
    p = Path(path)
    for cand in (p, p.with_suffix(".toml"), STARS / p.name,
                 STARS / (p.name + ".toml")):
        if cand.exists():
            return cand
    raise SystemExit(f"no such star spec: {path} (looked under {STARS})")


def _merge_star(parent: dict, child: dict) -> dict:
    """The twin's spec: the parent's, with the child's overrides."""
    s = {k: v for k, v in parent.items()
         if not k.startswith("_") and k not in ("name", "notes", "objective", "pair")}
    for k, v in child.items():
        if k in ("extends", "drop_axes"):
            continue
        if k == "baseline":
            s[k] = {**parent.get(k, {}), **v}
        elif k == "presets":
            pr = {t: dict(tbl) for t, tbl in (parent.get("presets") or {}).items()}
            for t, tbl in v.items():
                pr[t] = {**pr.get(t, {}), **tbl}
            s[k] = pr
        elif k == "axis":
            axes = [dict(a) for a in parent.get("axis", [])]
            for a in v:
                i = next((j for j, x in enumerate(axes) if x.get("name") == a.get("name")), None)
                if i is None:
                    axes.append(a)
                else:
                    axes[i] = a
            s[k] = axes
        else:
            s[k] = v
    drop = set(child.get("drop_axes") or [])
    s["axis"] = [a for a in s.get("axis", []) if a.get("name") not in drop]
    s.setdefault("pair", child["extends"])
    return s


def load_star(path: str | Path) -> dict:
    """The WP-15 loader, and a star that extends another."""
    p = _resolve_star(path)
    raw = tomllib.loads(p.read_text())
    if "extends" not in raw:
        return _load_star_wp15(p)
    return _finish_spec(_merge_star(load_star(raw["extends"]), raw), p)


PAIR_COLS = [("bv", ".4f"), ("K", ".3f"), ("rail", ".0%"), ("peak", "+.1f"),
             ("auc", ".0f"), ("recov", "d"), ("exc_real", "+.2f"), ("whale", "+.2f"),
             ("dm_real", "+.2f"), ("nav_bsk", ".1f"), ("netlp", "+.3f"),
             ("carry", ".3g"), ("k_tv", ".3f")]


def pair_report(spec: dict, outdir: Path) -> dict | None:
    """The twin's cells beside the paired star's: per arm and label, the
    verdicts and each metric (the twin's value, its delta against the pair).
    Needs both summaries (run the paired star first, or --report-only)."""
    other = spec.get("pair")
    if not other:
        return None
    mine_p, theirs_p = outdir / "summary.json", outdir.parent / other / "summary.json"
    if not (mine_p.exists() and theirs_p.exists()):
        print(f"[star] pair: waiting on {theirs_p if mine_p.exists() else mine_p}")
        return None
    mine = json.loads(mine_p.read_text())["cells"]
    theirs = json.loads(theirs_p.read_text())["cells"]
    lines = [f"# pair {spec['name']} against {other}", "",
             f"Each cell: {spec['name']}'s value ({spec['name']} - {other}, the cell of "
             "the same label); the verdicts side by side.", ""]
    table = {}
    for arm, rows in mine.items():
        base = {r["label"]: r for r in theirs.get(arm, [])}
        lines += [f"## arm {arm}", "",
                  "| cell | verdict | " + " | ".join(c for c, _ in PAIR_COLS) + " |",
                  "|---|---|" + "---|" * len(PAIR_COLS)]
        out = []
        for r in rows:
            b = base.get(r["label"])
            verdict = f"{r.get('verdict')} / {b.get('verdict') if b else '--'}"
            txt = [_cellfmt(c, s, r.get(c), b) for c, s in PAIR_COLS]
            lines.append(f"| {r['label']} | {verdict} | " + " | ".join(txt) + " |")
            out.append({"label": r["label"], "twin": r, "pair": b})
        lines.append("")
        table[arm] = out
    md = "\n".join(lines)
    (outdir / "pair.md").write_text(md)
    (outdir / "pair.json").write_text(json.dumps({"star": spec["name"], "pair": other,
                                                  "cells": table}, indent=1))
    print(md)
    print(f"[star] pair -> {outdir / 'pair.md'} / pair.json")
    return table


if __name__ == "__main__":
    sys.exit(main())
