"""Unit tests for the slot0 gauges (WAVE3.org decision 8, wp/d8-slot0, L1).

The pure conversions in both token orderings; the within-range quote's
exactness against the V3 closed form; the artefact itself (an out-of-range
position moves the balance ratio and not the gauge); and the memo's
lifecycle on the Chain handle.
"""

import math

import pytest
from web3 import Web3

from alberta_buck.sim import gauge
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.gauge import (
    E6, Q96, active_reserves, buck_usd6, oriented, sqrt_price_to_usd6,
    virtual_reserves,
)
from alberta_buck.sim.router import FEE_DEN, quote_hop, quote_path
from alberta_buck.sim.seeder_agent import sqrt_at_tick, tick_to_usd6, usd6_to_tick

USDC = Web3.to_checksum_address("0x" + "aa" * 20)
BUCK = Web3.to_checksum_address("0x" + "bb" * 20)
TOK = Web3.to_checksum_address("0x" + "dd" * 20)
POOL_UB = "0x" + "cc" * 20
POOL_TB = "0x" + "ee" * 20


def _sqrt_x96(price: float) -> int:
    """sqrtPriceX96 for a pool price (token1 per token0)."""
    return int(math.sqrt(price) * Q96)


def _sqrt_for_usd6(usd6: int, buck_is_token0: bool) -> int:
    return _sqrt_x96(usd6 / E6 if buck_is_token0 else E6 / usd6)


class _Chain:
    """A chain handle with the pool_state memo's shape and no chain."""

    def __init__(self, states: dict):
        self.states = states

    def pool_state(self, pool):
        return self.states[pool.lower()]


def _ub_chain(usd6: int, liq: int, buck_is_token0: bool) -> _Chain:
    t0, t1 = (BUCK, USDC) if buck_is_token0 else (USDC, BUCK)
    return _Chain({POOL_UB: (_sqrt_for_usd6(usd6, buck_is_token0), 0, liq,
                             t0, t1)})


# -- the spot conversion --------------------------------------------------- #

@pytest.mark.parametrize("b0", [True, False])
def test_sqrt_price_par_both_orientations(b0):
    assert sqrt_price_to_usd6(Q96, b0) == 1_000_000


def test_sqrt_price_orientation_flips_with_token_order():
    sp = _sqrt_x96(1.21)                       # pool price 1.21 (sqrt 1.1)
    assert sqrt_price_to_usd6(sp, True) == 1_210_000     # USDC per BUCK
    assert sqrt_price_to_usd6(sp, False) == 826_446      # 1e6 / 1.21


@pytest.mark.parametrize("b0", [True, False])
@pytest.mark.parametrize("usd6", [900_000, 1_082_793, 1_414_440])
def test_sqrt_price_agrees_with_the_seeder_tick_helpers(b0, usd6):
    t = usd6_to_tick(usd6, b0)
    assert abs(sqrt_price_to_usd6(sqrt_at_tick(t), b0) - tick_to_usd6(t, b0)) <= 1


# -- virtual reserves ------------------------------------------------------ #

def test_virtual_reserves_at_par_are_L_and_L():
    assert virtual_reserves(Q96, 10 ** 18) == (10 ** 18, 10 ** 18)


def test_virtual_reserves_carry_the_price_and_the_invariant():
    L = 10 ** 18
    r0, r1 = virtual_reserves(_sqrt_x96(1.21), L)
    assert abs(r1 / r0 - 1.21) < 1e-12               # y / x = price
    assert abs(r0 * r1 / L ** 2 - 1.0) < 1e-12       # x * y = L^2
    assert abs(r0 - L / 1.1) < 2 and abs(r1 - L * 1.1) < 2


def test_virtual_reserves_degenerate():
    assert virtual_reserves(0, 10 ** 18) == (0, 0)
    assert virtual_reserves(Q96, 0) == (0, 0)


# -- orientation ----------------------------------------------------------- #

def test_oriented_returns_the_callers_order_both_orderings():
    sp, L = _sqrt_x96(1.21), 10 ** 15
    r0, r1 = virtual_reserves(sp, L)
    usdc_first = (sp, 0, L, USDC, BUCK)
    assert oriented(usdc_first, USDC, BUCK) == (r0, r1)
    assert oriented(usdc_first, BUCK, USDC) == (r1, r0)
    buck_first = (sp, 0, L, BUCK, USDC)
    assert oriented(buck_first, USDC, BUCK) == (r1, r0)
    assert oriented(buck_first, BUCK, USDC) == (r0, r1)


