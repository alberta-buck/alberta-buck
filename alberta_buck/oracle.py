"""Chainlink oracle historical price reading utilities.

Queries Chainlink AggregatorV3 price feeds on Ethereum mainnet at daily block intervals,
returning time-series data suitable for analysis and plotting.
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
        rpc_url = os.environ.get("MAINNET_RPC_URL") or os.environ.get("ETH_RPC_URL")
    if not rpc_url:
        raise RuntimeError(
            "No RPC URL: set MAINNET_RPC_URL or ETH_RPC_URL environment variable"
        )
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise RuntimeError(f"Cannot connect to {rpc_url}")
    return w3


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
