"""The markout / LVR ledger -- CARRY-CONVEXITY.org 8.1, WAVE3.org WP-1.

Every agent act that moves a TOKEN/BUCK basket pool's reserves (or the
floating BUCK/USDC pool's) is booked as ONE trade against that pool's LP
-- the BuckBasket for the basket pools, SimLP (third parties) for
BUCK/USDC -- tagged with the acting agent's class.  Each trade is later
marked out at horizons D against

  (a) the pool's own price at t+D            -> the TOTAL markout, and
  (b) the par-referenced common-mode price
      p_exec * bvib(t+D) / bvib(t)            -> the COMMON-MODE markout:
      what the trade would have cost the LP had only BUCK's own valuation
      moved (reversion to par at the horizon);

the DIFFERENTIAL markout is the difference.  Negative markout is adverse
selection realized against the LP.  Fee income is the taker's input
times the pool's fee tier, valued in the pool's quote unit.

The ledger is pure Python (no chain access); `PoolProbe` is the small
chain-facing helper that reads reserves and turns balance deltas into
swap records.  Liquidity adds/removes move both reserves the same way
and are NOT swaps, so they are never recorded; agent classes whose pool
effects are custody flows (the direct-mint depositors) are skipped.

Units.  BUCK and USDC are 6-dec; TOKEN raw in native decimals; prices
are 6-dec quote per WHOLE base unit; bvib is a float.  Basket pools:
base = TOKEN, quote = BUCK.  BUCK/USDC: base = BUCK, quote = USDC.

Signs.  q_lp is the LP's base-side flow (+ = the LP received base, i.e.
the taker sold TOKEN / sold BUCK).  markout = q_lp * (p_ref - p_exec):
an LP that bought base which then cheapened books a loss.
"""

from __future__ import annotations

from dataclasses import dataclass, field

E6 = 10 ** 6
E18 = 10 ** 18

# Classes whose pool effects are custody (liquidity in/out through the
# basket's own deposit/redeem path), not takes against the LP.
LP_SIDE_CLASSES = frozenset({
    "DirectMintAgent", "DirectMintBuckAgent", "ArrivingDMAgent",
    "BootstrapDMAgent",
})

UB = "ub"


def is_lp_side(cls: str) -> bool:
    return cls.split(":", 1)[0] in LP_SIDE_CLASSES


def swap_from_delta(d_base_raw: int, d_quote: int, dec: int,
                    fee_frac: float):
    """Turn a pool's reserve deltas over one act into a swap record, or
    None when the deltas are not a swap (same sign: liquidity in/out; or
    one side unmoved).

    Returns (q_lp, p_exec, fee, notional): q_lp in whole base units
    (signed, + = LP received base), p_exec in quote units per whole base,
    fee = the LP's fee income in quote units, notional = |quote moved|.
    """
    if d_base_raw == 0 or d_quote == 0:
        return None
    if (d_base_raw > 0) == (d_quote > 0):
        return None                       # liquidity add/remove, not a swap
    q_lp = d_base_raw / (10 ** dec)
    p_exec = abs(d_quote) / abs(q_lp)
    if d_quote > 0:
        fee = d_quote * fee_frac          # taker paid quote; fee in quote
    else:
        fee = abs(q_lp) * p_exec * fee_frac   # taker paid base; valued at p_exec
    return q_lp, p_exec, fee, float(abs(d_quote))


@dataclass
class Trade:
    day: int
    tick: int
    cls: str
    pool: object                # int (basket pool index) or UB
    q_lp: float
    p_exec: float
    fee: float
    notional: float
    bvib: float
    open: set = field(default_factory=set)


def _agg():
    return {"n": 0, "vol": 0.0, "fees": 0.0, "adv": {}}


