"""One isolated world per stage. All prices and controller states are EVM reads.

Only the external feed driver swaps to a requested price. In replay mode it
also drives TOKEN/BUCK explicitly; in market mode those pools float freely.
"""
from __future__ import annotations

import contextlib
import math
import os
import time
from types import SimpleNamespace

from .model import DEFAULTS, STAGES, baseline_prices, feed_rows, history_rows

E6, E18, Q96 = 10**6, 10**18, 1 << 96
ZERO = "0x" + "0" * 40


@contextlib.contextmanager
def isolated_environment(cfg):
    """Legacy deployment reads env knobs: make the resolved TOML authoritative."""
    saved = {k: v for k, v in os.environ.items() if k.startswith(("SIM_", "DIRECTOR_"))}
    for k in saved:
        del os.environ[k]
    os.environ.update({"SIM_EQUITY_DIRECTOR": "0", "SIM_EQUITY_MACHINE": "0",
                       "SIM_OPS_CAPITAL_USD": str(DEFAULTS["desk"]["capital_per_token"])})
    try:
        yield
    finally:
        for k in list(os.environ):
            if k.startswith(("SIM_", "DIRECTOR_")):
                del os.environ[k]
        os.environ.update(saved)


class InitialPrices:
    def __init__(self, base):
        self.base = base

    def day0(self, i):
        return round(self.base[i] * E6)

    def ref(self, i, day):
        return self.day0(i)


