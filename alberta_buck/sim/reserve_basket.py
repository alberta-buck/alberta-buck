"""The BUCK basis for the equity BuckBasket (doc/BASKET-EQUITY.org section 12),
prototyped on alberta_buck.sim.equity_basket: the wallet's BUCK is a RESERVE
the wheel keeps near a target; exits are paid in BUCK from it; and the same
reserve is the float for the wheel's arbitrage against the outside market and
for the director's timed reweighting.

What changes from EquityBasket (everything else is inherited):

  * redeem pays BUCK.  The exit's value is taken at the exiter's marks (each
    pool at the lower of spot and TWAP), less the same charge a BUCK deposit
    pays; that value and the exit's share of the debt both leave the reserve,
    and no pool is touched.  An exit the reserve cannot cover goes pro rata
    instead -- its fraction of everything, the TOKEN sold for BUCK -- and
    bears its own conversion.
  * The wheel keeps the reserve.  Fund places only what the reserve holds
    beyond its band; Trim refills it, and trims the pool the director names.
    Every trade takes the better of the pool and the outside market.
  * Arb makes the trade an outside arbitrageur would make -- each pool back
    to the edge of the no-arbitrage band, the loop closed outside -- but
    first, with the reserve as its float, so the profit stays in the basket.
    There is no narrower band only the basket can take: on its own share of
    a pool the basket would be trading with itself, so inside the outsiders'
    band a "capture" is a rebalancing bet paying the outside cost, not an
    arbitrage (test_inside_the_band_a_trade_is_not_arbitrage).
  * A director chooses what Trim sells and Fund buys, and when: the band
    director by the weights alone; the turn director by a ladder of EMAs,
    parking the proceeds in the reserve between the sale and the purchase.
"""
from __future__ import annotations

import math

from alberta_buck.sim.equity_basket import (
    BUCK, ME, DeployKind, EquityBasket, Pool, SyncKind, Underwater, _grain, _mintable,
    _names)
from alberta_buck.sim.work_wheel import TaskResult, WheelTask


# -- the outside market -------------------------------------------------------------- #

class External:
    """Each TOKEN's outside market in BUCK (TOKEN/USDC, then USDC/BUCK), folded
    into one quote: the reference price, a cost per trade, no depth limit."""

    def __init__(self, prices: dict[str, float], cost: float = 0.001):
        self.price = dict(prices)
        self.cost = cost

    def sell_tok(self, t: str, dx: float) -> float:
        return dx * self.price[t] * (1 - self.cost)

    def sell_buck(self, t: str, dy: float) -> float:
        return dy / self.price[t] * (1 - self.cost)


def band(ref: float, fee: float, cost: float) -> tuple[float, float]:
    """The pool prices between which an arbitrage paying `fee` in the pool and
    `cost` outside makes nothing."""
    return ref * (1 - cost) * (1 - fee), ref * (1 + cost) / (1 - fee)


def arbitrage(pool: Pool, ext: External, t: str, fee: float,
              budget: float = math.inf) -> tuple[float, float]:
    """Trade `pool` to the edge of the band of an arbitrageur paying `fee` in
    it; close outside.  Spend at most `budget` BUCK.  Returns (BUCK spent,
    BUCK received)."""
    lo, hi = band(ext.price[t], fee, ext.cost)
    if pool.L <= 0 or lo <= pool.price <= hi:
        return 0.0, 0.0
    if pool.price < lo:                       # cheap: buy it here, sell outside
        dy = pool.L * (math.sqrt(lo) - pool.sqrtP) / (1 - pool.fee)
        dy = min(dy, budget)
        return dy, ext.sell_tok(t, pool.sell_buck(dy))
    s, s1 = pool.sqrtP, math.sqrt(hi)         # dear: buy it outside, sell here
    dx = pool.L * (s - s1) / (s * s1) / (1 - pool.fee)
    spend = min(dx * ext.price[t] / (1 - ext.cost), budget)
    return spend, pool.sell_tok(ext.sell_buck(t, spend))


# -- the basket ---------------------------------------------------------------------- #

