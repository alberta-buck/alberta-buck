"""Render the optimal-control debtor comparison to images/equilibrium-debtors.png.

Reads the eq-debtors experiment vector (frames[].octl per-agent states):
who retires interest-bearing debt best, across the aggressiveness (theta)
ladder and income patterns, against each agent's own no-BUCK counterfactual.

Run:
    python -m alberta_buck.sim.plot_octl [--data VECTOR] [--out PNG]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DATA = REPO / "test" / "vectors" / "eq-eq-debtors.json"
OUT = REPO / "images" / "equilibrium-debtors.png"

THETA_COLOR = {0.0: "#2a78d6", 0.25: "#1baf7a", 1.0: "#eda100", 3.0: "#e34948"}
INK, INK2 = "#0b0b0b", "#52514e"


def render(data_path: Path = DATA, out_path: Path = OUT) -> None:
    cache_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "alberta-buck-mpl"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_dir))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    data = json.loads(data_path.read_text())
    frames = [f for f in data["frames"] if f.get("octl")]
    if not frames:
        raise SystemExit("no octl frames in vector; run the debtors experiment")
    days = [f["day"] for f in frames]

    # agent id -> series
    agents: dict[int, dict] = {}
    for f in frames:
        for o in f["octl"]:
            a = agents.setdefault(o["idx"], {
                "theta": o["theta"], "pattern": o["pattern"],
                "adv": [], "mort": [], "drawn": [], "deploys": []})
            a["adv"].append((o["nw"] - o["hypo"]) / 1e6)
            a["mort"].append(o["mortgage"] / 1e6)
            a["drawn"].append(o["drawn"] / 1e6)
            a["deploys"].append(o["deploys"])
    bvib = [f["basketVal"] / 1e18 for f in frames]

    fig, axes = plt.subplots(2, 2, figsize=(13, 9))

    def _style(a):
        return dict(color=THETA_COLOR.get(a["theta"], INK2),
                    linestyle="-" if a["pattern"] == "salary" else "--",
                    linewidth=1.8)

    ax = axes[0][0]
    for a in agents.values():
        ax.plot(days, a["adv"], **_style(a))
    ax.axhline(0, color=INK, linewidth=0.8, alpha=0.3)
    ax.set_title("net-worth advantage vs own no-BUCK counterfactual\n"
                 "(color = theta tolerance; solid = salary, dashed = lumpy)")
    ax.set_ylabel("advantage  ($)")
    ax.grid(True, alpha=0.25)
    for th, c in THETA_COLOR.items():
        ax.plot([], [], color=c, label=f"theta={th}")
    ax.legend(fontsize=8, ncol=2)

    ax = axes[0][1]
    ax.plot(days, bvib, color=INK2, linewidth=1.6)
    ax.axhline(1.0, color=INK, linewidth=0.8, alpha=0.4)
    for th, c in THETA_COLOR.items():
        if th > 0:
            ax.axhline(1.0 + th * 0.055, color=c, linewidth=0.9,
                       linestyle=":", alpha=0.8)
    ax.set_title("basketValueInBuck (the discount signal)\n"
                 "(dotted lines: each theta's deploy tolerance)")
    ax.set_ylabel("bvib")
    ax.grid(True, alpha=0.25)

    ax = axes[1][0]
    for a in agents.values():
        ax.plot(days, a["mort"], **_style(a))
    ax.set_title("mortgage principal outstanding")
    ax.set_ylabel("$")
    ax.set_xlabel("day")
    ax.grid(True, alpha=0.25)

    ax = axes[1][1]
    order = sorted(agents.values(), key=lambda a: (a["theta"], a["pattern"]))
    labels = [f"{a['theta']}\n{a['pattern'][:3]}" for a in order]
    vals = [a["adv"][-1] for a in order]
    bars = ax.bar(range(len(order)), vals,
                  color=[THETA_COLOR.get(a["theta"], INK2) for a in order])
    for b, a in zip(bars, order):
        b.set_alpha(1.0 if a["pattern"] == "salary" else 0.55)
    ax.set_xticks(range(len(order)), labels, fontsize=8)
    ax.axhline(0, color=INK, linewidth=0.8, alpha=0.4)
    ax.set_title("terminal advantage by strategy")
    ax.set_ylabel("$")
    ax.grid(True, alpha=0.25, axis="y")

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    print(f"Wrote {out_path.relative_to(REPO) if out_path.is_relative_to(REPO) else out_path}")
    for a in order:
        print(f"  theta={a['theta']:<5} {a['pattern']:<7} "
              f"deploys={a['deploys'][-1]:>3}  "
              f"mortgage=${a['mort'][-1]:>12,.0f}  "
              f"advantage=${a['adv'][-1]:>12,.0f}")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m alberta_buck.sim.plot_octl")
    ap.add_argument("--data", default=str(DATA))
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    render(Path(a.data), Path(a.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
