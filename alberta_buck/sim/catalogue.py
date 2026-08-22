"""The excursion catalogue grid: injected excursions x defender mixes x
intensity -> excursion-response table.

    python -m alberta_buck.sim.catalogue
        [--arms none,dump,squeeze,grind-down,grind-up,spike,step]
        [--mixes none,usdc,buck,credit,basket,crb,all]
        [--scale 1[,2,0.5]]        # defender-count multiplier(s)
        [--jobs 10] [--outdir build/sim/catalogue]
        [--days N]                 # truncate every arm (smoke)
        [--set k=v ...]            # extra override for EVERY run
        [--force]                  # re-run cells whose vector exists
        [--report-only]            # rebuild the tables from the vectors
        [--dry-run]                # print the cells, run nothing

Each ARM is one experiments/catalogue-<arm>.toml: the portcast cast
(settled physics, keyed rng, no FatCredit) on a 2-year window with ONE
injected excursion at day 365 -- a whale dump/squeeze (short) or grind
(long) on the common-mode axis, a transient or permanent constituent
shock on the differential axis, or nothing (the endogenous control).

Each MIX is a defender population laid over that cast: which quadrants
of the quadrant-balance model are staffed, by which ExcursionArbAgent
neutral base (usdc = Q1 absorb only, buck = Q3 supply only, credit = Q4
issue/Q2 retire, basket = the two-sided rotation), plus the
differential-mode CommodityRebalArbAgent.  `--scale` multiplies every
defender count (intensity).  Because every defender is threshold-gated
and the portcast base has no cold-start transient, cells of one arm
share an identical pre-history up to the injection: the response
deltas between mixes are attributable to the mix.

Each cell is a `python -m alberta_buck.sim --experiment ... --backend
pyrevm` subprocess (in-process EVM, one core each).  Vectors + logs land
in --outdir; an existing vector is reused unless --force (so a killed
grid resumes).  When all cells finish: the eqmetrics verdict table, the
excursion-response table, and the arm x mix grid (peak deviation,
recovery, defender P&L) are printed and written to summary.json /
summary.md in --outdir.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alberta_buck.sim import eqmetrics

REPO = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO / "alberta_buck" / "sim" / "experiments"

ARMS = ["none", "dump", "squeeze", "grind-down", "grind-up", "spike", "step"]

# Every defender class the mixes govern: a mix sets ALL of them (to 0 when
# absent) so the TOML's own excursion entries never leak into a cell.
DEFENDERS = ["ExcursionArbAgent", "ExcursionCreditArbAgent",
             "ExcursionBasketArbAgent", "ExcursionBuckArbAgent",
             "CommodityRebalArbAgent"]

MIXES = {
    "none":   {},
    "usdc":   {"ExcursionArbAgent": 8},            # Q1 absorb (+Q3 at par)
    "buck":   {"ExcursionBuckArbAgent": 8},        # Q3 supply (+Q1 at par)
    "credit": {"ExcursionCreditArbAgent": 8},      # Q4 issue / Q2 retire
    "basket": {"ExcursionBasketArbAgent": 8},      # two-sided rotation
    "crb":    {"CommodityRebalArbAgent": 4},       # differential mode only
    "all":    {"ExcursionArbAgent": 5, "ExcursionCreditArbAgent": 3,
               "ExcursionBasketArbAgent": 4, "ExcursionBuckArbAgent": 2,
               "CommodityRebalArbAgent": 2},
}


def _scaled(n: int, scale: float) -> int:
    if n <= 0:
        return 0
    return max(1, int(round(n * scale)))


def cells(arms, mixes, scales, sets, days, outdir: Path) -> list[dict]:
    out = []
    for arm in arms:
        toml = EXPERIMENTS / f"catalogue-{arm}.toml"
        if not toml.exists():
            raise SystemExit(f"no such arm: {toml}")
        for mix in mixes:
            if mix not in MIXES:
                raise SystemExit(f"unknown mix {mix!r}; known: "
                                 f"{','.join(MIXES)}")
            for scale in scales:
                label = f"{arm}-{mix}" + (f"-x{scale:g}" if scale != 1 else "")
                cell_sets = [f"scenario.agents.{c}="
                             f"{_scaled(MIXES[mix].get(c, 0), scale)}"
                             for c in DEFENDERS] + list(sets)
                out.append({
                    "arm": arm, "mix": mix, "scale": scale, "label": label,
                    "toml": str(toml), "sets": cell_sets, "days": days,
                    "out": str(outdir / f"cat-{label}.json"),
                    "log": str(outdir / f"cat-{label}.log"),
                })
    return out


def complete(path: Path) -> bool:
    """True when `path` is a FINISHED vector: the sim writes checkpoint
    vectors mid-run, so existence is not completion.  Finished == the last
    frame's day reaches the experiment's horizon (years * 365, less a day)
    or, for truncated smokes (--days), at least 95% of the frames' span."""
    try:
        d = json.loads(path.read_text())
        fr = d.get("frames") or []
        if not fr:
            return False
        sc = (d.get("meta", {}).get("experiment", {}) or {}).get("scenario", {})
        horizon = float(sc.get("years") or 0) * 365
        last = int(fr[-1].get("day", 0))
        days = d.get("meta", {}).get("days")
        if days:
            return last >= int(days) - 1
        return horizon > 0 and last >= int(horizon) - 2
    except Exception:
        return False