class ReserveBasket(EquityBasket):
    """EquityBasket on a BUCK basis: payouts in BUCK from a reserve the wheel
    keeps near its target, floating within `rband` of it.

    The target is `reserve` of the gross or, with `flow_z`, enough that the
    band's floor -- where the wheel starts refilling -- still holds flow_z
    standard deviations of the daily net flow (an EMA over `flow_days` of
    its square) times 1 + K: an exit takes its equity and burns its share of
    the debt, both from the reserve."""

    def __init__(self, pools: dict[str, Pool], ext: External | None = None,
                 reserve: float = 0.05, rband: float = 0.5, director=None,
                 flow_z: float | None = None, flow_days: float = 30.0, **kw):
        super().__init__(pools, **kw)
        self.ext = ext
        self.reserve = reserve
        self.rband = rband
        self.director = director or BandDirector()
        self.flow_z = flow_z
        self.flow_days = flow_days
        self.flow = 0.0                # today's net flow in BUCK: deposits +, exits -
        self.flow_ms = 0.0             # the EMA of the daily net flow squared
        self.captured = 0.0            # BUCK the wheel's arbitrage has brought in
        self.pro_rata_exits = 0        # exits the reserve could not cover

    def reserve_target(self) -> float:
        r = self.reserve * self.gross()
        if self.flow_z:
            floor = self.flow_z * math.sqrt(self.flow_ms) * (1 + self.k())
            r = max(r, floor / (1 - self.rband))
        return r

    def close_day(self) -> None:
        """Fold the day's net flow into the reserve's sizing."""
        a = 2.0 / (self.flow_days + 1)
        self.flow_ms += (self.flow ** 2 - self.flow_ms) * a
        self.flow = 0.0

    def deposit(self, asset: str, amount: float, guard: float = 0.05) -> int:
        rid = super().deposit(asset, amount, guard)
        self.flow += self.receipts[rid].basis
        return rid

    def spare(self) -> float:
        """The reserve's BUCK beyond its target (negative: short of it)."""
        return self.idle_buck - self.reserve_target()

    def ceiling(self) -> float:
        """Fund places the reserve's BUCK beyond target x (1 + this): the
        reserve's band, or the director's parking room."""
        return self.director.ceiling(self)

    # -- the exit, in BUCK ----------------------------------------------------- #

    def _exit(self, out: float) -> float:
        """Retire `out` shares.  From the reserve when it covers the exit's
        value (at the exiter's marks, less the charge) and its share of the
        debt; pro rata otherwise.  Returns the BUCK paid."""
        left = out * self.exit_fee() if self.exit_fee else 0.0
        f = (out - left) / self.S
        d = self.debt * f
        value = f * self.equity(low=True)
        if value <= 0:
            raise Underwater(-value)
        pay = value * (1 - self.charge(BUCK))
        if pay + d <= self.idle_buck:
            self.idle_buck -= pay + d
            self._burn(d)
        else:
            pay = self._exit_pro_rata(f, d)
            self.pro_rata_exits += 1
        self.flow -= pay
        self.pending -= self.pending * f
        self.S -= out
        return pay

    def _exit_pro_rata(self, f: float, d: float) -> float:
        """Fraction f of the wallet, of every position and owed fee; every
        TOKEN sold for BUCK; the debt share burned; the rest paid."""
        buck = self.idle_buck * f
        self.idle_buck -= buck
        for t, p in self.pools.items():
            tok = self.idle[t] * f
            self.idle[t] -= tok
            a, b = p.remove(ME, p.liq.get(ME, 0.0) * f)
            fa, fb = p.collect(ME, f)
            buck += b + fb + self._route_sell_tok(t, tok + a + fa)
        if buck < d:
            raise Underwater(d - buck)         # on chain, the whole exit reverts
        self._burn(d)
        return buck - d

    # -- routing: the better of the pool and the outside market ----------------- #

    def _pool_share(self, t: str) -> float:
        p = self.pools[t]
        return p.liq.get(ME, 0.0) / p.L if p.L > 0 else 0.0

    def _route_sell_tok(self, t: str, dx: float) -> float:
        if dx <= 0:
            return 0.0
        p = self.pools[t]
        here = p.quote_sell_tok(dx) + dx * p.fee * self._pool_share(t) * p.price
        if self.ext and self.ext.sell_tok(t, dx) > here:
            return self.ext.sell_tok(t, dx)
        return p.sell_tok(dx)

    def _route_sell_buck(self, t: str, dy: float) -> float:
        if dy <= 0:
            return 0.0
        p = self.pools[t]
        here = p.quote_sell_buck(dy) + dy * p.fee * self._pool_share(t) / p.price
        if self.ext and self.ext.sell_buck(t, dy) > here:
            return self.ext.sell_buck(t, dy)
        return p.sell_buck(dy)

    def op_sell_tok(self, t: str, dx: float) -> None:
        dx = min(dx, self.idle[t])
        self.idle[t] -= dx
        self.idle_buck += self._route_sell_tok(t, dx)

    def op_sell_buck(self, t: str, dy: float) -> None:
        dy = min(dy, self.idle_buck)
        self.idle_buck -= dy
        self.idle[t] += self._route_sell_buck(t, dy)

    def op_arb(self, t: str, band_: float) -> float:
        """If pool t strays more than `band_` from the outside price, make
        the outside arbitrageur's trade first, the reserve as the float.
        Returns the BUCK captured."""
        p = self.pools[t]
        if not self.ext or abs(p.price / self.ext.price[t] - 1) <= band_:
            return 0.0
        spent, got = arbitrage(p, self.ext, t, p.fee, budget=self.idle_buck)
        self.idle_buck += got - spent
        self.captured += got - spent
        return got - spent