class World:
    def __init__(self, cfg, stage):
        from alberta_buck.sim.chain import Chain, load_artifact
        from alberta_buck.sim.deploy import deploy
        from alberta_buck.sim.experiment import load
        from alberta_buck.sim.identity import seeded_rng
        from alberta_buck.sim.pyrevm_backend import PyrevmAnvil

        self.cfg, self.flags = cfg, STAGES[stage]
        self.evm = PyrevmAnvil()
        self.evm.__enter__()
        self.chain = Chain(self.evm.w3, self.evm.w3.eth.accounts[0])
        self.gas = self.transactions = 0
        original_send = self.chain.send

        def send(fn, **kwargs):
            rc = original_send(fn, **kwargs)
            self.gas += int(rc["gasUsed"])
            self.transactions += 1
            return rc

        self.chain.send = send
        cp = cfg["controller"] if self.flags.get("k") else {
            **DEFAULTS["controller"], **{k: cfg["controller"][k] for k in ("k0", "kmin", "kmax")}}
        exp = load(sets=[f"deploy.{key}={value}" for key, value in cp.items()] + [
            f"deploy.target_buck_m={cfg['market']['depth_usd']/1e6}",
            f"deploy.target_buck_lp_m={cfg['market']['depth_usd']/1e6}",
            f"deploy.dtmax_days={1/cfg['ticks_per_day']}"])
        scenario = SimpleNamespace(name="control-lab", seed=cfg["seed"], agents={},
                                   tokens=[(s, s, 18) for s in ("A", "B", "C")],
                                   prices=InitialPrices(cfg["feed"]["base"]), experiment=exp)
        self.d = d = deploy(self.chain, self.evm, scenario, seeded_rng(cfg["seed"]),
                           verbose=False, basket_impl="equity-ops")
        pool_abi, _ = load_artifact("UniswapV3Pool")
        self.pools = {a: d.w3.eth.contract(address=a, abi=pool_abi)
                      for a in [*d.pool_usdc, *d.pool_buck, d.pool_ub]}
        self.pairs = {a: (p.functions.token0().call(), p.functions.token1().call())
                      for a, p in self.pools.items()}
        # All arms begin with the same independent LPs and passive basket.
        # Extra fixture credit belongs to the exogenous liquidity provider,
        # not to the price-sensitive supplier introduced in later stages.
        self.credit(d.simlp, 100 * cfg["market"]["depth_usd"])
        from alberta_buck.sim.router import full_range_ticks
        lo, hi = full_range_ticks(60)
        for addr in d.pool_buck:
            p = self.pools[addr]
            sp = int(p.functions.slot0().call()[0])
            t0, t1 = self.pairs[addr]
            depth = cfg["market"]["depth_usd"] * E6
            liquidity = depth * sp // Q96 if t0 == d.buck.address else depth * Q96 // sp
            self.chain.send(d.simlp.functions.mint(addr, lo, hi, liquidity, t0, t1))
        # Start every venue at the same unperturbed phase, using funded swaps
        # before the live experiment. The known mean defines the reference
        # quantities; it is not the first instantaneous price of all tokens.
        self.drive(baseline_prices(cfg, 0), replay=True)
        self.warp(int(d.w3.eth.get_block("latest")["timestamp"]) + 601)
        self.reference_amounts = [int(d.basket.functions.constituents(i).call()[2]) for i in range(3)]
        venue = self.chain.deploy("ValidatedBasketVenue")
        self.chain.send(d.basket.functions.setVenue(venue.address), sender=d.gov)
        self.chain.send(d.desk.functions.setVenue(venue.address), sender=d.gov)
        for i, token in enumerate(d.tokens):
            amount = round(cfg["market"]["basket_equity_usd"] / 3 / cfg["feed"]["base"][i] * E18)
            self.execute(d.simlp, token, "approve", [d.basket.address, amount])
            self.execute(d.simlp, d.basket, "depositToken", [token.address, amount, 0])
        warm = bool(cfg["bootstrap"]["basket_warmup_days"])
        mon = cfg["monitor"] if self.flags.get("monitor") or warm else DEFAULTS["monitor"]
        self.signals = self.chain.deploy("BasketSignals", d.basket.address,
                                        mon["interval"], mon["max_age"], mon["deviation_bp"])
        response = self.chain.deploy("BuckBasketEquityResponse")
        self.chain.send(d.basket.functions.setEquityWheel(response.address), sender=d.gov)
        rp = cfg["basket"] if self.flags.get("basket") or warm else DEFAULTS["basket"]
        equity_params = list(d.basket.functions.eq().call())
        if equity_params[6] != rp["weight_band_bp"]:
            equity_params[6] = rp["weight_band_bp"]
            self.chain.send(d.basket.functions.setEquityParams(tuple(equity_params)), sender=d.gov)
        self.chain.send(d.basket.functions.setResponse(self.signals.address,
            (rp["leash_bp"], mon["interval"], round(rp["tilt"] * E18))), sender=d.gov)
        self.chain.send(d.wheel.functions.setResponseMode(True), sender=d.gov)
        self.chain.send(d.wheel.functions.setEquity(d.basket.address if self.flags.get("basket") else ZERO), sender=d.gov)
        self.chain.send(d.wheel.functions.setDirector(ZERO), sender=d.gov)
        self.chain.send(d.wheel.functions.setReserveParams(0, 0), sender=d.gov)
        self.chain.send(d.wheel.functions.setController(d.kctrl.address if self.flags.get("k") else ZERO), sender=d.gov)
        # Retune the lab horizon explicitly to its servicing cadence, in all arms.
        self.chain.send(d.kctrl.functions.setDTMax(86400 // cfg["ticks_per_day"]), sender=d.gov)

        desk = cfg["desk"] if self.flags.get("desk") else DEFAULTS["desk"]
        # deploy() already granted the default capital. Adjust only this desk's
        # private inventory when a stage actually uses a different grant.
        if desk["capital_per_token"] != DEFAULTS["desk"]["capital_per_token"]:
            raise ValueError("capital_per_token is fixed at 400000 for matched initial books; vary budget/floor instead")
        self.policy = self.chain.deploy("OuterBandMonetaryDirector", self.signals.address,
            desk["enter_bp"], desk["release_bp"], desk["gain_bp"], desk["cap_bp"], desk["persist_days"])
        self.chain.send(d.desk.functions.setMonetaryDirector(self.policy.address), sender=d.gov)
        self.chain.send(d.desk.functions.setExecutionGuard(self.policy.address), sender=d.gov)
        self.chain.send(d.desk.functions.setRiskBudget(round(desk["turnover_buck"] * E6),
            round(desk["capital_floor_buck"] * E6), mon["deviation_bp"]), sender=d.gov)
        self.chain.send(d.wheel.functions.setOps(d.desk.address if self.flags.get("desk") else ZERO,
            self.policy.address if self.flags.get("desk") else ZERO), sender=d.gov)
        source = self.policy if self.flags.get("desk") else self.signals
        self.chain.send(d.wheel.functions.setSample(source.address if self.flags.get("monitor") else ZERO), sender=d.gov)

        # One consistency arbitrage actor, with a separate wheel so its
        # before/after-policy turns have explicit, reproducible timing.
        self.arb = self.proxy("BasketMachineWheel", d.buck.address, d.usdc.address, d.gov, 0, 0, carrying=True)
        self.basket_arb = cfg["market"].get("arb_beneficiary", "external") == "basket"
        if self.basket_arb:
            self.chain.send(d.basket.functions.setArbitrageCreditor(self.arb.address), sender=d.gov)
        self.chain.send(self.arb.functions.setArb(d.pool_ub, d.basket.address if self.basket_arb else ZERO,
            0, cfg["market"]["arb_cap_bp"], 1), sender=d.gov)
        self.chain.send(self.arb.functions.setStartMode(1), sender=d.gov)
        for i in range(3):
            self.chain.send(self.arb.functions.setTriangle(i,
                (d.tokens[i].address, d.pool_buck[i], d.pool_usdc[i], i)), sender=d.gov)
        self.buyer = self.proxy("SimLP", carrying=True)
        self.supplier = self.proxy("SimLP", carrying=False)
        dem = cfg["demand"] if self.flags.get("demand") else DEFAULTS["demand"]
        sup = cfg["supply"] if self.flags.get("supply") else DEFAULTS["supply"]
        self.chain.send(d.usdc.functions.mint(self.buyer.address, round(dem["budget_usdc"] * E6)))
        self.credit(self.supplier, sup["credit_face_buck"])
        self.counters = dict(arb_cycles=0, arb_profit_buck=0., desk_ops=0, demand_usdc=0., supply_buck=0.,
                             demand_trades=0, supply_trades=0)
        self.arb_income = dict(nominal_buck=0., net_buck=0., net_usd=0., arrival_fees_buck=0.)
        # Start on a UTC boundary after every oracle has sufficient history.
        self.start = (int(d.w3.eth.get_block("latest")["timestamp"]) // 86400 + 1) * 86400
        self.bootstrap_gas = self.gas
        self.bootstrap_transactions = self.transactions
        self.initial_market_book = self.market_book()
        self.bootstrap = dict(prices=self.prices(), market=self.initial_market_book,
            reference_amounts=[str(x) for x in self.reference_amounts],
            reference_at_means=self.reference_cost(cfg["feed"]["base"]),
            reference_at_start=self.reference_cost(baseline_prices(cfg, 0)))
        from .events import ExecutionRecorder
        self.recorder = ExecutionRecorder(self)
        self.events = []

    def close(self):
        self.evm.__exit__(None, None, None)

    def warp(self, stamp):
        self.evm.warp_to(stamp)
        self.chain.clear_balance_cache()

    def execute(self, proxy, contract, method, args):
        return self.chain.send(proxy.functions.exec(contract.address, contract.encode_abi(method, args=args)))

    def proxy(self, name, *args, carrying=False):
        from alberta_buck.sim.identity import bind_as_operator
        p = self.chain.deploy(name, *args)
        bind_as_operator(self.chain, self.d.reg, p.address, True, carrying, sender=self.chain.deployer)
        return p

    def credit(self, proxy, face):
        d = self.d
        self.execute(proxy, d.credit, "setCreditIssuer", [self.chain.deployer, True])
        face = round(face * E6)
        now = int(d.w3.eth.get_block("latest")["timestamp"])
        self.chain.send(d.credit.functions.createCredit(proxy.address, 0, face, 0, 0, 0, now, 0))
        self.execute(proxy, d.buck, "mint(uint256)", [face * d.kctrl.functions.buckK().call() // E18])

    def price(self, addr, base, base_dec, quote_dec=6):
        sp = int(self.pools[addr].functions.slot0().call()[0])
        ratio = sp * sp / (1 << 192)
        return (ratio if self.pairs[addr][0] == base else 1 / ratio) * 10**(base_dec - quote_dec)

    def prices(self):
        d = self.d
        return {"token_usdc": [self.price(a, t.address, 18) for a, t in zip(d.pool_usdc, d.tokens)],
                "token_buck": [self.price(a, t.address, 18) for a, t in zip(d.pool_buck, d.tokens)],
                "buck_usdc": self.price(d.pool_ub, d.buck.address, 6)}

    def reference_cost(self, prices):
        return sum(a * round(p * E6) // E18 for a, p in zip(self.reference_amounts, prices)) / E18

    def market_book(self):
        """Actual BUCK custody versus active V3 depth; no inferred burns.

        At fixed liquidity the change in pool-minus-virtual reserves is
        uncollected BUCK fees. When LP positions change it also includes
        changes in the finite-range virtual offset; retain the raw quantity.
        """
        d = self.d
        active = held = 0
        for addr in [*d.pool_buck, d.pool_ub]:
            pool = self.pools[addr]
            sp = int(pool.functions.slot0().call()[0])
            liquidity = int(pool.functions.liquidity().call())
            active += (liquidity * Q96 // sp if self.pairs[addr][0] == d.buck.address
                       else liquidity * sp // Q96)
            held += int(d.buck.functions.signedBalanceOf(addr).call())
        return dict(active_buck=active / E6, pool_buck=held / E6,
            pool_minus_virtual_buck=(held - active) / E6,
            arb_held_buck=int(d.buck.functions.signedBalanceOf(self.arb.address).call()) / E6)

    def drive(self, target, *, replay=None):
        from alberta_buck.sim.router import sqrt_price_x96
        d = self.d
        groups = [(d.pool_usdc, d.usdc)]
        if replay is None:
            replay = self.flags.get("replay", False)
        if replay:
            groups.append((d.pool_buck, d.buck))
        for pools, quote in groups:
            for i, addr in enumerate(pools):
                sp = sqrt_price_x96(d.tokens[i].address, E18, quote.address, round(target[i] * E6))
                current = int(self.pools[addr].functions.slot0().call()[0])
                if current != sp:
                    self.chain.send(d.simlp.functions.swap(addr, d.simlp.address, sp < current,
                        (1 << 127) - 1, sp, *self.pairs[addr]))

    def arbitrage(self, phase="arbitrage"):
        if not self.flags.get("arb"):
            return
        from web3.logs import DISCARD
        def account():
            # Accrued relief is already an asset. Settling it is not arb income.
            return (int(self.d.buck.functions.signedBalanceOf(self.d.basket.address).call())
                    + int(self.d.buck.functions.reliefOf(self.d.basket.address).call()))
        for _ in range(self.cfg["market"]["arb_rounds"]):
            self.chain.send(self.arb.functions.rearm())
            before = account() if self.basket_arb else 0
            rc = self.chain.send(self.arb.functions.tick(3, 12))
            events = self.arb.events.Cycled().process_receipt(rc, errors=DISCARD)
            self.counters["arb_cycles"] += len(events)
            self.counters["arb_profit_buck"] += sum(int(e["args"]["profit"]) for e in events) / E6
            if self.basket_arb:
                nominal = sum(int(e["args"]["credited"]) for e in events)
                profit = sum(int(e["args"]["profit"]) for e in events)
                if nominal != profit:
                    raise AssertionError("Basket-owned arb failed to credit its whole profit")
                net = account() - before
                if not 0 <= net <= nominal + 1:
                    raise AssertionError(f"Arb credit accounting mismatch: {net=} {nominal=}")
                self.arb_income["nominal_buck"] += nominal / E6
                self.arb_income["net_buck"] += net / E6
                self.arb_income["net_usd"] += net / E6 * self.prices()["buck_usdc"]
                self.arb_income["arrival_fees_buck"] += (nominal - net) / E6
            self.events.extend(self.recorder.capture(rc, phase))
            if not events:
                break

    def agents(self):
        from alberta_buck.sim.router import MIN_SQRT_RATIO, MAX_SQRT_RATIO
        d, cfg = self.d, self.cfg
        # Execute one buyer and one supplier at most once per step. Reverse
        # their ordering on alternate steps to avoid a permanent first mover.
        names = ["demand", "supply"] if self.step % 2 == 0 else ["supply", "demand"]
        for name in names:
            if not self.flags.get(name):
                continue
            p = self.prices()
            fair = self.reference_cost(p["token_usdc"])
            ac = cfg[name]
            if ac["rate_at_par_per_day"] == 0:
                continue
            premium = p["buck_usdc"] / fair - 1
            adverse = premium if name == "demand" else -premium
            # Intent exists at par and through a modest adverse price. A
            # favourable price increases the rate, up to the same daily cap.
            # Bound the exponent before exp so extreme quotes cannot overflow.
            exponent = min(-adverse / (ac["backoff_bp"] / 10000),
                           math.log(ac["max_per_day"] / ac["rate_at_par_per_day"]))
            value = ac["rate_at_par_per_day"] * math.exp(exponent)
            value /= cfg["ticks_per_day"]
            proxy, asset = (self.buyer, d.usdc) if name == "demand" else (self.supplier, d.buck)
            amount = min(round(value * E6), int(asset.functions.balanceOf(proxy.address).call()))
            if amount < E6:
                continue
            zero_for_one = self.pairs[d.pool_ub][0] == asset.address
            limit = MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1
            self.chain.send(proxy.functions.swap(d.pool_ub, proxy.address, zero_for_one,
                amount, limit, *self.pairs[d.pool_ub]))
            self.counters["demand_usdc" if name == "demand" else "supply_buck"] += amount / E6
            self.counters[name + "_trades"] += 1

    def snapshot(self, row, before, invalid_after_move):
        d, sig = self.d, self.signals
        p = self.prices()
        frame = {**row, **p, "before_policy": before, "cumulative": dict(self.counters), "events": self.events,
                 "gas": self.gas - self.bootstrap_gas,
                 "transactions": self.transactions - self.bootstrap_transactions}
        frame["bvib"] = int(d.basket.functions.basketValueInBuck().call()) / E18
        frame["reference"] = dict(target_usd=self.reference_cost(row["target_usd"]),
            market_usd=self.reference_cost(p["token_usdc"]),
            spot_buck=self.reference_cost(p["token_buck"]))
        frame["market"] = self.market_book()
        frame["k"] = int(d.kctrl.functions.buckK().call()) / E18
        frame["p_error"] = int(d.kctrl.functions.P().call()) / 1e6
        frame["p_contribution"] = int(d.kctrl.functions.P().call()) * int(d.kctrl.functions.Kp().call()) / E18
        frame["i_contribution"] = int(d.kctrl.functions.I().call()) * int(d.kctrl.functions.Ki().call()) / E18
        frame["controller_raw"] = {key: str(getattr(d.kctrl.functions, key)().call()) for key in ("P", "I", "D", "buckK")}
        if self.flags.get("monitor"):
            fresh = bool(sig.functions.fresh().call())
            ladders = [sig.functions.ladder(i).call() for i in range(3)]
            frame["signal"] = dict(fresh=fresh, invalid_after_move=invalid_after_move,
                history_ready=bool(sig.functions.historyReady().call()),
                sampled_at=int(sig.functions.lastSampleAt().call()),
                raw=int(sig.functions.rawValue().call()) / E18 if fresh else None,
                consecutive_days=int(sig.functions.consecutiveDays().call()),
                relative=[int(sig.functions.deviation(i).call()) / E18 for i in range(3)],
                ema=[[int(x) / E18 for x in l[0]] for l in ladders],
                velocity=[[int(x) / E18 for x in l[1]] for l in ladders],
                excursion=[int(sig.functions.excursion(i).call()) / E18 for i in range(3)],
                votes=[int(sig.functions.votes(i).call()) for i in range(3)],
                common=[int(sig.functions.common(i).call()) / E18 for i in range(7)])
        frame["desk"] = dict(account=int(d.buck.functions.signedBalanceOf(d.desk.address).call()) / E6,
            capital=int(d.desk.functions.netValue().call()) / E6,
            budget=int(d.desk.functions.turnoverRemaining().call()) / E6,
            side=int(self.policy.functions.side().call()), over_days=int(self.policy.functions.overDays().call()))
        frame["agents"] = dict(demand_cash=int(d.usdc.functions.balanceOf(self.buyer.address).call()) / E6,
            demand_held=int(d.buck.functions.signedBalanceOf(self.buyer.address).call()) / E6,
            supply_account=int(d.buck.functions.signedBalanceOf(self.supplier.address).call()) / E6,
            supply_headroom=int(d.buck.functions.balanceOf(self.supplier.address).call()) / E6)
        frame["basket"] = dict(equity=int(d.basket.functions.equity().call()) / E6,
            weights=list(d.basket.functions.responseWeightsBp().call()),
            account=int(d.buck.functions.signedBalanceOf(d.basket.address).call()) / E6,
            # The book itself, so a reader can revalue it at prices of their choosing.
            relief=int(d.buck.functions.reliefOf(d.basket.address).call()) / E6,
            idle=[int(d.basket.functions.idleToken(i).call()) / E18 for i in range(3)],
            liquidity=[str(d.basket.functions.liquidityOf(i).call()) for i in range(3)])
        frame["basket"]["arb_income"] = dict(beneficiary="basket" if self.basket_arb else "external", **self.arb_income)
        return frame

    def before_policy(self):
        """Optional experiment instrumentation after the oracle settling window."""

    def run(self, rows):
        from web3.logs import DISCARD
        from web3.exceptions import ContractLogicError, Web3RPCError
        from alberta_buck.sim.signal_history import initialize_history
        self.history = None
        # LP bootstrapping can leave pools at the historical means. Set both
        # quote routes to the actual opening feed, then mature their TWAPs
        # before any live arbitrage, observation or monetary action. Historical
        # EMA anchors still exclude all selected live perturbations.
        self.drive(rows[0]["target_usd"], replay=True)
        settled = int(self.d.w3.eth.get_block("latest")["timestamp"]) + 601
        self.start = (settled // 86400 + 1) * 86400
        self.warp(self.start)
        self.initial_market_book = self.market_book()
        self.bootstrap.update(prices=self.prices(), market=self.initial_market_book,
                              opening_target_usd=rows[0]["target_usd"], opening_twap_settled=True,
                              reference_at_start=self.reference_cost(rows[0]["target_usd"]))
        if self.flags.get("monitor"):
            self.history = initialize_history(self.chain, self.signals, self.d.basket,
                self.d.gov, history_rows(self.cfg, getattr(self, "profile", None)), self.cfg["feed"]["base"])
            self.history["perturbations_included"] = False
        self.bootstrap_gas = self.gas
        self.bootstrap_transactions = self.transactions
        frames = []
        for self.step, row in enumerate(rows):
            self.events = []
            stamp = self.start + self.step * (86400 // self.cfg["ticks_per_day"])
            self.warp(stamp)
            self.drive(row["target_usd"])
            self.arbitrage("after-feed")
            self.agents()
            self.arbitrage("after-agents")
            invalid_after_move = False
            if self.flags.get("monitor"):
                # Probe quality immediately, without letting a pre-move TWAP
                # become the day's normal sample. Publish actual invalidity
                # on a failed probe; the scheduled observation follows after
                # one full TWAP window, whether the probe succeeded or not.
                try:
                    self.signals.functions.readPrices().call()
                except (ContractLogicError, Web3RPCError):
                    invalid_after_move = True
                    self.chain.send(self.signals.functions.observe())
            self.warp(stamp + 601)
            before = self.prices()
            self.before_policy()
            if self.flags.get("monitor"):
                self.chain.send(self.d.wheel.functions.rearm())
                rc = self.chain.send(self.d.wheel.functions.tick(12, 32))
                self.counters["desk_ops"] += len(self.d.desk.events.MonetaryOperation().process_receipt(rc, errors=DISCARD))
                self.events.extend(self.recorder.capture(rc, "policy"))
            self.arbitrage("after-policy")
            frames.append(self.snapshot(row, before, invalid_after_move))
        return frames


def simulate(cfg, profile, stage, world_factory=None):
    started = time.perf_counter()
    rows = feed_rows(cfg, profile)
    with isolated_environment(cfg):
        world = (world_factory or World)(cfg, stage)
        try:
            world.profile = profile
            frames = world.run(rows)
            runtime = time.perf_counter() - started
            errors = [abs(f["bvib"] - 1) for f in frames]
            result = {"schema": 1, "backend": "pyrevm", "stage": stage, "flags": STAGES[stage],
                    "profile": profile, "config": cfg, "history": world.history,
                    "bootstrap": world.bootstrap, "frames": frames,
                    "summary": {"seconds": round(runtime, 3), "samples": len(frames),
                        "mean_abs_parity_error": sum(errors) / len(errors),
                        "end_abs_parity_error": errors[-1],
                        "end_k": frames[-1]["k"], **world.counters,
                        "gas": world.gas - world.bootstrap_gas,
                        "bootstrap_gas": world.bootstrap_gas,
                        "transactions": world.transactions - world.bootstrap_transactions}}
            if "history" in profile:
                from .historical import historical_inputs
                result["feed_source"] = historical_inputs(cfg, profile)["provenance"]
            if "commodities" in profile:
                from .commodities import commodity_inputs
                result["feed_source"] = commodity_inputs(cfg, profile)["provenance"]
            return result
        finally:
            world.close()
