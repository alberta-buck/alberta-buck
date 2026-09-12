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
# WP-8: the treasury seeder is governed by the mixes too -- count 0 in every
# existing mix, so no banked cell changes; enable it per cell with
# CAT_SET="scenario.agents.SeederAgent=1".
DEFENDERS.append("SeederAgent")

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

# WP-2: the undertakings desk (alberta_buck/sim/undertaking_agents.py) is
# a defender the mixes govern; `ut` is the desk alone, `ut-all` the full
# cast plus the desk.  NB --scale multiplies the desk count like any other
# defender (one book of reserve_frac x NAV per desk).
DEFENDERS.append("UndertakingAgent")
MIXES["ut"] = {"UndertakingAgent": 1}                # the undertakings desk
MIXES["ut-all"] = {**MIXES["all"], "UndertakingAgent": 1}
# -- WP-6: the latent-credit facility (facility_agent.py) ----------------- #
# Governed like every defender (set to 0 in every mix that omits it), so a
# TOML's own FacilityAgent count never leaks into a cell.
DEFENDERS.append("FacilityAgent")
MIXES.update({
    "fac":     {"FacilityAgent": 32},                     # D6 issue / retire
    "fac-all": {**MIXES["all"], "FacilityAgent": 32},     # the all mix + it
})
# -- WP-15: the controller-alternatives grid (WAVE3.org test plan) -------- #
# The disruption classes 5-10 as arms (experiments/catalogue-<arm>.toml);
# ARMS (the default grid) is unchanged.  Their injectors -- the periodic
# whale (period_days), PusherAgent (class 7), LpExitAgent (class 10) and
# BookLoaderAgent (class 9) -- are ARM agents like WhaleRaidAgent: the arm's
# TOML sets their count and the mixes never touch them (a mix zeroes
# DEFENDERS, and an attacker is not a defender), so `cat-bookload-none` is
# the loader against an undefended world and `cat-bookload-ut` the loader
# against the ladder.  INJECTORS names them for readers of a cell.
ARMS_D7 = ["cohort-step", "cohort-add", "ramp", "lpexit", "periodic",
           "guardtrip", "bookload"]
INJECTORS = ["WhaleRaidAgent", "PusherAgent", "LpExitAgent", "BookLoaderAgent"]


def _scaled(n: int, scale: float) -> int:
    if n <= 0:
        return 0
    return max(1, int(round(n * scale)))


