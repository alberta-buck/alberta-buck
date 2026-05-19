"""Per-day state capture -> JSON in the existing routing-sim schema.

Schema is byte-compatible with test/stabilizer-routing-op47/
test_routing_sim_plot.py so the established plot renders this unchanged:
  {"tokens":[sym...], "frames":[{day, refUsd[N], spotUsdc[N], spotBuck[N],
   basketVal, buckK, supply, directTrades, cycleTrades, aggPnl}, ...]}
"""

from __future__ import annotations

import json
from pathlib import Path

from web3 import Web3

from alberta_buck.sim.chain import load_artifact

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "test" / "vectors" / "routing-sim.json"
Q96 = 1 << 96
Q128 = 1 << 128
MASK256 = (1 << 256) - 1


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
        self._pool_abi, _ = load_artifact("UniswapV3Pool")
        # address -> day-0 USDC-micro value per RAW unit helper key
        self._kind = {Web3.to_checksum_address(d.usdc.address): ("usdc", 0),
                      Web3.to_checksum_address(d.buck.address): ("buck", 0)}
        for i, tc in enumerate(d.tokens):
            self._kind[Web3.to_checksum_address(tc.address)] = ("tok", i)
        self._lp_cap: dict | None = None     # group -> capital (USDC, day0)

    # -- LP (Uniswap V3 position) accounting --------------------------- #

    def _raw_usd0(self, addr: str, raw: int) -> int:
        """USDC-micro value at day-0 prices of `raw` units of token `addr`.
        USDC: 1:1.  BUCK: 1:1 (1 BUCK == 1 USDC at t0 by construction).
        TOKEN i: raw * ref0_i / 10**dec_i."""
        kind, i = self._kind[Web3.to_checksum_address(addr)]
        if kind in ("usdc", "buck"):
            return raw
        return raw * self.s.prices.ref(i, 0) // (10 ** self.d.dec[i])

    def _lp_groups(self) -> dict:
        """Per-group (usdc/buck/ub) cumulative LP fee income and deployed
        capital, both in USDC-micro at day-0 prices.  Full-range positions:
        uncollected fee ~= L*(feeGrowthGlobal - feeGrowthInsideLast)/2**128
        + tokensOwed (no poke needed); deployed capital = the L-backed
        redeemable amounts at the *first* observed price."""
        d = self.d
        fees = {"usdc": 0, "buck": 0, "ub": 0}
        cap = {"usdc": 0, "buck": 0, "ub": 0}
        for addr, owner, lo, hi, grp in d.pool_meta:
            p = d.w3.eth.contract(address=addr, abi=self._pool_abi)
            t0 = p.functions.token0().call()
            t1 = p.functions.token1().call()
            key = Web3.solidity_keccak(
                ["address", "int24", "int24"],
                [Web3.to_checksum_address(owner), lo, hi])
            L, fi0, fi1, owed0, owed1 = p.functions.positions(key).call()
            fg0 = p.functions.feeGrowthGlobal0X128().call()
            fg1 = p.functions.feeGrowthGlobal1X128().call()
            unc0 = L * ((fg0 - fi0) & MASK256) // Q128 + owed0
            unc1 = L * ((fg1 - fi1) & MASK256) // Q128 + owed1
            fees[grp] += self._raw_usd0(t0, unc0) + self._raw_usd0(t1, unc1)
            sp = p.functions.slot0().call()[0]
            if sp:
                a0 = L * Q96 // sp           # full-range redeemable amounts
                a1 = L * sp // Q96
                cap[grp] += self._raw_usd0(t0, a0) + self._raw_usd0(t1, a1)
        return {g: (fees[g], cap[g]) for g in fees}

    def agg_value(self, agents, day) -> int:
        """Total agent portfolio in USDC, valued at *day-0* prices so this
        is REALIZED economic P&L (arb edge + token accumulation), not
        mark-to-market noise from the day's GBM-moved CSV reference."""
        d = self.d
        v = 0
        for ag in agents:
            if not getattr(ag, "is_eoa", False) or ag.account is None:
                continue
            v += _bal(d.usdc, ag.address)
            for i, tc in enumerate(d.tokens):
                v += _bal(tc, ag.address) * self.s.prices.ref(i, 0) // (10 ** d.dec[i])
        return v

    def capture(self, day, ctr, agents, init_val) -> None:
        d = self.d
        ref, su, sb = [], [], []
        for i, tc in enumerate(d.tokens):
            ref.append(self.s.prices.ref(i, day))
            su.append(_implied(d, d.pool_usdc[i], tc, d.dec[i], d.usdc))
            sb.append(_implied(d, d.pool_buck[i], tc, d.dec[i], d.buck))
        # Floating BUCK/USDC pool: implied USDC-micro per 1 BUCK.
        buck_usd = 0
        if getattr(d, "pool_ub", ""):
            ru = _bal(d.usdc, d.pool_ub)
            rb = _bal(d.buck, d.pool_ub)
            if rb:
                buck_usd = ru * 1_000_000 // rb
        try:
            bv = int(d.basket.functions.basketValueInBuck().call())
        except Exception:
            bv = 0
        try:
            bk = int(d.kctrl.functions.buckK().call())
        except Exception:
            bk = 0
        lg = self._lp_groups()
        if self._lp_cap is None:                       # freeze capital basis
            self._lp_cap = {g: lg[g][1] for g in lg}
        lp = {g: [lg[g][0], self._lp_cap[g]] for g in lg}
        self.frames.append({
            "invested": init_val,                      # arb capital (USDC,d0)
            "lp": lp,                                  # group: [feeUsd, capUsd]
            "day": day,
            "refUsd": ref,
            "spotUsdc": su,
            "spotBuck": sb,
            "basketVal": bv,
            "buckK": bk,
            "supply": int(d.buck.functions.totalSupply().call()),
            "directTrades": ctr["directTrades"],
            "cycleTrades": ctr["cycleTrades"],
            "ubTrades": ctr.get("ubTrades", 0),
            "buckUsd": buck_usd,
            "aggPnl": self.agg_value(agents, day) - init_val,
        })

    def write(self, path=None) -> Path:
        p = Path(path) if path else DEFAULT_OUT
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"tokens": self.tokens, "frames": self.frames}))
        return p
