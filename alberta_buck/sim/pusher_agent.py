"""WP-15: two SimLP-style authority injectors for the controller-alternatives
grid (WAVE3.org, "The controller-alternatives test plan", classes 7 and 10).

PusherAgent -- class 7, the GUARD TRIP.  Every `every_days` days from
`start_day`, at one tick, it swaps enough in ONE TOKEN/BUCK pool to move
that pool's spot past the basket's spot/TWAP slippage guard
(BuckBasketUniswapV3._enforceSlippageGuard: defaultMaxDeviationBp 500 bp
against a 600 s TWAP in the sim deploy), holds for one tick and swaps back
to the pre-push price.  Inside the trip the desk's NAV is unreadable:
poolBuckValues() reverts Slippage, so BuckBasketOps.positionCap() reverts
NavUnreadable and monetaryOperation() cannot run; the observer's policy
(WAVE3.org decision 9) holds the desk's last good cap and flags it stale
(frame sh_stale / sh_desk_stale), while K's own process variable
(basketValueInBuck, a TWAP read with a spot fallback) never reverts.

The trip is visible only INSIDE the tick of the push: the TWAP over the
last 600 s still reads the old price until time advances, so a cycle that
runs in the same tick after the push sees spot != TWAP, and the frame
captured at the end of the day sees it when the push tick is the day's
last (the default push_tick = -1).  With `poke` (default 1) the pusher
calls the observer's permissionless refresh() after each swap, so the
observer registers the trip whether or not a keeper's cycle happened to
run inside the window; the next successful read clears it.

The push is a price-limit swap of TOKEN into the pool from the agent's own
SimLP proxy (minted TOKEN, no market impact until the swap), so the BUCK it
receives is HELD (a positive balance, supply unchanged) and paid back on the
unwind; the unwind is an exact-input swap of that BUCK with the pre-push
sqrtPrice as its limit, so it stops one fee-width short of the old price
and the cast arbs the residue.

  knobs ([agents.PusherAgent], all _spec -- no draws, no rng stream):
    start_day 365   every_days 10   push_bp 800 (> the 500 bp guard)
    push_tick -1 (the day's last tick)   pool -1 (rotate pools)   poke 1
    stop_day 0 (0 = the horizon)
  counters (ctr -> frame, only when the agent exists): pu_pushes,
    pu_unwinds, pu_pool (the last pool index), pu_move_bp (the realized
    spot move at the last push), pu_tripped (pushes after which the desk's
    positionCap() reverted), pu_err.

LpExitAgent -- class 10, the LIQUIDITY WITHDRAWAL.  At `exit_day` (365)
tick 0 it removes `frac` (0.5) of the private BUCK/USDC depth.  The
catalogue cast has no BuckPoolInvestorAgent, so the private depth is the
SimLP-seeded full-range position (deploy.target_buck_lp_m): venue "ub"
burns `frac` of that position through SimLP's exec passthrough and
collects the burned principal (the Burn event's amounts, not the fees) to
the agent's own proxy, where the BUCK and USDC are HELD -- the LP left with
its assets; supply is unchanged and the seed's fee accounting continues on
the remainder.  NB the observer's D (BuckBasketOps.shadowDepth) is the
BUCK in the TOKEN/BUCK basket pools, so venue "ub" thins the exit route
and the arbs' gates without moving D; venue "basket" deposits `depth_m`
of TOKEN into the basket at bootstrap (split by basket weight) and redeems
every receipt at exit_day, so D itself falls -- the design-S D-dependence
test, at the price of a different pre-history from the none arm.

  knobs ([agents.LpExitAgent], all _spec): exit_day 365, frac 0.5,
    venue "ub" | "basket", depth_m 10.0 (venue basket)
  counters: lx_exits, lx_venue, lx_frac, lx_liq_before, lx_liq_after (the
    BUCK/USDC pool's active liquidity, or the basket pools' BUCK depth),
    lx_buck, lx_usdc (collected), lx_err.

Determinism (TELEMETRY.md): neither agent draws from an rng stream; both
are absent at count 0, so every existing cell is byte-identical.
"""

from __future__ import annotations

import math

