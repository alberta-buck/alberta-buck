"""Render the equilibrium-sim result to images/equilibrium-sim.png.

Workflow:
  1.  make sim-run-equilibrium    # writes test/vectors/equilibrium-sim.json
  2.  make sim-plot-equilibrium   # reads JSON, writes images/equilibrium-sim.png

Three panels tell the BUCK-K feedback story:
  (A) basketValueInBuck vs the 1.0 parity setpoint, with buckK (the K-scaled
      LTV cap the controller moves) on a twin axis.  Equilibrium = basketVal
      hugging 1.0 with buckK settled.
  (B) the PID internals P (ppm error), I (ppm*s integral), D (ppm dError) --
      the integral is the dominant, slow-moving term that trims K.
  (C) monetary aggregates: BUCK totalSupply, DM outstanding principal, and
      idle BUCK held by savers.

Input vector / output image are overridable via EQ_VECTOR / EQ_OUT so a
short smoke run and a long run can be plotted independently.
"""

import json
import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def _resolve(p: Path) -> Path:
    return p if p.is_absolute() else (REPO / p)


DATA = _resolve(Path(os.environ.get(
    "EQ_VECTOR", REPO / "test" / "vectors" / "equilibrium-sim.json")))
OUT = _resolve(Path(os.environ.get(
    "EQ_OUT", REPO / "images" / "equilibrium-sim.png")))

E6 = 10 ** 6
E18 = 10 ** 18


@pytest.mark.skipif(
    not DATA.exists(),
    reason="equilibrium-sim.json not generated yet; run: "
           "python -m alberta_buck.sim --scenario equilibrium",
)
def test_equilibrium_sim_plot():
    cache_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "alberta-buck-mpl"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_dir))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = json.loads(DATA.read_text())
    fr = d["frames"]
    days = [f["day"] for f in fr]

    fig, axes = plt.subplots(3, 1, figsize=(13, 12), sharex=True)

    # ---- Panel A: basketValue vs parity + buckK ---------------------- #
    ax = axes[0]
    bval = [f.get("basketVal", 0) / E18 for f in fr]
    ax.axhline(1.0, color="black", linestyle="--", linewidth=1.2,
               label="parity setpoint (1.0)")
    ax.plot(days, bval, color="tab:blue", linewidth=1.5,
            label="basketValueInBuck")
    ax.set_ylabel("basket value (BUCK)")
    ax.grid(True, alpha=0.3)

    ax2 = ax.twinx()
    bk = [f.get("buckK", 0) / E18 for f in fr]
    ax2.plot(days, bk, color="tab:red", linewidth=1.4, label="buckK (LTV cap)")
    ax2.set_ylabel("buckK", color="tab:red")
    ax2.tick_params(axis="y", labelcolor="tab:red")

    l1, la1 = ax.get_legend_handles_labels()
    l2, la2 = ax2.get_legend_handles_labels()
    ax.legend(l1 + l2, la1 + la2, loc="upper left", fontsize=8)
    ax.set_title("BUCK-K feedback: basket value defended toward parity by buckK")

    # ---- Panel B: PID internals -------------------------------------- #
    ax = axes[1]
    p = [f.get("pid_p", 0) for f in fr]
    i = [f.get("pid_i", 0) for f in fr]
    dd = [f.get("pid_d", 0) for f in fr]
    ax.axhline(0, color="black", alpha=0.3, linewidth=0.8)
    ax.plot(days, p, color="tab:green", linewidth=1.2, label="P (ppm error)")
    ax.plot(days, dd, color="tab:purple", linewidth=1.0, linestyle=":",
            label="D (ppm dError)")
    ax.set_ylabel("P / D (ppm)")
    ax.grid(True, alpha=0.3)
    ax3 = ax.twinx()
    ax3.plot(days, i, color="tab:orange", linewidth=1.4,
             label="I (ppm*s integral)")
    ax3.set_ylabel("I (ppm*s)", color="tab:orange")
    ax3.tick_params(axis="y", labelcolor="tab:orange")
    l1, la1 = ax.get_legend_handles_labels()
    l2, la2 = ax3.get_legend_handles_labels()
    ax.legend(l1 + l2, la1 + la2, loc="upper left", fontsize=8)
    ax.set_title("PID internals (integral-dominant K trim)")

    # ---- Panel C: monetary aggregates -------------------------------- #
    ax = axes[2]
    supply = [f.get("supply", 0) / E6 for f in fr]
    outb = [f.get("dmOutstanding", 0) / E6 for f in fr]
    sav = [f.get("saver_hold", 0) / E6 for f in fr]
    ax.plot(days, supply, color="tab:blue", linewidth=1.5,
            label="BUCK total supply")
    ax.plot(days, outb, color="tab:orange", linewidth=1.2, linestyle="--",
            label="DM outstanding principal")
    ax.plot(days, sav, color="tab:green", linewidth=1.2,
            label="saver-held BUCK")
    ax.set_ylabel("BUCK")
    ax.set_xlabel("Day")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=8)
    ax.set_title("Monetary aggregates: supply, outstanding, idle savings")

    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=120)
    plt.close(fig)

    # ---- Convergence summary ----------------------------------------- #
    try:
        shown = OUT.relative_to(REPO)
    except ValueError:
        shown = OUT
    print(f"\nWrote {shown}  ({len(days)} days)")

    def _at(idx):
        f = fr[idx]
        return (f.get("basketVal", 0) / E18, f.get("buckK", 0) / E18,
                f.get("pid_i", 0), f.get("supply", 0) / E6,
                f.get("saver_hold", 0) / E6)

    for label, idx in (("first", 0), ("mid", len(fr) // 2), ("last", -1)):
        bv, k, pi, sup, sh = _at(idx)
        print(f"  {label:5s} day {fr[idx]['day']:4d}  "
              f"basketVal={bv:.6f}  buckK={k:.6f}  I={pi:,}  "
              f"supply={sup:,.0f}  saverHold={sh:,.0f}")
    print(f"  final basketVal deviation {100*(_at(-1)[0]-1.0):+.3f}% from parity")


if __name__ == "__main__":
    test_equilibrium_sim_plot()
