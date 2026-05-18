"""Per-day state capture -> JSON in the existing routing-sim schema.

Schema is byte-compatible with test/stabilizer-routing-op47/
test_routing_sim_plot.py so the established plot renders this unchanged:
  {"tokens":[sym...], "frames":[{day, refUsd[N], spotUsdc[N], spotBuck[N],
   basketVal, buckK, supply, directTrades, cycleTrades, aggPnl}, ...]}
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "test" / "vectors" / "routing-sim.json"


def _bal(c, who):
    return c.functions.balanceOf(who).call()


def _implied(d, pool, token_c, dec, quote_c):
    """Implied quote-units per 1 whole token, from live pool reserves."""
    rt = _bal(token_c, pool)
    rq = _bal(quote_c, pool)
    if rt == 0:
        return 0
    return rq * (10 ** dec) // rt


class Snapshotter:
    def __init__(self, d, scenario):
        self.d = d
        self.s = scenario
        self.frames: list[dict] = []
        self.tokens = [t[0] for t in scenario.tokens]

    def agg_value(self, agents, day) -> int:
        d = self.d
        v = 0
        for ag in agents:
            if not getattr(ag, "is_eoa", False) or ag.account is None:
                continue
            v += _bal(d.usdc, ag.address)
            for i, tc in enumerate(d.tokens):
                v += _bal(tc, ag.address) * self.s.prices.ref(i, day) // (10 ** d.dec[i])
        return v

    def capture(self, day, ctr, agents, init_val) -> None:
        d = self.d
        ref, su, sb = [], [], []
        for i, tc in enumerate(d.tokens):
            ref.append(self.s.prices.ref(i, day))
            su.append(_implied(d, d.pool_usdc[i], tc, d.dec[i], d.usdc))
            sb.append(_implied(d, d.pool_buck[i], tc, d.dec[i], d.buck))
        try:
            bv = int(d.basket.functions.basketValueInBuck().call())
        except Exception:
            bv = 0
        try:
            bk = int(d.kctrl.functions.buckK().call())
        except Exception:
            bk = 0
        self.frames.append({
            "day": day,
            "refUsd": ref,
            "spotUsdc": su,
            "spotBuck": sb,
            "basketVal": bv,
            "buckK": bk,
            "supply": int(d.buck.functions.totalSupply().call()),
            "directTrades": ctr["directTrades"],
            "cycleTrades": ctr["cycleTrades"],
            "aggPnl": self.agg_value(agents, day) - init_val,
        })

    def write(self, path=None) -> Path:
        p = Path(path) if path else DEFAULT_OUT
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"tokens": self.tokens, "frames": self.frames}))
        return p
