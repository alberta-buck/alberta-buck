"""The work wheel: a bounded round-robin of basket tasks any caller advances.

doc/BASKET-WHEEL.org.  RebalanceDirectorBase already carries one wheel --
an epoch clock, a round-robin cursor, poke(maxWork) over constituents,
policy supplied through two hooks -- and the shell advances it with
poke(1) on every activation.  This generalizes that chassis from
"constituents x epochs" to "tasks x clocks", so an activity is added by
deriving a task, not by writing another keeper:

  WheelTask     one KIND of work (the consistency arb on a constituent,
                the director's signal refresh, the treasury sweep, ...):
                how many slots it has, whether a slot is due (cheap), and
                how to run it (bounded).  It reports what the run was
                worth to the basket, if anything, and its gas.
  WorkWheel     the chassis: the slot table over all tasks, the cursor,
                the per-block idle memo (a full scan that finds nothing
                due makes every further tick in that block a single read),
                the re-arm when a basket-touching trade lands, and the
                caller's pay.
  RewardReserve the gas offset (design owner, 2026-09-26): a slice of the
                basket's yield funds it, and each tick that does work is
                paid a fraction kappa of what it holds -- so it builds up
                when ticks are under-called and pays out less when they
                are over-called.  At equilibrium a paid tick earns
                funding / calls, and callers call until that equals their
                gas: the calling rate sets itself per chain.
  ChainProfile  what gas costs where (the per-chain margin, ruling 1).

The caller's pay for a working tick is the task's share of the value it
captured (a value-producing task pays for itself) plus the reserve's
kappa (upkeep that captures nothing is paid from yield).  An idle tick is
paid nothing: callers simulate first, so the basket never funds a
wasted call.

The Solidity shape the Python mirrors is in BASKET-WHEEL.org section 8.4:
an abstract chassis whose task kinds are mixins chained through super
over slot ranges, deployable inside a shell with headroom or beside it
with narrowly-scoped capabilities (a flash mint for the arb kind).
"""
from __future__ import annotations

from dataclasses import dataclass, field


# -- what gas costs ------------------------------------------------------------ #

@dataclass(frozen=True)
class ChainProfile:
    """Gas on one chain.  Ballparks, knobs, to be refreshed per deployment:
    an L1 at a few gwei, a rollup at a hundredth of one (data included)."""
    name: str = "l1"
    gwei: float = 5.0
    eth_usd: float = 3000.0
    tx_base: int = 21_000        # a standalone transaction's floor
    idle_check: int = 2_600      # a done block: one cold read and a return

    def usd(self, gas: int) -> float:
        return gas * self.gwei * 1e-9 * self.eth_usd


PROFILES = {
    "l1": ChainProfile("l1", gwei=5.0),
    "l2": ChainProfile("l2", gwei=0.01),
}


# -- the reserve that offsets the callers' gas ---------------------------------- #

@dataclass
class RewardReserve:
    """A slice of basket yield, paid out as kappa of the balance per working
    tick.  `cap` bounds the pile: funding beyond it stays with depositors."""
    kappa: float = 0.02
    cap: float = 50_000.0
    balance: float = 0.0
    funded: float = 0.0
    paid: float = 0.0
    spilled: float = 0.0

    def fund(self, usd: float) -> None:
        if usd <= 0:
            return
        room = max(0.0, self.cap - self.balance)
        take = min(usd, room)
        self.balance += take
        self.funded += take
        self.spilled += usd - take

    def pay(self) -> float:
        out = self.kappa * self.balance
        self.balance -= out
        self.paid += out
        return out


# -- one kind of work ------------------------------------------------------------ #

@dataclass
class TaskResult:
    work: int = 1                # units of the caller's max_work consumed
    value: float = 0.0           # USD captured for the basket (0 for upkeep)
    gas: int = 0                 # gas units the run cost its caller
    why: dict = field(default_factory=dict)


class WheelTask:
    """One kind of work.  Derive and fill in slots / due / run.

    `share` is the fraction of the captured value paid to the caller (a
    task that captures value pays for its own calls); `fund_frac` the
    fraction of it the task puts into the reserve (value paying for
    upkeep).  `gas` is the run's estimate, used by callers' economics and
    booked by the wheel."""
    kind = "task"
    gas = 60_000
    share = 0.0
    fund_frac = 0.0

    def bind(self, wheel: "WorkWheel", d) -> None:
        self.wheel = wheel

    def slots(self, d) -> int:
        return 1

    def due(self, d, i: int, clk: "Clock") -> bool:
        raise NotImplementedError

    def run(self, d, i: int, clk: "Clock") -> TaskResult | None:
        raise NotImplementedError

    def estimate(self, i: int) -> float:
        """The value a run of slot i would capture, as its last `due` saw it
        (0 for upkeep).  What a caller's simulation would show it."""
        return 0.0


@dataclass(frozen=True)
class Clock:
    """The sim's stand-in for a block: one tick.  (On chain the clock is a
    per-chain adapter: an L2 whose block.number is the L1's, as on
    Arbitrum, would idle the wheel for many of its own blocks.)"""
    day: int
    tick: int
    ticks_per_day: int = 4

    @property
    def block(self) -> int:
        return self.day * self.ticks_per_day + self.tick


