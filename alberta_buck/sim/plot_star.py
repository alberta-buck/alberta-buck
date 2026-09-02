"""Render a star's panes: one figure per PANE, panels = arms (rows) x
axes (columns), each with the baseline and the axis's lo / hi cells
overlaid (WAVE3.org, "The star method"; WP-12).

    python -m alberta_buck.sim.plot_star STAR.toml --pane k
        [--outdir build/sim/star] [--out images/star-<name>-<pane>.png]
        [--pre 30] [--post 150] [--full]

Panes (one time series per vector):
    k        buckK                          bvib     basketValueInBuck
    dm_roi   depositor realized P&L ($M)    nav_bsk  basket NAV in baskets ($M)
    carry    the markout ledger's basket netlp, cumulative ($M)
    whale    raid P&L ($M)                  supply   BUCK supply ($M)
    exc_pnl  defender P&L, marked ($M)

The injection span (if any) is shaded; the x-range is the injection
window +/- pre/post days, or the whole run with --full or for arms
without an injection.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from alberta_buck.sim import eqmetrics
from alberta_buck.sim import star as starmod
from alberta_buck.sim.catalogue import complete

E18 = 10 ** 18
E6 = 10 ** 6
M = 1e6 * E6


def _carry(frames):
    out = []
    for f in frames:
        mx = f.get("mx")
        if not mx:
            out.append(None)
            continue
        pools = mx.get("pool") or []
        hs = mx.get("h") or []
        k = len(hs) - 1
        tot = sum(r[2] + (r[3 + 2 * k] if len(r) > 3 + 2 * k else 0)
                  for r in pools)
        out.append(tot / M)
    return out


PANES = {
    "k": ("buckK", lambda fr: [f.get("buckK", 0) / E18 for f in fr]),
    "bvib": ("basketValueInBuck", lambda fr: [f.get("basketVal", E18) / E18 for f in fr]),
    "dm_roi": ("depositor realized P&L $M", lambda fr: [(f.get("dmProfitUsd") or 0) / M for f in fr]),
    "nav_bsk": ("basket NAV in baskets $M",
                lambda fr: [((f.get("basketNav") or 0) / E6) / ((f.get("basketVal") or E18) / E18) / 1e6 for f in fr]),
    "carry": ("basket netlp (fees + 5d markout) $M", _carry),
    "whale": ("raid P&L $M", lambda fr: [(f.get("raid_pnl") or 0) / M for f in fr]),
    "supply": ("BUCK supply $M", lambda fr: [(f.get("supply") or 0) / M for f in fr]),
    "exc_pnl": ("defender P&L (marked) $M", lambda fr: [(f.get("exc_pnl") or 0) / M for f in fr]),
}

COLORS = {"base": "#222222", "lo": "#1f77b4", "hi": "#d62728"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alberta_buck.sim.plot_star")
    ap.add_argument("star")
    ap.add_argument("--pane", default="k", choices=sorted(PANES))
    ap.add_argument("--outdir", default="build/sim/star")
    ap.add_argument("--out", default="")
    ap.add_argument("--pre", type=int, default=30)
    ap.add_argument("--post", type=int, default=150)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--axes", default="", help="comma list; default all")
    ap.add_argument("--arms", default="", help="comma list; default all")
    a = ap.parse_args(argv)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    spec = starmod.load_star(a.star)
    outdir = Path(a.outdir)
    if not outdir.is_absolute():
        outdir = starmod.REPO / outdir
    outdir = outdir / spec["name"]
    want_arms = {s for s in a.arms.split(",") if s}
    want_axes = {s for s in a.axes.split(",") if s}
    arms = [ar["name"] for ar in spec["arm"]
            if not want_arms or ar["name"] in want_arms]
    axes_ = [ax["name"] for ax in spec["axis"]
             if not want_axes or ax["name"] in want_axes]
    label, extract = PANES[a.pane]

    def load(lbl):
        # Lenient: a partial (truncated or in-flight) vector still plots;
        # the catalogue plotter insists on completeness, the star's panes
        # are for looking at whatever ran.
        p = outdir / f"{lbl}.json"
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text())
        except Exception:
            return None
        return d if d.get("frames") else None

    fig, panels = plt.subplots(len(arms), max(1, len(axes_)),
                               figsize=(4.2 * max(1, len(axes_)), 2.8 * len(arms)),
                               squeeze=False)
    for r, arm in enumerate(arms):
        base = load(f"{arm}-base")
        span = None
        if base is not None:
            ws = eqmetrics.excursion_windows(base)
            inj = [w for w in ws if w.get("src") in ("raid", "iv")]
            if inj:
                span = (inj[0]["day0"], inj[0]["day1"])
        for c, axn in enumerate(axes_):
            ax = panels[r][c]
            axspec = next(x for x in spec["axis"] if x["name"] == axn)
            drawn = 0
            for level in ("lo", "base", "hi"):
                if axspec.get("arms") and arm not in axspec["arms"] \
                        and level != "base":
                    continue
                d = base if level == "base" else load(f"{arm}-{axn}-{level}")
                if d is None:
                    continue
                drawn += 1
                fr = d["frames"]
                if span and not a.full:
                    lo_d, hi_d = span[0] - a.pre, span[1] + a.post
                    fr = [f for f in fr if lo_d <= f.get("day", 0) <= hi_d]
                days = [f["day"] for f in fr]
                ys = extract(fr)
                val = axspec[level]
                ax.plot(days, ys, color=COLORS[level],
                        lw=1.8 if level == "base" else 1.1,
                        label=f"{level} = {val}")
            if span:
                ax.axvspan(span[0], span[1] + 1, color="#999999", alpha=0.25)
            if a.pane in ("bvib",):
                ax.axhline(1.0, color="k", lw=0.6, alpha=0.6)
            ax.grid(True, alpha=0.3)
            if c == 0:
                ax.set_ylabel(f"{arm}\n{label}")
            if r == 0:
                ax.set_title(f"axis: {axn}")
            if r == len(arms) - 1:
                ax.set_xlabel("day")
            if drawn:
                ax.legend(loc="best", fontsize=7)
            else:
                ax.text(0.5, 0.5, "(no vectors)", ha="center", va="center",
                        transform=ax.transAxes, color="#888888")
    fig.suptitle(f"star {spec['name']} -- pane {a.pane}: {label}; "
                 f"shaded = injection", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    out = Path(a.out) if a.out else \
        starmod.REPO / "images" / f"star-{spec['name']}-{a.pane}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110)
    print(f"[plot_star] -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