def _run_one(spec: dict) -> dict:
    out = Path(spec["out"])
    if out.exists() and not spec.get("force") and complete(out):
        return {"label": spec["label"], "skipped": True}
    cmd = [sys.executable, "-m", "alberta_buck.sim",
           "--experiment", spec["toml"], "--backend", "pyrevm",
           "--director", "pairs", "--out", spec["out"]]
    if spec.get("days"):
        cmd += ["--days", str(spec["days"])]
    for kv in spec["sets"]:
        cmd += ["--set", kv]
    with Path(spec["log"]).open("w") as lf:
        rc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT,
                            cwd=REPO).returncode
    return {"label": spec["label"], "exit": rc, "ok": out.exists()}


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _fmt(x, spec: str, none: str = "--") -> str:
    if x is None:
        return none
    try:
        return format(x, spec)
    except (TypeError, ValueError):
        return str(x)


def report(specs: list[dict], outdir: Path, resp_days: int, band: float):
    stats = []
    for sp in specs:
        p = Path(sp["out"])
        if not p.exists():
            stats.append({**{k: sp[k] for k in ("arm", "mix", "scale",
                                                 "label")},
                          "error": "no vector", "frames": 0})
            continue
        st = eqmetrics.summarize(p, resp_days=resp_days, band=band)
        st.update({k: sp[k] for k in ("arm", "mix", "scale", "label")})
        st["name"] = sp["label"]
        stats.append(st)

    lines = []
    P = lines.append
    P("## eqmetrics verdicts (2-year tail gate)")
    P("")
    P("```")
    P(f"{'name':<18} {'seed':<7} {'days':>5} {'bv~':>8} {'bv sd':>7} "
      f"{'K~':>7} {'rail':>5} {'iss$M':>8} {'ret$M':>9} {'thr':>6}  verdict")
    for st in stats:
        if st.get("error"):
            P(f"{st['label']:<18} ERROR: {st['error']}")
        else:
            P(eqmetrics.row(st))
    P("```")
    P("")
    P(f"## excursion response (resp {resp_days}d, band {band:.0%})")
    P("")
    P("```")
    P(eqmetrics.exc_header())
    for st in stats:
        for x in st.get("excursions", []) or []:
            P(eqmetrics.exc_row(st, x))
    P("```")
    P("")

    # -- the grid: arms x mixes -------------------------------------- #
    arms = []
    mixes = []
    scales = sorted({st["scale"] for st in stats})
    for st in stats:
        if st["arm"] not in arms:
            arms.append(st["arm"])
        if st["mix"] not in mixes:
            mixes.append(st["mix"])
    by = {(st["arm"], st["mix"], st["scale"]): st for st in stats}

    def cell(st) -> str:
        if st is None or st.get("error"):
            return "--"
        xs = st.get("excursions") or []
        inj = [x for x in xs if x.get("src") in ("raid", "iv")]
        if not inj:
            # control arm: tail stability + K
            return f"sd {st['bv_tail_std']:.4f} K {st['k_tail_mean']:.3f}"
        x = inj[0]
        rec = x.get("recovery_days")
        return (f"{_fmt(x.get('peak_dev') * 100 if x.get('peak_dev') is not None else None, '+.1f')}%"
                f" r{('never' if rec is None else int(rec))}"
                f" auc{_fmt(x.get('auc_pct_days'), '.0f')}"
                f" real{_fmt(x.get('d_exc_real_m'), '+.2f')}"
                f" mark{_fmt(x.get('d_exc_pnl_m'), '+.2f')}"
                f" raid{_fmt(x.get('d_raid_pnl_m'), '+.2f')}")

    for scale in scales:
        P(f"## grid: peak bv dev % / recovery d / auc %-days / defender "
          f"realized $M / defender marked $M / raid P&L $M  (scale x{scale:g})")
        P("")
        P("| arm \\ mix | " + " | ".join(mixes) + " |")
        P("|---|" + "---|" * len(mixes))
        for arm in arms:
            P(f"| {arm} | " + " | ".join(
                cell(by.get((arm, m, scale))) for m in mixes) + " |")
        P("")

    md = "\n".join(lines)
    (outdir / "summary.md").write_text(md)
    (outdir / "summary.json").write_text(json.dumps(stats, indent=1))
    print(md)
    print(f"[catalogue] summary -> {outdir / 'summary.md'} / summary.json")
    return stats


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alberta_buck.sim.catalogue")
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--mixes", default="none,usdc,buck,credit,basket,all")
    ap.add_argument("--scale", default="1")
    ap.add_argument("--jobs", type=int, default=10)
    ap.add_argument("--outdir", default="build/sim/catalogue")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--set", action="append", default=[], dest="sets",
                    metavar="KEY=VAL")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resp-days", type=int, default=eqmetrics.RESP_DAYS)
    ap.add_argument("--band", type=float, default=eqmetrics.EXC_BAND)
    a = ap.parse_args(argv)

    outdir = Path(a.outdir)
    if not outdir.is_absolute():
        outdir = REPO / outdir
    outdir.mkdir(parents=True, exist_ok=True)
    arms = [s for s in a.arms.split(",") if s]
    mixes = [s for s in a.mixes.split(",") if s]
    scales = [float(s) for s in a.scale.split(",") if s]
    specs = cells(arms, mixes, scales, a.sets, a.days, outdir)
    for sp in specs:
        sp["force"] = a.force

    if a.dry_run:
        for sp in specs:
            print(sp["label"], " ".join(sp["sets"]))
        return 0

    if not a.report_only:
        # Pre-generate the shared price CSVs once (children hit the cache).
        from alberta_buck.sim.gen_historical import gen
        gen(years=2.0)
        todo = [sp for sp in specs
                if a.force or not complete(Path(sp["out"]))]
        print(f"[catalogue] {len(specs)} cells, {len(todo)} to run, "
              f"{a.jobs} jobs -> {outdir}", flush=True)
        with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
            for r in pool.map(_run_one, todo):
                print(f"[catalogue] {r}", flush=True)

    report(specs, outdir, a.resp_days, a.band)
    return 0


if __name__ == "__main__":
    sys.exit(main())