# -- the directors ------------------------------------------------------------------- #

def neediest(d, pos: dict, total: float, targets: dict) -> str:
    """The pool furthest below its target share of `total`."""
    return max(d.pools, key=lambda x: targets[x] * total - pos[x])


class BandDirector:
    """The simple policy: fixed targets; trim a pool above target + band; buy
    only when the reserve overflows its band (Fund then takes the neediest)."""

    def __init__(self, band: float = 0.02):
        self.band = band

    def observe(self, d, clk) -> None:
        pass

    def targets(self, d) -> dict[str, float]:
        return d.targets

    def pick_trim(self, d) -> str | None:
        w, tg = d.weights(), self.targets(d)
        t = max(d.pools, key=lambda x: w[x] - tg[x])
        return t if w[t] > tg[t] + self.band else None

    def pick_fund(self, d, pos: dict, total: float, forced: bool = False) -> str | None:
        """A pool to buy now from the reserve's spare, within its band -- or,
        `forced` (credit to place, or the reserve overflowing), the pool to
        take it."""
        return neediest(d, pos, total, self.targets(d)) if forced else None

    def ceiling(self, d) -> float:
        return d.rband


class TurnDirector(BandDirector):
    """The multi-scale turn detector (doc/BASKET-REBALANCE.md 2c, per leg).

    A ladder of EMAs of each TOKEN's log price against the basket's mean log
    price (the common numeraire cancels), sampled once a day.  A leg's
    EXCURSION is its price against its longest EMA: RICH above, CHEAP below.
    Each shorter EMA votes when it is moving back toward the longest; a
    quorum of votes is a TURN.  A rich leg that has not turned is RUNNING up,
    a cheap one running down.

    The policy: the targets lean against the excursions (`tilt`: a leg's
    target x exp(-tilt x excursion), renormalized), and the director blocks
    only the trades that fight a run -- it never trims a leg still running
    up (let the run run) and never buys one still running down (no falling
    knives).  Between the sale and the purchase the proceeds park in the
    reserve, up to `park` x its target, past which Fund places into the
    neediest pool anyway.  Beyond the `leash` -- a weight off its DECLARED
    target (not the leaning one) by that fraction of it -- a leg trades
    regardless: the leash keeps the mandate, the lean only times it."""

    def __init__(self, windows=(5, 10, 20, 40, 80, 160, 5000), quorum: int = 4,
                 band: float = 0.01, tilt: float = 1.0, leash: float = 0.3,
                 park: float = 3.0):
        super().__init__(band)
        self.windows = windows         # days; the last is the anchor
        self.quorum = quorum
        self.tilt = tilt
        self.leash = leash
        self.park = park
        self.ema: dict[str, list[float]] = {}
        self.vel: dict[str, list[float]] = {}
        self.x: dict[str, float] = {}

    def observe(self, d, clk) -> None:
        """The day's sample (the Daily component calls it once a day)."""
        logs = {t: math.log(p.twap_price) for t, p in d.pools.items()}
        mean = sum(logs.values()) / len(logs)
        for t in d.pools:
            x = logs[t] - mean
            self.x[t] = x
            if t not in self.ema:
                self.ema[t] = [x] * len(self.windows)
                self.vel[t] = [0.0] * len(self.windows)
                continue
            for k, w in enumerate(self.windows):
                step = (x - self.ema[t][k]) * 2 / (w + 1)
                self.ema[t][k] += step
                self.vel[t][k] = step

    def excursion(self, t: str) -> float:
        return self.x[t] - self.ema[t][-1] if t in self.ema else 0.0

    def turned(self, t: str) -> bool:
        e = self.excursion(t)
        if e == 0:
            return False
        votes = sum(1 for v in self.vel[t][:-1] if v * e < 0)
        return votes >= self.quorum

    def running(self, t: str, side: int) -> bool:
        """Still running away on `side` (+1 up, -1 down): not yet turned."""
        e = self.excursion(t)
        return e * side > 0 and not self.turned(t)

    def targets(self, d) -> dict[str, float]:
        if not self.ema or self.tilt == 0:
            return d.targets
        raw = {t: d.targets[t] * math.exp(-self.tilt * self.excursion(t)) for t in d.pools}
        z = sum(raw.values())
        return {t: v / z for t, v in raw.items()}

    def pick_trim(self, d) -> str | None:
        w, tg = d.weights(), self.targets(d)
        best, most = None, 0.0
        for t in d.pools:
            excess = w[t] - tg[t]
            if excess <= self.band:
                continue
            leashed = w[t] - d.targets[t] > self.leash * d.targets[t]
            if leashed or not self.running(t, +1):
                if excess > most:
                    best, most = t, excess
        return best

    def pick_fund(self, d, pos: dict, total: float, forced: bool = False) -> str | None:
        tg = self.targets(d)
        best, most = None, 0.0
        for t in d.pools:
            gap = tg[t] * total - pos[t]
            if gap <= (0.0 if forced else self.band * total):
                continue
            leashed = d.targets[t] * total - pos[t] > self.leash * d.targets[t] * total
            if leashed or not self.running(t, -1):
                if gap > most:
                    best, most = t, gap
        if best is None and forced:
            best = neediest(d, pos, total, tg)
        return best

    def ceiling(self, d) -> float:
        return self.park


