"""Chainlink oracle historical price reading utilities.

Two query strategies:

  get_round_history() -- walks backwards through getRoundData(roundId) at the current
      (fork) block.  All calls hit one block height, so Anvil's storage cache is maximally
      effective.  Preferred when running against a local Anvil fork.

  get_daily_prices()  -- calls latestRoundData() at estimated block numbers for each day.
      Each call targets a different block height.  Works against any RPC but is not
      cache-friendly across blocks.
"""

import logging
import os
from datetime import datetime, timezone

from web3 import Web3

log = logging.getLogger(__name__)

# Chainlink AggregatorV3Interface -- minimal ABI for price reading
AGGREGATOR_V3_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "_roundId", "type": "uint80"}],
        "name": "getRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "description",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Chainlink mainnet feed addresses (from BuckKController.t.sol)
FEEDS = {
    "XAU/USD": "0x214eD9Da11D2fbe465a6fc601a91E62EbEc1a0D6",
    "XAG/USD": "0x379589227b15F1a12195D3f2d90bBc9F31f95235",
}

# Average Ethereum block time post-merge (seconds)
AVG_BLOCK_TIME = 12.05
SECONDS_PER_DAY = 86400


def get_web3(rpc_url=None):
    """Connect to Ethereum mainnet via the provided or environment RPC URL."""
    if rpc_url is None:
        rpc_url = os.environ.get("ETH_RPC_URL") or os.environ.get("MAINNET_RPC_URL")
    if not rpc_url:
        raise RuntimeError(
            "No RPC URL: set MAINNET_RPC_URL or ETH_RPC_URL environment variable"
        )
    log.info(
        "Connecting to %s (ETH_RPC_URL=%s, MAINNET_RPC_URL=%s)",
        rpc_url,
        os.environ.get("ETH_RPC_URL", "(unset)"),
        os.environ.get("MAINNET_RPC_URL", "(unset)"),
    )
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise RuntimeError(f"Cannot connect to {rpc_url}")

    # Patch the provider to count actual RPC calls and cache eth_chainId.
    # web3.py's fill_transaction_defaults calls eth_chainId on every
    # contract.functions.foo().call() -- ~2x per eth_call.  Caching at
    # the provider level intercepts all code paths (middleware,
    # fill_transaction_defaults, etc.).
    rpc_counts = {}       # actual calls that hit the node
    cached_counts = {}    # calls served from cache
    _cached = {}
    _original = w3.provider.make_request

    def _counting_provider(method, params):
        if method == "eth_chainId" and method in _cached:
            cached_counts[method] = cached_counts.get(method, 0) + 1
            return _cached[method]
        rpc_counts[method] = rpc_counts.get(method, 0) + 1
        result = _original(method, params)
        if method == "eth_chainId":
            _cached[method] = result
        return result

    w3.provider.make_request = _counting_provider
    w3.rpc_counts = rpc_counts
    w3.cached_counts = cached_counts

    return w3


def rpc_call_summary(w3, label=""):
    """Return a human-readable summary of RPC call counts."""
    if not hasattr(w3, "rpc_counts") or not w3.rpc_counts:
        return ""
    total = sum(w3.rpc_counts.values())
    parts = ", ".join(
        f"{method}: {count}" for method, count in sorted(w3.rpc_counts.items())
    )
    prefix = f"RPC calls [{label}]" if label else "RPC calls"
    summary = f"{prefix}: {total} total ({parts})"
    if hasattr(w3, "cached_counts") and w3.cached_counts:
        cached_total = sum(w3.cached_counts.values())
        summary += f" + {cached_total} cached"
    return summary


def log_call_counts(w3, label=""):
    """Log a summary of actual (non-cached) RPC call counts."""
    summary = rpc_call_summary(w3, label)
    if summary:
        log.info("%s", summary)


