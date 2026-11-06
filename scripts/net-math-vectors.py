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


def expected(op, args, round_up):
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