class MarkoutLedger:
    """Records trades, resolves markouts at each horizon, keeps cumulative
    per-pool and per-class aggregates; `frame()` is the JSON summary."""

    HORIZONS = (1, 5)

    def __init__(self, horizons=HORIZONS, n_pools: int = 0):
        self.h = tuple(int(x) for x in horizons)
        self.n_pools = int(n_pools)
        self.pending: list[Trade] = []
        self.pool: dict = {}
        self.cls: dict = {}
        self.resolved = 0

    # -- recording ---------------------------------------------------- #

    def record(self, day: int, tick: int, cls: str, pool, d_base_raw: int,
               d_quote: int, dec: int, fee_frac: float, bvib: float) -> bool:
        """Book one act's reserve deltas on `pool` as a trade.  Returns
        True when a swap was recorded."""
        if is_lp_side(cls):
            return False
        sw = swap_from_delta(d_base_raw, d_quote, dec, fee_frac)
        if sw is None:
            return False
        q_lp, p_exec, fee, notional = sw
        t = Trade(int(day), int(tick), str(cls), pool, q_lp, p_exec, fee,
                  notional, float(bvib), set(self.h))
        self.pending.append(t)
        pa = self.pool.setdefault(pool, _agg())
        ca = self.cls.setdefault(t.cls, _agg())
        for a in (pa, ca):
            a["n"] += 1
            a["vol"] += notional
            a["fees"] += fee
        if pool == UB:
            ca.setdefault("vol_ub", 0.0)
            ca["vol_ub"] += notional
            ca["vol"] -= notional         # keep "vol" = basket-pool volume
            ca["fees"] -= fee             # and "fees" = basket-pool fees
        return True

    # -- resolution --------------------------------------------------- #

    def resolve(self, day: int, prices: dict, bvib: float) -> int:
        """Mark out every pending trade whose horizon D has elapsed
        (day >= trade.day + D) against `prices[pool]` (quote per whole
        base, 6-dec) and the current bvib.  Returns the count resolved."""
        n = 0
        keep = []
        for t in self.pending:
            for D in sorted(t.open):
                if day < t.day + D:
                    continue
                p_ref = prices.get(t.pool)
                if not p_ref:
                    t.open.discard(D)
                    continue
                total = t.q_lp * (p_ref - t.p_exec)
                if t.pool == UB or t.bvib <= 0 or bvib <= 0:
                    cm = 0.0
                else:
                    cm = t.q_lp * t.p_exec * (bvib / t.bvib - 1.0)
                for a in (self.pool.setdefault(t.pool, _agg()),
                          self.cls.setdefault(t.cls, _agg())):
                    key = D if t.pool != UB else f"ub{D}"
                    acc = a["adv"].setdefault(key, [0.0, 0.0])
                    acc[0] += total
                    acc[1] += cm
                t.open.discard(D)
                n += 1
            if t.open:
                keep.append(t)
        self.pending = keep
        self.resolved += n
        return n

    # -- summary ------------------------------------------------------ #

    def frame(self) -> dict:
        """Cumulative aggregates, JSON-small (6-dec units rounded to int).

        pool[i] = [n, vol, fees, advT_h1, advC_h1, advT_h2, advC_h2, ...]
        ub      = [n, vol, fees, adv_h1, adv_h2, ...]
        cls[c]  = [n, vol, fees, advT_h1, advC_h1, ..., vol_ub, advUb_h1, ...]
        """
        def r(x):
            return int(round(x))

        pools = []
        for i in range(self.n_pools):
            a = self.pool.get(i)
            row = [0, 0, 0] + [0] * (2 * len(self.h))
            if a:
                row[0], row[1], row[2] = a["n"], r(a["vol"]), r(a["fees"])
                for k, D in enumerate(self.h):
                    tot, cm = a["adv"].get(D, (0.0, 0.0))
                    row[3 + 2 * k] = r(tot)
                    row[4 + 2 * k] = r(cm)
            pools.append(row)
        ub = [0, 0, 0] + [0] * len(self.h)
        a = self.pool.get(UB)
        if a:
            ub[0], ub[1], ub[2] = a["n"], r(a["vol"]), r(a["fees"])
            for k, D in enumerate(self.h):
                ub[3 + k] = r(a["adv"].get(f"ub{D}", (0.0, 0.0))[0])
        cls = {}
        for c, a in self.cls.items():
            row = [a["n"], r(a["vol"]), r(a["fees"])]
            for D in self.h:
                tot, cm = a["adv"].get(D, (0.0, 0.0))
                row += [r(tot), r(cm)]
            row.append(r(a.get("vol_ub", 0.0)))
            for D in self.h:
                row.append(r(a["adv"].get(f"ub{D}", (0.0, 0.0))[0]))
            cls[c] = row
        return {"h": list(self.h), "pool": pools, "ub": ub, "cls": cls,
                "pending": len(self.pending)}


class PoolProbe:
    """Reads the reserves the ledger diffs.  Direct contract calls (the
    session's balance cache is per-tick and would hide an act's own
    effect)."""

    def __init__(self, d):
        self.d = d

    def read(self) -> dict:
        d = self.d
        out = {}
        for i, tc in enumerate(d.tokens):
            out[i] = (tc.functions.balanceOf(d.pool_buck[i]).call(),
                      d.buck.functions.balanceOf(d.pool_buck[i]).call())
        if getattr(d, "pool_ub", ""):
            out[UB] = (d.buck.functions.balanceOf(d.pool_ub).call(),
                       d.usdc.functions.balanceOf(d.pool_ub).call())
        return out

    def diff(self, before: dict, after: dict):
        """Yield (pool, d_base_raw, d_quote, dec, fee_frac) for every pool
        whose reserves moved."""
        d = self.d
        for pool, (b0, q0) in before.items():
            b1, q1 = after.get(pool, (b0, q0))
            db, dq = b1 - b0, q1 - q0
            if db == 0 and dq == 0:
                continue
            if pool == UB:
                yield pool, db, dq, 6, (getattr(d, "fee_ub", 0) or 0) / 1e6
            else:
                yield pool, db, dq, d.dec[pool], (getattr(d, "fee_buck", 0) or 0) / 1e6

    def bvib(self) -> float:
        try:
            return self.d.basket.functions.basketValueInBuck().call() / E18
        except Exception:
            return 0.0


def actor_tag(agent) -> str:
    """The class tag a trade is booked under: the class name, and for the
    whale its phase (accumulate / dump / re-buy are different flows)."""
    cls = type(agent).__name__
    ph = getattr(agent, "phase", None)
    if cls == "WhaleRaidAgent" and ph is not None:
        return f"{cls}:p{int(ph)}"
    return cls
