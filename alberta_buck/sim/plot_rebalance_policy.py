"""Render the rebalance-policy sim result to images/rebalance-policy.png."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DATA = REPO / "test" / "vectors" / "rebalance-policy.json"
OUT = REPO / "images" / "rebalance-policy.png"

POLICY_STYLE = {
    "hold": dict(color="black", linestyle="--", linewidth=1.2),
    "prop": dict(color="tab:orange", linewidth=1.4),
    "band": dict(color="tab:purple", linewidth=1.2),
    "factor": dict(color="tab:blue", linewidth=1.8),
    "vrate": dict(color="tab:green", linewidth=1.8),
}
ACTIVE = ("prop", "band", "factor", "vrate")


def render(data_path: Path = DATA, out_path: Path = OUT) -> None:
    cache_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "alberta-buck-mpl"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_dir))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    data = json.loads(data_path.read_text())
    syms = data["config"]["syms"]
    windows = data["config"]["windows"]
    hist = data.get("historical")
    synth = data.get("synthetic")
    sweep = data.get("sweep")

    fig, axes = plt.subplots(3, 2, figsize=(14, 12))

    # (0,0) historical prices, normalized to day 0
    ax = axes[0][0]
    if hist:
        days = range(hist["days"])
        for i, sym in enumerate(syms):
            p = hist["prices"][i]
            ax.plot(days, [v / p[0] for v in p], linewidth=1.2, label=sym)
        ax.set_yscale("log")
        ax.legend(fontsize=8, ncol=2)
    ax.set_title("historical constituents (norm., 2020-09 .. 2025-09)")
    ax.set_ylabel("price / day-0 price")
    ax.grid(True, alpha=0.3)

    # (0,1) showcase constituent: deviation, MA, gate, trades
    ax = axes[0][1]
    if hist and "showcase" in hist:
        sc = hist["showcase"]
        sym = hist.get("showcaseSym", "?")
        days = range(len(sc["delta"]))
        gmax = max(sc["delta"]) * 1.05
        gmin = min(sc["delta"]) * 1.05
        ax.fill_between(days, gmin, gmax,
                        where=[g > 0 for g in sc["gate"]],
                        color="tab:green", alpha=0.12,
                        label="gate open (accel toward target)")
        ax.plot(days, sc["delta"], color="0.55", linewidth=0.7,
                label="share deviation (raw)")
        ax.plot(days, sc["ma"], color="tab:blue", linewidth=1.8,
                label=f"{windows[sym]}d MA")
        sells = [(t, d) for t, v, d in sc["trades"] if v < 0]
        buys = [(t, d) for t, v, d in sc["trades"] if v > 0]
        if sells:
            ax.scatter(*zip(*sells), marker="v", color="tab:red", s=14,
                       zorder=5, label="sell")
        if buys:
            ax.scatter(*zip(*buys), marker="^", color="tab:green", s=14,
                       zorder=5, label="buy")
        ax.axhline(0, color="black", linewidth=0.8, alpha=0.3)
        ax.set_title(f"{hist.get('showcasePolicy', 'factor')} policy on {sym}: "
                     "quench while diverging, pour on the turn")
        ax.set_ylabel("deviation from target weight")
        ax.legend(fontsize=8, loc="upper left")
    ax.grid(True, alpha=0.3)

    # (1,0) historical NAV vs hold
    ax = axes[1][0]
    if hist:
        hold_nav = hist["series"]["hold"]["nav"]
        for name in (n for n in ACTIVE if n in hist["series"]):
            nav = hist["series"][name]["nav"]
            rel = [(a / b - 1.0) * 100.0 for a, b in zip(nav, hold_nav)]
            ax.plot(range(len(rel)), rel, label=name, **POLICY_STYLE[name])
        ax.axhline(0, color="black", linewidth=0.8, alpha=0.3)
        ax.legend(fontsize=9)
    ax.set_title("historical NAV vs buy-and-hold (mandate cost in a trend)")
    ax.set_ylabel("NAV / hold NAV - 1  (%)")
    ax.grid(True, alpha=0.3)

    # (1,1) historical tracking error
    ax = axes[1][1]
    if hist:
        for name in (n for n in ("hold",) + ACTIVE if n in hist["series"]):
            ma = hist["series"][name]["meanAbsDev"]
            ax.plot(range(len(ma)), [v * 100.0 for v in ma], label=name,
                    **POLICY_STYLE[name])
        ax.set_yscale("log")
        ax.legend(fontsize=9)
    ax.set_title("historical mean |share deviation|")
    ax.set_ylabel("mean |deviation|  (%)")
    ax.set_xlabel("day")
    ax.grid(True, alpha=0.3)

    # (2,0) synthetic summary: premium and turnover per policy
    ax = axes[2][0]
    if synth:
        names = [n for n in ACTIVE if n in synth["metrics"]]
        xs = range(len(names))
        prem = [synth["metrics"][n].get("premiumVsHoldBpYr", 0.0) for n in names]
        errs = [synth["metrics"][n].get("premiumVsHoldBpYrStd", 0.0) for n in names]
        bars = ax.bar(xs, prem, yerr=errs, capsize=4,
                      color=[POLICY_STYLE[n]["color"] for n in names],
                      alpha=0.85)
        for x, n, b in zip(xs, names, bars):
            m = synth["metrics"][n]
            ax.annotate(
                f"turnover {m['turnoverPerYr']:.2f}/yr\n"
                f"TE {m['teMeanAbsPct']:.1f}%\n"
                f"capture90 {m['capture90Bp']:+.0f}bp",
                (b.get_x() + b.get_width() / 2.0, b.get_height() / 2.0),
                ha="center", va="center", fontsize=8)
        ax.set_xticks(list(xs), names)
        ax.set_title(f"synthetic rebalancing premium "
                     f"({synth['years']:.0f}y x {synth['seeds']} seeds)")
        ax.set_ylabel("premium vs hold  (bp/yr)")
    ax.grid(True, alpha=0.3, axis="y")

    # (2,1) window sweep: premium vs MA window per constituent
    ax = axes[2][1]
    if sweep:
        cmap = plt.get_cmap("tab10")
        for i, sym in enumerate(syms):
            color = cmap(i % 10)
            ax.plot(sweep["windows"], sweep["premiumBpYr"][sym],
                    marker="o", markersize=3.5, linewidth=1.2, color=color,
                    label=sym)
            ax.axvline(sweep["defaultWindow"][sym], color=color,
                       linestyle=":", linewidth=1.0, alpha=0.6)
        ax.set_title("window sweep (dotted = M2-lag-derived default)")
        ax.set_xlabel("MA window X (days)")
        ax.set_ylabel("premium vs hold (bp/yr)")
        ax.legend(fontsize=8, ncol=2)
    else:
        ax.text(0.5, 0.5, "run with --sweep for the window sweep",
                ha="center", va="center", transform=ax.transAxes, fontsize=10)
    ax.grid(True, alpha=0.3)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)

    print(f"Wrote {out_path.relative_to(REPO) if out_path.is_relative_to(REPO) else out_path}")
    if hist:
        for name, m in hist["metrics"].items():
            print(f"  hist {name:8} cagr {m['cagrPct']:6.2f}%  "
                  f"TE {m['teMeanAbsPct']:6.2f}%  "
                  f"turnover {m['turnoverPerYr']:5.2f}/yr  "
                  f"capture90 {m['capture90Bp']:+8.1f}bp")
    if synth:
        for name, m in synth["metrics"].items():
            print(f"  synth {name:8} premium {m.get('premiumVsHoldBpYr', 0.0):+8.1f}bp/yr  "
                  f"TE {m['teMeanAbsPct']:6.2f}%  "
                  f"turnover {m['turnoverPerYr']:5.2f}/yr")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m alberta_buck.sim.plot_rebalance_policy")
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)

    render(Path(args.data), Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
