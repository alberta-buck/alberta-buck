"""The equity BuckBasket, prototyped: the simplest design meeting the design
owner's rulings (doc/BASKET-EQUITY.org, 2026-09-28/29), in pure Python -- no
EVM -- so the accounting and the wheel's state machine are settled before the
contracts are written.

The rulings:
  * every deposit, TOKEN or BUCK, is EQUITY valued in BUCK;
  * each is matched by K worth of BUCK credit the basket may mint;
  * depositors own the gains; payouts are in TOKEN;
  * the basket takes 25% of each receipt's gain above its cost basis, at exit;
  * withdrawal is pro rata;
  * a deposit only lands the assets in the basket's WALLET -- the wheel's
    state-machine components deploy, fund and rebalance them.

The pieces:
  Pool           a Uniswap V3 pool holding only full-range liquidity:
                 constant product on virtual reserves (x = L/sqrtP,
                 y = L*sqrtP); fees owed to positions pro rata until
                 collected (V3's tokensOwed); a TWAP that catches up at
                 `mark()` (a block passing)
  EquityBasket   the accounting: the wallet (idle TOKEN_i and BUCK), one
                 full-range position per pool, ONE pooled debt (the BUCK it
                 has minted and not burned), shares, receipts (shares, cost
                 basis), the treasury's shares; deposit / redeem; and the
                 operator primitives the wheel's components call
  the kinds      WheelTasks for alberta_buck.sim.work_wheel.WorkWheel:
                   Sync       a pool's owed fees -> the wallet
                   Deploy     pair the wallet's TOKEN_i with BUCK (the
                              wallet's, then minted credit) into pool i
                   Fund       the wallet's BUCK (+ credit) -> the neediest
                              pool: half swapped into its TOKEN, for Deploy
                   Rebalance  an overweight pool's slice -> the wallet, its
                              TOKEN sold for BUCK, for Fund

Everything is valued in BUCK at the TWAP.  Credit: a deposit of E brings
K x E of PENDING credit (K on the day); the wheel mints it as it deploys,
never taking the debt past K x equity (the K of the day it mints).  Earnings
-- fees, the wheel's captures -- compound unlevered, and rebalancing or exit
residue never mints.  A K cut stops minting and calls nothing back.

Entry is conservative (the deposit at the lower of its pool's spot and
TWAP, the basket at the higher of each pool's) and net of the pool fee on
the swap the wheel will make to pair it; price impact stays shared.  Each
component works only on more than `grain` of the gross, and Rebalance only
on a settled basket (nothing worth placing in the wallet or mintable), so
rebalancing never fights the placing of new money.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from alberta_buck.sim.work_wheel import TaskResult, WheelTask

BUCK = "BUCK"
ME = "basket"          # the basket's position owner in every pool


# -- the pool --------------------------------------------------------------------- #

class Pool:
    """Full-range V3: constant product on virtual reserves.  P = BUCK per
    TOKEN = sqrtP^2; L the active liquidity; `liq` each owner's."""

    def __init__(self, price: float, fee: float = 0.003):
        self.sqrtP = math.sqrt(price)
        self.twap = self.sqrtP
        self.fee = fee
        self.L = 0.0
        self.liq: dict[str, float] = {}
        self.owed: dict[str, list[float]] = {}

    @property
    def price(self) -> float:
        return self.sqrtP ** 2

    @property
    def twap_price(self) -> float:
        return self.twap ** 2

    def mark(self) -> None:
        """A block passes: the TWAP catches up to the spot."""
        self.twap = self.sqrtP

    # liquidity

    def add(self, owner: str, tok: float, buck: float) -> tuple[float, float]:
        """Add what balances at the spot; return (tok, buck) used."""
        s = self.sqrtP
        l = min(tok * s, buck / s)
        if l <= 0:
            return 0.0, 0.0
        self.liq[owner] = self.liq.get(owner, 0.0) + l
        self.owed.setdefault(owner, [0.0, 0.0])
        self.L += l
        return l / s, l * s

    def remove(self, owner: str, l: float) -> tuple[float, float]:
        l = min(l, self.liq.get(owner, 0.0))
        if l <= 0:
            return 0.0, 0.0
        self.liq[owner] -= l
        self.L -= l
        return l / self.sqrtP, l * self.sqrtP

    def collect(self, owner: str, frac: float = 1.0) -> tuple[float, float]:
        o = self.owed.setdefault(owner, [0.0, 0.0])
        t, b = o[0] * frac, o[1] * frac
        o[0] -= t
        o[1] -= b
        return t, b

    def _accrue(self, tok_fee: float, buck_fee: float) -> None:
        if self.L <= 0:
            return
        for o, l in self.liq.items():
            if l > 0:
                w = self.owed.setdefault(o, [0.0, 0.0])
                w[0] += tok_fee * l / self.L
                w[1] += buck_fee * l / self.L

    # swaps (exact in)

    def sell_tok(self, dx: float) -> float:
        """TOKEN in, BUCK out."""
        if dx <= 0 or self.L <= 0:
            return 0.0
        s = self.sqrtP
        eff = dx * (1 - self.fee)
        s1 = self.L * s / (self.L + eff * s)
        self.sqrtP = s1
        self._accrue(dx * self.fee, 0.0)
        return self.L * (s - s1)

    def sell_buck(self, dy: float) -> float:
        """BUCK in, TOKEN out."""
        if dy <= 0 or self.L <= 0:
            return 0.0
        s = self.sqrtP
        s1 = s + dy * (1 - self.fee) / self.L
        self.sqrtP = s1
        self._accrue(0.0, dy * self.fee)
        return self.L * (1 / s - 1 / s1)

    def tok_for_buck(self, dy_out: float) -> float:
        """TOKEN in needed for exactly `dy_out` BUCK out (inf if too deep)."""
        if dy_out <= 0:
            return 0.0
        s1 = self.sqrtP - dy_out / self.L if self.L > 0 else 0.0
        if s1 <= 0:
            return math.inf
        s = self.sqrtP
        return self.L * (s - s1) / (s * s1) / (1 - self.fee)

    def arb_to(self, price: float) -> None:
        """An outside trader moves the pool to `price` (paying its fees)."""
        s1 = math.sqrt(price)
        s = self.sqrtP
        if self.L <= 0 or s1 == s:
            self.sqrtP = s1
            return
        if s1 < s:
            self.sell_tok(self.L * (s - s1) / (s * s1) / (1 - self.fee))
        else:
            self.sell_buck(self.L * (s1 - s) / (1 - self.fee))


