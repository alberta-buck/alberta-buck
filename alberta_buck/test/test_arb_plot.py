"""Generate arbitrage scenario visualisation from BuckKArbScenario.t.sol output.

Workflow:
  1.  forge test --match-contract BuckKArbScenarioTest
        # writes test/vectors/arb-scenario.json
  2.  python -m pytest alberta_buck/test/test_arb_plot.py -v -s
        # reads JSON, writes images/arb-scenario.png

The plot has four panels stacked vertically, sharing the same time axis:

  1. Spot vs TWAP vs basket cost (USDT per BUCK).  The spread between
     spot and TWAP is the controller's blind spot; the spread between
     TWAP and basket is what the PID is working to close.
  2. buckK trajectory.  The PID's response, shown against the
     [0.50, 1.50] anti-windup bounds.
  3. Alice's realized PnL (cumulative USDT).  Step changes mark closed
     arb cycles; flat segments are her holding open positions.
  4. Bob population (alive vs retired).  The shape of the mint/burn
     wave that drove the price action.
"""

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "test" / "vectors" / "arb-scenario.json"
OUT  = REPO / "images" / "arb-scenario.png"

E18 = 10**18
E6  = 10**6


@pytest.mark.skipif(
    not DATA.exists(),
    reason="arb-scenario.json not generated yet; run: "
           "forge test --match-contract BuckKArbScenarioTest",
)
def test_arb_plot():
    """Read arb-scenario.json and save images/arb-scenario.png."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with DATA.open() as f:
        d = json.load(f)

    snaps = d["snapshots"]
    bobs  = d["bobs"]

    days  = [s["t"] / 86400.0 for s in snaps]
    spot  = [int(s["spot"])   / E18 for s in snaps]
    twap  = [int(s["twap"])   / E18 for s in snaps]
    bask  = [int(s["basket"]) / E18 for s in snaps]
    buckk = [int(s["buckK"])  / E18 for s in snaps]

    # PnL (signed; JSON had it serialized as a string with possible '-' prefix).
    def _signed(v):
        if isinstance(v, str):
            return int(v)
        return int(v)
    pnl = [_signed(s["aliceRealizedPnl"]) / E6 for s in snaps]
    arb_dir = [s["aliceArbDir"] for s in snaps]

    bobs_alive   = [s["bobsAlive"]   for s in snaps]
    bobs_retired = [s["bobsRetired"] for s in snaps]

    fig, axes = plt.subplots(4, 1, figsize=(12, 11), sharex=True)

    # ---- Panel 1: prices ---------------------------------------------- #
    ax = axes[0]
    ax.plot(days, spot, label="BUCK spot",   color="tab:red",  alpha=0.6, linewidth=1.0)
    ax.plot(days, twap, label="BUCK TWAP",   color="tab:blue", linewidth=1.5)
    ax.plot(days, bask, label="basket cost", color="tab:gray", linestyle="--")
    ax.axhline(1.00, color="black", alpha=0.2, linewidth=0.5)
    ax.set_ylabel("USDT / BUCK")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title("BUCK pool spot vs 600 s TWAP vs basket cost")

    # Mark Alice arb position-open spans.
    in_arb = False
    arb_start = 0.0
    for i, dirn in enumerate(arb_dir):
        if dirn != 0 and not in_arb:
            in_arb = True
            arb_start = days[i]
        elif dirn == 0 and in_arb:
            in_arb = False
            ax.axvspan(arb_start, days[i], color="gold", alpha=0.10, zorder=0)
    if in_arb:
        ax.axvspan(arb_start, days[-1], color="gold", alpha=0.10, zorder=0)

    # ---- Panel 2: buckK ---------------------------------------------- #
    ax = axes[1]
    ax.plot(days, buckk, color="tab:green", linewidth=1.5)
    ax.axhline(1.00, color="black", alpha=0.2, linewidth=0.5)
    ax.axhline(0.50, color="red",   alpha=0.3, linewidth=0.5, linestyle=":")
    ax.axhline(1.50, color="red",   alpha=0.3, linewidth=0.5, linestyle=":")
    ax.set_ylabel("buckK")
    ax.set_ylim(0.45, 1.55)
    ax.grid(True, alpha=0.3)
    ax.set_title("BUCK_K (output of PID)")

    # ---- Panel 3: Alice realized PnL --------------------------------- #
    ax = axes[2]
    ax.plot(days, pnl, color="tab:purple", linewidth=1.5, drawstyle="steps-post")
    ax.fill_between(days, 0, pnl, alpha=0.2, step="post", color="tab:purple")
    ax.axhline(0, color="black", alpha=0.2, linewidth=0.5)
    ax.set_ylabel("USDT")
    ax.grid(True, alpha=0.3)
    ax.set_title(
        f"Alice cumulative realized PnL  (final: ${pnl[-1]:,.0f}; "
        f"{d.get('arbCount', '?')} cycles)"
    )

    # ---- Panel 4: Bob population ------------------------------------- #
    ax = axes[3]
    ax.fill_between(days, 0, bobs_alive,                 color="tab:orange", alpha=0.5, label="alive")
    ax.fill_between(days, bobs_alive,
                    [a + r for a, r in zip(bobs_alive, bobs_retired)],
                    color="tab:gray",   alpha=0.4, label="retired")
    ax.set_ylabel("# Bobs")
    ax.set_xlabel("Days since simulation start")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title(f"Bob lifecycle  (n = {len(bobs)})")

    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=120)
    plt.close(fig)

    print(f"\nWrote {OUT.relative_to(REPO)}")
    print(f"  snapshots:   {len(snaps)}")
    print(f"  bobs:        {len(bobs)}")
    print(f"  alice PnL:   ${pnl[-1]:,.2f}")
    print(f"  arb cycles:  {d.get('arbCount')}")
    print(f"  buckK final: {buckk[-1]:.4f}")
