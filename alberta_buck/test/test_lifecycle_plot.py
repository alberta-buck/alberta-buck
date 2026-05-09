"""Generate lifecycle visualisation from BuckLifecycle.t.sol test output.

Workflow:
  1.  forge test --match-test test_lifecycle    # writes test/vectors/lifecycle.json
  2.  python -m pytest alberta_buck/test/test_lifecycle_plot.py -v -s
      (or: make test-python)                   # reads JSON, writes images/lifecycle.png

The test is skipped if lifecycle.json does not exist yet.
"""

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "test" / "vectors" / "lifecycle.json"
OUT  = REPO / "images" / "lifecycle.png"

P = 1_000_000   # 6-decimal BUCK/USDC: raw / P = dollar amount


def _days(d):
    t0 = d["t"][0]
    return [(t - t0) / 86400 for t in d["t"]]


def _buck(d, key):
    return [v / P for v in d[key]]


@pytest.mark.skipif(not DATA.exists(), reason="lifecycle.json not generated yet; run: forge test --match-test test_lifecycle")
def test_lifecycle_plot():
    """Read lifecycle.json produced by BuckLifecycle.t.sol and save images/lifecycle.png."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.lines import Line2D

    with DATA.open() as f:
        d = json.load(f)

    n      = len(d["labels"])
    days   = _days(d)
    labels = d["labels"]

    alice_spendable = _buck(d, "alice_buck")
    alice_raw       = _buck(d, "alice_raw")
    alice_usdc      = _buck(d, "alice_usdc")
    pool_buck       = _buck(d, "pool_buck")
    pool_usdc       = _buck(d, "pool_usdc")
    jubilee         = _buck(d, "jubilee")
    supply          = _buck(d, "supply")
    credit_val      = _buck(d, "credit_val")
    bob_buck        = _buck(d, "bob_buck")
    bob_usdc        = _buck(d, "bob_usdc")

    # Days of swap events for vertical guide lines.
    swap_days = [days[i] for i, l in enumerate(labels) if l.startswith("swap")]

    fig, axes = plt.subplots(3, 1, figsize=(13, 11), sharex=False)
    fig.suptitle(
        "Alberta Buck Lifecycle: Vehicle Insurance  →  AMM Liquidity  →  Redemption",
        fontsize=14, fontweight="bold", y=0.98,
    )

    # ── Panel 1: BUCK token economy ──────────────────────────────────────────
    ax1 = axes[0]
    ax1.set_title("BUCK Balances and Total Supply", fontsize=11)

    ax1.plot(days, supply,          color="gray",      lw=1.5, ls="--", label="Total supply (incl. pool principal)")
    ax1.plot(days, alice_raw,       color="steelblue", lw=1,   ls=":",  label="Alice raw balance")
    ax1.plot(days, alice_spendable, color="steelblue", lw=2,            label="Alice spendable BUCK")
    ax1.plot(days, pool_buck,       color="darkorange", lw=2,           label="Pool BUCK reserve")
    ax1.plot(days, bob_buck,        color="seagreen",  lw=2,            label="Bob BUCK (from swaps)")
    ax1.plot(days, jubilee,         color="purple",    lw=2,            label="Jubilee fund (accrued at burn)")

    for sd in swap_days:
        ax1.axvline(x=sd, color="green", alpha=0.12, lw=1)

    # Mark key lifecycle events.
    for tag, idx_name in [("Mint", "buck-minted"), ("Pool seeded", "pool-seeded"),
                          ("LP removed", "liquidity-removed"), ("Burn", "buck-burned")]:
        idx = labels.index(idx_name)
        ax1.axvline(x=days[idx], color="black", alpha=0.35, lw=1, ls="-.")
        ax1.text(days[idx] + 1, ax1.get_ylim()[1] if ax1.get_ylim()[1] > 0 else 6000,
                 tag, fontsize=7, rotation=90, va="top", color="black", alpha=0.6)

    ax1.set_ylabel("BUCK  ($)")
    ax1.set_ylim(bottom=0)
    ax1.legend(loc="upper right", fontsize=8, ncol=2)
    ax1.grid(True, alpha=0.3)
    ax1.set_xlim(left=-5, right=max(days) + 5)

    # ── Panel 2: Pool reserves ────────────────────────────────────────────────
    ax2 = axes[1]
    ax2.set_title("Uniswap V2 Pool Reserves  (0.3 % fee; Bob: 12 × 500 USDC → BUCK)", fontsize=11)

    ax2.plot(days, pool_buck, color="steelblue", lw=2, label="Pool BUCK reserve")
    ax2.plot(days, pool_usdc, color="darkgreen", lw=2, label="Pool USDC reserve")

    ax2.fill_between(days, pool_buck, pool_usdc,
                     where=[b < u for b, u in zip(pool_buck, pool_usdc)],
                     alpha=0.07, color="darkgreen", label="USDC surplus (Bob's payments)")

    for sd in swap_days:
        ax2.axvline(x=sd, color="green", alpha=0.2, lw=1)

    # Annotate final reserves.
    last_pool = next((i for i in range(n - 1, -1, -1) if pool_buck[i] > 0), None)
    if last_pool is not None:
        ax2.annotate(
            f"${pool_usdc[last_pool]:,.0f} USDC\n${pool_buck[last_pool]:,.0f} BUCK",
            xy=(days[last_pool], pool_usdc[last_pool]),
            xytext=(days[last_pool] - 60, pool_usdc[last_pool] * 0.85),
            fontsize=8, color="darkgreen",
            arrowprops=dict(arrowstyle="->", color="darkgreen", lw=0.8),
        )

    ax2.set_ylabel("Reserve  ($)")
    ax2.set_ylim(bottom=0)
    ax2.legend(loc="center right", fontsize=8)
    ax2.grid(True, alpha=0.3)
    ax2.set_xlim(left=-5, right=max(days) + 5)

    # ── Panel 3: Credit depreciation ─────────────────────────────────────────
    ax3 = axes[2]
    ax3.set_title("Vehicle BUCK_CREDIT: Declining-Balance Depreciation  (15 %/yr, 5 % salvage)", fontsize=11)

    # Analytical 30-year curve for context.
    import math
    face, floor, rate = 10_000, 500, 0.15
    depreciable = face - floor
    year_days = [d * 365 for d in range(31)]
    analytic_vals = []
    for yr in range(31):
        v = depreciable * ((1 - rate) ** yr)
        analytic_vals.append(floor + v)

    ax3.plot(year_days, analytic_vals, color="red", lw=1.5, ls="--",
             alpha=0.5, label="30-yr depreciation schedule (analytical)")

    # Actual on-chain values from test.
    cval_nonzero = [(days[i], credit_val[i]) for i in range(n) if credit_val[i] > 0]
    if cval_nonzero:
        cx, cy = zip(*cval_nonzero)
        ax3.plot(cx, cy, color="red", lw=2.5, label="On-chain currentValue() (year 1)")

    ax3.axhline(y=face,  color="red",   lw=1, ls=":", alpha=0.5, label=f"Face value  ${face:,}")
    ax3.axhline(y=floor, color="black", lw=1, ls=":", alpha=0.4, label=f"Salvage floor  ${floor:,}")

    # Annotate year-1 end.
    if cval_nonzero:
        ax3.annotate(
            f"${cy[-1]:,.0f}  after 1 yr",
            xy=(cx[-1], cy[-1]),
            xytext=(cx[-1] + 200, cy[-1] + 400),
            fontsize=9, color="red",
            arrowprops=dict(arrowstyle="->", color="red", lw=0.8),
        )

    ax3.set_xlabel("Days since contract creation")
    ax3.set_ylabel("Asset value  ($)")
    ax3.set_xlim(left=-50, right=10950)   # 0..30 years
    ax3.set_ylim(bottom=0, top=face * 1.1)
    ax3.legend(loc="upper right", fontsize=8)
    ax3.grid(True, alpha=0.3)

    plt.tight_layout(rect=[0, 0, 1, 0.97])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\nLifecycle plot saved to {OUT}")

    # Basic sanity assertions.
    assert max(pool_usdc) > 10_000, "Pool USDC should exceed initial deposit after 12 swaps"
    assert min(v for v in credit_val if v > 0) < 10_000, "Credit should have depreciated"
    assert jubilee[-1] > 0, "Jubilee should have accrued"
