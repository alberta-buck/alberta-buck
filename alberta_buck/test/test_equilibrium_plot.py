"""Generate equilibrium-scenario visualisation from BuckEquilibriumScenario.t.sol.

Workflow:
  1.  forge test --match-contract BuckEquilibriumScenarioTest
        # writes test/vectors/equilibrium-scenario.json
  2.  python -m pytest alberta_buck/test/test_equilibrium_plot.py -v -s
        # reads JSON, writes images/equilibrium-scenario.png

Four-panel layout:
  Panel 1: BUCK pool spot price vs the $1.00 pegged basket.
  Panel 2: buckK (left axis) and insurance fundingFactor (right axis), so the
           feedback couple between credit-multiplier and pre-mint reserve
           requirement is visible at a glance.
  Panel 3: Total BUCK supply and Jubilee balance (both 6-dec).
  Panel 4: Active Carol count and aggregate mintedNet (per-Carol BUCK
           liability tracked outside the contract; useful for diagnosing
           the rebalance loop).
"""

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "test" / "vectors" / "equilibrium-scenario.json"
OUT  = REPO / "images" / "equilibrium-scenario.png"

E18 = 10**18
E6  = 10**6


def _signed_int(v):
    if isinstance(v, str):
        return int(v)
    return int(v)


@pytest.mark.skipif(
    not DATA.exists(),
    reason="equilibrium-scenario.json not generated yet; run: "
           "forge test --match-contract BuckEquilibriumScenarioTest",
)
def test_equilibrium_plot():
    """Read equilibrium-scenario.json and save images/equilibrium-scenario.png."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with DATA.open() as f:
        d = json.load(f)

    t0   = d["t"][0]
    days = [(t - t0) / 86400 for t in d["t"]]

    spot   = [_signed_int(v) / E18 for v in d["spot"]]
    basket = [_signed_int(v) / E18 for v in d["basket"]]
    buckk  = [int(v) / E18 for v in d["buckK"]]
    factor = [int(v) / E18 for v in d["factor"]]
    supply = [int(v) / E6  for v in d["supply"]]
    jubilee= [int(v) / E6  for v in d["jubilee"]]
    poolB  = [int(v) / E6  for v in d["pool_buck"]]
    poolU  = [int(v) / E6  for v in d["pool_usdc"]]
    active = d["active"]
    retired= d.get("retired", [0] * len(active))
    hanks  = d.get("hanks",   [0] * len(active))
    hankHld= [int(v) / E6 for v in d.get("hank_hold", [0] * len(active))]
    aggMint= [int(v) / E6 for v in d["agg_minted"]]

    fig, axes = plt.subplots(4, 1, figsize=(13, 12), sharex=True)

    # ---- Panel 1: prices --------------------------------------------- #
    ax = axes[0]
    ax.plot(days, spot,   label="BUCK pool spot (USDC/BUCK)", color="tab:red",  linewidth=1.4)
    ax.plot(days, basket, label="basket peg ($1.00)",         color="tab:gray", linestyle="--")
    ax.axhline(1.00, color="black", alpha=0.2, linewidth=0.5)
    ax.set_ylabel("USDC / BUCK")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title("BUCK price (V2 pool spot) vs pegged $1.00 basket")

    # ---- Panel 2: buckK + factor twin axes --------------------------- #
    ax = axes[1]
    l1, = ax.plot(days, buckk, color="tab:green", linewidth=1.6, label="buckK")
    ax.axhline(1.00, color="black", alpha=0.2, linewidth=0.5)
    ax.set_ylabel("buckK", color="tab:green")
    ax.tick_params(axis="y", labelcolor="tab:green")
    ax.grid(True, alpha=0.3)

    ax2 = ax.twinx()
    l2, = ax2.plot(days, factor, color="tab:purple", linewidth=1.2,
                    linestyle="--", label="fundingFactor")
    ax2.set_ylabel("fundingFactor", color="tab:purple")
    ax2.tick_params(axis="y", labelcolor="tab:purple")

    ax.set_title("BUCK_K (credit-limit multiplier) vs insurance funding factor")
    ax.legend(handles=[l1, l2], loc="upper right", fontsize=9)

    # ---- Panel 3: supply + jubilee ----------------------------------- #
    ax = axes[2]
    ax.plot(days, supply,  label="totalSupply",     color="tab:blue", linewidth=1.4)
    ax.plot(days, jubilee, label="Jubilee balance", color="tab:orange", linewidth=1.0)
    ax.set_ylabel("BUCK")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title("Total BUCK supply and Jubilee accumulation")

    # ---- Panel 4: actor populations + flows -------------------------- #
    ax = axes[3]
    l1, = ax.plot(days, active,  color="tab:orange", linewidth=1.4, label="active Carols")
    l3, = ax.plot(days, retired, color="tab:brown",  linewidth=1.0, linestyle="--",
                  label="retired Carols")
    l4, = ax.plot(days, hanks,   color="tab:green",  linewidth=1.2, label="active Hanks")
    ax.set_ylabel("# actors")
    ax.set_xlabel("Days since simulation start")
    ax.grid(True, alpha=0.3)

    ax2 = ax.twinx()
    l2, = ax2.plot(days, aggMint, color="tab:cyan",   linewidth=1.0,
                    label="Carol agg mintedNet")
    l5, = ax2.plot(days, hankHld, color="tab:purple", linewidth=1.0, linestyle=":",
                    label="Hank holdings")
    ax2.set_ylabel("BUCK", color="tab:cyan")
    ax2.tick_params(axis="y", labelcolor="tab:cyan")

    ax.set_title("Carol lifecycle (arrive -> mint -> hold -> retire) and Hank accumulation")
    ax.legend(handles=[l1, l3, l4, l2, l5], loc="upper right", fontsize=8, ncol=2)

    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=120)
    plt.close(fig)

    print(f"\nWrote {OUT.relative_to(REPO)}")
    print(f"  snapshots:     {len(days)}")
    print(f"  span:          {days[-1]:.0f} days")
    print(f"  spot end:      ${spot[-1]:.4f}")
    print(f"  buckK end:     {buckk[-1]:.4f}")
    print(f"  factor end:    {factor[-1]:.4f}")
    print(f"  supply end:    ${supply[-1]:,.0f}")
    print(f"  active Carols: {active[-1]}")
