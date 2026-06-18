"""Render the basket-flow sim result to images/basket-flow-sim.png."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DATA = REPO / "test" / "vectors" / "basket-flow-sim.json"
OUT = REPO / "images" / "basket-flow-sim.png"


def _money_axis(v: float) -> str:
    av = abs(v)
    if av >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if av >= 1_000:
        return f"${v / 1_000:.0f}k"
    return f"${v:.0f}"


def _pct_axis(v: float) -> str:
    return f"{v * 100:.0f}%"


def render(data_path: Path = DATA, out_path: Path = OUT) -> None:
    cache_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "alberta-buck-mpl"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_dir))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    data = json.loads(data_path.read_text())
    frames = data["frames"]
    config = data.get("config", {})
    initial_nav = float(config.get("initialNav", 0.0))

    days = [f["day"] for f in frames]
    total_invested = [
        initial_nav + float(f.get("cumulativeInvested", 0.0))
        for f in frames
    ]
    rebalanced_return = [float(f["sharePrice"]) - 1.0 for f in frames]
    original_return = [float(f["passiveIndex"]) - 1.0 for f in frames]
    rebalanced_pl = [
        ret * invested for ret, invested in zip(rebalanced_return, total_invested)
    ]
    original_pl = [
        ret * invested for ret, invested in zip(original_return, total_invested)
    ]
    excess_pl = [
        rb - original for rb, original in zip(rebalanced_pl, original_pl)
    ]

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    ax = axes[0]
    ax.plot(days, rebalanced_return, color="tab:blue", linewidth=1.8,
            label="rebalanced pool")
    ax.plot(days, original_return, color="black", linestyle="--", linewidth=1.4,
            label="initial ratios, buy-and-hold")
    ax.axhline(0, color="black", linewidth=0.8, alpha=0.25)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: _pct_axis(y)))
    ax.set_ylabel("P/L / invested")
    ax.set_title("Rebalanced pool vs original-ratio commodity basket")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)

    ax = axes[1]
    ax.plot(days, rebalanced_pl, color="tab:blue", linewidth=1.3,
            label="rebalanced pool P/L")
    ax.plot(days, original_pl, color="black", linestyle="--", linewidth=1.2,
            label="original-ratio P/L")
    ax.fill_between(days, 0, excess_pl, color="tab:green", alpha=0.20,
                    where=[v >= 0 for v in excess_pl],
                    label="excess P/L")
    ax.fill_between(days, 0, excess_pl, color="tab:red", alpha=0.18,
                    where=[v < 0 for v in excess_pl])
    ax.axhline(0, color="black", linewidth=0.8, alpha=0.25)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: _money_axis(y)))
    ax.set_ylabel("scaled P/L")
    ax.set_xlabel("day")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)

    last = frames[-1]
    final_invested = total_invested[-1]
    print(f"Wrote {out_path.relative_to(REPO)}")
    print(f"  total invested scale: ${final_invested:,.0f}")
    print(f"  rebalanced P/L: ${rebalanced_pl[-1]:,.0f} "
          f"({rebalanced_return[-1] * 100:+.2f}%)")
    print(f"  original-ratio P/L: ${original_pl[-1]:,.0f} "
          f"({original_return[-1] * 100:+.2f}%)")
    print(f"  excess P/L: ${excess_pl[-1]:,.0f}")
    print(f"  active investors: {last.get('activeInvestors', 0)}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m alberta_buck.sim.plot_basket_flow")
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)

    render(Path(args.data), Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
