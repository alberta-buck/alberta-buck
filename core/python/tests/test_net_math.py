"""Canonical native math against an independent unbounded-integer oracle.

The dedicated Make acceptance target builds the extension before this test;
missing/stale bindings are failures, not skipped arithmetic evidence.
"""
import json
from pathlib import Path
import random

import pytest

from buck_core import buck_math as math

Q = 10**36
YEAR = 31_557_600
MAX = 2**256 - 1
VECTORS = Path(__file__).resolve().parents[3] / "test/vectors/net-math-vectors.json"


def test_shared_canonical_integer_vectors():
    data = json.loads(VECTORS.read_text())
    assert data["format"] == 1
    assert len(data["cases"]) == data["count"] >= 300
    functions = dict(decay=math.net_decay, fundNetAfter=math.net_fund_after,
                     targetRelief=math.net_target_relief, mulDiv=math.mul_div_256,
                     carryToNet=math.carry_to_net, carryToNetScaled=math.carry_to_net_scaled,
                     carryToNominal=math.carry_to_nominal, quoteAtTick=math.quote_at_tick)
    for row in data["cases"]:
        args = [int(a, 16) for a in row["args"]]
        if row["op"] == "mulDiv":
            args.append(row["roundUp"])
        if row["op"] == "quoteAtTick":      # a tick travels as tick + 2^23; then the base, and token0
            args = [args[0] - 2**23, args[1], bool(args[2])]
        if row["error"]:
            with pytest.raises(ValueError, match=row["error"]):
                functions[row["op"]](*args)
        else:
            result = functions[row["op"]](*args)
            assert type(result) is int
            assert result == int(row["value"], 16), row["name"]


def test_full_width_random_products_obey_exact_quotient_bounds():
    rng = random.Random(0x256512)
    for _ in range(1024):
        a, b = rng.getrandbits(256), rng.getrandbits(256)
        denominator = rng.randint(max(1, a * b >> 256), MAX)
        floor, remainder = divmod(a * b, denominator)
        assert math.mul_div_256(a, b, denominator) == floor
        assert floor * denominator <= a * b < (floor + 1) * denominator
        ceiling = floor + bool(remainder)
        if ceiling <= MAX:
            assert math.mul_div_256(a, b, denominator, True) == ceiling
        else:
            with pytest.raises(ValueError, match="overflow"):
                math.mul_div_256(a, b, denominator, True)


def test_cash_and_fund_conserve_across_checkpoints_without_linear_wallet_expiry():
    issued, outside, fund = 100 * 10**6 * Q, 100 * 10**6 * Q, 0
    for _ in range(50 * 12):
        elapsed = YEAR // 12
        new_outside = math.net_decay(outside, elapsed)
        fund = math.net_fund_after(issued, fund, elapsed)
        outside = new_outside
        assert outside + fund == issued
    assert 36 * 10**6 * Q < outside < 37 * 10**6 * Q
    assert math.net_target_relief(100 * 10**6, 100 * 10**6 * 50 * YEAR, 100 * 10**6) == 100 * 10**6
    # A Carrying pool transfers floor(net * nominal / gross), while an exact
    # net debit needs the ceiling. These differ from the old floored-fee rule.
    assert math.mul_div_256(8 * Q, 1, 10) == 8 * Q // 10
    assert math.mul_div_256(1, 10, 8, True) == 2


@pytest.mark.parametrize("value", [-1, 2**256])
def test_binding_rejects_out_of_range_inputs_without_truncation(value):
    with pytest.raises(OverflowError):
        math.net_decay(value, 1)
    with pytest.raises(OverflowError):
        math.net_decay(1, value)


@pytest.mark.parametrize("value", [1.0, "1", True])
def test_binding_requires_integer_units(value):
    with pytest.raises(TypeError):
        math.net_decay(value, 1)


def test_binding_uses_the_entire_256_bit_range_in_both_directions():
    assert math.NET_SCALE == Q
    for value in [0, 1, 2**128 - 1, 2**128, 2**200 + 123, MAX]:
        assert math.net_decay(value, 0) == value
        assert math.mul_div_256(value, MAX, MAX) == value
    assert math.net_decay(MAX, MAX) == 0


def test_integer_subclasses_cannot_replace_the_lossless_conversion():
    class IntWithAnotherEncoding(int):
        def to_bytes(self, *args, **kwargs):
            return bytes(32)

    amount = IntWithAnotherEncoding(2**200 + 123)
    assert math.net_decay(amount, 0) == int(amount)
    with pytest.raises(TypeError):
        math.mul_div_256(1, 1, 1, "ceil")
