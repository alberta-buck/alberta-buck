"""BasketWheelAgent: the Alberta Buck agent that turns the basket's work wheel
(doc/BASKET-WHEEL.org), and -- in this prototype -- the basket's hands.

Two roles the contracts will separate, kept apart in the books here:

  THE WHEEL (the basket's).  A WorkWheel over the task kinds named in
  `tasks` (alberta_buck/sim/wheel_tasks.py).  Its value, the share it pays
  out and the reserve it funds are booked in the wheel's ledger.  The
  trades go through this agent's SimLP proxy, which holds a zero-premium
  BuckCredit face: drawing it and ending with more BUCK than was drawn is
  the flash mint's semantics, the way the undertakings desk (WP-2) stood
  in for the basket's issuance authority.
  THE CALLER.  Each tick it decides whether a call pays -- `call`
  "profitable" quotes the pay first (its simulation) and calls only when
  the pay covers the gas on its chain; "always" calls every tick -- and
  books the pay it earns and the gas it spends.  So the chain profile's
  gas sets the action margin (ruling 1) without the wheel knowing it.

The reserve is funded two ways: by the arb task's fund_frac of what it
captures, and by `yield_usd_day` -- a stand-in for the slice of the
basket's pool-fee yield the design owner proposed (2026-09-26), accrued
per tick.  It pays kappa of its balance to each working tick.

Knobs ([agents.BasketWheelAgent]): tasks ["arb"], chain "l1" | "l2", gwei,
eth_usd, call "profitable", max_work 3, max_scan (all), kappa 0.02, reserve_cap 50000,
yield_usd_day 0, face_m 100 (the flash-mint stand-in's face, $M), and the
arb task's share 0.10, fund_frac 0.05, lp_share 1.0, cap_frac 0.02,
min_usd 1.0.

Frame fields (present only when the agent is in the cast): the wheel's
wh_* counters (work_wheel.WorkWheel.counters) and the caller's
wh_call_* (calls, idle, pay, gas_usd, net).
"""
from __future__ import annotations

from dataclasses import replace

from alberta_buck.sim.agents import _register
from alberta_buck.sim.equilibrium_agents import _ProxyAgent
from alberta_buck.sim.experiment import spec as _spec
from alberta_buck.sim.wheel_tasks import TASKS, ConsistencyArbTask
from alberta_buck.sim.work_wheel import (PROFILES, Clock, RewardReserve,
                                         WorkWheel)

E6 = 10 ** 6
E18 = 10 ** 18


@_register
class BasketWheelAgent(_ProxyAgent):
    def setup(self, d, scenario, rng) -> None:
        cls = type(self).__name__
        sp = lambda k, v: _spec(scenario, cls, k, v)
        chain = str(sp("chain", "l1"))
        profile = PROFILES[chain]
        profile = replace(profile, gwei=float(sp("gwei", profile.gwei)),
                          eth_usd=float(sp("eth_usd", profile.eth_usd)))
        reserve = RewardReserve(kappa=float(sp("kappa", 0.02)),
                                cap=float(sp("reserve_cap", 50_000.0)))
        tasks = []
        for name in sp("tasks", ["arb"]):
            if name == "arb":
                tasks.append(ConsistencyArbTask(
                    lp_share=float(sp("lp_share", 1.0)),
                    cap_frac=float(sp("cap_frac", 0.02)),
                    min_usd=float(sp("min_usd", 1.0)),
                    share=float(sp("share", 0.10)),
                    fund_frac=float(sp("fund_frac", 0.05))))
            else:
                tasks.append(TASKS[name]())
        self.call_mode = str(sp("call", "profitable"))
        self.max_work = int(sp("max_work", 3))
        ms = sp("max_scan", None)
        self.max_scan = None if ms is None else int(ms)
        self.yield_per_tick = float(sp("yield_usd_day", 0.0)) / max(
            1, int(getattr(scenario, "ticks_per_day", 4)))
        self.face_m = float(sp("face_m", 100.0))

        # the hands: a proxy with the zero-premium face (the flash-mint
        # stand-in), created the way the undertakings desk creates its own
        self._bind_proxy(d)
        now_ts = d.w3.eth.get_block("latest")["timestamp"]
        self._proxy_exec(d, d.credit.address, d.credit.encode_abi(
            "setCreditIssuer",
            args=[getattr(d.chain.deployer, "address", d.chain.deployer),
                  True]))
        d.chain.send(d.credit.functions.createCredit(
            self.proxy.address, 0, int(self.face_m * 1_000_000 * E6),
            0, 0, 0, now_ts, 0))

        self.wheel = WorkWheel(tasks, profile, reserve)
        self.wheel.hands = self
        self.wheel.bind(d)
        self.calls = self.idle_calls = self.skipped = 0
        self.pay = 0.0
        self.gas_usd = 0.0

    def ensure_spendable(self, d, token, amount: int) -> None:
        """Before a BUCK leg: activate enough of the face that the draw is
        within the credit limit (zero premium: no deposit, no funding gate)."""
        if token is not d.buck or amount <= 0:
            return
        bal = int(d.buck.functions.balanceOf(self.proxy.address).call())
        if bal >= amount:
            return
        k = int(d.kctrl.functions.buckK().call())
        if k <= 0:
            return
        m = ((amount - bal) * E18 // k) * 105 // 100
        self._proxy_exec(d, d.buck.address,
                         d.buck.encode_abi("mint(uint256)", args=[int(m)]))

    def act(self, d, scenario, day, tick, ctr) -> None:
        if self.proxy is None or not getattr(self, "wheel", None):
            return
        tpd = int(getattr(scenario, "ticks_per_day", 4))
        clk = Clock(day, tick, tpd)
        self.wheel.reserve.fund(self.yield_per_tick)
        prof = self.wheel.profile
        if self.call_mode == "profitable":
            pay, gas = self.wheel.quote(d, clk, self.max_work, self.max_scan)
            if pay < prof.usd(prof.tx_base + gas):
                self.skipped += 1
                self._book(ctr)
                return
        try:
            rc = self.wheel.tick(d, clk, self.max_work, self.max_scan)
        except Exception as e:
            ctr["wh_err"] = repr(e)[:200]
            self._book(ctr)
            return
        self.calls += 1
        self.idle_calls += 1 if rc.idle else 0
        self.pay += rc.pay
        self.gas_usd += prof.usd(prof.tx_base + rc.gas)
        for kind, i, res in rc.runs:
            self.note(d, kind, slot=i, value=round(res.value, 2), **res.why)
        self._book(ctr)

    def _book(self, ctr) -> None:
        ctr.update(self.wheel.counters())
        ctr.update({"wh_call_calls": self.calls, "wh_call_idle": self.idle_calls,
                    "wh_call_skipped": self.skipped,
                    "wh_call_pay": round(self.pay, 2),
                    "wh_call_gas_usd": round(self.gas_usd, 2),
                    "wh_call_net": round(self.pay - self.gas_usd, 2)})
        for t in self.wheel.tasks:
            if isinstance(t, ConsistencyArbTask):
                ctr["wh_arb_losses"] = round(t.losses, 2)