def cells(arms, mixes, scales, sets, days, outdir: Path,
          tag: str = "") -> list[dict]:
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
                label = (f"{arm}-{mix}" + (f"-x{scale:g}" if scale != 1 else "")
                         + (f"-{tag}" if tag else ""))
                cell_sets = [f"scenario.agents.{c}="
                             f"{_scaled(MIXES[mix].get(c, 0), scale)}"
                             for c in DEFENDERS] + list(sets)
                out.append({
                    "arm": arm, "mix": mix, "scale": scale, "label": label,
                    "tag": tag,
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


def report(specs: list[dict], outdir: Path, resp_days: int, band: float,
           summary: str = "summary"):
    stats = []
    for sp in specs:
        p = Path(sp["out"])
        if not p.exists():
            stats.append({**{k: sp[k] for k in ("arm", "mix", "scale",
                                                 "label", "tag")},
                          "error": "no vector", "frames": 0})
            continue
        if not complete(p):
            try:
                last = json.loads(p.read_text())["frames"][-1].get("day")
            except Exception:
                last = "?"
            stats.append({**{k: sp[k] for k in ("arm", "mix", "scale",
                                                 "label", "tag")},
                          "error": f"partial (day {last})", "frames": 0})
            continue
        st = eqmetrics.summarize(p, resp_days=resp_days, band=band)
        st.update({k: sp[k] for k in ("arm", "mix", "scale", "label", "tag")})
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
    for st in stats:
        if st.get("tag"):
            st["arm"] = f"{st['arm']}-{st['tag']}"
    arms = []
    for st in stats:
        if st["arm"] not in arms:
            arms.append(st["arm"])
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
        wrec = x.get("wdev_recovery_days")
        s = (f"{_fmt(x.get('peak_dev') * 100 if x.get('peak_dev') is not None else None, '+.1f')}%"
             f" r{('never' if rec is None else int(rec))}"
             f" auc{_fmt(x.get('auc_pct_days'), '.0f')}")
        if x.get("src") == "iv":
            s += (f" wdev{_fmt(x.get('wdev_peak'), '+.3f')}"
                  f" wr{('never' if wrec is None else int(wrec))}")
        s += (f" real{_fmt(x.get('d_exc_real_m'), '+.2f')}"
              f" mark{_fmt(x.get('d_exc_pnl_m'), '+.2f')}")
        if x.get("src") == "raid":
            s += f" raid{_fmt(x.get('d_raid_pnl_m'), '+.2f')}"
        if (x.get("d_crb_pnl_m") or 0) != 0 or (x.get("crb_trades") or 0):
            s += f" crb{_fmt(x.get('d_crb_pnl_m'), '+.2f')}"
        return s

    for scale in scales:
        P(f"## grid: peak bv dev % / recovery d / auc %-days [/ axis-2 wdev "
          f"peak, recovery] / defender realized $M / marked $M [/ raid P&L "
          f"$M] [/ crb P&L $M]  (scale x{scale:g})")
        P("")
        P("| arm \\ mix | " + " | ".join(mixes) + " |")
        P("|---|" + "---|" * len(mixes))
        for arm in arms:
            P(f"| {arm} | " + " | ".join(
                cell(by.get((arm, m, scale))) for m in mixes) + " |")
        P("")

    # -- WP-1: the basket's own side, vs the undefended cell ------------ #
    # Deltas are reported only against the `none` mix of the same arm /
    # scale (same tag), and flagged [uncontrolled] when the pre-injection
    # basket NAV differs (a mix whose agents trade before the injection --
    # e.g. the crb rebalancers -- has its own history).
    def bs_cell(st, ref) -> str:
        if st is None or st.get("error"):
            return "--"
        b = st.get("basket") or {}
        e = b.get("end") or {}
        if not e:
            return "--"
        s = (f"navU {_fmt(e.get('nav_usd_m'), '.1f')} "
             f"navK {_fmt(e.get('nav_bsk_m'), '.1f')} "
             f"tr {_fmt(e.get('treasury_k'), '.0f')}k "
             f"dmR {_fmt(e.get('dm_real_m'), '+.2f')}")
        if ref is not None and ref is not st and not ref.get("error"):
            rb = ref.get("basket") or {}
            re_, rp, p = rb.get("end") or {}, rb.get("pre") or {}, b.get("pre") or {}
            if re_:
                same = abs((p.get("nav_b_m") or 0) - (rp.get("nav_b_m") or 0)) \
                    <= 1e-9 * max(1.0, abs(rp.get("nav_b_m") or 1.0))

                def dd(key, spec):
                    a, r = e.get(key), re_.get(key)
                    return _fmt(None if a is None or r is None else a - r, spec)

                s += (f" ; d navU {dd('nav_usd_m', '+.2f')} "
                      f"navK {dd('nav_bsk_m', '+.2f')} "
                      f"tr {dd('treasury_k', '+.0f')}k "
                      f"dmR {dd('dm_real_m', '+.2f')}"
                      + ("" if same else " [uncontrolled]"))
        return s

    def mx_cell(st) -> str:
        if st is None or st.get("error"):
            return "--"
        m = st.get("markout")
        if not m:
            return "(no ledger)"
        b, w = m["basket"], m["whale"]
        return (f"fees {b['fees_m']:+.3f} adv {b['adv_m']:+.3f} "
                f"cm {b['adv_cm_m']:+.3f} carry "
                f"{_fmt(b['worst_carry'], '.2f', none='>1')} "
                f"; whale adv {w['adv_basket_m']:+.3f} ub {w['adv_ub_m']:+.3f}")

    def kfc_cell(st) -> str:
        if st is None or st.get("error"):
            return "--"
        kfc = st.get("kfc") or {}
        law = kfc.get("law") or {}
        k = kfc.get("h") or {}

        def g(h, key, spec):
            v = (k.get(h) or k.get(str(h)) or {}).get(key)
            return _fmt(v, spec)

        return (f"law: Kp {_fmt(law.get('kp_fit'), '.3f')} "
                f"(R2 {_fmt(law.get('kp_r2'), '.2f')}) "
                f"Ki/d {_fmt(law.get('ki_fit_per_day'), '.4f')} "
                f"(R2 {_fmt(law.get('ki_r2'), '.2f')}) "
                f"post-hit30 {_fmt(law.get('post_hit_30'), '.0%')} "
                f"; calm: p30 {g(30, 'mae_persist', '.4f')} "
                f"in1 {g(30, 'within_1pt_persist', '.0%')} "
                f"; ex-ante obs30 {g(30, 'mae_observer', '.4f')} "
                f"naive30 {g(30, 'mae_naive', '.4f')}")

    for title, fn in (
            ("basket side at end: NAV $M USD / $M baskets / treasury k / "
             "depositor realized $M; | delta vs the undefended (none) cell",
             lambda st, arm, scale: bs_cell(st, by.get((arm, "none", scale)))),
            ("markout ledger (injection window; whole run for controls): "
             "basket fees / adverse total / adverse common-mode $M, worst "
             "carry ratio | whale adverse in basket pools / in BUCK-USDC",
             lambda st, arm, scale: mx_cell(st)),
            ("K comprehensibility (WAVE3 R14 / G8): the law fitted -- "
             "dK ~ Kp d(1-bvib) day to day, dK ~ Ki mean(1-bvib) 30 over a "
             "month -- with R2 and the ex-post 30d direction hit; calm "
             "(persistence MAE, within 1pt); ex-ante forecast MAE",
             lambda st, arm, scale: kfc_cell(st))):
        for scale in scales:
            P(f"## {title}  (scale x{scale:g})")
            P("")
            P("| arm \\ mix | " + " | ".join(mixes) + " |")
            P("|---|" + "---|" * len(mixes))
            for arm in arms:
                P(f"| {arm} | " + " | ".join(
                    fn(by.get((arm, m, scale)), arm, scale) for m in mixes)
                  + " |")
            P("")

    md = "\n".join(lines)
    (outdir / f"{summary}.md").write_text(md)
    (outdir / f"{summary}.json").write_text(json.dumps(stats, indent=1))
    print(md)
    print(f"[catalogue] summary -> {outdir / (summary + '.md')} / "
          f"{summary}.json")
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
    ap.add_argument("--tag", default="", help="label suffix for a variant "
                    "run sharing --outdir (e.g. inj2 with --set overrides)")
    ap.add_argument("--summary", default="summary",
                    help="basename of the report files written to --outdir")
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
    specs = cells(arms, mixes, scales, a.sets, a.days, outdir, a.tag)
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

    report(specs, outdir, a.resp_days, a.band, a.summary)
    return 0


# ---------------------------------------------------------------------------
# WP-15: the "d7" panel of the summary (eqmetrics' WP-15 block) -- one row
# per cell: habituation of s after the last disturbance, carry BUCK-days,
# saturation / stale dwell, K economy, book-loading (K's excursion against
# the none-mix cell of the same arm and scale, the undefended twin) and the
# loop attribution.  Appended to summary.md / summary.json after the WP-1
# tables; `main` binds `report` at call time.
# ---------------------------------------------------------------------------

_report_wp1 = report


def report(specs: list[dict], outdir: Path, resp_days: int, band: float,
           summary: str = "summary"):
    stats = _report_wp1(specs, outdir, resp_days, band, summary)
    by = {(st.get("arm"), st.get("mix"), st.get("scale")): st for st in stats}
    lines = ["", "## d7 panel (WP-15): habituation [t_hab d, residual/peak, "
             "sign changes, P9] carry [BUCK-days] dwell [saturated, stale] "
             "K [total variation, max |dK|/day, rail days] book-loading "
             "[P&L $M, loaded fraction, max |K - K_none| over the hold, "
             "per unit fraction] attribution [position loop's share of |dK|, "
             "stale / excluded frames]", "", "```"]
    for st in stats:
        if st.get("error") or not st.get("d7"):
            continue
        bl = (st["d7"] or {}).get("book_loading")
        twin = by.get((st.get("arm"), "none", st.get("scale")))
        if bl and twin is not None and twin is not st and not twin.get("error"):
            try:
                a = json.loads(Path(st["path"]).read_text()).get("frames") or []
                b = json.loads(Path(twin["path"]).read_text()).get("frames") or []
                st["d7"]["book_loading"] = eqmetrics.book_loading(a, b)
            except Exception:
                pass
        lines.append(eqmetrics.d7_row(st))
    lines.append("```")
    md = "\n".join(lines)
    with (outdir / f"{summary}.md").open("a") as fh:
        fh.write(md + "\n")
    (outdir / f"{summary}.json").write_text(json.dumps(stats, indent=1))
    print(md)
    return stats


if __name__ == "__main__":
    sys.exit(main())
