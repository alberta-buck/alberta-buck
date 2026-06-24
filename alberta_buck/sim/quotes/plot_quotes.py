#!/usr/bin/env python3
"""Render a verification figure for the historical quote source.

  images/commodity-quotes-sim.png

Panels:
  (1) Nominal-USD monthly anchors (sim default unit) for all five tokens, log y
      (gold and the BCPI indices span orders of magnitude).
  (2) A zoom (1979-1982 oil shock) overlaying NRGY monthly anchors (dots) on the
      seeded hourly walk (line) -- the bridge lands exactly on each month.
  (3) Unit comparison for gold (XAU): nominal USD vs CAD_NI (inflation-
      neutralized) -- nominal runs away; real shows the long-run store-of-value.

Run:  python -m alberta_buck.sim.quotes.plot_quotes
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from alberta_buck.sim.quotes import QuoteSource, TOKENS, Unit

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "images" / "commodity-quotes-sim.png"

COLORS = {"NRGY": "tab:red", "BULN": "tab:gray", "FOOD": "tab:green",
          "XAU": "tab:orange", "LABR": "tab:blue"}


def _years(dts):
    return [d.year + (d.month - 1) / 12 + (d.day - 1) / 365 for d in dts]


def main():
    usd = QuoteSource(Unit.USD)
    real = QuoteSource(Unit.CAD_NI)

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(12, 13))

    # (1) Nominal USD anchors, log scale.
    for token in TOKENS:
        a = usd.anchors(token)
        ax1.plot(_years([d for d, _ in a]), [v for _, v in a],
                 color=COLORS[token], lw=1.6, alpha=0.85, label=token)
    ax1.set_yscale("log")
    ax1.set_title("Nominal-USD quotes (sim default): BCPI indices (100=1972), "
                  "gold $/oz, labour $/hr", fontweight="bold")
    ax1.set_ylabel("USD (log)")
    ax1.grid(alpha=0.3, which="both")
    ax1.legend(loc="upper left", ncol=5, fontsize=9)

    # (2) Zoom: hourly walk vs monthly anchors (NRGY, 1979-1982 oil shock).
    start, end = datetime(1979, 1, 1), datetime(1982, 1, 1)
    hours = int((end - start).total_seconds() // 3600)
    ts = [start + timedelta(hours=h) for h in range(hours)]
    ax2.plot(_years(ts), [usd.at("NRGY", t) for t in ts],
             color=COLORS["NRGY"], lw=0.6, alpha=0.8, label="NRGY hourly walk")
    a = [(d, v) for d, v in usd.anchors("NRGY") if start <= d <= end]
    ax2.plot(_years([d for d, _ in a]), [v for _, v in a],
             "o", color="black", ms=5, label="monthly anchors (BCPI)")
    ax2.set_title("Seeded hourly walk lands exactly on each monthly anchor "
                  "(NRGY USD, 1979-1982)", fontweight="bold")
    ax2.set_ylabel("USD index")
    ax2.grid(alpha=0.3)
    ax2.legend(loc="upper left", fontsize=9)

    # (3) Unit comparison for gold.
    gu = usd.anchors("XAU")
    gr = real.anchors("XAU")
    ax3.plot(_years([d for d, _ in gu]), [v for _, v in gu],
             color="tab:orange", lw=1.8, label="XAU nominal USD")
    ax3.plot(_years([d for d, _ in gr]), [v for _, v in gr],
             color="tab:purple", lw=1.8, ls="--", label="XAU inflation-neutralized (CAD_NI)")
    ax3.set_yscale("log")
    ax3.set_title("Same metric, two units: nominal vs inflation-neutralized gold",
                  fontweight="bold")
    ax3.set_ylabel("value (log)")
    ax3.set_xlabel("year")
    ax3.grid(alpha=0.3, which="both")
    ax3.legend(loc="upper left", fontsize=9)

    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=140, bbox_inches="tight")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
