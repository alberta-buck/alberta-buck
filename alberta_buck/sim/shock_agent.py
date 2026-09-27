"""ShockAgent: a BUCK demand or supply shock, armed on demand.

The savings demonstration's two buttons (doc/CONVERGENCE.org 6.2, 7a): the
server's "shock" control arms this agent, which then leans on the floating
BUCK/USDC pool for the chosen number of days --

  side "buy"   a DEMAND shock: USDC (minted: the sim's USDC is a faucet)
               spent on BUCK, evenly over the days, uncapped.  The BUCK is
               held: someone wanted BUCK and keeps it.
  side "sell"  a SUPPLY shock: BUCK drawn on a zero-premium BuckCredit face
               (the stand-in the undertakings desk uses for issuance) and
               dumped for USDC, evenly over the days, uncapped.  Nothing is
               bought back: the BUCK stays in circulation.

-- and leaves the restoring to the system: the undertakings' standing
offers, the arbitrage, the wheel, K.  Unlike WhaleRaidAgent (the catalogue's
scheduled injector, which accumulates for weeks before a dump and unwinds
after), a shock needs no preparation and has no second leg.  Several may be
armed at once; each runs its own days.

Counters: shk_bought_usd, shk_sold_usd, shk_active, shk_armed.
"""
from __future__ import annotations

from alberta_buck.sim.agents import _register
from alberta_buck.sim.equilibrium_agents import _ProxyAgent
from alberta_buck.sim.experiment import spec as _spec

E6 = 10 ** 6
E18 = 10 ** 18


@_register
class ShockAgent(_ProxyAgent):
    def setup(self, d, scenario, rng) -> None:
        cls = type(self).__name__
        self.face_m = float(_spec(scenario, cls, "face_m", 1_000.0))
        self._bind_proxy(d)
        now_ts = d.w3.eth.get_block("latest")["timestamp"]
        self._proxy_exec(d, d.credit.address, d.credit.encode_abi(
            "setCreditIssuer",
            args=[getattr(d.chain.deployer, "address", d.chain.deployer), True]))
        d.chain.send(d.credit.functions.createCredit(
            self.proxy.address, 0, int(self.face_m * 1_000_000 * E6),
            0, 0, 0, now_ts, 0))
        self.shocks: list[dict] = []
        self.bought_usd = 0.0
        self.sold_usd = 0.0
        self.armed = 0

    def arm(self, side: str, usd: float, days: int = 1) -> None:
        """Queue a shock: `usd` of pressure over `days`, from the next tick 0."""
        side = "buy" if str(side) == "buy" else "sell"
        days = max(1, int(days))
        self.shocks.append({"side": side, "per_day": float(usd) / days,
                            "days_left": days})
        self.armed += 1

    def _spendable(self, d, amount: int) -> None:
        bal = int(d.buck.functions.balanceOf(self.proxy.address).call())
        if bal >= amount:
            return
        k = int(d.kctrl.functions.buckK().call())
        if k > 0:
            m = ((amount - bal) * E18 // k) * 105 // 100
            self._proxy_exec(d, d.buck.address,
                             d.buck.encode_abi("mint(uint256)", args=[int(m)]))

    def act(self, d, scenario, day, tick, ctr) -> None:
        if tick != 0 or self.proxy is None or not d.pool_ub:
            return
        for s in self.shocks:
            if s["days_left"] <= 0:
                continue
            amt = int(s["per_day"] * E6)
            try:
                if s["side"] == "buy":
                    d.chain.send(d.usdc.functions.mint(self.proxy.address, amt))
                    self._swap_via_simlp(d, d.pool_ub, d.usdc, amt,
                                         self.proxy.address)
                    self.bought_usd += s["per_day"]
                else:
                    # BUCK at ~$1: size the draw by the pool's price
                    self._spendable(d, amt)
                    self._swap_via_simlp(d, d.pool_ub, d.buck, amt,
                                         self.proxy.address)
                    self.sold_usd += s["per_day"]
            except Exception as e:
                ctr["shk_err"] = repr(e)[:200]
            s["days_left"] -= 1
        self.shocks = [s for s in self.shocks if s["days_left"] > 0]
        ctr["shk_bought_usd"] = round(self.bought_usd, 2)
        ctr["shk_sold_usd"] = round(self.sold_usd, 2)
        ctr["shk_active"] = len(self.shocks)
        ctr["shk_armed"] = self.armed