# -- the wheel's components ---------------------------------------------------------- #

def _token_waiting(d) -> bool:
    g = _grain(d)
    return any(d.idle[t] * d.pools[t].price > g for t in d.pools)


def _fund_plan(d: ReserveBasket) -> tuple[str, float, float] | None:
    """(pool, BUCK to place, of it from the reserve's spare) Fund would do
    now, or None -- never more than the pool's gap:

      * the director picks a pool to buy: the spare and the mintable credit;
      * the reserve overflows its ceiling: the same, to the director's
        forced pick (it avoids a leg running down while it can);
      * credit is mintable: the credit alone, to the forced pick -- the
        spare stays parked."""
    if _token_waiting(d):
        return None
    g = _grain(d)
    spare, mint = max(d.spare(), 0.0), _mintable(d)
    if spare + mint <= g:
        return None
    pos = {t: d.position_value(t) for t in d.pools}
    total = sum(pos.values()) + spare + mint
    dr = d.director
    j = dr.pick_fund(d, pos, total)
    if j is None and spare > d.ceiling() * d.reserve_target() + g:
        j = dr.pick_fund(d, pos, total, forced=True)
    if j is None:
        if mint <= g:
            return None
        spare = 0.0
        j = dr.pick_fund(d, pos, total, forced=True)
    amount = min(spare + mint, dr.targets(d)[j] * total - pos[j])
    return (j, amount, min(spare, amount)) if amount > g else None


def _fund_due(d: ReserveBasket) -> bool:
    return _fund_plan(d) is not None


def _reserve_short(d: ReserveBasket) -> float:
    """BUCK the reserve lacks, once it is below its band (else 0)."""
    R = d.reserve_target()
    return R - d.idle_buck if d.idle_buck < R * (1 - d.rband) - _grain(d) else 0.0


