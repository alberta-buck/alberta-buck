"""Pool gauges from slot0 -- WAVE3.org decision 8 (branch wp/d8-slot0).

WHY

A Uniswap V3 pool's token balances are not its reserves.  A balance is the
sum over every position of what its range holds at the current price, plus
the fees not yet collected (LP and protocol), and only a FULL-RANGE position
holds its two amounts in the constant-product ratio.  A concentrated
position adds its funding asset to the balance without moving the price --
the treasury seeder's USDC-funded range on day 1 of the WP-8 smoke left the
balances implying 1.41 USD per BUCK while slot0 said 1.08 -- so a reader
that divides balances misreads a placement as a price move, and an amount
quote on balances overstates the depth the price actually sees.  The sim's
K then moved on that misreading (WAVE3.org, decisions pending 8).

WHAT

The pool's own state is the gauge:

    slot0.sqrtPriceX96    sqrt(token1 / token0) in Q64.96 -- the price
    liquidity()           L, the liquidity active at the current tick

Inside the current tick range the pool IS a constant-product market on the
virtual reserves

    x = L * 2^96 / sqrtPriceX96        (token0)
    y = L * sqrtPriceX96 / 2^96        (token1)

with x * y = L^2 and y / x = price, so every constant-product formula the
sim already uses (`_cp_out`, `_impact_cap`, `_amount_in_for_out`,
`router.quote_hop`) is exact on (x, y) for a swap that stays inside the
active range, and needs no change of FORM, only of INPUT.  The
approximation, named: a swap that crosses a tick boundary meets a different
L beyond it; the quote here is the within-range one, which is also what the
balance quote silently assumed.  For a pool whose only positions are
full-range, (x, y) is the balances less uncollected fees and rounding dust
-- the fee-sized drift the decision-8 identity pair measures.

UNITS AND ORIENTATION

BUCK and USDC are both 6-dec, so micro-USD per BUCK is 1e6 * price when
BUCK is token0 and 1e6 / price when BUCK is token1 (token0 is the lower
address; the sim's BUCK/USDC pool has USDC as token0, so a BUCK rally is a
FALLING pool price there -- BuckPoolInvestorAgent and the seeder carry the
same rule).  `active_reserves(chain, pool, a, b)` returns the pair in the
CALLER's token order, so callers keep writing (r_usdc, r_buck).

MEMO

`Chain.pool_state` (alberta_buck.sim.chain) memoizes (sqrtPriceX96, tick,
liquidity, token0, token1) per pool under exactly the balance cache's
lifecycle -- cleared on every send and deploy, and on every time warp -- so
a quote costs no more than the balance reads it replaces.  A bare
Web3Session without the memo falls back to an uncached read.
"""

from __future__ import annotations

from web3 import Web3

from alberta_buck.sim.chain import load_artifact

Q96 = 1 << 96
E6 = 10 ** 6

_POOL_ABI: list | None = None


def _pool_abi() -> list:
    global _POOL_ABI
    if _POOL_ABI is None:
        _POOL_ABI, _ = load_artifact("UniswapV3Pool")
    return _POOL_ABI


def _addr(token) -> str:
    """Lower-cased address of a web3 contract or an address string."""
    return str(token.address if hasattr(token, "address") else token).lower()


# -- chain reads ----------------------------------------------------------- #

def pool_contract(w3, pool: str):
    """(contract, token0, token1) of a V3 pool; the tokens are immutable."""
    c = w3.eth.contract(address=Web3.to_checksum_address(pool),
                        abi=_pool_abi())
    return c, c.functions.token0().call(), c.functions.token1().call()


def read_pool_state(w3, pool: str, contract=None) -> tuple[int, int, int, str, str]:
    """Uncached (sqrtPriceX96, tick, liquidity, token0, token1)."""
    c, t0, t1 = contract if contract is not None else pool_contract(w3, pool)
    s = c.functions.slot0().call()
    return int(s[0]), int(s[1]), int(c.functions.liquidity().call()), t0, t1


def pool_state(chain, pool: str) -> tuple[int, int, int, str, str]:
    """The memoized read when `chain` has one (alberta_buck.sim.chain.Chain),
    else an uncached read through chain.w3."""
    fn = getattr(chain, "pool_state", None)
    if fn is not None:
        return fn(pool)
    return read_pool_state(chain.w3, pool)


# -- pure conversions (unit-tested, no chain) ------------------------------ #

def virtual_reserves(sqrt_price_x96: int, liquidity: int) -> tuple[int, int]:
    """(x, y): the constant-product reserves the active liquidity presents at
    this price -- token0 = L 2^96 / sqrtP, token1 = L sqrtP / 2^96."""
    if sqrt_price_x96 <= 0 or liquidity <= 0:
        return 0, 0
    return (liquidity * Q96 // sqrt_price_x96,
            liquidity * sqrt_price_x96 // Q96)


def sqrt_price_to_usd6(sqrt_price_x96: int, buck_is_token0: bool) -> int:
    """Exact spot from slot0: micro-USD per BUCK (both tokens 6-dec)."""
    ratio = (sqrt_price_x96 / Q96) ** 2
    return int(round(E6 * ratio if buck_is_token0 else E6 / ratio))


def oriented(state, token_a, token_b) -> tuple[int, int]:
    """The virtual reserves of a pool `state` in (token_a, token_b) order."""
    sp, _tick, liq, t0, t1 = state
    r0, r1 = virtual_reserves(sp, liq)
    a, b = _addr(token_a), _addr(token_b)
    t0, t1 = t0.lower(), t1.lower()
    if a == t0 and b == t1:
        return r0, r1
    if a == t1 and b == t0:
        return r1, r0
    raise ValueError(f"{a}/{b} are not the tokens of the pool ({t0}/{t1})")


# -- the gauges ------------------------------------------------------------ #

def active_reserves(chain, pool: str, token_a, token_b) -> tuple[int, int]:
    """The pool's active liquidity at slot0 as constant-product reserves, in
    the caller's token order: the drop-in for the pair of balanceOf(pool)
    reads it replaces."""
    return oriented(pool_state(chain, pool), token_a, token_b)


def buck_usd6(chain, pool: str, buck) -> int:
    """Micro-USD per BUCK from the BUCK/USDC pool's slot0; 0 without a pool."""
    if not pool:
        return 0
    sp, _tick, _liq, t0, _t1 = pool_state(chain, pool)
    if sp <= 0:
        return 0
    return sqrt_price_to_usd6(sp, _addr(buck) == t0.lower())