# -- the chassis ------------------------------------------------------------------ #

@dataclass
class Receipt:
    idle: bool = False
    work: int = 0
    value: float = 0.0
    pay: float = 0.0
    gas: int = 0
    runs: list = field(default_factory=list)   # (kind, slot, TaskResult)


@dataclass
class KindLedger:
    runs: int = 0
    value: float = 0.0
    paid: float = 0.0
    gas: int = 0


class WorkWheel:
    def __init__(self, tasks: list[WheelTask], profile: ChainProfile | None = None,
                 reserve: RewardReserve | None = None):
        self.tasks = list(tasks)
        self.profile = profile or PROFILES["l1"]
        self.reserve = reserve or RewardReserve()
        self.cursor = 0
        self.idle_block: int | None = None
        self._scan_block: int | None = None
        self._scanned = 0         # consecutive slots found not due, this block
        self._table: list[tuple[WheelTask, int]] = []
        self.ledger = {t.kind: KindLedger() for t in self.tasks}
        self.ticks = 0
        self.idle_ticks = 0
        self.retained = 0.0       # value kept by the basket, after pay and funding

    def bind(self, d) -> None:
        for t in self.tasks:
            t.bind(self, d)
        self.rebuild(d)

    def rebuild(self, d) -> None:
        """The slot table: every task's slots, in task order.  Re-run when a
        task's slot count changes (a constituent added)."""
        self._table = [(t, i) for t in self.tasks for i in range(t.slots(d))]
        self.cursor %= max(1, len(self._table))

    def mark_dirty(self) -> None:
        """Re-arm: a basket-touching trade landed, so a gap may have opened
        after the wheel went idle for this block."""
        self.idle_block = None
        self._scanned = 0

    def pending(self, d, clk: Clock) -> int:
        return sum(1 for t, i in self._table if t.due(d, i, clk))

    def quote(self, d, clk: Clock, max_work: int = 1,
              max_scan: int | None = None) -> tuple[float, int]:
        """What a tick would pay its caller and cost in gas, without running
        it: the caller's simulation (eth_call) before it sends."""
        n = len(self._table)
        if n == 0 or self.idle_block == clk.block:
            return 0.0, self.profile.idle_check
        pay, gas, work = 0.0, 0, 0
        for step in range(n if max_scan is None else min(n, max_scan)):
            if work >= max_work:
                break
            task, i = self._table[(self.cursor + step) % n]
            if task.due(d, i, clk):
                pay += task.share * task.estimate(i)
                gas += task.gas
                work += 1
        if work:
            pay += self.reserve.kappa * self.reserve.balance
        return pay, gas or self.profile.idle_check

    def tick(self, d, clk: Clock, max_work: int = 1,
             max_scan: int | None = None) -> Receipt:
        """Advance up to `max_work` due slots, examining at most `max_scan`
        slots from the cursor.  The cursor moves past every slot examined, so
        many small calls in one block share one scan; once consecutive calls
        have found every slot idle, the block is memoized and every further
        tick in it costs one read."""
        self.ticks += 1
        n = len(self._table)
        if n == 0 or self.idle_block == clk.block:
            self.idle_ticks += 1
            return Receipt(idle=True, gas=self.profile.idle_check)
        if self._scan_block != clk.block:
            self._scan_block = clk.block
            self._scanned = 0
        rc = Receipt()
        start = self.cursor          # read once, as the director's poke does
        for step in range(n if max_scan is None else min(n, max_scan)):
            if rc.work >= max_work:
                break
            slot = (start + step) % n
            self.cursor = (slot + 1) % n
            task, i = self._table[slot]
            if not task.due(d, i, clk):
                self._scanned += 1
                continue
            self._scanned = 0
            res = task.run(d, i, clk)
            if res is None:
                continue
            rc.work += max(1, res.work)
            rc.gas += res.gas
            rc.value += res.value
            rc.runs.append((task.kind, i, res))
            led = self.ledger[task.kind]
            led.runs += 1
            led.value += res.value
            led.gas += res.gas
            share = task.share * res.value
            self.reserve.fund(task.fund_frac * res.value)
            self.retained += res.value - share - task.fund_frac * res.value
            led.paid += share
            rc.pay += share
        if rc.work > 0:
            rc.pay += self.reserve.pay()
        else:
            rc.idle = True
            rc.gas = self.profile.idle_check
            if self._scanned >= n:
                self.idle_block = clk.block
                self.idle_ticks += 1
        return rc

    def counters(self) -> dict:
        """Frame fields (house rule 5: present only when a wheel exists)."""
        out = {"wh_ticks": self.ticks, "wh_idle": self.idle_ticks,
               "wh_retained": round(self.retained, 2),
               "wh_reserve": round(self.reserve.balance, 2),
               "wh_reserve_funded": round(self.reserve.funded, 2),
               "wh_reserve_paid": round(self.reserve.paid, 2)}
        for kind, led in self.ledger.items():
            k = kind.replace("-", "_")
            out[f"wh_{k}_runs"] = led.runs
            out[f"wh_{k}_value"] = round(led.value, 2)
            out[f"wh_{k}_paid"] = round(led.paid, 2)
            out[f"wh_{k}_gas_usd"] = round(self.profile.usd(led.gas), 2)
        return out