from web3 import Web3

from alberta_buck.sim.agents import _register
from alberta_buck.sim.chain import load_artifact
from alberta_buck.sim.equilibrium_agents import (
    ExcursionArbAgent, _ProxyAgent, _pool_tokens,
)
from alberta_buck.sim.experiment import spec as _spec
from alberta_buck.sim.gauge import pool_state
from alberta_buck.sim.router import MAX_SQRT_RATIO, MIN_SQRT_RATIO

E6 = 10 ** 6
E18 = 10 ** 18
HUGE = (1 << 127) - 1
BIG = 10 ** 30
U128 = 2 ** 128 - 1
BURN_TOPIC = Web3.keccak(text="Burn(address,int24,int24,uint128,uint256,uint256)")
DEPOSITED_KEY = "deposited_topic"


def _hexbytes(x) -> bytes:
    if isinstance(x, (bytes, bytearray)):
        return bytes(x)
    s = str(x)
    return bytes.fromhex(s[2:] if s.startswith("0x") else s)


def _pool(d, addr: str):
    abi, _ = load_artifact("UniswapV3Pool")
    return d.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=abi)


def _basket_depth(d) -> int:
    """The observer's D: BUCK in the TOKEN/BUCK basket pools (6-dec)."""
    return sum(d.chain.balance_of(d.buck, p) for p in d.pool_buck)


@_register
class PusherAgent(_ProxyAgent):
    """The one-pool guard-trip injector (module docstring)."""

    CTR = "pu"

    def setup(self, d, scenario, rng) -> None:
        cls = type(self).__name__
        self.start_day = int(_spec(scenario, cls, "start_day", 365))
        self.every_days = max(1, int(_spec(scenario, cls, "every_days", 10)))
        self.push_bp = int(_spec(scenario, cls, "push_bp", 800))
        self.push_tick = int(_spec(scenario, cls, "push_tick", -1))
        self.pool = int(_spec(scenario, cls, "pool", -1))
        self.poke = int(_spec(scenario, cls, "poke", 1))
        self.stop_day = int(_spec(scenario, cls, "stop_day", 0))
        self._bind_proxy(d)
        for tok in d.tokens:                      # the authority's TOKEN
            d.chain.send(tok.functions.mint(self.proxy.address, BIG))
        self.pushes = self.unwinds = self.tripped = 0
        self.move_bp = 0
        self._pool_i = -1
        self._sp_pre = 0
        self._push_at: tuple[int, int] | None = None
        self._pending = False

    def bootstrap(self, d, scenario, ctr) -> None:
        for k in ("pu_pushes", "pu_unwinds", "pu_pool", "pu_move_bp",
                  "pu_tripped"):
            ctr.setdefault(k, 0)
        ctr.setdefault("pu_err", "")

    def telemetry_static(self) -> dict:
        return {"start_day": self.start_day, "every_days": self.every_days,
                "push_bp": self.push_bp, "push_tick": self.push_tick,
                "pool": self.pool, "poke": self.poke}

    def telemetry(self, d) -> dict | None:
        if self.proxy is None:
            return None
        rec = self._telemetry_common(d)
        rec.update({"pushes": self.pushes, "unwinds": self.unwinds,
                    "tripped": self.tripped, "pool": self._pool_i,
                    "move_bp": self.move_bp, "pending": int(self._pending)})
        return rec

    # -- the swaps ---------------------------------------------------------- #

    def _refresh(self, d) -> None:
        obs = getattr(d, "observer", None)
        if obs is None or not self.poke:
            return
        try:
            d.chain.send(obs.functions.refresh())
        except Exception:
            pass

    def _cap_reverts(self, d) -> bool:
        dk = d.desk if getattr(d, "desk", None) is not None else d.basket
        try:
            dk.functions.positionCap().call()
            return False
        except Exception:
            return True

    def _push(self, d, i: int) -> None:
        pool = d.pool_buck[i]
        t0, t1 = _pool_tokens(d, pool)
        tok = d.tokens[i]
        sp0, _tick, _liq, _t0, _t1 = pool_state(d.chain, pool)
        zero_for_one = tok.address.lower() == t0.lower()
        move = self.push_bp / 10_000.0
        factor = math.sqrt(max(1e-9, 1.0 - move)) if zero_for_one \
            else math.sqrt(1.0 + move)
        target = int(sp0 * factor)
        target = max(MIN_SQRT_RATIO + 1, min(MAX_SQRT_RATIO - 1, target))
        d.chain.send(self.proxy.functions.swap(
            pool, self.proxy.address, zero_for_one, HUGE, target, t0, t1))
        sp1 = pool_state(d.chain, pool)[0]
        self._sp_pre, self._pool_i = sp0, i
        self.move_bp = int(round(abs((sp1 / sp0) ** 2 - 1.0) * 10_000))
        self.pushes += 1
        self._pending = True
        self._refresh(d)
        if getattr(d, "basket_impl", "") in ("ops", "equity-ops") and self._cap_reverts(d):
            self.tripped += 1

    def _unwind(self, d) -> None:
        i = self._pool_i
        pool = d.pool_buck[i]
        t0, t1 = _pool_tokens(d, pool)
        zero_for_one = d.buck.address.lower() == t0.lower()
        held = max(0, int(d.buck.functions.balanceOf(self.proxy.address).call()))
        limit = max(MIN_SQRT_RATIO + 1, min(MAX_SQRT_RATIO - 1, self._sp_pre))
        if held >= E6:
            d.chain.send(self.proxy.functions.swap(
                pool, self.proxy.address, zero_for_one, held, limit, t0, t1))
        self.unwinds += 1
        self._pending = False
        self._refresh(d)

    def act(self, d, scenario, day, tick, ctr) -> None:
        if self.proxy is None or not d.pool_buck:
            return
        ticks = max(1, int(getattr(scenario, "ticks_per_day", 1)))
        push_tick = self.push_tick if self.push_tick >= 0 else ticks - 1
        try:
            if self._pending and (day, tick) != self._push_at:
                self._unwind(d)
            elif (not self._pending and tick == push_tick
                  and day >= self.start_day
                  and (not self.stop_day or day < self.stop_day)
                  and (day - self.start_day) % self.every_days == 0):
                k = (day - self.start_day) // self.every_days
                i = self.pool if self.pool >= 0 else k % len(d.pool_buck)
                self._push_at = (day, tick)
                self._push(d, i % len(d.pool_buck))
        except Exception as e:
            ctr["pu_err"] = repr(e)[:160]
        ctr["pu_pushes"] = self.pushes
        ctr["pu_unwinds"] = self.unwinds
        ctr["pu_pool"] = self._pool_i
        ctr["pu_move_bp"] = self.move_bp
        ctr["pu_tripped"] = self.tripped


