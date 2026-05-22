"""Render the rebalancing-sim result to images/rebalancing-sim.png.

Workflow:
  1.  make sim-run-rebalancing   # writes test/vectors/rebalancing-sim.json
  2.  make sim-plot-rebalancing   # reads JSON, writes images/rebalancing-sim.png

JSON schema extends routing-sim.json with:
  poolWeights[i] = [actualWeight, targetWeight]
  rebalancerPnl: value of rebalancer portfolio - initial (USDC micro, day-0)
  rebalanceTrades: cumulative rebalance operations
"""

import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DATA = REPO / "test" / "vectors" / "rebalancing-sim.json"
OUT = REPO / "images" / "rebalancing-sim.png"

E6 = 10 ** 6
E18 = 10 ** 18


def _i(v):
    return int(v)


@pytest.mark.skipif(
    not DATA.exists(),
    reason="rebalancing-sim.json not generated yet; run: "
           "python -m alberta_buck.sim --scenario rebalancing",
)
def test_rebalancing_sim_plot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = json.loads(DATA.read_text())
    names = d["tokens"]
    dec = d.get("decimals", [18, 8, 18])
    fr = d["frames"]
    days = [f["day"] for f in fr]

    def col(key, t):
        return [f.get(key, [])[t] for f in fr]

    fig, axes = plt.subplots(5, 1, figsize=(13, 18), sharex=True)

    # buckUsd = USDC-micro per 1 BUCK from the floating BUCK/USDC pool.
    bu = [f.get("buckUsd", 0) for f in fr]

    # ---- Panels 1-3: per token, both pools vs market reference -------- #
    for t in range(3):
        ax = axes[t]
        ref = [v / E6 for v in col("refUsd", t)]
        su = [v / E6 for v in col("spotUsdc", t)]
        sbk = col("spotBuck", t)
        sb = [(sbk[i] * bu[i] / 1e12) if bu[i] else float("nan")
              for i in range(len(fr))]
        ax.plot(days, ref, color="black", linestyle="--", linewidth=1.4,
                label="market reference (CSV)")
        ax.plot(days, su, color="tab:red", linewidth=1.2,
                label=f"{names[t]}/USDC pool (direct)")
        ax.plot(days, sb, color="tab:purple", linewidth=1.2,
                label=f"{names[t]}/BUCK -> USD via BUCK/USDC (indirect)")
        ax.set_ylabel(f"{names[t]}  USD")
        ax.grid(True, alpha=0.3)

        # Pool balance axes (same as routing plot).
        bal_tok = [f["poolBal"][t][0] / (10 ** dec[t]) for f in fr]
        bal_buck = [f["poolBal"][t][1] / E18 for f in fr]
        ax2 = ax.twinx()
        ax2.plot(days, bal_tok, color="tab:orange", linewidth=1.0,
                 linestyle="--", label=f"{names[t]} in TOKEN/BUCK pool")
        ax2.set_ylabel(f"{names[t]} bal", color="tab:orange")
        ax2.tick_params(axis="y", labelcolor="tab:orange")
        ax3 = ax.twinx()
        ax3.spines.right.set_position(("axes", 1.12))
        ax3.plot(days, bal_buck, color="tab:green", linewidth=1.0,
                 linestyle=":", label="BUCK in TOKEN/BUCK pool")
        ax3.set_ylabel("BUCK bal", color="tab:green")
        ax3.tick_params(axis="y", labelcolor="tab:green")

        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        lines3, labels3 = ax3.get_legend_handles_labels()
        ax.legend(lines1 + lines2 + lines3, labels1 + labels2 + labels3,
                  loc="upper left", fontsize=7)
        if t == 0:
            ax.set_title("Direct vs indirect price tracking "
                         "(BUCK-unaware arbs + basket rebalancer)")

    # ---- Panel 4: pool value-weight deviations ----------------------- #
    ax = axes[3]
    colors_w = ["tab:orange", "tab:blue", "tab:green"]
    handles = []
    for t in range(3):
        aw = [f.get("poolWeights", [[0, 0]] * 3)[t][0] for f in fr]
        tw = [f.get("poolWeights", [[0, 0]] * 3)[t][1] for f in fr]
        l1, = ax.plot(days, aw, color=colors_w[t], linewidth=1.4,
                      label=f"{names[t]} actual")
        l2, = ax.plot(days, tw, color=colors_w[t], linewidth=1.0,
                      linestyle="--",
                      label=f"{names[t]} target")
        handles.extend([l1, l2])
    ax.axhline(1.0 / 3, color="black", alpha=0.15, linewidth=0.5)
    ax.set_ylabel("value weight")
    ax.legend(handles=handles, loc="upper left", fontsize=7, ncol=2)
    ax.grid(True, alpha=0.3)
    ax.set_title("TOKEN/BUCK pool value weights: actual vs basket target")

    # ---- Panel 5: treasury compounding (NAV vs outstanding) --------- #
    ax = axes[4]
    handles5 = []
    nav = [f.get("basketNav", 0) / E18 for f in fr]
    l1, = ax.plot(days, nav, color="tab:blue", linewidth=1.5,
                  label="basket NAV (total LP value in BUCK)")
    handles5.append(l1)
    out = [f.get("dmOutstanding", 0) / E18 for f in fr]
    l2, = ax.plot(days, out, color="tab:orange", linewidth=1.2,
                  linestyle="--",
                  label="DM outstanding (BUCK principal)")
    handles5.append(l2)
    ax.set_ylabel("BUCK (18d)")
    ax.set_xlabel("Day")
    ax.grid(True, alpha=0.3)

    ax2 = ax.twinx()
    ts = [f.get("treasuryShare", 0) * 100 for f in fr]
    l3, = ax2.plot(days, ts, color="tab:green", linewidth=1.4,
                   label="treasury share (%)")
    handles5.append(l3)
    ax2.set_ylabel("treasury share (%)", color="tab:green")
    ax2.tick_params(axis="y", labelcolor="tab:green")

    ax.legend(handles=handles5, loc="upper left", fontsize=8)
    ax.set_title("Basket NAV, DM outstanding liability & treasury share")

    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=120)
    plt.close(fig)

    # Convergence summary.
    print(f"\nWrote {OUT.relative_to(REPO)}  ({len(days)} days)")
    for t in range(3):
        ref = col("refUsd", t)[-1] / E6
        su = col("spotUsdc", t)[-1] / E6
        sb = (col("spotBuck", t)[-1] * (bu[-1] / 1e6) / E6
              if bu[-1] else float("nan"))
        print(f"  {names[t]:5s}  ref ${ref:,.2f}  "
              f"USDC-pool ${su:,.2f} ({100*(su-ref)/ref:+.2f}%)  "
              f"BUCK-pool->USD ${sb:,.2f} ({100*(sb-ref)/ref:+.2f}%)")
    last = fr[-1]
    dm_entries = last.get("dmEntries", 0)
    dm_exits = last.get("dmExits", 0)
    nav_final = last.get("basketNav", 0) / E18
    out_final = last.get("dmOutstanding", 0) / E18
    ts_final = last.get("treasuryShare", 0) * 100
    print(f"  direct trades: {last.get('directTrades',0)}  "
          f"BUCK-routed: {last.get('cycleTrades',0)}  "
          f"rebalance: {last.get('rebalanceTrades',0)}")
    print(f"  direct-mint entries: {dm_entries}  exits: {dm_exits}")
    print(f"  basket NAV: {nav_final:,.2f} BUCK  "
          f"outstanding: {out_final:,.2f} BUCK  "
          f"treasury share: {ts_final:.2f}%")
    rp_final = last.get("rebalancerPnl", 0) / E6
    dp_final = last.get("directMintPnl", 0) / E6
    print(f"  rebalancer P&L: ${rp_final:,.0f}  "
          f"direct-mint P&L: ${dp_final:,.0f}")
    pw = last.get("poolWeights", [[0, 0]] * 3)
    for t in range(3):
        aw, tw = pw[t]
        dev = (aw - tw) * 100
        print(f"  {names[t]:5s}  weight actual={aw:.4f} target={tw:.4f}  "
              f"deviation {dev:+.2f}%")


if __name__ == "__main__":
    test_rebalancing_sim_plot()
