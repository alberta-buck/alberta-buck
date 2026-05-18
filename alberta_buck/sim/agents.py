"""Pluggable agents.

`Agent` subclasses register themselves in `REGISTRY` by class name; a
scenario lists how many of each to spawn.  Two ship today:

  * AnonymousArbAgent -- a registered EOA holding only USDC + RWA TOKENs.
    Each tick it scans USDC->x->BUCK->y->USDC cycles with the exact
    reserve quoter and, when one clears its margin, executes it via the
    Universal Router pre-fund route (BUCK is router-internal; the agent
    never holds BUCK).  This is the BUCK-unaware optimized routing that
    *uses the TOKEN/BUCK pools as part of TOKEN routes*.
  * MarketMakerWhale -- a registered EOA with ~unlimited resources that,
    once per day at a random tick, snaps every TOKEN/USDC pool's spot to
    that day's CSV reference (the exogenous truth driver), executed
    through the SimLP helper.

Adding a new agent type: subclass Agent, implement act(); it is picked up
by REGISTRY automatically and selectable from a Scenario's `agents` map.
"""

from __future__ import annotations

from web3 import Web3

from alberta_buck.sim import identity as idmod
from alberta_buck.sim.chain import load_artifact
from alberta_buck.sim.router import (
    quote_path, encode_path, ur_exec_args, sqrt_price_x96,
)

REGISTRY: dict = {}


def _register(cls):
    REGISTRY[cls.__name__] = cls
    return cls


class Agent:
    is_eoa = True

    def __init__(self, idx: int):
        self.idx = idx
        self.account = None        # eth_account LocalAccount (set in setup)

    @property
    def address(self) -> str:
        return self.account.address

    # registered as a real cryptographic identity
    def setup(self, d, scenario, rng) -> None:
        self.account = d.chain.new_account()
        d.anvil.set_balance(self.address, 100 * 10 ** 18)
        addr_int = int(self.address, 16)
        args = idmod.register_args(d.issuer_kp, addr_int,
                                   idmod.fields_for(type(self).__name__, self.idx),
                                   rng)
        d.chain.send(
            d.reg.functions.register(d.issuer_addr, *args),
            sender=self.account, gas=3_000_000,
        )

    def act(self, d, scenario, day, tick, ctr) -> None:  # pragma: no cover
        raise NotImplementedError


@_register
class AnonymousArbAgent(Agent):
    USDC_SEED = 2_000_000 * 10 ** 6      # scaled $2M
    POOL_FRAC_BP = 10                     # tiny fill (0.1% of binding pool):
                                          # slippage << edge so the exact
                                          # quoter detects real opportunities
    MARGIN_BP = 15                        # cycle must beat input by >0.15%

    def setup(self, d, scenario, rng) -> None:
        super().setup(d, scenario, rng)
        d.chain.send(d.usdc.functions.mint(self.address, self.USDC_SEED))

    def act(self, d, scenario, day, tick, ctr) -> None:
        w3, ab = d.w3, d.erc20_abi
        usdc = d.usdc.address
        bal = d.usdc.functions.balanceOf(self.address).call()
        if bal == 0:
            return
        best = None
        for x in range(len(d.tokens)):
            tx = d.tokens[x].address
            # Size off the *binding* pool on the entry side: USDC reserve of
            # x/USDC and BUCK reserve of x/BUCK (both 6-dec, ~same scale).
            usdc_res = d.usdc.functions.balanceOf(d.pool_usdc[x]).call()
            buck_res = d.buck.functions.balanceOf(d.pool_buck[x]).call()
            cap = min(usdc_res, buck_res) * self.POOL_FRAC_BP // 10_000
            amt = min(bal, cap)
            if amt == 0:
                continue
            for y in range(len(d.tokens)):
                if y == x:
                    continue
                ty = d.tokens[y].address
                hops = [
                    (d.pool_usdc[x], usdc, tx, d.fee_usdc),
                    (d.pool_buck[x], tx, d.buck.address, d.fee_buck),
                    (d.pool_buck[y], d.buck.address, ty, d.fee_buck),
                    (d.pool_usdc[y], ty, usdc, d.fee_usdc),
                ]
                out = quote_path(w3, ab, hops, amt)
                if out > amt * (10_000 + self.MARGIN_BP) // 10_000:
                    if best is None or out - amt > best[0]:
                        best = (out - amt, amt, x, y)
        if best is None:
            return
        _, amt, x, y = best
        ctr["cycle_attempt"] = ctr.get("cycle_attempt", 0) + 1
        tx, ty = d.tokens[x].address, d.tokens[y].address
        path = encode_path([usdc, d.fee_usdc, tx, d.fee_buck,
                             d.buck.address, d.fee_buck, ty, d.fee_usdc, usdc])
        # pre-fund the router, then execute (payerIsUser=false)
        d.chain.send(d.usdc.functions.transfer(d.router.address, amt),
                     sender=self.account)
        deadline = w3.eth.get_block("latest")["timestamp"] + 3600
        cmds, inputs = ur_exec_args(self.address, amt, path)
        try:
            d.chain.send(d.router.functions.execute(cmds, inputs, deadline),
                         sender=self.account, gas=3_000_000)
            ctr["cycleTrades"] += 1
        except Exception as e:  # serialized slippage / gate -- record once
            ctr["cycle_err"] = repr(e)[:300]


@_register
class MarketMakerWhale(Agent):
    """A single market maker with ~unlimited resources.  Once per day, at a
    random tick, it snaps exactly ONE (randomly chosen) TOKEN/USDC pool to
    that day's CSV close -- never all of them at once.  Executed through the
    SimLP helper (holds ~unlimited TOKEN+USDC); the whale EOA is still
    registered for identity fidelity.  The arbs propagate the move to the
    other pools via optimized routing between whale interventions."""

    HUGE = (1 << 127) - 1

    def snap(self, d, scenario, day, token_idx, ctr) -> None:
        pool_abi, _ = load_artifact("UniswapV3Pool")
        i = token_idx
        ref = scenario.prices.ref(i, day)
        pool_addr = d.pool_usdc[i]
        pool = d.w3.eth.contract(address=pool_addr, abi=pool_abi)
        cur = pool.functions.slot0().call()[0]            # sqrtPriceX96
        targ = sqrt_price_x96(d.tokens[i].address, 10 ** d.dec[i],
                              d.usdc.address, ref)
        if cur == targ:
            return
        t0 = pool.functions.token0().call()
        t1 = pool.functions.token1().call()
        zero_for_one = targ < cur
        d.chain.send(d.simlp.functions.swap(
            pool_addr, d.simlp.address, zero_for_one,
            self.HUGE, targ, t0, t1))
        ctr["directTrades"] += 1

    def act(self, d, scenario, day, tick, ctr) -> None:  # unused (loop drives)
        pass
