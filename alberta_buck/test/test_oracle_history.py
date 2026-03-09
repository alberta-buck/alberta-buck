"""Test: query Chainlink Gold and Silver oracles on mainnet over the past year, plot results.

Requires MAINNET_RPC_URL (or ETH_RPC_URL) environment variable pointing to an
Ethereum archive-capable RPC endpoint (e.g. Alchemy, Infura).

    pytest alberta_buck/test/test_oracle_history.py -v -s

The test produces alberta_buck/test/gold_silver_history.png.
"""

import logging
import os
from pathlib import Path

import pytest

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from alberta_buck.oracle import FEEDS, get_daily_prices, get_web3

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

PLOT_DIR = Path(__file__).parent
DAYS = 365

needs_rpc = pytest.mark.skipif(
    not (os.environ.get("MAINNET_RPC_URL") or os.environ.get("ETH_RPC_URL")),
    reason="MAINNET_RPC_URL not set -- skipping mainnet fork test",
)


@needs_rpc
def test_gold_silver_history():
    """Fetch ~1 year of daily Gold and Silver prices from Chainlink mainnet oracles and plot."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    w3 = get_web3()
    chain_id = w3.eth.chain_id
    log.info("Connected to chain %d, block %d", chain_id, w3.eth.block_number)
    assert chain_id == 1, f"Expected mainnet (chain 1), got {chain_id}"

    # Query both feeds
    series = {}
    for name, address in FEEDS.items():
        desc, decimals, records = get_daily_prices(w3, address, days=DAYS)
        log.info("%s: %d records, decimals=%d", desc, len(records), decimals)
        assert len(records) > 300, f"Expected >300 daily records for {name}, got {len(records)}"
        series[name] = (desc, records)

    # Plot
    fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    fig.suptitle("Chainlink Oracle Prices -- Ethereum Mainnet (1 Year)", fontsize=14)

    colors = {"XAU/USD": "#DAA520", "XAG/USD": "#C0C0C0"}

    for ax, (name, (desc, records)) in zip(axes, series.items()):
        dates = [r["date"] for r in records]
        prices = [r["price"] for r in records]

        ax.plot(dates, prices, color=colors.get(name, "steelblue"), linewidth=1.2)
        ax.set_ylabel(f"{desc} (USD)", fontsize=11)
        ax.grid(True, alpha=0.3)
        ax.set_title(desc, fontsize=12)

        # Annotate latest
        ax.annotate(
            f"${prices[-1]:,.2f}",
            xy=(dates[-1], prices[-1]),
            fontsize=9,
            color=colors.get(name, "steelblue"),
            fontweight="bold",
        )

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    axes[-1].xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    fig.autofmt_xdate(rotation=30)
    plt.tight_layout(rect=[0, 0, 1, 0.96])

    out = PLOT_DIR / "gold_silver_history.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    log.info("Plot saved to %s", out)

    # Sanity checks
    for name, (desc, records) in series.items():
        prices = [r["price"] for r in records]
        assert min(prices) > 0, f"{name}: got non-positive price"
        if "XAU" in name:
            assert min(prices) > 1000, f"Gold price suspiciously low: ${min(prices)}"
        if "XAG" in name:
            assert min(prices) > 10, f"Silver price suspiciously low: ${min(prices)}"
