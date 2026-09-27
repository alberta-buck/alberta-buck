"""The work wheel's task kinds (doc/BASKET-WHEEL.org; the chassis is
alberta_buck/sim/work_wheel.py).

ConsistencyArbTask   one slot per constituent: the triangle TOKEN/BUCK (the
                     basket's own pool), TOKEN/USDC and BUCK/USDC, gone round
                     from BUCK to BUCK when it pays.  Captures value.
DirectorPokeTask     the director's epoch signal refresh (poke(1)), today
                     done by the shell and DirectorKeeperAgent.  Upkeep.
TreasurySweepTask    re-LP the treasury's pending BUCK (sweepTreasury), which
                     no sim agent calls today.  Upkeep.

THE CYCLE, and why it can be sized in closed form.  A constant-product
swap of x is out(x) = g R_out x / (R_in + g x), a Mobius map with
coefficients (a, b, c) = (g R_out, R_in, g); maps of that form compose to
the same form, A x / (B + C x) with A = a1 a2, B = b1 b2, C = b2 c1 + a1 c2.
A cycle pays iff its slope at zero, A / B, exceeds 1, and its profit
A x / (B + C x) - x peaks at x* = (sqrt(A B) - B) / C.

FEE RECYCLING.  On its own TOKEN/BUCK pool the basket is (nearly) the
only LP, so the fee it pays there comes back to it.  The cycle is sized
with that leg's fee counted at (1 - lp_share) and valued as the realized
BUCK profit plus lp_share of the fee paid on the leg: the basket's cost
is the two external legs (0.35% at the sim's tiers), a searcher's all
three (0.65%) -- the band only the basket can take.

THE HANDS.  The task trades through its wheel's `hands`, a
_ProxyAgent-derived agent whose proxy holds a zero-premium BuckCredit
face: drawing it and ending with more BUCK than was drawn is the flash
mint's semantics (supply unchanged, the profit existing BUCK moved to the
basket).  The on-chain cycle reverts below its minimum profit; the sim
cannot, so it trades only on an ex-ante quote at a size capped well
inside the active liquidity, and books any realized shortfall as
`losses` -- the quote's error, measured.
"""
from __future__ import annotations

import math

from alberta_buck.sim.gauge import active_reserves, buck_usd6
from alberta_buck.sim.work_wheel import TaskResult, WheelTask

E6 = 10 ** 6


# -- the closed form --------------------------------------------------------------- #

def leg(r_in: float, r_out: float, fee: float) -> tuple[float, float, float]:
    """The Mobius coefficients of one constant-product swap."""
    g = 1.0 - fee
    return (g * r_out, r_in, g)


def compose(m1, m2):
    """m2 after m1."""
    a1, b1, c1 = m1
    a2, b2, c2 = m2
    return (a1 * a2, b1 * b2, b2 * c1 + a1 * c2)


def apply(m, x: float) -> float:
    a, b, c = m
    return a * x / (b + c * x) if b + c * x > 0 else 0.0


def optimum(m) -> tuple[float, float]:
    """(x*, profit at x*) for a cycle map; (0, 0) when it does not pay."""
    a, b, c = m
    if b <= 0 or c <= 0 or a <= b:
        return 0.0, 0.0
    x = (math.sqrt(a * b) - b) / c
    return x, apply(m, x) - x


# -- the consistency arbitrage ------------------------------------------------------ #

