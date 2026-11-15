#!/usr/bin/env python3
"""Generate/check canonical monetary vectors with Python's unbounded integers.

The oracle does not call Rust, WASM or Solidity. Decimal independently derives
the one-second factor. A fixed table of squared factors defines the Solidity
rounding convention; arbitrary-precision products supply the division oracle.
All uint256 values are hexadecimal strings so no JSON consumer uses floats.
"""

import argparse
from decimal import Decimal, localcontext
import json
from pathlib import Path
import random

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "test/vectors/net-math-vectors.json"
MAX = 2**256 - 1
Q = 10**36
YEAR = 31_557_600


def factor_table():
    with localcontext() as ctx:
        ctx.prec = 120
        second = int((-Decimal("0.02") / YEAR).exp() * Q)
    factors = [second]
    for _ in range((1000 * YEAR).bit_length() - 1):
        factors.append(factors[-1] ** 2 // Q)
    return factors


FACTORS = factor_table()


def factor(seconds):
    value = Q
    for bit, power in enumerate(FACTORS):
        if seconds & (1 << bit):
            value = value * power // Q
    return value


def decay(amount, elapsed):
    if elapsed >= 9000 * YEAR:
        return 0
    chunks, remainder = divmod(elapsed, 1000 * YEAR)
    for _ in range(chunks):
        amount = amount * factor(1000 * YEAR) // Q
    return amount * factor(remainder) // Q


MAX_TICK = 887272
TICK_OFFSET = 2**23          # a tick travels as tick + 2^23: vector arguments are unsigned
TICK_FACTORS = [
    (0x2, 0xfff97272373d413259a46990580e213a), (0x4, 0xfff2e50f5f656932ef12357cf3c7fdcc),
    (0x8, 0xffe5caca7e10e4e61c3624eaa0941cd0), (0x10, 0xffcb9843d60f6159c9db58835c926644),
    (0x20, 0xff973b41fa98c081472e6896dfb254c0), (0x40, 0xff2ea16466c96a3843ec78b326b52861),
    (0x80, 0xfe5dee046a99a2a811c461f1969c3053), (0x100, 0xfcbe86c7900a88aedcffc83b479aa3a4),
    (0x200, 0xf987a7253ac413176f2b074cf7815e54), (0x400, 0xf3392b0822b70005940c7a398e4b70f3),
    (0x800, 0xe7159475a2c29b7443b29c7fa6e889d9), (0x1000, 0xd097f3bdfd2022b8845ad8f792aa5825),
    (0x2000, 0xa9f746462d870fdf8a65dc1f90e061e5), (0x4000, 0x70d869a156d2a1b890bb3df62baf32f7),
    (0x8000, 0x31be135f97d08fd981231505542fcfa6), (0x10000, 0x9aa508b5b7a84e1c677de54f3e99bc9),
    (0x20000, 0x5d6af8dedb81196699c329225ee604), (0x40000, 0x2216e584f5fa1ea926041bedfe98),
    (0x80000, 0x48a170391f7dc42444e8fa2)]


def sqrt_ratio_at_tick(tick):
    """Uniswap V3 TickMath.getSqrtRatioAtTick, in unbounded integers."""
    a = abs(tick)
    if a > MAX_TICK:
        raise ValueError("tick out of range")
    ratio = 0xfffcb933bd6fad37aa2d162d1a594001 if a & 1 else 1 << 128
    for bit, factor in TICK_FACTORS:
        if a & bit:
            ratio = ratio * factor >> 128
    if tick > 0:
        ratio = MAX // ratio
    return (ratio >> 32) + (1 if ratio % (1 << 32) else 0)


def checked(value):
    if value > MAX:
        raise ValueError("uint256 overflow")
    return value


def quote_at_tick(tick, base, base_is_token0):
    """OracleLibrary.getQuoteAtTick: token1 per token0 is 1.0001^tick."""
    sqrt = sqrt_ratio_at_tick(tick)
    ratio, one = (sqrt * sqrt, 1 << 192) if sqrt <= 2**128 - 1 else (checked(sqrt * sqrt // (1 << 64)), 1 << 128)
    return checked(ratio * base // one if base_is_token0 else one * base // ratio)


def expected(op, args, round_up):
    # CarryRatio (src/lib/CarryRatio.sol): an account's (gross, net) and an amount.
    if op == "carryToNet":
        gross, net, nominal = args
        return nominal if gross == 0 or gross == net else nominal * net // gross
    if op == "carryToNetScaled":
        gross, net, nominal = args
        scaled = checked(nominal * Q)
        return scaled if gross == 0 else scaled * net // gross
    if op == "carryToNominal":
        gross, net, value = args
        if gross == 0 or gross == net:
            return value
        if net == 0:
            raise ValueError("no net value")
        return -(-value * gross // net)
    if op == "quoteAtTick":
        tick, base, token0 = args
        return quote_at_tick(tick - TICK_OFFSET, base, bool(token0))
    if op == "decay":
        return decay(*args)
    if op == "fundNetAfter":
        issuance, fund, elapsed = args
        if fund > issuance:
            raise ValueError("fund exceeds issuance")
        return issuance - decay(issuance - fund, elapsed)
    if op == "targetRelief":
        principal, seconds, reduction = args
        if not principal or reduction > principal:
            raise ValueError("invalid principal")
        return min(reduction, (seconds * reduction // principal) // (50 * YEAR))
    a, b, denominator = args
    if not denominator:
        raise ValueError("division by zero")
    quotient, remainder = divmod(a * b, denominator)
    return quotient + int(round_up and bool(remainder))


def vectors():
    rows = []

    def add(name, op, args, round_up=False):
        assert all(type(a) is int and 0 <= a <= MAX for a in args)
        value, error = 0, ""
        try:
            value = expected(op, args, round_up)
            if value > MAX:
                raise ValueError("uint256 overflow")
        except ValueError as exc:
            error = str(exc)
        rows.append(dict(name=name, op=op, args=list(map(hex, args)), roundUp=round_up,
                         value=hex(value if not error else 0), error=error))

    ages = [0, 1, YEAR, 49 * YEAR, 50 * YEAR, 51 * YEAR, 1000 * YEAR - 1,
            1000 * YEAR, 1000 * YEAR + 1, 8000 * YEAR, 9000 * YEAR - 1, 9000 * YEAR, MAX]
    for amount in [0, 1, 10**6, Q, 10**6 * Q, 2**200, MAX]:
        for age in ages:
            add(f"decay-{amount}-{age}", "decay", [amount, age])
    for issuance in [100 * 10**6 * Q, MAX]:
        for fund in [0, issuance // 10, issuance // 2, issuance]:
            for age in [0, YEAR, 50 * YEAR, 9000 * YEAR]:
                add(f"fund-{issuance}-{fund}-{age}", "fundNetAfter", [issuance, fund, age])
    add("fund-too-large", "fundNetAfter", [99, 100, YEAR])
    for principal in [1, 100 * 10**6, MAX]:
        for seconds in [0, min(MAX, principal * YEAR), min(MAX, principal * 50 * YEAR), MAX]:
            for reduction in sorted({0, 1, principal // 3, principal}):
                add(f"relief-{principal}-{seconds}-{reduction}", "targetRelief", [principal, seconds, reduction])
    add("zero-principal", "targetRelief", [0, 0, 0])
    add("excess-reduction", "targetRelief", [100, 100 * YEAR, 101])
    boundary = [(0, 0, 0), (0, MAX, 1), (1, 1, MAX), (MAX, MAX, 0),
                (MAX, MAX, MAX), (MAX, MAX, MAX - 1), (MAX, MAX, 1),
                (MAX - 1, MAX - 1, MAX - 2), (Q, Q, 3), (2**255, 2, 2),
                (2**128, 2**128, MAX), (2**200, 2**200, 2**145)]
    rng = random.Random(0xB0C256)
    for i in range(64):
        a, b = rng.getrandbits(256), rng.getrandbits(256)
        # Include both fitting full-width quotients and deliberate overflow.
        den = rng.randint(max(1, (a * b) >> 256) if i % 2 else 1, MAX)
        boundary.append((a, b, den))
    for i, args in enumerate(boundary):
        for up in [False, True]:
            add(f"muldiv-{i}-{'ceil' if up else 'floor'}", "mulDiv", args, up)
    # The carry: an account's gross and net (both x 1e36) and an amount (raw, or a price).
    for gross in [0, Q, 100 * 10**6 * Q, 2**200, MAX]:
        for net in sorted({0, gross // 2, gross * 98 // 100, gross}):
            for amount in [0, 1, 10**6, 123_456_789, 2**200, MAX]:
                for op in ("carryToNet", "carryToNetScaled", "carryToNominal"):
                    add(f"{op}-{gross}-{net}-{amount}", op, [gross, net, amount])
    # A pool's price at a tick (both orientations), and the quote's limits.
    for tick in [-MAX_TICK - 1, -MAX_TICK, -MAX_TICK + 1, -500_000, -276_325, -100, -1, 0, 1, 100,
                 276_324, 500_000, MAX_TICK - 1, MAX_TICK, MAX_TICK + 1]:
        for base in [1, 10**6, 10**18, 2**128 - 1]:
            for token0 in (1, 0):
                add(f"quote-{tick}-{base}-{token0}", "quoteAtTick", [tick + TICK_OFFSET, base, token0])
    return dict(format=1, model="Buck/compounded-cash/per-facility-jubilee/v1", scale=hex(Q), count=len(rows), cases=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="replace the canonical vector file")
    args = parser.parse_args()
    content = json.dumps(vectors(), indent=2) + "\n"
    if args.write:
        DEST.write_text(content)
    elif not DEST.exists() or DEST.read_text() != content:
        parser.exit(1, "net math vectors differ; review and regenerate with make core-net-math-vectors\n")
    print(f"{len(vectors()['cases'])} canonical net math vectors {'written' if args.write else 'verified'}")


if __name__ == "__main__":
    main()