class DailyKind(WheelTask):
    """Once a day (`every` blocks): the director's sample, and the day's net
    flow folded into the reserve's sizing."""
    kind = "daily"

    def __init__(self, every: int = 1):
        self.every = every
        self.last: int | None = None

    def due(self, d, i, clk) -> bool:
        return self.last is None or clk.block - self.last >= self.every

    def run(self, d, i, clk):
        self.last = clk.block
        d.close_day()
        d.director.observe(d, clk)
        return TaskResult()


class ArbKind(WheelTask):
    """Pool i back to the outside price, closed outside, the reserve as the
    float: the harvest a CPMM otherwise hands to outside arbitrageurs.  Its
    `band` should exceed the pool fee plus the outside cost (plus gas):
    inside it there is nothing to take."""
    kind = "arb"

    def __init__(self, band: float = 0.005):
        self.band = band

    def slots(self, d) -> int:
        return len(d.pools)

    def due(self, d, i, clk) -> bool:
        t = _names(d)[i]
        return (d.ext is not None and d.idle_buck > _grain(d)
                and abs(d.pools[t].price / d.ext.price[t] - 1) > self.band)

    def run(self, d, i, clk):
        return TaskResult(value=d.op_arb(_names(d)[i], self.band))


class ReserveDeployKind(DeployKind):
    """Deploy, pairing TOKEN_i with the reserve's SPARE BUCK (never below its
    target), then with minted credit, then by selling part of the TOKEN."""

    def run(self, d, i, clk):
        t = _names(d)[i]
        p = d.pools[t]
        v = d.idle[t] * p.price
        have = min(max(d.spare(), 0.0), v)
        have += d.op_mint(v - have)
        if have < v:                  # balance the pair: the reserve stays whole
            d.op_sell_tok(t, (v - have) / 2 / p.price)
        d.op_add(t)
        return TaskResult()


class ReserveFundKind(WheelTask):
    """The reserve's spare BUCK and the mintable credit to the director's
    pick, sized to its gap: half bought as its TOKEN, for Deploy."""
    kind = "fund"

    def due(self, d, i, clk) -> bool:
        return _fund_due(d)

    def run(self, d, i, clk):
        plan = _fund_plan(d)
        if plan is None:
            return None
        j, amount, from_spare = plan
        d.op_mint(amount - from_spare)
        d.op_sell_buck(j, amount / 2)
        return TaskResult()


class TrimKind(WheelTask):
    """Refill a short reserve, or trim the pool the director names: unwind
    at most `step` of the position to the wallet and sell its TOKEN (routed).
    Only a settled basket trims."""
    kind = "trim"

    def __init__(self, step: float = 0.05):
        self.step = step

    def due(self, d, i, clk) -> bool:
        if _token_waiting(d) or _fund_due(d):
            return False
        return _reserve_short(d) > 0 or d.director.pick_trim(d) is not None

    def run(self, d, i, clk):
        short = _reserve_short(d)
        pick = d.director.pick_trim(d)
        w, tg = d.weights(), d.director.targets(d)
        total = sum(d.position_value(x) for x in d.pools)
        if pick is not None:
            t = pick
            need = (w[t] - tg[t]) * total
            if short > 0:
                need = max(need, short)
        else:
            t = max(d.pools, key=lambda x: w[x] - tg[x])
            need = short
        value = min(need, self.step * d.position_value(t))
        if value <= _grain(d):
            return None
        tok0 = d.idle[t]
        d.op_remove(t, value / (2 * d.pools[t].sqrtP))
        d.op_sell_tok(t, d.idle[t] - tok0)
        return TaskResult()


def reserve_wheel_tasks(arb_band: float | None = 0.005, step: float = 0.05,
                        day: int = 1) -> list[WheelTask]:
    """The BUCK basis's components, in slot order: the arbitrage first (it is
    the one racing outside arbitrageurs), then the upkeep.  `day` is a day
    in blocks; `arb_band` None leaves the arbitrage out."""
    tasks: list[WheelTask] = [ArbKind(arb_band)] if arb_band is not None else []
    return tasks + [DailyKind(day), SyncKind(), ReserveDeployKind(), ReserveFundKind(),
                    TrimKind(step)]