# -- the basket ------------------------------------------------------------------- #

@dataclass
class Receipt:
    shares: float
    basis: float            # BUCK: the equity it brought, less what has left


class Underwater(Exception):
    """A redemption's debt share cannot be covered by what it withdraws."""


class EquityBasket:
    """Shares of equity; one pooled debt; the wallet; the wheel's primitives.

    Valuation (BUCK, at the TWAP): gross = wallet + positions + owed fees;
    equity = gross - debt; price = equity / shares.  A deposit lands in the
    wallet and mints shares at the price; a redemption takes its pro-rata
    share of the wallet, every position and every owed fee, burns its share
    of the debt first, and pays TOKEN; the treasury's cut of a gain is paid
    in shares."""

    def __init__(self, pools: dict[str, Pool], K=0.75, lam: float = 0.25,
                 targets: dict[str, float] | None = None, band: float = 0.02,
                 dust: float = 1e-9, grain: float = 1e-4, exit_fee=None):
        self.pools = pools
        self.K = K
        self.lam = lam
        self.exit_fee = exit_fee       # () -> fraction of an exit left behind
        n = len(pools)
        self.targets = targets or {t: 1.0 / n for t in pools}
        self.band = band
        self.dust = dust
        self.grain = grain             # the least work worth doing: of gross
        self.idle: dict[str, float] = {t: 0.0 for t in pools}
        self.idle_buck = 0.0
        self.debt = 0.0
        self.pending = 0.0             # credit deposits brought, not yet minted
        self.minted = 0.0
        self.burned = 0.0
        self.S = 0.0
        self.treasury = 0.0            # the basket's own shares
        self.receipts: dict[int, Receipt] = {}
        self._next = 1

    def k(self) -> float:
        return self.K() if callable(self.K) else self.K

    # -- valuation ------------------------------------------------------------ #

    def position_value(self, t: str, high: bool = False) -> float:
        """The position's fair value, 2 L sqrtP, at the TWAP (or at the
        higher of the TWAP and the spot: the incumbents' side of an entry)."""
        p = self.pools[t]
        s = max(p.twap, p.sqrtP) if high else p.twap
        return 2 * p.liq.get(ME, 0.0) * s

    def gross(self, high: bool = False) -> float:
        g = self.idle_buck
        for t, p in self.pools.items():
            P = max(p.twap_price, p.price) if high else p.twap_price
            ot, ob = p.owed.get(ME, [0.0, 0.0])
            g += (self.idle[t] + ot) * P + self.position_value(t, high) + ob
        return g

    def equity(self, high: bool = False) -> float:
        return self.gross(high) - self.debt

    def price(self) -> float:
        return self.equity() / self.S if self.S > 0 else 1.0

    def headroom(self) -> float:
        """BUCK the basket may still mint: K x equity less the debt."""
        return self.k() * self.equity() - self.debt

    def weights(self) -> dict[str, float]:
        v = {t: self.position_value(t) for t in self.pools}
        tot = sum(v.values())
        return {t: (x / tot if tot > 0 else 0.0) for t, x in v.items()}

    def value_of(self, receipt: int) -> float:
        return self.receipts[receipt].shares * self.price()

    # -- the depositor's verbs ------------------------------------------------ #

    def deposit(self, asset: str, amount: float, guard: float = 0.05) -> int:
        """Book equity; the assets wait in the wallet for the wheel.

        Entry is conservative both ways: the deposit is valued at the LOWER
        of its pool's spot and TWAP, the basket it joins at the HIGHER of
        each pool's -- so neither a lagging TWAP nor a pushed spot gives the
        entrant an edge.  `guard` only bounds a manipulated pool."""
        if amount <= 0:
            raise ValueError("amount")
        for p in self.pools.values():
            if abs(p.sqrtP / p.twap - 1) > guard / 2:
                raise ValueError("spot too far from the TWAP")
        if asset == BUCK:
            value = amount
        else:
            p = self.pools[asset]
            value = amount * min(p.price, p.twap_price)
        # the deposit pays for its own deployment: the pool fee on what the
        # wheel will swap to pair it -- (1+K)/2 of a BUCK deposit (into the
        # dearest pool, not yet chosen), (1-K)/2 of a TOKEN one
        k = self.k()
        if asset == BUCK:
            charge = max(p.fee for p in self.pools.values()) * (1 + k) / 2
        else:
            charge = self.pools[asset].fee * max(1 - k, 0.0) / 2
        net = value * (1 - charge)
        shares = net if self.S == 0 else net * self.S / self.equity(high=True)
        if asset == BUCK:
            self.idle_buck += amount
        else:
            self.idle[asset] += amount
        self.pending += self.k() * value
        self.S += shares
        rid = self._next
        self._next += 1
        self.receipts[rid] = Receipt(shares, value)
        return rid

    def redeem(self, rid: int, frac: float = 1.0) -> dict[str, float]:
        """Pro rata: the receipt's share of everything, the debt burned
        first, the treasury's cut of the gain in shares, the rest in TOKEN."""
        r = self.receipts[rid]
        shares, basis = r.shares * frac, r.basis * frac
        pi = self.price()
        gain = shares * pi - basis
        cut = self.lam * max(gain, 0.0) / pi if pi > 0 else 0.0
        self.treasury += cut
        pay = self._exit(shares - cut)
        r.shares -= shares
        r.basis -= basis
        return pay

    def redeem_treasury(self, frac: float = 1.0) -> dict[str, float]:
        """The basket's own shares leave by the same door, with no cut."""
        out = self.treasury * frac
        self.treasury -= out
        return self._exit(out)

    def _exit(self, out: float) -> dict[str, float]:
        """Retire `out` shares: withdraw their fraction of the wallet, of
        every position and of every owed fee; burn that fraction of the
        debt; drop that fraction of the pending credit; pay TOKEN.  An exit
        fee (the stress fee) retires the shares but leaves its fraction of
        the assets -- and of the debt -- with the holders who stay."""
        left = out * self.exit_fee() if self.exit_fee else 0.0
        f = (out - left) / self.S

        pay = {t: self.idle[t] * f for t in self.pools}
        buck = self.idle_buck * f
        for t in self.pools:
            self.idle[t] -= pay[t]
        self.idle_buck -= buck
        for t, p in self.pools.items():
            a, b = p.remove(ME, p.liq.get(ME, 0.0) * f)
            fa, fb = p.collect(ME, f)
            pay[t] += a + fa
            buck += b + fb

        d = self.debt * f
        if buck >= d:
            self._settle_residue(pay, buck - d)
        else:
            self._raise_buck(pay, d - buck)
        self._burn(d)
        self.pending -= self.pending * f     # its share of the credit not yet drawn
        self.S -= out
        return pay

    def _settle_residue(self, pay: dict[str, float], residue: float) -> None:
        """BUCK beyond the debt share becomes TOKEN, pro rata to the TOKEN
        being paid (payouts are in TOKEN)."""
        if residue <= self.dust:
            return
        val = {t: pay[t] * self.pools[t].price for t in pay}
        tot = sum(val.values()) or 1.0
        for t, p in self.pools.items():
            pay[t] += p.sell_buck(residue * val[t] / tot)

    def _raise_buck(self, pay: dict[str, float], short: float) -> None:
        """Sell TOKEN, pro rata to value, to cover the rest of the debt share."""
        val = {t: pay[t] * self.pools[t].price for t in pay}
        tot = sum(val.values())
        if tot <= 0:
            raise Underwater(short)
        for t, p in self.pools.items():
            need = short * val[t] / tot
            dx = p.tok_for_buck(need)
            if dx > pay[t] * (1 + 1e-12):
                raise Underwater(short)
            p.sell_tok(dx)
            pay[t] -= dx

    def _burn(self, amount: float) -> None:
        self.debt -= amount
        self.burned += amount

    # -- the operator primitives (the wheel's, and only the wheel's) ---------- #

    def op_mint(self, amount: float) -> float:
        """Mint pending credit into the wallet, never past K x equity."""
        amount = max(0.0, min(amount, self.pending, self.headroom()))
        self.pending -= amount
        assert amount == 0 or self.debt + amount <= self.k() * self.equity() + 1e-6
        self.debt += amount
        self.minted += amount
        self.idle_buck += amount
        return amount

    def op_collect(self, t: str) -> None:
        a, b = self.pools[t].collect(ME)
        self.idle[t] += a
        self.idle_buck += b

    def op_add(self, t: str) -> None:
        a, b = self.pools[t].add(ME, self.idle[t], self.idle_buck)
        self.idle[t] -= a
        self.idle_buck -= b

    def op_sell_tok(self, t: str, dx: float) -> None:
        dx = min(dx, self.idle[t])
        self.idle[t] -= dx
        self.idle_buck += self.pools[t].sell_tok(dx)

    def op_sell_buck(self, t: str, dy: float) -> None:
        dy = min(dy, self.idle_buck)
        self.idle_buck -= dy
        self.idle[t] += self.pools[t].sell_buck(dy)

    def op_remove(self, t: str, l: float) -> None:
        a, b = self.pools[t].remove(ME, l)
        self.idle[t] += a
        self.idle_buck += b