def test_oriented_accepts_contract_like_tokens_and_rejects_foreign_ones():
    class _C:
        def __init__(self, a):
            self.address = a
    st = (Q96, 0, 10 ** 12, USDC, BUCK)
    assert oriented(st, _C(USDC), _C(BUCK)) == (10 ** 12, 10 ** 12)
    with pytest.raises(ValueError):
        oriented(st, USDC, TOK)


# -- the gauges ------------------------------------------------------------ #

@pytest.mark.parametrize("b0", [True, False])
@pytest.mark.parametrize("usd6", [826_446, 1_000_000, 1_082_793])
def test_active_reserve_ratio_is_the_slot0_price(b0, usd6):
    ch = _ub_chain(usd6, 10 ** 13, b0)
    ru, rb = active_reserves(ch, POOL_UB, USDC, BUCK)
    spot = buck_usd6(ch, POOL_UB, BUCK)
    assert abs(spot - usd6) <= 1
    assert abs(ru * E6 // rb - spot) <= 1
    # And the pair in the other order is the same pair, swapped.
    assert active_reserves(ch, POOL_UB, BUCK, USDC) == (rb, ru)


def test_buck_usd6_without_a_pool_is_zero():
    assert buck_usd6(_ub_chain(1_000_000, 10 ** 12, False), "", BUCK) == 0


@pytest.mark.parametrize("b0", [True, False])
def test_out_of_range_position_moves_the_balances_not_the_gauge(b0):
    """The WP-8 day-1 artefact, in numbers: a USDC-funded range placed
    entirely beyond the price adds its USDC to the pool's balance and
    nothing to slot0 or to liquidity().  The balance ratio then reads a
    price move that never happened; the gauge does not."""
    ch = _ub_chain(1_082_793, 10 ** 13, b0)
    ru, rb = active_reserves(ch, POOL_UB, USDC, BUCK)
    seeded_usdc = 15_000_000 * E6                    # the seeder's budget
    balance_ratio = (ru + seeded_usdc) * E6 // rb
    assert balance_ratio > 1_300_000                 # what the old gauge saw
    assert abs(buck_usd6(ch, POOL_UB, BUCK) - 1_082_793) <= 1


# -- the within-range quote ------------------------------------------------ #

def _v3_out_token0_in(sp: int, L: int, dx: int) -> int:
    sp_new = L * sp * Q96 // (L * Q96 + dx * sp)
    return L * (sp - sp_new) // Q96


def _v3_out_token1_in(sp: int, L: int, dy: int) -> int:
    sp_new = sp + dy * Q96 // L
    return L * Q96 * (sp_new - sp) // (sp * sp_new)


@pytest.mark.parametrize("b0", [True, False])
@pytest.mark.parametrize("fee", [0, 500, 3000])
def test_quote_hop_is_the_v3_within_range_swap(b0, fee):
    L, sp = 10 ** 14, _sqrt_for_usd6(1_082_793, b0)
    ch = _ub_chain(1_082_793, L, b0)
    amt = 250_000 * E6
    eff = amt * (FEE_DEN - fee) // FEE_DEN
    usdc_is0 = not b0
    # USDC in, BUCK out.
    got = quote_hop(None, [], POOL_UB, USDC, BUCK, amt, fee, chain=ch)
    want = (_v3_out_token0_in if usdc_is0 else _v3_out_token1_in)(sp, L, eff)
    assert abs(got - want) <= 2
    # BUCK in, USDC out.
    got = quote_hop(None, [], POOL_UB, BUCK, USDC, amt, fee, chain=ch)
    want = (_v3_out_token1_in if usdc_is0 else _v3_out_token0_in)(sp, L, eff)
    assert abs(got - want) <= 2
    # Prices: near par a 250k trade on 1e14 of L costs well under 1%.
    assert 0.99 < got / amt < 1.10


def test_quote_hop_edges():
    ch = _ub_chain(1_000_000, 10 ** 12, False)
    assert quote_hop(None, [], POOL_UB, USDC, BUCK, 0, 500, chain=ch) == 0
    empty = _Chain({POOL_UB: (Q96, 0, 0, USDC, BUCK)})
    assert quote_hop(None, [], POOL_UB, USDC, BUCK, E6, 500, chain=empty) == 0
    with pytest.raises(ValueError):
        quote_hop(None, [], POOL_UB, TOK, BUCK, E6, 500, chain=ch)


def test_quote_path_chains_hops_through_the_memo():
    ch = _Chain({
        POOL_UB: (_sqrt_for_usd6(1_000_000, False), 0, 10 ** 13, USDC, BUCK),
        POOL_TB: (_sqrt_x96(2.0), 0, 10 ** 13, BUCK, TOK),
    })
    amt = 1_000 * E6
    hops = [(POOL_UB, USDC, BUCK, 500), (POOL_TB, BUCK, TOK, 3000)]
    mid = quote_hop(None, [], POOL_UB, USDC, BUCK, amt, 500, chain=ch)
    end = quote_hop(None, [], POOL_TB, BUCK, TOK, mid, 3000, chain=ch)
    assert quote_path(None, [], hops, amt, None, chain=ch) == end
    assert end > 0
    # The old positional call shape (a balance_of callable) still works.
    assert quote_path(None, [], hops, amt, lambda t, h: 0, chain=ch) == end


# -- the Chain memo -------------------------------------------------------- #

class _Fn:
    def __init__(self, v):
        self.v = v

    def call(self):
        return self.v() if callable(self.v) else self.v


class _Functions:
    def __init__(self, st):
        self.st = st

    def slot0(self):
        return _Fn(lambda: (self.st["sp"], self.st["tick"], 0, 1, 1, 0, True))

    def liquidity(self):
        return _Fn(lambda: self.st["liq"])

    def token0(self):
        return _Fn(USDC)

    def token1(self):
        return _Fn(BUCK)


class _Contract:
    def __init__(self, st):
        self.functions = _Functions(st)


class _Eth:
    chain_id = 31337

    def __init__(self, st):
        self.st = st
        self.contracts = 0

    def contract(self, address=None, abi=None):
        self.contracts += 1
        return _Contract(self.st)


class _W3:
    def __init__(self, st):
        self.eth = _Eth(st)


def test_chain_pool_state_memo_follows_the_balance_cache(monkeypatch):
    monkeypatch.setattr(gauge, "_pool_abi", lambda: [])
    st = {"sp": Q96, "tick": 0, "liq": 10 ** 12}
    chain = Chain(_W3(st), "0x" + "11" * 20)
    assert chain.pool_state(POOL_UB) == (Q96, 0, 10 ** 12, USDC, BUCK)
    st["sp"], st["tick"], st["liq"] = _sqrt_x96(1.21), 1906, 5 * 10 ** 11
    # Memoized: no tx or warp has cleared the cache.
    assert chain.pool_state(POOL_UB) == (Q96, 0, 10 ** 12, USDC, BUCK)
    chain.clear_balance_cache()
    assert chain.pool_state(POOL_UB) == (_sqrt_x96(1.21), 1906, 5 * 10 ** 11,
                                         USDC, BUCK)
    # One contract object per pool, whatever the number of reads.
    assert chain.w3.eth.contracts == 1
    # The gauge reads through the memo ...
    ru, rb = active_reserves(chain, POOL_UB, USDC, BUCK)
    assert (ru, rb) == virtual_reserves(_sqrt_x96(1.21), 5 * 10 ** 11)
    assert buck_usd6(chain, POOL_UB, BUCK) == 826_446       # USDC is token0


def test_pool_state_falls_back_to_an_uncached_read(monkeypatch):
    monkeypatch.setattr(gauge, "_pool_abi", lambda: [])
    st = {"sp": Q96, "tick": 0, "liq": 10 ** 12}

    class _Bare:                      # a session without the memo
        def __init__(self):
            self.w3 = _W3(st)

    bare = _Bare()
    assert gauge.pool_state(bare, POOL_UB)[0] == Q96
    st["sp"] = _sqrt_x96(4.0)
    assert gauge.pool_state(bare, POOL_UB)[0] == _sqrt_x96(4.0)
    assert quote_hop(bare.w3, [], POOL_UB, USDC, BUCK, E6, 0) > 0
