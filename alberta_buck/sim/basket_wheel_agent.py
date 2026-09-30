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

`impl` "solidity" (CONVERGENCE step 3) replaces the Python wheel with the
contract: the agent deploys src/wheel/BasketWheel.sol beside the basket,
binds it (public, Carrying -- a contract working for the basket, like the
pools), lets the basket accept its credits (=setWheel=, on the venue facet
through the shell's fallback), configures one triangle per constituent,
and becomes a pure CALLER: its proxy sends =tick(max_work, max_scan)=, and
the frame books the contract's own events (=Ticked=, =Cycled=) and the
receipts' real gas.  Knobs: start "token" | "buck" (where the profit lands:
the depositors, or the reserve then the treasury), share_bp 1000, cap_bp
200, min_edge_bp 1, kappa_bp 200, reserve_cap_buck 100 (a gas budget), kinds ["arb"]
(+ "compute", "director", "sweep", "ops").  Its frame fields are wh_sol_*.
"""
from __future__ import annotations

from dataclasses import replace

from web3.logs import DISCARD

from alberta_buck.sim import identity as idmod
from alberta_buck.sim.agents import _register
from alberta_buck.sim.chain import load_artifact
from alberta_buck.sim.equilibrium_agents import _ProxyAgent
from alberta_buck.sim.experiment import spec as _spec
from alberta_buck.sim.gauge import active_reserves, buck_usd6
from alberta_buck.sim.wheel_tasks import TASKS, ConsistencyArbTask
from alberta_buck.sim.work_wheel import (PROFILES, Clock, RewardReserve,
                                         WorkWheel)

E6 = 10 ** 6


@_register
class BasketWheelAgent(_ProxyAgent):
    def setup(self, d, scenario, rng) -> None:
        cls = type(self).__name__
        sp = lambda k, v: _spec(scenario, cls, k, v)
        self.impl = str(sp("impl", "python"))
        if self.impl == "solidity":
            self._setup_solidity(d, scenario, sp)
            return
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

    def set_chain(self, name: str) -> None:
        """The page's L1 / L2 toggle: the caller's gas profile, live."""
        if name not in PROFILES:
            return
        if getattr(self, "impl", "python") == "solidity":
            self.profile = PROFILES[name]
        elif getattr(self, "wheel", None) is not None:
            self.wheel.profile = PROFILES[name]

    def ensure_spendable(self, d, token, amount: int) -> None:
        """Before a BUCK leg: activate enough of the face that the draw is
        within the credit limit (zero premium: no deposit, no funding gate).
        Buck.mint(m) raises spendable by m, so mint the shortfall + 5%."""
        if token is not d.buck or amount <= 0:
            return
        bal = int(d.buck.functions.balanceOf(self.proxy.address).call())
        if bal >= amount:
            return
        m = (amount - bal) * 105 // 100
        self._proxy_exec(d, d.buck.address,
                         d.buck.encode_abi("mint(uint256)", args=[int(m)]))

    def act(self, d, scenario, day, tick, ctr) -> None:
        if getattr(self, "impl", "python") == "solidity":
            self._act_solidity(d, scenario, day, tick, ctr)
            return
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

    # -- impl "solidity": the contract turns, this agent only calls --------------- #

    def _setup_solidity(self, d, scenario, sp) -> None:
        self.profile = PROFILES[str(sp("chain", "l1"))]
        self.profile = replace(self.profile, gwei=float(sp("gwei", self.profile.gwei)),
                               eth_usd=float(sp("eth_usd", self.profile.eth_usd)))
        self.call_mode = str(sp("call", "profitable"))
        self.max_work = int(sp("max_work", 12))
        ms = sp("max_scan", None)
        self.max_scan = 0 if ms is None else int(ms)
        self.start = str(sp("start", "token"))
        self.share_bp = int(sp("share_bp", 1000))
        gov = d.gov
        self.equity = getattr(d, "wheel", None) is not None
        if self.equity:
            # The equity basket's wheel is deployed with it (it places the
            # deposits); this agent adds its kinds and calls it.
            wheel = d.wheel
            d.chain.send(wheel.functions.setReserveParams(
                int(sp("kappa_bp", 200)), int(float(sp("reserve_cap_buck", 100)) * E6)),
                sender=gov)
        else:
            wheel = d.chain.deploy(
                "BasketWheel", d.buck.address, d.usdc.address, gov,
                # the reserve offsets the callers' GAS: cap it to a gas budget, not
                # to the profit, or a BUCK start passes the profit to the callers
                # (BASKET-WHEEL 8.7: at 50,000 BUCK and kappa 2% they took $47k of $53k)
                int(sp("kappa_bp", 200)), int(float(sp("reserve_cap_buck", 100)) * E6))
            # a contract working for the basket, bound like the pools
            idmod.bind_as_operator(d.chain, d.reg, wheel.address, True, True,
                                   sender=d.chain.deployer)
            facet_abi, _ = load_artifact("BuckBasketUniswapV3", "BuckBasketUniswapV3")
            host = d.w3.eth.contract(address=d.basket.address, abi=facet_abi)
            d.chain.send(host.functions.setWheel(wheel.address), sender=gov)
        d.chain.send(wheel.functions.setArb(
            d.pool_ub, d.basket.address, self.share_bp, int(sp("cap_bp", 200)),
            int(sp("min_edge_bp", 1))), sender=gov)
        d.chain.send(wheel.functions.setStartMode(1 if self.start == "buck" else 0),
                     sender=gov)
        k = 0
        for i, tok in enumerate(d.tokens):
            idx = int(d.basket.functions.indexOf(tok.address).call()) - 1
            if idx < 0 or not d.pool_usdc[i] or not d.pool_buck[i]:
                continue
            d.chain.send(wheel.functions.setTriangle(
                k, (tok.address, d.pool_buck[i], d.pool_usdc[i], idx)), sender=gov)
            k += 1                  # (the equity deploy set the same triangles; this re-sets them)
        kinds = list(sp("kinds", ["arb"]))
        if "compute" in kinds:
            d.chain.send(wheel.functions.setController(d.kctrl.address), sender=gov)
        if "director" in kinds and d.director is not None:
            d.chain.send(wheel.functions.setDirector(d.director.address), sender=gov)
        if "sweep" in kinds and not self.equity:     # the equity basket has no treasury BUCK
            d.chain.send(wheel.functions.setSweep(d.basket.address, 10 ** 15), sender=gov)
        if "ops" in kinds and d.director is not None:
            d.chain.send(wheel.functions.setOps(d.basket.address, d.director.address),
                         sender=gov)
        self.sol = wheel
        self._bind_proxy(d)
        self._tok_index = {tok.address: i for i, tok in enumerate(d.tokens)}
        self.sol_ctr = {"wh_sol_ticks": 0, "wh_sol_skipped": 0, "wh_sol_work": 0,
                        "wh_sol_cycles": 0, "wh_sol_profit_usd": 0.0,
                        "wh_sol_share_usd": 0.0, "wh_sol_credited_usd": 0.0,
                        "wh_sol_reserve_pay_usd": 0.0, "wh_sol_gas": 0,
                        "wh_sol_gas_usd": 0.0, "wh_sol_triangles": k}

    def _usd_per_unit(self, d, token_addr: str) -> float:
        """USD per whole unit of a token: BUCK at the BUCK/USDC pool, a
        constituent at its TOKEN/USDC pool (the market's own quotes)."""
        if token_addr == d.buck.address:
            return buck_usd6(d.chain, d.pool_ub, d.buck) / E6
        i = self._tok_index.get(token_addr)
        if i is None:
            return 0.0
        r_tok, r_usd = active_reserves(d.chain, d.pool_usdc[i], d.tokens[i], d.usdc)
        if r_tok <= 0:
            return 0.0
        return (r_usd / E6) / (r_tok / 10 ** d.dec[i])

    def _quote_pay_usd(self, d) -> float:
        """The caller's simulation: the share of what the due triangles would
        capture now (the contract's own plan), in USD."""
        pay = 0.0
        n = int(self.sol.functions.triangleCount().call())
        buck = self.start == "buck"
        for k in range(n):
            x, _dir, profit = self.sol.functions.plan(k).call()
            if x == 0:
                continue
            tok = d.buck.address if buck else self.sol.functions.triangles(k).call()[0]
            dec = 6 if buck else d.dec[self._tok_index[tok]]
            pay += profit / 10 ** dec * self._usd_per_unit(d, tok) * self.share_bp / 1e4
        return pay

    def _act_solidity(self, d, scenario, day, tick, ctr) -> None:
        c = self.sol_ctr
        # An equity basket's components must run whatever the arbitrage pays:
        # they are the basket's placing, and the reserve pays for the upkeep.
        upkeep = False
        if getattr(self, "equity", False):
            self.sol.functions.rearm().call()
            upkeep = int(self.sol.functions.pending().call()) > 0
        if self.call_mode == "profitable" and not upkeep:
            pay = self._quote_pay_usd(d)
            if pay < self.profile.usd(self.profile.tx_base + 700_000):
                c["wh_sol_skipped"] += 1
                ctr.update({k: (round(v, 2) if isinstance(v, float) else v)
                            for k, v in c.items()})
                return
        try:
            rcpt = self._proxy_exec(d, self.sol.address, self.sol.encode_abi(
                "tick", args=[self.max_work, self.max_scan]))
        except Exception as e:
            ctr["wh_sol_err"] = repr(e)[:200]
            return
        c["wh_sol_ticks"] += 1
        gas = int(rcpt.get("gasUsed", 0) or 0)
        c["wh_sol_gas"] += gas
        c["wh_sol_gas_usd"] += self.profile.usd(gas)
        buck_usd = buck_usd6(d.chain, d.pool_ub, d.buck) / E6
        for ev in self.sol.events.Ticked().process_receipt(rcpt, errors=DISCARD):
            c["wh_sol_work"] += int(ev["args"]["work"])
            c["wh_sol_reserve_pay_usd"] += int(ev["args"]["reservePay"]) / E6 * buck_usd
        buck = self.start == "buck"
        for ev in self.sol.events.Cycled().process_receipt(rcpt, errors=DISCARD):
            a = ev["args"]
            tok = d.buck.address if buck else self.sol.functions.triangles(int(a["k"])).call()[0]
            dec = 6 if buck else d.dec[self._tok_index[tok]]
            px = self._usd_per_unit(d, tok) / 10 ** dec
            c["wh_sol_cycles"] += 1
            c["wh_sol_profit_usd"] += int(a["profit"]) * px
            c["wh_sol_share_usd"] += int(a["callerShare"]) * px
            c["wh_sol_credited_usd"] += int(a["credited"]) * px
            self.note(d, "cycle", k=int(a["k"]), dir=int(a["dir"]),
                      x=int(a["amountIn"]), profit_usd=round(int(a["profit"]) * px, 2),
                      credited_usd=round(int(a["credited"]) * px, 2), gas=gas)
        ctr.update({k: (round(v, 2) if isinstance(v, float) else v) for k, v in c.items()})
