"""Test: query Chainlink Gold and Silver oracles on mainnet over the past year, plot results.

Two modes of operation:

  Anvil (fast, cached):
    Terminal 1:  make fork-mainnet-cache
    Terminal 2:  ETH_RPC_URL=http://localhost:8545 make nix-test-python
    First run fetches from remote and populates Anvil's disk cache.
    Subsequent runs serve entirely from cache (~seconds).

  Direct RPC (no Anvil):
    MAINNET_RPC_URL=https://... make nix-test-python
    Each run makes ~732 remote RPC calls (~10-15 min).

The test produces alberta_buck/test/gold_silver_history.png.
"""

import logging
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

import pytest

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from alberta_buck.oracle import (
    FEEDS, get_daily_prices, get_round_history, get_web3,
    log_call_counts, rpc_call_summary,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

PLOT_DIR = Path(__file__).parent
DAYS = int(os.environ.get("DAYS", "365"))


def _rpc_unreachable_reason():
    """Return None if an RPC is configured AND reachable; otherwise a skip reason.

    Bare presence of MAINNET_RPC_URL/ETH_RPC_URL is not enough -- a developer
    may have ETH_RPC_URL=http://localhost:8545 in .env without Anvil running.
    Probe the host with a short TCP connect to skip cleanly in that case.
    """
    url = os.environ.get("ETH_RPC_URL") or os.environ.get("MAINNET_RPC_URL")
    if not url:
        return "MAINNET_RPC_URL/ETH_RPC_URL not set"
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return f"could not parse host from {url!r}"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # Remote HTTPS endpoints (Alchemy, Infura) -- assume reachable, don't probe.
    if parsed.scheme == "https" and host not in ("localhost", "127.0.0.1"):
        return None
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return None
    except (OSError, socket.timeout) as exc:
        return f"RPC at {url} unreachable ({exc.__class__.__name__}); skipping"


needs_rpc = pytest.mark.skipif(
    _rpc_unreachable_reason() is not None,
    reason=_rpc_unreachable_reason() or "RPC reachable",
)


def _is_anvil(w3):
    """Detect whether we are connected to an Anvil node."""
    try:
        info = w3.provider.make_request("web3_clientVersion", [])
        version = info.get("result", "")
        return "anvil" in version.lower()
    except Exception:
        return False


@needs_rpc
def test_gold_silver_history():
    """Fetch ~1 year of daily Gold and Silver prices from Chainlink mainnet oracles and plot."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    w3 = get_web3()
    chain_id = w3.eth.chain_id
    anvil = _is_anvil(w3)
    log.info(
        "Connected to chain %d, block %d%s",
        chain_id, w3.eth.block_number,
        " (Anvil -- using round-based cache-friendly queries)" if anvil else "",
    )
    assert chain_id == 1, f"Expected mainnet (chain 1), got {chain_id}"

    # Choose query strategy: round-walk for Anvil (cache-friendly), block-based otherwise
    query_fn = get_round_history if anvil else get_daily_prices

    series = {}
    for name, address in FEEDS.items():
        desc, decimals, records = query_fn(w3, address, days=DAYS)
        log.info("%s: %d records, decimals=%d", desc, len(records), decimals)
        min_records = max(1, int(DAYS * 0.8))
        assert len(records) >= min_records, (
            f"Expected >={min_records} daily records for {name}, got {len(records)}"
        )
        series[name] = (desc, records)

    # Plot
    fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    fig.suptitle(f"Chainlink Oracle Prices -- Ethereum Mainnet ({DAYS} days)", fontsize=14)

    colors = {"XAU/USD": "#DAA520", "XAG/USD": "#C0C0C0"}

    for ax, (name, (desc, records)) in zip(axes, series.items()):
        dates = [r["date"] for r in records]
        prices = [r["price"] for r in records]

        ax.plot(dates, prices, color=colors.get(name, "steelblue"), linewidth=1.2)
        ax.set_ylabel(f"{desc} (USD)", fontsize=11)
        ax.grid(True, alpha=0.3)
        ax.set_title(desc, fontsize=12)

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
    log_call_counts(w3, label="total")
    print(f"\nPlot saved to {out}")
    print(rpc_call_summary(w3, label="total"))

    # Sanity checks
    for name, (desc, records) in series.items():
        prices = [r["price"] for r in records]
        assert min(prices) > 0, f"{name}: got non-positive price"
        if "XAU" in name:
            assert min(prices) > 1000, f"Gold price suspiciously low: ${min(prices)}"
        if "XAG" in name:
            assert min(prices) > 10, f"Silver price suspiciously low: ${min(prices)}"
