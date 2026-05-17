"""Render the routing-sim result to images/routing-sim.png.

Workflow:
  1.  forge test --match-contract RoutingSimTest
        # writes test/vectors/routing-sim.json
  2.  python -m pytest test/stabilizer-routing-op47/test_routing_sim_plot.py -v -s
        # reads JSON, writes images/routing-sim.png

JSON schema (parallel per-frame, N=3 tokens [PAXG, cbBTC, AOIL]):
  frames[i] = { day, refUsd[3], spotUsdc[3], spotBuck[3],
                basketVal, buckK, supply,
                directTrades, cycleTrades, aggPnl }

Units: refUsd/spotUsdc/spotBuck are all USDC/BUCK micro-dollars / token
(1e6 == $1.00); BUCK is 6-dec and anchor/basket-pegged ~ $1, so spotBuck
is directly comparable to the reference.  The headline result: the BUCK-routed
(indirect) pools converge onto the same market reference as the direct
TOKEN/USDC pools, driven only by BUCK-unaware routed arbitrage.
"""

import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DATA = REPO / "test" / "vectors" / "routing-sim.json"
OUT = REPO / "images" / "routing-sim.png"

E6 = 10 ** 6
E18 = 10 ** 18


def _i(v):
    return int(v)


@pytest.mark.skipif(
    not DATA.exists(),
    reason="routing-sim.json not generated yet; run: "
           "forge test --match-contract RoutingSimTest",
)
def test_routing_sim_plot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = json.loads(DATA.read_text())
    names = d["tokens"]
    fr = d["frames"]
    days = [f["day"] for f in fr]

    def col(key, t):
        return [f[key][t] for f in fr]

    fig, axes = plt.subplots(5, 1, figsize=(13, 16), sharex=True)
    colors = ["tab:orange", "tab:blue", "tab:green"]

    # ---- Panels 1-3: per token, both pools vs market reference -------- #
    for t in range(3):
        ax = axes[t]
        ref = [v / E6 for v in col("refUsd", t)]
        su = [v / E6 for v in col("spotUsdc", t)]
        sb = [v / E6 for v in col("spotBuck", t)]
        ax.plot(days, ref, color="black", linestyle="--", linewidth=1.4,
                label="market reference (CSV)")
        ax.plot(days, su, color="tab:red", linewidth=1.2,
                label=f"{names[t]}/USDC pool (direct)")
        ax.plot(days, sb, color="tab:purple", linewidth=1.2,
                label=f"{names[t]}/BUCK pool (indirect, BUCK~$1)")
        ax.set_ylabel(f"{names[t]}  USD")
        ax.legend(loc="upper left", fontsize=8)
        ax.grid(True, alpha=0.3)
        if t == 0:
            ax.set_title("Direct (TOKEN/USDC) and indirect (TOKEN/BUCK) pool "
                         "prices vs market reference")

    # ---- Panel 4: cumulative route usage ----------------------------- #
    ax = axes[3]
    direct = [f["directTrades"] for f in fr]
    cycle = [f["cycleTrades"] for f in fr]
    ax.plot(days, direct, color="tab:red", linewidth=1.4,
            label="direct TOKEN/USDC reference-arb trades")
    ax.plot(days, cycle, color="tab:purple", linewidth=1.4,
            label="BUCK-routed trades (TOKEN<->BUCK<->TOKEN / USDC<->BUCK<->TOKEN)")
    ax.set_ylabel("cumulative trades")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title("Route usage -- proof the BUCK pools are routed alongside "
                 "the TOKEN/USDC pools")

    # ---- Panel 5: controller + agent PnL ----------------------------- #
    ax = axes[4]
    bk = [_i(f["buckK"]) / E18 for f in fr]
    bv = [_i(f["basketVal"]) / E18 for f in fr]
    l1, = ax.plot(days, bk, color="tab:green", linewidth=1.5, label="buckK")
    l2, = ax.plot(days, bv, color="tab:gray", linewidth=1.2, linestyle="--",
                  label="basketValueInBuck")
    ax.axhline(1.0, color="black", alpha=0.2, linewidth=0.5)
    ax.set_ylabel("BUCK (18d)")
    ax.set_xlabel("Day")
    ax.grid(True, alpha=0.3)
    ax2 = ax.twinx()
    pnl = [_i(f["aggPnl"]) / E6 for f in fr]
    l3, = ax2.plot(days, pnl, color="tab:cyan", linewidth=1.0,
                   label="aggregate agent realized PnL (USDC)")
    ax2.set_ylabel("USDC", color="tab:cyan")
    ax2.tick_params(axis="y", labelcolor="tab:cyan")
    ax.legend(handles=[l1, l2, l3], loc="upper left", fontsize=9)
    ax.set_title("Controller (buckK, basketValueInBuck) and arbitrageur PnL")

    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=120)
    plt.close(fig)

    # convergence summary
    print(f"\nWrote {OUT.relative_to(REPO)}  ({len(days)} days)")
    for t in range(3):
        ref = col("refUsd", t)[-1] / E6
        su = col("spotUsdc", t)[-1] / E6
        sb = col("spotBuck", t)[-1] / E6
        print(f"  {names[t]:5s}  ref ${ref:,.2f}  "
              f"USDC-pool ${su:,.2f} ({100*(su-ref)/ref:+.2f}%)  "
              f"BUCK-pool ${sb:,.2f} ({100*(sb-ref)/ref:+.2f}%)")
    print(f"  direct trades: {fr[-1]['directTrades']}  "
          f"BUCK-routed trades: {fr[-1]['cycleTrades']}")
