"""Off-chain AMM math + path discovery + Universal Router encoding.

The org doc's "exact eth_call quoter": each hop is quoted as the
constant-product market its pool presents INSIDE the active tick range --
the virtual reserves of slot0's price and liquidity() (alberta_buck.sim.gauge,
WAVE3.org decision 8) -- net of the pool fee.  Until decision 8 the quote
divided the pool's token balances, which a concentrated position distorts.
The Universal Router only *executes* the chosen path (pre-fund /
payerIsUser=false).
"""

from __future__ import annotations

import math

from web3 import Web3

from alberta_buck.sim.gauge import oriented, pool_state, read_pool_state

FEE_DEN = 1_000_000
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342


# ---- pool init math ------------------------------------------------- #

def sqrt_price_x96(base: str, base_amt: int, quote: str, quote_amt: int) -> int:
    """sqrtPriceX96 so 1 `base_amt` of base costs `quote_amt` of quote."""
    base_is0 = base.lower() < quote.lower()
    a0 = base_amt if base_is0 else quote_amt
    a1 = quote_amt if base_is0 else base_amt
    return math.isqrt((a1 * (1 << 192)) // a0)


def full_range_ticks(tick_spacing: int) -> tuple[int, int]:
    # Truncate toward zero (NOT Python floor) so the bounds stay inside
    # [-887272, 887272] -- matches Solidity (MIN_TICK/spacing)*spacing.
    hi = (887272 // tick_spacing) * tick_spacing
    return -hi, hi


# ---- within-range constant-product quote ---------------------------- #

def quote_hop(w3: Web3, erc20_abi: list, pool: str,
              token_in: str, token_out: str, amt_in: int, fee: int,
              balance_of=None, chain=None) -> int:
    """Exact xy=k output for a single V3 hop inside its active range, net of
    `fee` (pip).  The reserves are the pool's virtual reserves at slot0
    (gauge.virtual_reserves): read through `chain.pool_state` (memoized)
    when the sim's Chain is given, else uncached through `w3`.  `balance_of`
    is accepted for the old call shape and unused -- the balance quote is
    what decision 8 retired."""
    if amt_in == 0:
        return 0
    state = (pool_state(chain, pool) if chain is not None
             else read_pool_state(w3, pool))
    r_in, r_out = oriented(state, token_in, token_out)
    if r_in == 0 or r_out == 0:
        return 0
    eff = amt_in * (FEE_DEN - fee) // FEE_DEN
    return (r_out * eff) // (r_in + eff)


def quote_path(w3, erc20_abi, hops: list[tuple[str, str, str, int]],
               amt_in: int, balance_of=None, chain=None) -> int:
    """hops = [(pool, token_in, token_out, fee), ...] chained."""
    amt = amt_in
    for pool, ti, to, fee in hops:
        amt = quote_hop(w3, erc20_abi, pool, ti, to, amt, fee, balance_of,
                        chain=chain)
        if amt == 0:
            return 0
    return amt


# ---- Universal Router V3_SWAP_EXACT_IN encoding --------------------- #

def encode_path(tokens_fees: list) -> bytes:
    """[tokenA, fee0, tokenB, fee1, tokenC, ...] -> packed V3 path bytes."""
    out = b""
    for i, item in enumerate(tokens_fees):
        if i % 2 == 0:  # address
            out += bytes.fromhex(Web3.to_checksum_address(item)[2:])
        else:           # uint24 fee
            out += int(item).to_bytes(3, "big")
    return out


def ur_exec_args(recipient: str, amount_in: int, path: bytes):
    """(commands, inputs) for UniversalRouter.execute V3_SWAP_EXACT_IN,
    pre-fund route (payerIsUser=false, router pays from its own balance)."""
    from eth_abi import encode
    commands = bytes([0x00])  # V3_SWAP_EXACT_IN
    inp = encode(
        ["address", "uint256", "uint256", "bytes", "bool", "uint256[]"],
        [Web3.to_checksum_address(recipient), amount_in, 0, path, False, []],
    )
    return commands, [inp]
