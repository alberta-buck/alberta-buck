"""Article figures for alberta-buck-rebalance.org.

Four purpose-built comparison charts from test/vectors/rebalance-policy.json,
each illustrating the gated policies' merit against one alternative:

  images/rebalance-mechanism.png  how the gate works (vs everything)
  images/rebalance-vs-hold.png    synthetic premium accumulation (vs hold)
  images/rebalance-vs-prop.png    historical trend stress (vs prop)
  images/rebalance-frontier.png   timing/turnover efficiency (vs band)

Colors follow the entity across all figures (categorical order validated:
factor blue, vrate aqua, prop yellow, band green; hold = neutral reference).

Run:
    python -m alberta_buck.sim.plot_rebalance_article
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DATA = REPO / "test" / "vectors" / "rebalance-policy.json"
OUT_DIR = REPO / "images"

INK = "#0b0b0b"
INK2 = "#52514e"
COLOR = {                       # fixed categorical order, validated
    "factor": "#2a78d6",
    "vrate":  "#1baf7a",
    "prop":   "#eda100",
    "band":   "#008300",
    "hold":   "#52514e",
}
ACTIVE = ("factor", "vrate", "prop", "band")


def _style(plt):
    plt.rcParams.update({
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "axes.edgecolor": INK2,
        "axes.labelcolor": INK,
        "text.color": INK,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "axes.grid": True,
        "grid.alpha": 0.22,
        "grid.linewidth": 0.6,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "font.size": 10.5,
        "axes.titlesize": 11.5,
        "legend.frameon": False,
    })


def _endlabel(ax, xs, ys, text, color, dy=0):
    ax.annotate(text, (xs[-1], ys[-1]), xytext=(6, dy),
                textcoords="offset points", color=color,
                fontsize=9.5, fontweight="bold", va="center")


def fig_mechanism(data, plt, out):
    hist = data["historical"]
    sc = hist["showcase"]
    sym = hist["showcaseSym"]
    pol = hist.get("showcasePolicy", "factor")
    window = data["config"]["windows"][sym]
    days = list(range(len(sc["delta"])))

    fig, ax = plt.subplots(figsize=(9.6, 4.6))
    lo = min(sc["delta"]) * 1.08
    hi = max(sc["delta"]) * 1.08
    ax.fill_between(days, lo, hi, where=[g > 0 for g in sc["gate"]],
                    color=COLOR["vrate"], alpha=0.10, linewidth=0,
                    label="gate open (turn detected)")
    ax.plot(days, sc["delta"], color=INK2, linewidth=0.8, alpha=0.8,
            label="share deviation (raw)")
    ax.plot(days, sc["ma"], color=COLOR["factor"], linewidth=2.0,
            label=f"{window}-day moving average")
    sells = [(t, d) for t, v, d in sc["trades"] if v < 0]
    buys = [(t, d) for t, v, d in sc["trades"] if v > 0]
    if sells:
        ax.scatter(*zip(*sells), marker="v", s=22, color="#e34948",
                   zorder=5, label="sell (overweight, levelling)")
    if buys:
        ax.scatter(*zip(*buys), marker="^", s=22, color="#008300",
                   zorder=5, label="buy (underweight, levelling)")
    ax.axhline(0, color=INK, linewidth=0.8, alpha=0.35)
    ax.set_xlabel("day (2020-09 .. 2025-09)")
    ax.set_ylabel(f"{sym} deviation from target weight")
    ax.set_title(f"The {pol} policy on {sym}: quench while diverging, "
                 "pour on the turn")
    ax.legend(fontsize=9, loc="upper left", ncol=2)
    fig.tight_layout()
    fig.savefig(out / "rebalance-mechanism.png", dpi=150)
    plt.close(fig)


def fig_vs_hold(data, plt, out):
    syn = data["synthetic"]
    stride = syn.get("exampleStride", 7)
    ex = syn["example"]
    hold = ex["hold"]["nav"]
    n = min(len(v["nav"]) for v in ex.values())
    xs = [i * stride / 365.0 for i in range(n)]

    fig, ax = plt.subplots(figsize=(9.6, 4.6))
    for name in ACTIVE:
        nav = ex[name]["nav"]
        rel = [(nav[i] / hold[i] - 1.0) * 100.0 for i in range(n)]
        ax.plot(xs, rel, color=COLOR[name], linewidth=2.0)
        prem = syn["metrics"][name].get("premiumVsHoldBpYr", 0.0)
        _endlabel(ax, xs, rel, f"{name}  {prem:+.0f} bp/yr", COLOR[name])
    ax.axhline(0, color=COLOR["hold"], linewidth=1.2, linestyle="--")
    ax.annotate("hold (never rebalance)", (xs[int(n * 0.02)], 0),
                xytext=(0, -12), textcoords="offset points",
                color=COLOR["hold"], fontsize=9.5)
    ax.set_xlabel("years (synthetic M2-lag paths, seed 0 of "
                  f"{syn['seeds']}; labels = {syn['seeds']}-seed mean)")
    ax.set_ylabel("NAV vs hold  (%)")
    ax.set_title("vs hold: every policy harvests the excursions hold "
                 "just rides")
    ax.set_xlim(0, xs[-1] * 1.18)
    fig.tight_layout()
    fig.savefig(out / "rebalance-vs-hold.png", dpi=150)
    plt.close(fig)


def fig_vs_prop(data, plt, out):
    hist = data["historical"]
    hold = hist["series"]["hold"]["nav"]
    days = hist["days"]
    xs = [t / 365.0 for t in range(days)]

    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4))

    ax = axes[0]
    for name in ACTIVE:
        nav = hist["series"][name]["nav"]
        rel = [(nav[t] / hold[t] - 1.0) * 100.0 for t in range(days)]
        ax.plot(xs, rel, color=COLOR[name],
                linewidth=2.2 if name in ("factor", "vrate") else 1.4,
                alpha=1.0 if name in ("factor", "vrate") else 0.85)
        m = hist["metrics"][name]
        _endlabel(ax, xs, rel, f"{name} ({m['turnoverPerYr']:.2f}x/yr)",
                  COLOR[name],
                  dy={"factor": 16, "vrate": 2, "band": -12, "prop": -26}[name])
    ax.axhline(0, color=COLOR["hold"], linewidth=1.2, linestyle="--")
    ax.set_xlabel("years (2020-09 .. 2025-09, BTC x9)")
    ax.set_ylabel("NAV vs hold  (%)")
    ax.set_title("The mandate's cost in a secular trend\n"
                 "(labels: policy and turnover per year)")
    ax.set_xlim(0, xs[-1] * 1.35)

    ax = axes[1]
    for name in ("hold",) + ACTIVE:
        ma = hist["series"][name]["meanAbsDev"]
        ax.plot(xs, [v * 100.0 for v in ma], color=COLOR[name],
                linewidth=1.6 if name != "hold" else 1.2,
                linestyle="--" if name == "hold" else "-",
                alpha=0.9,
                label=name)
    ax.set_yscale("log")
    ax.set_xlabel("years")
    ax.set_ylabel("mean |share deviation|  (%)")
    ax.set_title("...while the mandate still holds\n"
                 "(hold's weights run away; the rest stay bounded)")
    ax.legend(fontsize=9, ncol=2, loc="lower right")

    fig.tight_layout()
    fig.savefig(out / "rebalance-vs-prop.png", dpi=150)
    plt.close(fig)


def fig_frontier(data, plt, out):
    syn = data["synthetic"]
    met = syn["metrics"]

    fig, ax = plt.subplots(figsize=(7.6, 4.8))
    for name in ACTIVE:
        m = met[name]
        x = m["turnoverPerYr"]
        y = m["capture90Bp"]
        prem = m.get("premiumVsHoldBpYr", 0.0)
        size = 28 * (prem / 100.0)              # bubble area ~ premium
        ax.scatter([x], [y], s=size, color=COLOR[name], alpha=0.85,
                   edgecolors="white", linewidths=1.5, zorder=5)
        ax.annotate(f"{name}\n{prem:+.0f} bp/yr premium",
                    (x, y), xytext=(12, 2), textcoords="offset points",
                    color=COLOR[name], fontsize=9.5, fontweight="bold")
    ax.axhline(0, color=INK, linewidth=0.8, alpha=0.3)
    ax.set_xlabel("turnover (fraction of NAV traded per year)")
    ax.set_ylabel("90-day forward capture per trade  (bp)")
    ax.set_title("Timing efficiency: what each traded dollar was worth\n"
                 "(bubble area = rebalancing premium; up-left is better)")
    ax.set_xlim(0, max(met[n]["turnoverPerYr"] for n in ACTIVE) * 1.45)
    ax.set_ylim(-40, max(met[n]["capture90Bp"] for n in ACTIVE) * 1.22)
    fig.tight_layout()
    fig.savefig(out / "rebalance-frontier.png", dpi=150)
    plt.close(fig)


def render(data_path: Path = DATA, out_dir: Path = OUT_DIR) -> None:
    cache_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "alberta-buck-mpl"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_dir))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _style(plt)
    data = json.loads(data_path.read_text())
    out_dir.mkdir(parents=True, exist_ok=True)

    fig_mechanism(data, plt, out_dir)
    fig_vs_hold(data, plt, out_dir)
    fig_vs_prop(data, plt, out_dir)
    fig_frontier(data, plt, out_dir)
    for f in ("rebalance-mechanism", "rebalance-vs-hold",
              "rebalance-vs-prop", "rebalance-frontier"):
        print(f"Wrote images/{f}.png")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m alberta_buck.sim.plot_rebalance_article")
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    args = parser.parse_args(argv)
    render(Path(args.data), Path(args.out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