# -- the wheel's components -------------------------------------------------------- #

def _names(d: EquityBasket) -> list[str]:
    return list(d.pools)


class SyncKind(WheelTask):
    """A pool's owed fees -> the wallet (then Deploy compounds them)."""
    kind = "sync"

    def slots(self, d) -> int:
        return len(d.pools)

    def due(self, d, i, clk) -> bool:
        t = _names(d)[i]
        p = d.pools[t]
        ot, ob = p.owed.get(ME, [0.0, 0.0])
        return ot * p.price + ob > _grain(d)

    def run(self, d, i, clk):
        d.op_collect(_names(d)[i])
        return TaskResult()


class DeployKind(WheelTask):
    """Pair the wallet's TOKEN_i with BUCK -- the wallet's first, then pending
    credit, minted -- selling part of the TOKEN only if both fall short; add
    it to pool i."""
    kind = "deploy"

    def slots(self, d) -> int:
        return len(d.pools)

    def due(self, d, i, clk) -> bool:
        t = _names(d)[i]
        return d.idle[t] * d.pools[t].price > _grain(d)

    def run(self, d, i, clk):
        t = _names(d)[i]
        p = d.pools[t]
        v = d.idle[t] * p.price                       # the BUCK it needs
        d.op_mint(v - min(d.idle_buck, v))
        have = min(d.idle_buck, v)
        if have < v:                                  # balance the pair
            d.op_sell_tok(t, (v - have) / 2 / p.price)
        d.op_add(t)
        return TaskResult()


