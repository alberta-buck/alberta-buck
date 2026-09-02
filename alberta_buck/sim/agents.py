"""Pluggable agents.

`Agent` subclasses register themselves in `REGISTRY` by class name; a
scenario lists how many of each to spawn.  Three ship today
(AnonymousArbAgent, TokenAccumulatorAgent, MarketMakerWhale):

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

    # -- per-agent telemetry (schema: alberta_buck/sim/TELEMETRY.md) -------- #
    #
    # Opt-in: the default returns None and the snapshot emits nothing, so
    # large background populations cost nothing.  A class that wants to
    # appear on dashboards implements BOTH methods:
    #   telemetry_static() -> dict   resolved knobs / identity facts, captured
    #                                ONCE into the vector's meta.telemetry;
    #   telemetry(d)       -> dict   the per-frame record (positions, P&L).
    # TELEMETRY_STRIDE spaces per-frame emission in calendar days (emit when
    # day % stride == 0); populations larger than ~16 should set
    # stride >= ceil(count/16) to keep vector growth bounded.

    TELEMETRY_STRIDE = 1

    def telemetry_static(self) -> dict | None:
        return None

    def telemetry(self, d) -> dict | None:
        return None

    def deposit_info(self, d) -> tuple | None:
        """Return (token_idx, principal_tok, principal_buck) for LP position
        value tracking.  None means the agent has no BuckBasket deposit."""
        return None

    # registered as a real cryptographic identity (disk-cached per seed).
    def setup(self, d, scenario, rng) -> None:
        self.account, args = idmod.cached_eoa_setup(
            scenario.seed, type(self).__name__, self.idx,
            d.issuer_kp, rng)
        d.anvil.set_balance(self.address, 100 * 10 ** 18)
        d.chain.send(
            d.reg.functions.register(d.issuer_addr, *args),
            sender=self.account, gas=3_000_000,
        )

    def bootstrap(self, d, scenario, ctr) -> None:
        """Called once after every agent's `setup()` but before the
        day/tick loop.  Default: no-op.  Agents that need to seed
        on-chain state before any market activity (e.g., the first DM
        agents seeding empty BuckBasket pools) override this so all
        pools are live by tick 0."""
        pass

    def _exec(self, d, in_tok_c, amt, toks, uses_ub, ctr) -> bool:
        """Pre-fund the router and run one V3 multi-hop (payerIsUser=false,
        recipient = self).  Returns True on success.  Shared by all
        BUCK-unaware routing agents."""
        ctr["cycle_attempt"] = ctr.get("cycle_attempt", 0) + 1
        d.chain.send(in_tok_c.functions.transfer(d.router.address, amt),
                     sender=self.account)
        deadline = d.w3.eth.get_block("latest")["timestamp"] + 3600
        cmds, inputs = ur_exec_args(self.address, amt, encode_path(toks))
        try:
            d.chain.send(d.router.functions.execute(cmds, inputs, deadline),
                         sender=self.account, gas=3_000_000)
            ctr["cycleTrades"] += 1
            # Gross arb volume routed, valued in USD so the meter is
            # unit-consistent (the input is USDC for arb cycles but an 18-/8-dec
            # commodity for accumulator cycles -- summing raw amounts mixes
            # decimals).  Lets us validate basket fee income against actual
            # throughput rather than a trade count.
            if in_tok_c.address.lower() == d.usdc.address.lower():
                vol_usd = amt
            else:
                vol_usd = 0
                refs = ctr.get("refUsd", [])
                for j, tc in enumerate(d.tokens):
                    if tc.address.lower() == in_tok_c.address.lower() and j < len(refs):
                        vol_usd = amt * refs[j] // (10 ** d.dec[j])
                        break
            ctr["cycleVolumeUsdc"] = ctr.get("cycleVolumeUsdc", 0) + vol_usd
            if uses_ub:
                ctr["ubTrades"] = ctr.get("ubTrades", 0) + 1
            return True
        except Exception as e:  # serialized slippage / gate -- record once
            ctr["cycle_err"] = repr(e)[:300]
            return False

    def act(self, d, scenario, day, tick, ctr) -> None:  # pragma: no cover
        raise NotImplementedError


@_register
class AnonymousArbAgent(Agent):
    USDC_SEED = 2_000_000 * 10 ** 6      # scaled $2M
    POOL_FRAC_BP = 25                     # small fill (0.25% of binding
                                          # pool): low slippage, but enough
                                          # to book a material realized edge
    MARGIN_BP = 60                        # only take cycles with a REAL edge
                                          # (>0.60% net) so arb profit is
                                          # visible, not competed to ~0

    def setup(self, d, scenario, rng) -> None:
        super().setup(d, scenario, rng)
        d.chain.send(d.usdc.functions.mint(self.address, self.USDC_SEED))

    def _candidates(self, d, amt, x):
        """Routes that start+end in USDC and pass through BUCK pools.
        Each: (tokens_fees for encode_path, hops for the quoter, uses_ub)."""
        U, B = d.usdc.address, d.buck.address
        tx = d.tokens[x].address
        fu, fb, fub = d.fee_usdc, d.fee_buck, d.fee_ub
        out = []
        # C1: USDC -> x -> BUCK -> y -> USDC (4-hop, cross-token)
        for y in range(len(d.tokens)):
            if y == x:
                continue
            ty = d.tokens[y].address
            out.append((
                [U, fu, tx, fb, B, fb, ty, fu, U],
                [(d.pool_usdc[x], U, tx, fu), (d.pool_buck[x], tx, B, fb),
                 (d.pool_buck[y], B, ty, fb), (d.pool_usdc[y], ty, U, fu)],
                False))
        if d.pool_ub:
            # C2: USDC -> BUCK -> x -> USDC  (enters BUCK via BUCK/USDC)
            out.append((
                [U, fub, B, fb, tx, fu, U],
                [(d.pool_ub, U, B, fub), (d.pool_buck[x], B, tx, fb),
                 (d.pool_usdc[x], tx, U, fu)],
                True))
            # C3: USDC -> x -> BUCK -> USDC  (exits BUCK via BUCK/USDC)
            out.append((
                [U, fu, tx, fb, B, fub, U],
                [(d.pool_usdc[x], U, tx, fu), (d.pool_buck[x], tx, B, fb),
                 (d.pool_ub, B, U, fub)],
                True))
        return out

    def act(self, d, scenario, day, tick, ctr) -> None:
        w3, ab = d.w3, d.erc20_abi
        balance_of = lambda token, holder: d.chain.balance_of(token, holder, ab)
        # Act on the best opportunity *per token* every tick -- NOT a single
        # global winner.  Otherwise the deepest pool's larger absolute
        # profit makes the agent always trade that one token and the other
        # TOKEN/BUCK pools never get arbed (the BUCK/USDC pool then only
        # tracks the dominant token).
        for x in range(len(d.tokens)):
            bal = d.chain.balance_of(d.usdc, self.address)
            if bal == 0:
                return
            usdc_res = d.chain.balance_of(d.usdc, d.pool_usdc[x])
            buck_res = d.chain.balance_of(d.buck, d.pool_buck[x])
            binders = [usdc_res, buck_res]
            if d.pool_ub:
                binders.append(d.chain.balance_of(d.usdc, d.pool_ub))
            amt = min(bal, min(binders) * self.POOL_FRAC_BP // 10_000)
            if amt == 0:
                continue
            best = None
            for toks, hops, uses_ub in self._candidates(d, amt, x):
                o = quote_path(w3, ab, hops, amt, balance_of)
                if o > amt * (10_000 + self.MARGIN_BP) // 10_000:
                    if best is None or o - amt > best[0]:
                        best = (o - amt, toks, uses_ub)
            if best is not None:
                self._exec(d, d.usdc, amt, best[1], best[2], ctr)


@_register
class MarketMakerWhale(Agent):
    """A single market maker with ~unlimited resources.  Once per day, at a
    random tick, it snaps every TOKEN/USDC pool to that day's CSV close.
    Executed through the SimLP helper (holds ~unlimited TOKEN+USDC); the
    whale EOA is still registered for identity fidelity.  BUCK-route arbs
    then propagate those exogenous truth moves into TOKEN/BUCK pools."""

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


@_register
class TokenAccumulatorAgent(Agent):
    """A BUCK-unaware arber that maximizes a single target TOKEN's holdings
    (not USDC).  It hunts cycles that start and end in its target token and
    pass through the floating BUCK/USDC pool, netting more of the token.
    BUCK and USDC are router-internal between hops -- the agent only ever
    holds its target TOKEN.  Each instance targets a different token
    (idx %% N), so the population pulls every TOKEN/BUCK pool, not just the
    deepest one."""

    SEED_USDC = 2_000_000 * 10 ** 6       # ~$2M of the target token (sized
                                          # in USD so ROI is comparable to
                                          # the USDC arbs -- NOT a fixed
                                          # token count, which at cbBTC
                                          # prices dwarfs every other base)
    POOL_FRAC_BP = 25                     # small fill (0.25%) -- low slippage
    MARGIN_BP = 60                        # only take cycles netting >0.60%
                                          # more of the token (visible profit)

    def setup(self, d, scenario, rng) -> None:
        super().setup(d, scenario, rng)
        self.tgt = self.idx % len(d.tokens)
        c = d.tokens[self.tgt]
        ref0 = scenario.prices.ref(self.tgt, 0)          # USDC-micro / token
        seed = self.SEED_USDC * (10 ** d.dec[self.tgt]) // ref0
        d.chain.send(c.functions.mint(self.address, seed))

    def act(self, d, scenario, day, tick, ctr) -> None:
        if not d.pool_ub:
            return
        w3, ab = d.w3, d.erc20_abi
        balance_of = lambda token, holder: d.chain.balance_of(token, holder, ab)
        t = self.tgt
        tc = d.tokens[t]
        T, B, U = tc.address, d.buck.address, d.usdc.address
        fu, fb, fub = d.fee_usdc, d.fee_buck, d.fee_ub
        bal = d.chain.balance_of(tc, self.address)
        if bal == 0:
            return
        # Size off the token side of the pools the cycle traverses.
        binders = [d.chain.balance_of(tc, d.pool_buck[t]),
                   d.chain.balance_of(tc, d.pool_usdc[t])]
        amt = min(bal, min(binders) * self.POOL_FRAC_BP // 10_000)
        if amt == 0:
            return
        # Both directions of the token<->BUCK<->USDC<->token triangle, each
        # using the floating BUCK/USDC pool; pick the more profitable.
        cands = [
            ([T, fb, B, fub, U, fu, T],
             [(d.pool_buck[t], T, B, fb), (d.pool_ub, B, U, fub),
              (d.pool_usdc[t], U, T, fu)]),
            ([T, fu, U, fub, B, fb, T],
             [(d.pool_usdc[t], T, U, fu), (d.pool_ub, U, B, fub),
              (d.pool_buck[t], B, T, fb)]),
        ]
        best = None
        for toks, hops in cands:
            o = quote_path(w3, ab, hops, amt, balance_of)
            if o > amt * (10_000 + self.MARGIN_BP) // 10_000:
                if best is None or o - amt > best[0]:
                    best = (o - amt, toks)
        if best is not None:
            self._exec(d, tc, amt, best[1], True, ctr)
