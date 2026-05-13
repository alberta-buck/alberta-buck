"""Generate arbitrage scenario visualisation from BuckKArbScenario.t.sol output.

Workflow:
  1.  forge test --match-contract BuckKArbScenarioTest
        # writes test/vectors/arb-scenario.json
  2.  python -m pytest alberta_buck/test/test_arb_plot.py -v -s
        # reads JSON, writes images/arb-scenario.png

The plot has five panels stacked vertically, sharing the same time axis:

  1. Spot vs TWAP vs basket cost (USDT per BUCK).
  2. buckK (official PID) overlaid with aliceK (Alice's faster PID).  The
     spread is Alice's tradable signal.
  3. Alice signal magnitude (aliceK - buckK).  Entry / exit thresholds are
     drawn as horizontal lines.
  4. Alice's realized PnL (cumulative USDT).
  5. Bob (farmer) and Fred (builder) populations stacked.  The phase-offset
     seasonal waves are the source of the price action.
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

    days   = [s["t"] / 86400.0 for s in snaps]
    spot   = [int(s["spot"])   / E18 for s in snaps]
    twap   = [int(s["twap"])   / E18 for s in snaps]
    bask   = [int(s["basket"]) / E18 for s in snaps]
    buckk  = [int(s["buckK"])  / E18 for s in snaps]
    alicek = [int(s["aliceK"]) / E18 for s in snaps]
    signal = [a - b for a, b in zip(alicek, buckk)]

    def _signed(v):
        return int(v) if isinstance(v, str) else int(v)
    pnl = [_signed(s["aliceRealizedPnl"]) / E6 for s in snaps]
    arb_dir = [s["aliceArbDir"] for s in snaps]

    bobs_alive    = [s["bobsAlive"]    for s in snaps]
    bobs_retired  = [s["bobsRetired"]  for s in snaps]
    freds_alive   = [s["fredsAlive"]   for s in snaps]
    freds_retired = [s["fredsRetired"] for s in snaps]

    n_bobs  = sum(1 for b in bobs if b.get("kind", 0) == 0)
    n_freds = sum(1 for b in bobs if b.get("kind", 0) == 1)

    fig, axes = plt.subplots(5, 1, figsize=(13, 14), sharex=True)

    # ---- Panel 1: prices --------------------------------------------- #
    ax = axes[0]
    ax.plot(days, spot, label="BUCK spot",   color="tab:red",  alpha=0.5, linewidth=0.8)
    ax.plot(days, twap, label="BUCK TWAP",   color="tab:blue", linewidth=1.5)
    ax.plot(days, bask, label="basket cost", color="tab:gray", linestyle="--")
    ax.axhline(1.00, color="black", alpha=0.2, linewidth=0.5)
    ax.set_ylabel("USDT / BUCK")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title("BUCK pool spot vs 600 s TWAP vs basket cost")

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

    # ---- Panel 2: buckK vs aliceK ------------------------------------ #
    ax = axes[1]
    ax.plot(days, buckk,  color="tab:green",  linewidth=1.6, label="buckK (official)")
    ax.plot(days, alicek, color="tab:orange", linewidth=1.2, linestyle="--",
            label="aliceK (5x gains)")
    ax.axhline(1.00, color="black", alpha=0.2, linewidth=0.5)
    ax.axhline(0.50, color="red",   alpha=0.3, linewidth=0.5, linestyle=":")
    ax.axhline(1.50, color="red",   alpha=0.3, linewidth=0.5, linestyle=":")
    ax.set_ylabel("PID output")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title("PID outputs: official BUCK_K vs Alice's faster private PID")

    # ---- Panel 3: signal --------------------------------------------- #
    ax = axes[2]
    ax.plot(days, signal, color="tab:purple", linewidth=1.0)
    ax.fill_between(days, 0, signal, alpha=0.2, color="tab:purple")
    ax.axhline(0,       color="black", alpha=0.3, linewidth=0.5)
    ax.axhline( 0.003,  color="tab:red", alpha=0.4, linewidth=0.6,
                linestyle="--", label="entry threshold (+/-0.3 %)")
    ax.axhline(-0.003,  color="tab:red", alpha=0.4, linewidth=0.6, linestyle="--")
    ax.axhline( 0.0005, color="tab:blue", alpha=0.3, linewidth=0.5,
                linestyle=":", label="exit threshold")
    ax.axhline(-0.0005, color="tab:blue", alpha=0.3, linewidth=0.5, linestyle=":")
    ax.set_ylabel("aliceK - buckK")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title("Alice signal (PID spread); shading shows actively open arb spans")

    # ---- Panel 4: Alice PnL ------------------------------------------ #
    ax = axes[3]
    ax.plot(days, pnl, color="tab:purple", linewidth=1.5, drawstyle="steps-post")
    ax.fill_between(days, 0, pnl, alpha=0.2, step="post", color="tab:purple")
    ax.axhline(0, color="black", alpha=0.2, linewidth=0.5)
    ax.set_ylabel("USDT")
    ax.grid(True, alpha=0.3)
    ax.set_title(
        f"Alice cumulative realized PnL  (final: ${pnl[-1]:,.0f}; "
        f"{d.get('arbCount', '?')} cycles)"
    )

    # ---- Panel 5: actor populations --------------------------------- #
    ax = axes[4]
    bobs_total  = [a + r for a, r in zip(bobs_alive,  bobs_retired)]
    freds_total = [a + r for a, r in zip(freds_alive, freds_retired)]
    ax.fill_between(days, 0,           bobs_alive,
                    color="tab:orange", alpha=0.55, label=f"Bob (farmer) alive (n={n_bobs})")
    ax.fill_between(days, bobs_alive,  bobs_total,
                    color="tab:orange", alpha=0.15, label="Bob retired")
    ax.fill_between(days, [-a for a in freds_alive], 0,
                    color="tab:cyan",   alpha=0.55, label=f"Fred (builder) alive (n={n_freds})")
    ax.fill_between(days, [-t for t in freds_total], [-a for a in freds_alive],
                    color="tab:cyan",   alpha=0.15, label="Fred retired")
    ax.axhline(0, color="black", alpha=0.3, linewidth=0.5)
    ax.set_ylabel("# actors")
    ax.set_xlabel("Days since simulation start")
    ax.legend(loc="upper right", fontsize=8, ncol=2)
    ax.grid(True, alpha=0.3)
    ax.set_title(
        f"Counter-cyclical actor populations  "
        f"(Bob = farmer above 0 ; Fred = builder below 0)"
    )

    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=120)
    plt.close(fig)

    print(f"\nWrote {OUT.relative_to(REPO)}")
    print(f"  snapshots:    {len(snaps)}")
    print(f"  bobs:         {n_bobs}")
    print(f"  freds:        {n_freds}")
    print(f"  alice PnL:    ${pnl[-1]:,.2f}")
    print(f"  arb cycles:   {d.get('arbCount')}")
    print(f"  buckK final:  {buckk[-1]:.4f}")
    print(f"  aliceK final: {alicek[-1]:.4f}")