class FundKind(WheelTask):
    """The wallet's BUCK, and the pending credit, to the neediest pool -- as
    much as brings it to its target weight of the whole (positions plus what
    is waiting), no more: half swapped into that pool's TOKEN, which Deploy
    then pairs.  Waits while any TOKEN is still waiting to be deployed."""
    kind = "fund"

    def due(self, d, i, clk) -> bool:
        if any(d.idle[t] * d.pools[t].price > _grain(d) for t in d.pools):
            return False
        return d.idle_buck + _mintable(d) > _grain(d)

    def run(self, d, i, clk):
        pos = {t: d.position_value(t) for t in d.pools}
        avail = d.idle_buck + _mintable(d)
        total = sum(pos.values()) + avail
        j = max(d.pools, key=lambda t: d.targets[t] * total - pos[t])
        amount = min(avail, d.targets[j] * total - pos[j])
        d.op_mint(amount - min(d.idle_buck, amount))
        d.op_sell_buck(j, min(amount, d.idle_buck) / 2)
        return TaskResult()


def _mintable(d: EquityBasket) -> float:
    return min(d.pending, max(d.headroom(), 0.0))


def _grain(d: EquityBasket) -> float:
    """The least work worth a component's run: `grain` of the gross, and
    never less than dust."""
    return max(d.grain * d.gross(), 1e3 * d.dust)