def get_daily_prices(w3, feed_address, days=365):
    """Query a Chainlink price feed at daily block intervals.

    Returns (description, decimals, records) where each record is a dict with
    keys: date, price, block, round_id, updated_at.
    """
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(feed_address),
        abi=AGGREGATOR_V3_ABI,
    )
    decimals = contract.functions.decimals().call()
    description = contract.functions.description().call()

    latest_block = w3.eth.block_number
    latest = w3.eth.get_block(latest_block)
    now_ts = latest.timestamp

    log.info(
        "Querying %s (%d decimals) over %d days from block %d",
        description, decimals, days, latest_block,
    )

    records = []
    for day_offset in range(days, -1, -1):
        blocks_ago = int(day_offset * SECONDS_PER_DAY / AVG_BLOCK_TIME)
        block_num = max(1, latest_block - blocks_ago)

        try:
            data = contract.functions.latestRoundData().call(
                block_identifier=block_num
            )
            round_id, answer, _started, updated_at, _answered = data
            price = answer / (10**decimals)
            dt = datetime.fromtimestamp(updated_at, tz=timezone.utc)

            records.append({
                "date": dt,
                "price": price,
                "block": block_num,
                "round_id": round_id,
                "updated_at": updated_at,
            })
        except Exception as exc:
            log.warning("  block %d (day -%d): %s", block_num, day_offset, exc)

        if day_offset % 30 == 0:
            log.info("  ... %d days remaining", day_offset)

    return description, decimals, records


# ---------------------------------------------------------------------------
# Round-based history -- cache-friendly (all calls at one block height)
# ---------------------------------------------------------------------------

def get_round_history(w3, feed_address, days=365):
    """Query a Chainlink feed by walking backwards through getRoundData().

    All calls execute at the current (fork) block, reading historical round
    storage.  This makes every call cacheable by Anvil at a single block height.

    Walks backwards one round at a time, collecting one record per calendar day.

    Returns (description, decimals, records) -- same shape as get_daily_prices().
    """
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(feed_address),
        abi=AGGREGATOR_V3_ABI,
    )
    decimals = contract.functions.decimals().call()
    description = contract.functions.description().call()

    round_id, _, _, latest_ts, _ = contract.functions.latestRoundData().call()
    phase_id = round_id >> 64
    agg_round = round_id & ((1 << 64) - 1)
    cutoff_ts = latest_ts - days * SECONDS_PER_DAY

    log.info(
        "Querying %s via getRoundData: phase=%d, latest round=%d",
        description, phase_id, agg_round,
    )

    records = {}  # date -> record (one per calendar day)
    cursor = agg_round
    consecutive_errors = 0

    while cursor >= 1 and phase_id >= 1:
        rid = (phase_id << 64) | cursor
        try:
            _, answer, _, updated_at, _ = contract.functions.getRoundData(rid).call()
            consecutive_errors = 0
        except Exception:
            consecutive_errors += 1
            if consecutive_errors > 5:
                # Likely hit phase boundary -- try previous phase
                phase_id -= 1
                if phase_id >= 1:
                    cursor = _find_phase_last_round(contract, phase_id)
                    consecutive_errors = 0
                    continue
                break
            cursor -= 1
            continue

        if updated_at <= cutoff_ts:
            break

        dt = datetime.fromtimestamp(updated_at, tz=timezone.utc)
        date_key = dt.date()
        if date_key not in records:
            records[date_key] = {
                "date": dt,
                "price": answer / (10**decimals),
                "block": None,
                "round_id": rid,
                "updated_at": updated_at,
            }

        cursor -= 1

        if len(records) % 30 == 0 and cursor == agg_round - 1 or len(records) % 60 == 0:
            log.info("  ... %d days collected, round %d", len(records), cursor)

    result = sorted(records.values(), key=lambda r: r["date"])
    log.info("%s: %d daily records via round walk", description, len(result))
    return description, decimals, result


def _find_phase_last_round(contract, phase_id, hi=100_000):
    """Binary search for the highest valid aggregator round in a phase."""
    lo = 1
    last_valid = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        rid = (phase_id << 64) | mid
        try:
            contract.functions.getRoundData(rid).call()
            last_valid = mid
            lo = mid + 1
        except Exception:
            hi = mid - 1
    return last_valid