@_register
class LpExitAgent(_ProxyAgent):
    """The liquidity-withdrawal injector (module docstring)."""

    CTR = "lx"

    def setup(self, d, scenario, rng) -> None:
        cls = type(self).__name__
        self.exit_day = int(_spec(scenario, cls, "exit_day", 365))
        self.frac = float(_spec(scenario, cls, "frac", 0.5))
        self.venue = str(_spec(scenario, cls, "venue", "ub"))
        self.depth_m = float(_spec(scenario, cls, "depth_m", 10.0))
        self._bind_proxy(d)
        self._receipts: list[int] = []
        self.exits = 0
        self.buck = self.usdc = 0
        self._done = False

    def bootstrap(self, d, scenario, ctr) -> None:
        for k in ("lx_exits", "lx_frac", "lx_liq_before", "lx_liq_after",
                  "lx_buck", "lx_usdc"):
            ctr.setdefault(k, 0)
        ctr["lx_frac"] = self.frac
        ctr["lx_venue"] = self.venue
        ctr.setdefault("lx_err", "")
        if self.venue == "basket":
            try:
                self._deposit(d, scenario)
            except Exception as e:
                ctr["lx_err"] = repr(e)[:160]

    def telemetry_static(self) -> dict:
        return {"exit_day": self.exit_day, "frac": self.frac,
                "venue": self.venue, "depth_m": self.depth_m}

    def telemetry(self, d) -> dict | None:
        if self.proxy is None:
            return None
        rec = self._telemetry_common(d)
        rec.update({"exits": self.exits, "buck": self.buck, "usdc": self.usdc,
                    "receipts": len(self._receipts)})
        return rec

    # -- venue basket: a depositor that leaves --------------------------------- #

    def _deposit(self, d, scenario) -> None:
        """TOKEN worth depth_m $M at the day-0 references, in basket
        proportions, deposited from the proxy (it holds the receipt NFTs)."""
        total = int(self.depth_m * 1_000_000 * E6)
        for i, tok in enumerate(d.tokens):
            w = ExcursionArbAgent._weight(scenario, d, i)
            ref = max(1, scenario.prices.ref(i, 0))
            amt = int(total * w) * (10 ** d.dec[i]) // ref
            if amt <= 0:
                continue
            d.chain.send(tok.functions.mint(self.proxy.address, amt))
            self._proxy_exec(d, tok.address, tok.encode_abi(
                "approve", args=[d.basket.address, amt]))
            rcpt = self._proxy_exec(d, d.basket.address, d.basket.encode_abi(
                "depositToken", args=[tok.address, amt, 0]))
            topic = getattr(d, DEPOSITED_KEY, None)
            for log in rcpt["logs"]:
                if topic is not None and log["topics"][0] == topic:
                    self._receipts.append(
                        int.from_bytes(_hexbytes(log["topics"][2]), "big"))
                    break

    def _redeem_all(self, d) -> None:
        for rid in list(self._receipts):
            self._proxy_exec(d, d.basket.address, d.basket.encode_abi(
                "redeem", args=[rid, 0, 0]))
            self._receipts.remove(rid)
            self.exits += 1

    # -- venue ub: the seed LP burns frac of its position ------------------- #

    def _burn_seed(self, d) -> None:
        meta = next((m for m in d.pool_meta if m[4] == "ub"), None)
        if meta is None:
            raise RuntimeError("no BUCK/USDC seed position in pool_meta")
        pool_addr, owner, lo, hi, _grp = meta
        pool = _pool(d, pool_addr)
        key = Web3.solidity_keccak(["address", "int24", "int24"],
                                   [Web3.to_checksum_address(owner), lo, hi])
        L = int(pool.functions.positions(key).call()[0])
        burn = int(L * self.frac)
        if burn <= 0:
            return
        rcpt = d.chain.send(d.simlp.functions.exec(
            pool_addr, pool.encode_abi("burn", args=[lo, hi, burn])))
        a0 = a1 = 0
        for log in rcpt["logs"]:
            if (log["address"].lower() == pool_addr.lower()
                    and _hexbytes(log["topics"][0]) == bytes(BURN_TOPIC)):
                data = _hexbytes(log["data"])
                a0 = int.from_bytes(data[32:64], "big")
                a1 = int.from_bytes(data[64:96], "big")
                break
        d.chain.send(d.simlp.functions.exec(
            pool_addr, pool.encode_abi(
                "collect", args=[self.proxy.address, lo, hi,
                                 min(a0, U128), min(a1, U128)])))
        t0 = pool.functions.token0().call()
        if d.buck.address.lower() == t0.lower():
            self.buck, self.usdc = a0, a1
        else:
            self.buck, self.usdc = a1, a0
        self.exits += 1

    def act(self, d, scenario, day, tick, ctr) -> None:
        if self.proxy is None or self._done or tick != 0 or day < self.exit_day:
            return
        self._done = True
        try:
            if self.venue == "basket":
                ctr["lx_liq_before"] = _basket_depth(d)
                self._redeem_all(d)
                ctr["lx_liq_after"] = _basket_depth(d)
            else:
                pool = _pool(d, d.pool_ub)
                ctr["lx_liq_before"] = int(pool.functions.liquidity().call())
                self._burn_seed(d)
                ctr["lx_liq_after"] = int(pool.functions.liquidity().call())
        except Exception as e:
            ctr["lx_err"] = repr(e)[:160]
        ctr["lx_exits"] = self.exits
        ctr["lx_buck"] = self.buck
        ctr["lx_usdc"] = self.usdc