class ConsistencyArbTask(WheelTask):
    """Go round the triangle through constituent i, from BUCK to BUCK.

    Two directions: SELL-OWN (BUCK -> TOKEN on the basket's pool, TOKEN ->
    USDC, USDC -> BUCK) when the basket's pool prices TOKEN cheap in BUCK,
    BUY-OWN (BUCK -> USDC, USDC -> TOKEN, TOKEN -> BUCK on the basket's
    pool) when it prices TOKEN dear.  `due` quotes both on slot0 active
    reserves (gauge.py, decision 8) and keeps the plan; `run` executes it.

    Knobs: lp_share (the basket's share of its pool's fees, 1.0), cap_frac
    (the most a cycle takes of the thinnest leg's input reserve, 0.02),
    min_usd (dust, $1), share (the caller's cut of the value, 0.10),
    fund_frac (the reserve's cut, 0.05), gas (a three-swap cycle, 420k)."""
    kind = "arb"
    gas = 420_000

    def __init__(self, lp_share: float = 1.0, cap_frac: float = 0.02,
                 min_usd: float = 1.0, share: float = 0.10,
                 fund_frac: float = 0.05):
        self.lp_share = lp_share
        self.cap_frac = cap_frac
        self.min_usd = min_usd
        self.share = share
        self.fund_frac = fund_frac
        self._plan: dict[int, dict] = {}
        self.losses = 0.0

    def slots(self, d) -> int:
        return len(getattr(d, "pool_buck", None) or []) if d.pool_ub else 0

    def _legs(self, d, i: int, own_fee: float):
        """(sell-own legs, buy-own legs): lists of (pool, token_in, token_out,
        fee used for sizing, fee paid on the basket's pool or 0)."""
        tok = d.tokens[i]
        fb, fu, fub = d.fee_buck / 1e6, d.fee_usdc / 1e6, d.fee_ub / 1e6
        own_eff = fb * (1.0 - self.lp_share)
        sell = [(d.pool_buck[i], d.buck, tok, own_eff, fb),
                (d.pool_usdc[i], tok, d.usdc, fu, 0.0),
                (d.pool_ub, d.usdc, d.buck, fub, 0.0)]
        buy = [(d.pool_ub, d.buck, d.usdc, fub, 0.0),
               (d.pool_usdc[i], d.usdc, tok, fu, 0.0),
               (d.pool_buck[i], tok, d.buck, own_eff, fb)]
        return sell, buy

    def _quote(self, d, legs):
        """Size and value one direction from active reserves: the closed-form
        optimum, capped at cap_frac of each leg's input reserve (mapped back
        to BUCK), then the recycled fee on the basket's leg."""
        m = None
        caps = []
        x_scale = 1.0              # BUCK in -> this leg's input, at the margin
        for pool, t_in, t_out, f_size, _f_own in legs:
            r_in, r_out = active_reserves(d.chain, pool, t_in, t_out)
            if r_in <= 0 or r_out <= 0:
                return None
            caps.append(self.cap_frac * r_in / x_scale)
            x_scale *= (1.0 - f_size) * r_out / r_in
            lg = leg(r_in, r_out, f_size)
            m = lg if m is None else compose(m, lg)
        x, _ = optimum(m)
        x = min(x, *caps)
        if x < E6:
            return None
        # the leg-by-leg path at x, with the basket's leg at its real fee, so
        # the quote is what the swaps will pay; the recycled fee on top
        amt, recycled = x, 0.0
        for pool, t_in, t_out, _f_size, f_own in legs:
            r_in, r_out = active_reserves(d.chain, pool, t_in, t_out)
            fee = f_own if f_own else _f_size
            if f_own:
                # the fee on the basket's leg, in BUCK: on the input when the
                # input is BUCK, else on the BUCK the leg pays out
                out = apply(leg(r_in, r_out, fee), amt)
                recycled += self.lp_share * f_own * (
                    amt if t_in is d.buck else out / (1.0 - f_own))
                amt = out
            else:
                amt = apply(leg(r_in, r_out, fee), amt)
        return {"x": x, "profit": amt - x, "recycled": recycled}

    def due(self, d, i: int, clk) -> bool:
        self._plan.pop(i, None)
        sell, buy = self._legs(d, i, d.fee_buck / 1e6)
        usd = buck_usd6(d.chain, d.pool_ub, d.buck) / E6 or 1.0
        best = None
        for name, legs in (("sell-own", sell), ("buy-own", buy)):
            q = self._quote(d, legs)
            if q is None:
                continue
            value = (q["profit"] + q["recycled"]) / E6 * usd
            if q["profit"] > 0 and value >= self.min_usd and (
                    best is None or value > best["value"]):
                best = {**q, "dir": name, "legs": legs, "value": value,
                        "usd": usd}
        if best is None:
            return False
        self._plan[i] = best
        return True

    def estimate(self, i: int) -> float:
        p = self._plan.get(i)
        return p["value"] if p else 0.0

    def run(self, d, i: int, clk) -> TaskResult | None:
        plan = self._plan.pop(i, None)
        hands = getattr(self.wheel, "hands", None)
        if plan is None or hands is None:
            return None
        x = int(plan["x"])
        s0 = int(d.buck.functions.signedBalanceOf(hands.proxy.address).call())
        amt = x
        for pool, t_in, t_out, _f, _own in plan["legs"]:
            before = d.chain.balance_of(t_out, hands.proxy.address)
            hands.ensure_spendable(d, t_in, amt)
            hands._swap_via_simlp(d, pool, t_in, amt, hands.proxy.address)
            amt = d.chain.balance_of(t_out, hands.proxy.address) - before
            if amt <= 0:
                break
        s1 = int(d.buck.functions.signedBalanceOf(hands.proxy.address).call())
        profit = (s1 - s0) / E6 * plan["usd"]
        recycled = plan["recycled"] / E6 * plan["usd"]
        if profit < 0:
            self.losses += -profit
        return TaskResult(work=1, value=profit + recycled, gas=self.gas,
                          why={"dir": plan["dir"], "i": i, "x": x // E6,
                               "quote": round(plan["profit"] / E6, 2),
                               "profit": round(profit, 2),
                               "recycled": round(recycled, 2)})


# -- upkeep the wheel subsumes ---------------------------------------------------------- #

class DirectorPokeTask(WheelTask):
    """The director's per-constituent signal refresh, one poke per slot run.
    Permissionless; the shell already pokes it with a budget of 1 on every
    activation.  Captures nothing: paid from the reserve."""
    kind = "director"
    gas = 60_000

    def slots(self, d) -> int:
        return 1 if getattr(d, "director", None) is not None else 0

    def due(self, d, i: int, clk) -> bool:
        try:
            return int(d.director.functions.pending().call()) > 0
        except Exception:
            return False

    def run(self, d, i: int, clk) -> TaskResult | None:
        d.chain.send(d.director.functions.poke(1))
        return TaskResult(work=1, gas=self.gas)


class TreasurySweepTask(WheelTask):
    """Re-LP the treasury's pending BUCK once it passes the shell's minimum
    (BuckBasketProRata.sweepTreasury).  Permissionless; captures nothing."""
    kind = "sweep"
    gas = 250_000

    def slots(self, d) -> int:
        try:
            d.basket.functions.treasuryBuckPending().call()
            return 1
        except Exception:
            return 0

    def due(self, d, i: int, clk) -> bool:
        try:
            pend = int(d.basket.functions.treasuryBuckPending().call())
            floor = int(d.basket.functions.MIN_REINVEST_BUCK().call())
        except Exception:
            return False
        return pend >= floor

    def run(self, d, i: int, clk) -> TaskResult | None:
        d.chain.send(d.basket.functions.sweepTreasury())
        return TaskResult(work=1, gas=self.gas)


TASKS = {"arb": ConsistencyArbTask, "director": DirectorPokeTask,
         "sweep": TreasurySweepTask}