def _settled(d: EquityBasket) -> bool:
    """Nothing worth placing waits in the wallet or as mintable credit."""
    g = _grain(d)
    return (d.idle_buck + _mintable(d) <= g
            and all(d.idle[t] * d.pools[t].price <= g for t in d.pools))


class RebalanceKind(WheelTask):
    """An overweight pool's slice -> the wallet, its TOKEN sold for BUCK
    there (the rebalancing trade), for Fund to route.  A slice is at most
    `step` of the position and never past the target.  Only a settled
    basket rebalances: while the wallet holds anything, or credit is still
    mintable, Deploy and Fund are placing it, and the weights are in flux."""
    kind = "rebalance"

    def __init__(self, step: float = 0.05):
        self.step = step

    def slots(self, d) -> int:
        return len(d.pools)

    def due(self, d, i, clk) -> bool:
        if not _settled(d):
            return False
        t = _names(d)[i]
        return d.weights()[t] > d.targets[t] + d.band

    def run(self, d, i, clk):
        t = _names(d)[i]
        p = d.pools[t]
        total = sum(d.position_value(x) for x in d.pools)
        excess = (d.weights()[t] - d.targets[t]) * total
        value = min(excess, self.step * d.position_value(t))
        l = value / (2 * p.sqrtP)
        tok0 = d.idle[t]
        d.op_remove(t, l)
        d.op_sell_tok(t, d.idle[t] - tok0)
        return TaskResult()


