"""Explicit, auditable historical initialization for BasketSignals.

Daily prices are supplied in a common numeraire. Differential ticks cancel
that numeraire; common-mode history additionally assumes one unit of it per
BUCK. Bootstrap is a governance input, not an on-chain proof of past prices.
"""
from __future__ import annotations

import hashlib
import json
import math

WAD = 10**18
WINDOWS = (5, 10, 20, 40, 80, 160, 1280)
COMMON_WINDOWS = (5, 10, 20, 40, 80, 160, 320)


def trunc_div(value, divisor):
    return value // divisor if value >= 0 else -((-value) // divisor)


def historical_state(rows, mean_prices, decimals, buck_is_token0, basket_amounts):
    n = len(mean_prices)
    if not rows or not n or any(len(v) != n for v in (decimals, buck_is_token0, basket_amounts)):
        raise ValueError("history and means must cover every constituent")
    days = [r["day"] for r in rows]
    if days != list(range(-len(rows), 0)):
        raise ValueError("history must be consecutive daily observations ending at day -1")

    def relative(prices):
        if len(prices) != n or any(not math.isfinite(p) or p <= 0 for p in prices):
            raise ValueError("historical prices must be finite and positive for every constituent")
        oriented = []
        for p, dec, b0 in zip(prices, decimals, buck_is_token0):
            # Same orientation/decimal offset as the V3 pool's integer tick.
            log_tick = math.log(p * 10**(6 - dec)) / math.log(1.0001)
            tick = math.floor(-log_tick if b0 else log_tick)
            if not -887272 <= tick <= 887272:
                raise ValueError("historical price lies outside the V3 tick domain")
            oriented.append((-tick if b0 else tick) * WAD)
        mean = trunc_div(sum(oriented), n)
        return [t - mean for t in oriented]

    def common(prices):
        return sum(a * round(p * 10**6) // WAD for a, p in zip(basket_amounts, prices)) - WAD

    # Start from supplied historical means, not from the first phase of the
    # historical trace. Then process only genuine pre-run samples in order.
    mean_relative = relative(mean_prices)
    levels = [[x] * 7 for x in mean_relative]
    common_levels = [common(mean_prices)] * 7
    for row in rows:
        x = relative(row["prices"])
        for i in range(n):
            for k, window in enumerate(WINDOWS):
                levels[i][k] += trunc_div((x[i] - levels[i][k]) * 2, window + 1)
        c = common(row["prices"])
        for k, window in enumerate(COMMON_WINDOWS):
            common_levels[k] += trunc_div((c - common_levels[k]) * 2, window + 1)
    source = dict(schema=1, rows=rows, mean_prices=mean_prices, decimals=decimals,
                  buck_is_token0=buck_is_token0, basket_amounts=[str(x) for x in basket_amounts],
                  windows=WINDOWS, common_windows=COMMON_WINDOWS,
                  levels=[[str(x) for x in v] for v in levels],
                  common_levels=[str(x) for x in common_levels])
    commitment = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return levels, common_levels, {**source, "sha256": commitment,
                                   "mean_relative_ticks": [x / WAD for x in mean_relative]}


def initialize_history(chain, signals, basket, governance, rows, mean_prices):
    constituents = [basket.functions.constituents(i).call()
                    for i in range(basket.functions.constituentsLength().call())]
    levels, common, source = historical_state(rows, mean_prices,
        [c[1] for c in constituents], [c[8] for c in constituents], [c[2] for c in constituents])
    through_day = int(chain.w3.eth.get_block("latest")["timestamp"]) // 86400 - 1
    chain.send(signals.functions.initializeHistory(levels, common, through_day, len(rows),
               bytes.fromhex(source["sha256"])), sender=governance)
    return {**source, "through_day": through_day}
