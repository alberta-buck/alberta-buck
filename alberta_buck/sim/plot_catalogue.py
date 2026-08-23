"""Render the excursion catalogue grid: one row per arm, the response of
every defender mix overlaid around the injection.

    python -m alberta_buck.sim.plot_catalogue [--outdir build/sim/catalogue]
        [--out images/catalogue-wave1.png] [--arms a,b,..] [--mixes m,n,..]
        [--pre 20] [--post 120] [--scale 1]

Columns: basketValueInBuck (the controller's process variable; 1.0 =
parity, >1 BUCK cheap), BUCK/USDC, and the axis-2 weight deviation
sum_i |actual_i - target_i| from poolWeights.  The injection span is
shaded.  Reads cat-<arm>-<mix>[-x<scale>].json vectors written by
alberta_buck.sim.catalogue.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from alberta_buck.sim import catalogue as cat
from alberta_buck.sim import eqmetrics

E18 = 10 ** 18
E6 = 10 ** 6

COLORS = {"none": "#444444", "usdc": "#1f77b4", "buck": "#d62728",
          "credit": "#9467bd", "basket": "#2ca02c", "crb": "#8c564b",
          "all": "#ff7f0e"}


def _series(frames, key):
    out = []
    for f in frames:
        v = f.get(key)
        out.append(v)
    return out


def _wdev(frames):
    out = []
    for f in frames:
        pw = f.get("poolWeights") or []
        try:
            out.append(sum(abs(a - t) for a, t in pw))
        except Exception:
            out.append(None)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alberta_buck.sim.plot_catalogue")
    ap.add_argument("--outdir", default="build/sim/catalogue")
    ap.add_argument("--out", default="images/catalogue-wave1.png")
    ap.add_argument("--arms", default=",".join(cat.ARMS))
    ap.add_argument("--mixes", default="none,usdc,buck,credit,basket,crb,all")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--pre", type=int, default=20)
    ap.add_argument("--post", type=int, default=120)
    a = ap.parse_args(argv)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    outdir = Path(a.outdir)
    if not outdir.is_absolute():
        outdir = cat.REPO / outdir
    arms = [s for s in a.arms.split(",") if s]
    mixes = [s for s in a.mixes.split(",") if s]
    suffix = "" if a.scale == 1 else f"-x{a.scale:g}"

    fig, axes = plt.subplots(len(arms), 3, figsize=(16, 2.6 * len(arms)),
                             squeeze=False, sharex=False)
    for r, arm in enumerate(arms):
        ax_bv, ax_bu, ax_w = axes[r]
        span = None
        for mix in mixes:
            p = outdir / f"cat-{arm}-{mix}{suffix}.json"
            if not p.exists() or not cat.complete(p):
                continue
            d = json.loads(p.read_text())
            fr = d["frames"]
            if span is None:
                ws = eqmetrics.excursion_windows(d)
                inj = [w for w in ws if w.get("src") in ("raid", "iv")]
                if inj:
                    span = (inj[0]["day0"], inj[0]["day1"])
                else:
                    span = (365, 365)
            lo, hi = span[0] - a.pre, span[1] + a.post
            sel = [f for f in fr if lo <= f.get("day", 0) <= hi]
            days = [f["day"] for f in sel]
            c = COLORS.get(mix, None)
            lw = 1.8 if mix in ("none", "all") else 1.1
            ax_bv.plot(days, [f["basketVal"] / E18 for f in sel], color=c,
                       lw=lw, label=mix)
            ax_bu.plot(days, [(f.get("buckUsd") or 0) / E6 for f in sel],
                       color=c, lw=lw)
            ax_w.plot(days, _wdev(sel), color=c, lw=lw)
        for ax in (ax_bv, ax_bu, ax_w):
            if span:
                ax.axvspan(span[0], span[1] + 1, color="#999999", alpha=0.25)
            ax.grid(True, alpha=0.3)
        ax_bv.axhline(1.0, color="k", lw=0.6, alpha=0.6)
        ax_bv.set_ylabel(f"{arm}\nbasketValueInBuck")
        ax_bu.set_ylabel("BUCK/USDC")
        ax_w.set_ylabel("sum |w - w*|")
        if r == 0:
            ax_bv.set_title("common mode (bvib)")
            ax_bu.set_title("BUCK/USDC")
            ax_w.set_title("axis 2: basket weight deviation")
            ax_bv.legend(loc="upper right", fontsize=8, ncol=4)
        if r == len(arms) - 1:
            for ax in (ax_bv, ax_bu, ax_w):
                ax.set_xlabel("day")
    fig.suptitle(f"Excursion catalogue -- response by defender mix "
                 f"(scale x{a.scale:g}); shaded = injection", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.98))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110)
    print(f"[plot_catalogue] -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