def equity_wheel_tasks(step: float = 0.05) -> list[WheelTask]:
    """The equity basket's components, in slot order."""
    return [SyncKind(), DeployKind(), FundKind(), RebalanceKind(step)]


# -- a scenario: the whole machine at once ------------------------------------------ #

def run_scenario(seed: int = 7, steps: int = 720, n_tokens: int = 3, K=0.75,
                 fee: float = 0.003, vol: float = 0.02, revert: float = 0.02,
                 check=None) -> dict:
    """Mean-reverting prices, an outside arbitrage pinning each pool to its
    price every step (paying fees), depositors arriving and leaving with TOKEN
    or BUCK, the wheel ticking every step.  `check(b)` (if given) runs after
    every step -- the invariants.  Returns each exit's outcome against
    holding what it deposited, and the treasury's."""
    import random
    from alberta_buck.sim.work_wheel import Clock, WorkWheel

    rng = random.Random(seed)
    names = [f"T{i}" for i in range(n_tokens)]
    base = {t: 1.0 + i for i, t in enumerate(names)}
    logp = {t: 0.0 for t in names}
    pools = {}
    for t in names:
        p = Pool(base[t], fee)
        p.add("seed", 2e5 / base[t], 2e5)            # an outside LP's depth
        pools[t] = p
    b = EquityBasket(pools, K=K)
    w = WorkWheel(equity_wheel_tasks())
    w.bind(b)

    open_: dict[int, tuple[str, float, float]] = {}  # rid -> (asset, amount, BUCK value)
    exits = []
    for step in range(steps):
        for t in names:                              # the outside market
            logp[t] += -revert * logp[t] + vol * rng.gauss(0.0, 1.0)
            pools[t].arb_to(base[t] * math.exp(logp[t]))
            pools[t].mark()
        if rng.random() < 0.15:                      # a depositor arrives
            if rng.random() < 0.3:
                amt = rng.uniform(1e3, 2e4)
                rid = b.deposit(BUCK, amt)
                open_[rid] = (BUCK, amt, amt)
            else:
                t = rng.choice(names)
                amt = rng.uniform(1e3, 2e4) / pools[t].price
                rid = b.deposit(t, amt)
                open_[rid] = (t, amt, amt * pools[t].twap_price)
        if open_ and rng.random() < 0.08:            # one leaves
            rid = rng.choice(sorted(open_))
            asset, amt, basis = open_.pop(rid)
            pay = b.redeem(rid)
            got = sum(pay[t] * pools[t].price for t in names)
            held = amt if asset == BUCK else amt * pools[asset].price
            exits.append({"asset": asset, "basis": basis, "got": got, "held": held})
        w.mark_dirty()
        w.tick(b, Clock(step, 0), max_work=6)
        for p in pools.values():
            p.mark()
        if check:
            check(b)
    ret = [e["got"] / e["basis"] - 1 for e in exits]
    vs = [e["got"] / e["held"] - 1 for e in exits]
    return {"exits": len(exits), "open": len(open_),
            "mean_return": sum(ret) / len(ret) if ret else 0.0,
            "mean_vs_holding": sum(vs) / len(vs) if vs else 0.0,
            "treasury_value": b.treasury * b.price(),
            "equity": b.equity(), "debt": b.debt, "leverage": b.debt / b.equity(),
            "pending": b.pending, "weights": b.weights(), "basket": b}
