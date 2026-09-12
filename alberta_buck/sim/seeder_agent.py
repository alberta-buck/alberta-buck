"""The treasury seeder -- WAVE3.org WP-8, the proof of concept of
CARRY-CONVEXITY.org decision D5(2).

THE POSITION

At launch the treasury seeds BUCK/USDC depth TEMPORARILY, to lubricate the
BUCK-unaware arb route that repins the TOKEN/BUCK pools via BUCK/USDC.  It
does not own the venue and takes no lasting USD exposure: it holds one
concentrated Uniswap V3 range from the CURRENT BUCK/USDC price toward the
IDEAL -- the bundle's USD value at par, read from the TOKEN/USDC truth
pools -- and not beyond.  Private LPs supply depth beyond ideal, and the
treasury withdraws as private depth arrives.

A single-sided V3 range placed on the far side of the price is funded
ENTIRELY with the asset the price would have to SELL to enter it, which is
exactly the asset the corrective arb wants to TAKE:

    ideal > current   BUCK must RISE vs USDC.  The corrective arb buys BUCK
                      from the pool.  The range from current toward ideal
                      is funded with BUCK, and converts to USDC as the arb
                      lifts it.
    ideal < current   BUCK must FALL.  The corrective arb sells BUCK for
                      USDC.  The range is funded with USDC and converts to
                      BUCK as the arb hits it.

So the seeder is the counterparty to CORRECTIVE flow by construction
(R10: the position converts in the corrective direction only) and it is
self-reversing: once the price reaches ideal the position has converted
into the other asset and stops.  It is BuckPoolInvestorAgent's placement
machinery with a different objective -- lubricate, not earn -- and unlike
that agent it never straddles the price and never sits beyond ideal.

ORIENTATION, WORKED FROM FIRST PRINCIPLES (the ops doc's "silent killer")

A V3 pool's price is token1 per token0 and a tick is its log:
price = 1.0001^tick.  A range entirely ABOVE the current tick (tickLower >
tick) is funded with token0 only; entirely BELOW (tickUpper <= tick) with
token1 only (UniswapV3Pool._modifyPosition compares slot0.tick against the
bounds).  Which side of the tick "BUCK rises toward ideal" is depends on
the pool's token ordering, and the FUNDING asset does not:

  BUCK is token0, USDC is token1.  Pool price = USDC per BUCK = the BUCK/USD
    price itself.  BUCK rising toward a higher ideal is a RISING pool price:
    the range sits ABOVE the tick and is funded with token0 = BUCK.  BUCK
    falling: range BELOW, funded with token1 = USDC.

  USDC is token0, BUCK is token1.  Pool price = BUCK per USDC = 1 / (BUCK/USD).
    BUCK STRENGTHENING (fewer BUCK per USDC) is a FALLING pool price: the
    range sits BELOW the tick and is funded with token1 = BUCK.  BUCK
    weakening: range ABOVE, funded with token0 = USDC.

In both orientations the range from current toward ideal is funded with
BUCK when BUCK/USD must rise and with USDC when it must fall -- the tick
DIRECTION flips with the ordering, the FUNDING side is a function of
sign(ideal - current) alone.  `seeder_range` takes the orientation flag and
returns both; `test_seeder.py` pins both orderings.  The sim's pool_ub has
whichever ordering the deployed addresses give (token0 is the lower
address); the agent reads it from the pool and reports it as
`buck_is_token0` in its static telemetry.

UNITS

BUCK and USDC are both 6-dec, so tick 0 is exactly $1.00 per BUCK in
either orientation and a tick is (very nearly) a basis point.  `ideal` and
`current` are micro-USD per BUCK.  The basket stores basketAmount_i =
weight_i x 1e36 / initialPriceInBuck_i, where initialPriceInBuck_i is the
token's price in RAW BUCK per whole token at init (deploy.py passes the
6-dec p0; the Solidity comment's "18-dec" is the contract's decimal-
agnostic convention, not the sim's), so sum_i basketAmount_i x P_i is
1e36 x the bundle's value in BUCK for any P quoted per whole token on the
same raw scale.  The truth pools quote micro-USD per whole token and 1
BUCK == 1 USDC == 1e6 raw at par, so the ideal is sum_i basketAmount_i x
P_i x 1e6 / 1e36 micro-USD per BUCK -- independent of the tokens' own
decimals.  Verified against a live deployment (the sum is 1.0000 BUCK at
p0); the first smoke, which divided by 1e18, read an ideal of $1.2e12 and
struck a range across the whole tick space.

The sim's truth pools carry the bootstrap deposits' impact (+21% on every
token in catalogue-none) until the whale's first snap during day 0, so a
day-0 read of the ideal is a sim artefact; the seeder starts on
`start_day` (1).

WITHDRAW RULE, AND ITS APPROXIMATION

"Private depth" is the pool's liquidity() at the current tick, less the
seeder's own L when its range contains the tick, less (by default) the
SimLP's deploy-time full-range position -- that position is the sim's
stand-in for the venue's founding depth, not depth that "arrived"
(CARRY-CONVEXITY.org: "a withdraw rule keyed to SimLP-independent depth");
`count_simlp = true` restores the literal rule.  Two approximations,
named: liquidity() counts only positions in range at the current tick, so
concentrated private positions elsewhere are invisible until the price
reaches them; and L is converted to a USD depth by the full-range identity
(USDC-side reserve = L sqrtP / 2^96 when USDC is token1, L 2^96 / sqrtP
when token0), which for a concentrated position overstates its actual
reserves.  It is a depth gauge, not a reserve count.

Telemetry (ctr -> frame, snapshot.py WP-8 block): sdPositions /
sdRepositions / sdConverted / sdWithdrawn / sdSide / sdIdeal / sdCurrent /
sdGapBp / sdLiq / sdDepth / sdLast / sd_err.

THE RANGE AS A POSITION (WP-14; WAVE3.org decision 17).  What the range
has CONVERTED is a level-1 inventory in the seam's sense: a BUCK-funded
range that the corrective arb has lifted has SOLD BUCK into the market
(issued, negative), a USDC-funded range that has been hit has BOUGHT BUCK
(absorbed, positive).  `position_amounts` (Uniswap V3 LiquidityAmounts)
reads the range's current BUCK side off its liquidity, ticks and the
pool's sqrtPrice; `range_position` signs it: -(funded - buck_in_range) on
the buck side, +buck_in_range on the usdc side.  When a range is exited
its converted amount is FROZEN into the position (the sold BUCK stays in
circulation; the treasury program's unwind is out of scope), and a fresh
range adds its own.  Published as sd_q with cap sd_cap = the budget (the
treasury's funded amount, decision 11) for the per-class stabilizer
(shadow_book.py).

LOCAL SKEW (WP-14; CARRY-CONVEXITY.org D7 "The lever count"; R16).  The
range's centre -- the ideal it is struck toward -- is skewed by the
seeder's own position, ideal_eff = ideal x (1 + kappa * q / cap)
(`skew_ideal`): a seeder that has sold BUCK (q < 0) lowers its target
toward the current price and stops offering sooner; one that has bought
BUCK raises it, absorbing less.  kappa 0 (the default) uses the raw
ideal itself, so every banked cell is byte-identical.  Knob kappa (drawn
AFTER budget_m, the class's only other draw); counters sk_sd (the applied
skew, a fraction of the ideal) and sk_sd_n (placements and exits decided
under a non-zero skew), written only when a skew is applied;
note(kind="skew") carries the why before the sends.
"""

from __future__ import annotations

import math

from web3 import Web3

from alberta_buck.sim.agents import _register
from alberta_buck.sim.chain import load_artifact
from alberta_buck.sim.equilibrium_agents import _ProxyAgent, _agent_rng
from alberta_buck.sim.experiment import draw as _draw, spec as _spec
# Decision 8 moved the slot0 spot conversion (with Q96 and E6) to the shared
# gauge; re-exported here so the names test_seeder.py imports stay put.
from alberta_buck.sim.gauge import E6, Q96, sqrt_price_to_usd6  # noqa: F401
from alberta_buck.sim.snapshot import _implied

E18 = 10 ** 18
LN_TICK = math.log(1.0001)
MAX_L = 2 ** 127 - 1


# -- pure helpers (unit-tested, no chain) ---------------------------------- #

def ideal_buck_usd(basket_amounts, prices_usd6, decimals=None,
                   buck_dec: int = 6) -> int:
    """The bundle's USD value at par, in micro-USD per BUCK.

    basket_amounts_i are the basket's stored basketAmount_i (= weight_i x
    1e36 / initialPriceInBuck_i, with the init price in raw BUCK per whole
    token -- module doc, UNITS); prices_usd6_i are micro-USD per WHOLE
    token (what `snapshot._implied` reads off a TOKEN/USDC truth pool).
    sum_i a_i p_i / 1e36 is the bundle in BUCK, x 10^buck_dec for raw
    micro-USD.  Token decimals do not enter -- both inputs are per whole
    token -- `decimals` is accepted only so a caller passing d.dec gets a
    length check rather than silence."""
    if decimals is not None and len(decimals) != len(basket_amounts):
        raise ValueError("decimals/basket_amounts length mismatch")
    if len(prices_usd6) != len(basket_amounts):
        raise ValueError("prices/basket_amounts length mismatch")
    total = sum(int(a) * int(p) for a, p in zip(basket_amounts, prices_usd6))
    return total * 10 ** buck_dec // 10 ** 36


def usd6_to_tick(usd6: int, buck_is_token0: bool) -> int:
    """The pool tick at which BUCK trades at `usd6` micro-USD.  Both tokens
    are 6-dec, so the raw pool price is usd6/1e6 when BUCK is token0 and
    1e6/usd6 when BUCK is token1; the tick is floor(log_1.0001 price)."""
    if usd6 <= 0:
        raise ValueError("usd6 must be positive")
    ratio = usd6 / E6 if buck_is_token0 else E6 / usd6
    return int(math.floor(math.log(ratio) / LN_TICK))


def tick_to_usd6(tick: int, buck_is_token0: bool) -> int:
    """Inverse of usd6_to_tick at the tick's lower edge (micro-USD)."""
    ratio = 1.0001 ** tick
    return int(round(E6 * ratio if buck_is_token0 else E6 / ratio))


def sqrt_at_tick(t: int) -> int:
    """sqrtPriceX96 at a tick: price = 1.0001^t, so sqrt is 1.0001^(t/2)."""
    return int((1.0001 ** (t / 2.0)) * Q96)


def bp_to_ticks(bp: int) -> int:
    """A tick is a log price, so a `bp` move spans ln(1+x)/ln(1.0001) ticks
    (900bp is 862 ticks, not 900) -- the same form BuckPoolInvestorAgent
    uses."""
    return max(1, int(math.log(1.0 + bp / 10_000.0) / LN_TICK))


def seeder_range(current_tick: int, ideal_tick: int, spacing: int,
                 buck_is_token0: bool):
    """The range from the current tick toward the ideal, and who funds it.

    Returns (tickLower, tickUpper, side) with side in {"buck", "usdc"}, or
    None when no spacing-aligned range at least one spacing wide fits
    STRICTLY between current and ideal (the range never crosses beyond the
    ideal; the caller's min_gap_bp normally keeps it well clear of this).

    Above the tick (ideal_tick > current_tick): tickLower is the first
    spacing boundary strictly above the current tick, so the pool sees
    slot0.tick < tickLower and asks for token0 only; tickUpper is the last
    boundary at or below the ideal.  Below: tickUpper is the boundary at or
    below the current tick (slot0.tick >= tickUpper: token1 only);
    tickLower the first boundary at or above the ideal.

    The funding side follows from the orientation: token0 is BUCK iff
    buck_is_token0.  In USD terms that is always BUCK when the BUCK/USD
    price must rise toward ideal and USDC when it must fall -- see the
    module docstring for the derivation in both orderings."""
    if spacing <= 0:
        raise ValueError("spacing must be positive")
    if ideal_tick == current_tick:
        return None
    if ideal_tick > current_tick:
        lo = -((-(current_tick + 1)) // spacing) * spacing      # ceil, > tick
        hi = (ideal_tick // spacing) * spacing                   # floor, <= ideal
        if hi < lo + spacing:
            return None
        side = "buck" if buck_is_token0 else "usdc"              # token0 funds
        return lo, hi, side
    hi = (current_tick // spacing) * spacing                     # floor, <= tick
    lo = -((-ideal_tick) // spacing) * spacing                   # ceil, >= ideal
    if lo > hi - spacing:
        return None
    side = "usdc" if buck_is_token0 else "buck"                  # token1 funds
    return lo, hi, side


def corrective_side(ideal_usd6: int, current_usd6: int) -> str:
    """The funding asset in USD terms, orientation-free: BUCK when BUCK must
    rise toward ideal, USDC when it must fall, "" at parity."""
    if ideal_usd6 > current_usd6:
        return "buck"
    if ideal_usd6 < current_usd6:
        return "usdc"
    return ""


def single_sided_liquidity(lo: int, hi: int, amount: int, above: bool,
                           shave: float = 0.995) -> int:
    """L for a range funded with ONE asset.

    Entirely above the price (token0 only):
        amount0 = L (sqrtB - sqrtA) Q96 / (sqrtA sqrtB)
    entirely below (token1 only):
        amount1 = L (sqrtB - sqrtA) / Q96
    inverted for L, then shaved so the pool's round-up in the mint callback
    never asks for a wei more than the proxy holds (BuckPoolInvestorAgent's
    ERC20InsufficientBalance lesson)."""
    if hi <= lo or amount <= 0:
        return 0
    sa, sb = sqrt_at_tick(lo), sqrt_at_tick(hi)
    if sb <= sa:
        return 0
    if above:
        L = amount * sa * sb // (Q96 * (sb - sa))
    else:
        L = amount * Q96 // (sb - sa)
    return int(min(int(L * shave), MAX_L))


def depth_usd6(liquidity: int, sqrt_price_x96: int, usdc_is_token0: bool) -> int:
    """USD depth of `liquidity` by the full-range identity: the USDC-side
    reserve a full-range position of that L would hold at this price."""
    if liquidity <= 0 or sqrt_price_x96 <= 0:
        return 0
    if usdc_is_token0:
        return liquidity * Q96 // sqrt_price_x96
    return liquidity * sqrt_price_x96 // Q96


def position_amounts(liquidity: int, sqrt_price_x96: int, lo: int, hi: int
                     ) -> tuple[int, int]:
    """(amount0, amount1) a V3 position of `liquidity` on [lo, hi) holds
    at `sqrt_price_x96` (LiquidityAmounts.getAmountsForLiquidity): token0
    only below the range, token1 only above it, both inside."""
    if liquidity <= 0 or hi <= lo or sqrt_price_x96 <= 0:
        return 0, 0
    sa, sb = sqrt_at_tick(lo), sqrt_at_tick(hi)
    if sb <= sa:
        return 0, 0
    if sqrt_price_x96 <= sa:
        return liquidity * (sb - sa) * Q96 // (sa * sb), 0
    if sqrt_price_x96 >= sb:
        return 0, liquidity * (sb - sa) // Q96
    return (liquidity * (sb - sqrt_price_x96) * Q96 // (sqrt_price_x96 * sb),
            liquidity * (sqrt_price_x96 - sa) // Q96)


def range_position(side: str, funded: int, buck_in_range: int) -> int:
    """The range's converted amount as a stabilizer position (module doc):
    a BUCK-funded range has SOLD funded - buck_in_range (negative); a
    USDC-funded range has BOUGHT buck_in_range (positive); 0 otherwise."""
    if side == "buck":
        return -max(0, int(funded) - int(buck_in_range))
    if side == "usdc":
        return max(0, int(buck_in_range))
    return 0


def skew_ideal(ideal: int, kappa: float, q: int, cap: int
               ) -> tuple[int, float, float]:
    """(ideal_eff, skew, fill): the range centre skewed by the seeder's own
    position, ideal x (1 + kappa * fill), fill = q / cap in [-1, 1].  With
    kappa 0, cap 0 or an empty position the ideal itself comes back."""
    fill = 0.0
    if cap > 0 and q:
        fill = max(-1.0, min(1.0, q / cap))
    if not kappa or fill == 0.0:
        return ideal, 0.0, fill
    skew = kappa * fill
    return max(1, int(round(ideal * (1.0 + skew)))), skew, fill


# -- the agent ------------------------------------------------------------ #

@_register
class SeederAgent(_ProxyAgent):
    """The treasury's temporary BUCK/USDC seeding position (module doc).

    Knobs ([agents.SeederAgent]): budget_m [10, 20] ($M; USDC minted to the
    proxy AND an equal BUCK capacity through a zero-premium credit, so
    either side can fund), restrike_bp (100: re-strike when the price has
    moved this far AWAY from the range), withdraw_depth_m (50: withdraw for
    good once private depth exceeds this), max_days (365: withdraw for good
    after this many days), min_gap_bp (25: do nothing while |current -
    ideal| is inside this), count_simlp (false: see the withdraw rule),
    start_day (1: the truth pools are not pinned to the reference until
    the whale's first snap during day 0 -- module doc, UNITS).

    Daily at tick 0: read ideal and current; if a position exists and the
    price has passed through it (converted), moved away from it by more
    than restrike_bp, or the ideal has crossed to the other side of the
    price (the range would now face WORSENING flow) or moved inside the
    range's far edge, burn + collect it; then, when |gap| >= min_gap_bp,
    place a fresh single-sided range from current toward ideal funded with
    the corrective asset.  Withdraw for good (burn + collect, both calls)
    when private depth exceeds withdraw_depth_m or after max_days.
    """

    CTR = "sd"

    def setup(self, d, scenario, rng) -> None:
        cls = type(self).__name__
        self._rng = _agent_rng(scenario.seed, cls, self.idx)
        r = self._rng
        self.budget = int(_draw(scenario, cls, "budget_m", r, (10.0, 20.0))
                          * 1_000_000 * E6)
        self.kappa = float(_draw(scenario, cls, "kappa", r, 0.0))   # WP-14
        self.restrike_bp = int(_spec(scenario, cls, "restrike_bp", 100))
        self.withdraw_depth_m = float(_spec(scenario, cls, "withdraw_depth_m",
                                            50.0))
        self.max_days = int(_spec(scenario, cls, "max_days", 365))
        self.min_gap_bp = int(_spec(scenario, cls, "min_gap_bp", 25))
        self.count_simlp = bool(_spec(scenario, cls, "count_simlp", False))
        self.start_day = int(_spec(scenario, cls, "start_day", 1))
        self._pos: tuple[int, int] | None = None     # (tickLower, tickUpper)
        self._above = False                          # range above the tick?
        self._side = ""
        self._liq = 0
        self._funded = 0
        self._placed_day = -1
        self._positions = 0
        self._repositions = 0
        self._converted = 0
        self._withdrawn = 0
        self._done = False
        self._start_day: int | None = None
        self._face = 0
        self._minted = 0
        self._simlp_key = None
        self._last = {}
        self._q_frozen = 0          # WP-14: exited ranges' converted BUCK
        self._q_live = 0            # WP-14: the open range's converted BUCK
        self._skew = 0.0

        self._bind_proxy(d)
        # The USDC side: the treasury's cash.
        d.chain.send(d.usdc.functions.mint(self.proxy.address, self.budget))
        # The BUCK side: an equal capacity through a zero-premium credit,
        # sized off the live K0 with deploy.py's 20% margin (a zero-premium
        # mint yields SPENDABLE headroom == amount * K, not amount).  Minting
        # activates coverage only; BUCK enters supply when the mint callback
        # transfers it into the pool and draws the proxy's signed balance
        # negative -- an outstanding claim on its own assets, released when
        # the converted USDC is used to buy the BUCK back (out of scope here;
        # the treasury program's job).
        k0 = int(d.kctrl.functions.buckK().call())
        mint_amt = (self.budget * E18 // max(1, k0)) * 12 // 10
        self._face = max(2 * self.budget, mint_amt * 12 // 10)
        now_ts = d.w3.eth.get_block("latest")["timestamp"]
        self._proxy_exec(d, d.credit.address, d.credit.encode_abi(
            "setCreditIssuer",
            args=[getattr(d.chain.deployer, "address", d.chain.deployer), True]))
        d.chain.send(d.credit.functions.createCredit(
            self.proxy.address, 0, self._face, 0, 0, 0, now_ts, 0))
        self._proxy_exec(d, d.buck.address, d.buck.encode_abi(
            "mint(uint256)", args=[mint_amt]))
        self._minted = mint_amt
        # Orientation, read once from the pool and reported.
        self._b0: bool | None = None
        if getattr(d, "pool_ub", ""):
            try:
                _, _, _, t0, _ = self._pool_state(d)
                self._b0 = d.buck.address.lower() == t0.lower()
            except Exception:
                self._b0 = None

    # -- chain reads -------------------------------------------------------- #

    def _pool(self, d):
        pool_abi, _ = load_artifact("UniswapV3Pool")
        return d.w3.eth.contract(address=d.pool_ub, abi=pool_abi)

    def _pool_state(self, d):
        """(sqrtPriceX96, tick, tickSpacing, token0, token1) of BUCK/USDC."""
        pool = self._pool(d)
        slot0 = pool.functions.slot0().call()
        return (slot0[0], slot0[1], pool.functions.tickSpacing().call(),
                pool.functions.token0().call(), pool.functions.token1().call())

    def _ideal(self, d) -> int:
        """sum_i basketAmount_i x truth-pool USD price_i, micro-USD per BUCK."""
        n = int(d.basket.functions.constituentsLength().call())
        amounts, prices = [], []
        for i in range(n):
            amounts.append(int(d.basket.functions.constituents(i).call()[2]))
            prices.append(int(_implied(d, d.pool_usdc[i], d.tokens[i],
                                       d.dec[i], d.usdc)))
        return ideal_buck_usd(amounts, prices, d.dec[:n])

    def _simlp_liquidity(self, d, pool) -> int:
        """The SimLP's deploy-time full-range L in BUCK/USDC (always in
        range), read from the pool's positions map."""
        if self._simlp_key is None:
            for (p, owner, lo, hi, grp) in getattr(d, "pool_meta", []):
                if grp == "ub" and p.lower() == d.pool_ub.lower():
                    self._simlp_key = Web3.solidity_keccak(
                        ["address", "int24", "int24"], [owner, lo, hi])
                    break
            if self._simlp_key is None:
                self._simlp_key = b""
        if not self._simlp_key:
            return 0
        return int(pool.functions.positions(self._simlp_key).call()[0])

    def _private_depth(self, d, pool, sp: int, tick: int) -> int:
        """Private (arrived) depth in micro-USD -- module doc, withdraw rule."""
        L = int(pool.functions.liquidity().call())
        if self._pos is not None and self._pos[0] <= tick < self._pos[1]:
            L -= self._liq
        if not self.count_simlp:
            L -= self._simlp_liquidity(d, pool)
        return depth_usd6(max(0, L), sp, not self._b0)

    # -- position primitives ------------------------------------------------ #

    def _live_position(self, d, sp: int | None = None) -> int:
        """WP-14: the open range's converted BUCK, signed (module doc)."""
        if self._pos is None or self._liq <= 0:
            return 0
        if sp is None:
            sp = self._pool_state(d)[0]
        a0, a1 = position_amounts(self._liq, sp, self._pos[0], self._pos[1])
        buck_in_range = a0 if self._b0 else a1
        return range_position(self._side, self._funded, buck_in_range)

    def _exit_position(self, d, ctr) -> bool:
        """Burn the range and collect everything owed -- BOTH calls; a burn
        alone only credits the owed amounts and strands the capital."""
        if self._pos is None:
            return True
        lo, hi = self._pos
        pool = self._pool(d)
        try:
            frozen = self._live_position(d)         # WP-14: before the burn
        except Exception:
            frozen = self._q_live
        try:
            self._proxy_exec(d, d.pool_ub, pool.encode_abi(
                "burn(int24,int24,uint128)", args=[lo, hi, int(self._liq)]))
            self._proxy_exec(d, d.pool_ub, pool.encode_abi(
                "collect(address,int24,int24,uint128,uint128)",
                args=[self.proxy.address, lo, hi, 2 ** 128 - 1, 2 ** 128 - 1]))
        except Exception as e:
            ctr["sd_err"] = repr(e)[:160]
            return False
        self._pos = None
        self._liq = 0
        self._side = ""
        self._funded = 0
        self._q_frozen += frozen                    # WP-14
        self._q_live = 0
        return True

    def _funding_available(self, d, side: str) -> int:
        """What the proxy can put up on `side`, capped at the budget.  BUCK
        is Buck.balanceOf: held plus unused K-scaled headroom, net of any
        draw already in the pool."""
        if side == "buck":
            have = int(d.buck.functions.balanceOf(self.proxy.address).call())
        else:
            have = int(d.chain.balance_of(d.usdc, self.proxy.address))
        return max(0, min(have, self.budget))

    def _place(self, d, ctr, day, lo, hi, side, above, ideal, current,
               gap_bp, t0, t1) -> bool:
        amount = self._funding_available(d, side)
        L = single_sided_liquidity(lo, hi, amount, above)
        if L < 1 or amount < E6:
            ctr["sd_err"] = f"unfunded {side} {amount}"
            return False
        try:
            d.chain.send(self.proxy.functions.mint(d.pool_ub, lo, hi, L, t0, t1))
        except Exception as e:
            ctr["sd_err"] = repr(e)[:160]
            return False
        self._pos = (lo, hi)
        self._above = above
        self._side = side
        self._liq = L
        self._funded = amount
        self._placed_day = day
        self._positions += 1
        self._last = {"day": day, "lo": lo, "hi": hi, "side": side,
                      "ideal": ideal, "current": current, "gap_bp": gap_bp,
                      "liq": L, "funded": amount}
        return True

    # -- the daily act ------------------------------------------------------ #

    def act(self, d, scenario, day, tick_i, ctr) -> None:
        if tick_i != 0 or self.proxy is None or not d.pool_ub or self._done:
            return
        if day < self.start_day:
            return
        if self._start_day is None:
            self._start_day = day
        try:
            sp, tick, spacing, t0, t1 = self._pool_state(d)
            if self._b0 is None:
                self._b0 = d.buck.address.lower() == t0.lower()
            ideal = self._ideal(d)
        except Exception as e:
            ctr["sd_err"] = repr(e)[:160]
            return
        current = sqrt_price_to_usd6(sp, self._b0)
        if ideal <= 0 or current <= 0:
            ctr["sd_err"] = f"bad prices ideal={ideal} current={current}"
            return
        # WP-14: the range as a position, and the local skew on its centre
        # (module doc); kappa 0 uses the raw ideal itself.
        try:
            self._q_live = self._live_position(d, sp)
        except Exception as e:
            ctr["sd_err"] = repr(e)[:160]
        ideal_raw = ideal
        ideal, skew, fill = skew_ideal(ideal_raw, self.kappa,
                                       self._q_frozen + self._q_live,
                                       self.budget)
        self._skew = skew
        if skew:
            ctr["sk_sd"] = round(skew, 8)
            self.note(d, "skew", fill=fill, skew=skew, ideal=ideal_raw,
                      ideal_eff=ideal, current=current,
                      q=self._q_frozen + self._q_live, cap=self.budget)
        gap_bp = (ideal - current) * 10_000 / current
        ideal_t = usd6_to_tick(ideal, self._b0)
        pool = self._pool(d)
        try:
            depth = self._private_depth(d, pool, sp, tick)
        except Exception as e:
            ctr["sd_err"] = repr(e)[:160]
            depth = 0
        ctr["sdIdeal"] = ideal_raw
        ctr["sdCurrent"] = current
        ctr["sdGapBp"] = int(round(gap_bp))
        ctr["sdDepth"] = depth

        # Withdraw for good: private depth has arrived, or time is up.
        aged = (day - self._start_day) >= self.max_days
        deep = depth > int(self.withdraw_depth_m * 1_000_000 * E6)
        if aged or deep:
            if self._pos is not None and not self._exit_position(d, ctr):
                self._book(ctr)
                return
            self._withdrawn += 1
            self._done = True
            self._book(ctr)
            return

        # An existing range: leave it working unless it has converted, the
        # price has walked away from it, or the ideal no longer lies beyond
        # it on the corrective side.
        replacing = False
        if self._pos is not None:
            lo, hi = self._pos
            slack = bp_to_ticks(self.restrike_bp)
            if self._above:
                converted = tick >= hi
                away = tick < lo - slack
                overshoot = ideal_t < hi - slack
            else:
                converted = tick < lo
                away = tick >= hi + slack
                overshoot = ideal_t > lo + slack
            flipped = corrective_side(ideal, current) not in ("", self._side)
            if not (converted or away or overshoot or flipped):
                self._book(ctr)
                return
            if not self._exit_position(d, ctr):
                self._book(ctr)
                return
            if converted:
                self._converted += 1
            if self._skew:
                ctr["sk_sd_n"] = ctr.get("sk_sd_n", 0) + 1
            replacing = True

        # A fresh range from current toward ideal, when the gap is real.
        if abs(gap_bp) >= self.min_gap_bp:
            rng_ = seeder_range(tick, ideal_t, spacing, self._b0)
            if rng_ is not None:
                lo, hi, side = rng_
                above = ideal_t > tick
                if self._place(d, ctr, day, lo, hi, side, above, ideal,
                               current, int(round(gap_bp)), t0, t1):
                    if replacing:
                        self._repositions += 1
                    if self._skew:
                        ctr["sk_sd_n"] = ctr.get("sk_sd_n", 0) + 1
        self._book(ctr)

    def _book(self, ctr) -> None:
        ctr["sdPositions"] = self._positions
        ctr["sdRepositions"] = self._repositions
        ctr["sdConverted"] = self._converted
        ctr["sdWithdrawn"] = self._withdrawn
        ctr["sdSide"] = self._side
        ctr["sdLiq"] = int(self._liq)
        ctr["sdLast"] = dict(self._last)
        ctr["sd_q"] = int(self._q_frozen + self._q_live)     # WP-14
        ctr["sd_cap"] = int(self.budget)

    # -- telemetry ---------------------------------------------------------- #

    def telemetry_static(self) -> dict:
        return {"budget": self.budget, "restrike_bp": self.restrike_bp,
                "withdraw_depth_m": self.withdraw_depth_m,
                "max_days": self.max_days, "min_gap_bp": self.min_gap_bp,
                "count_simlp": self.count_simlp, "start_day": self.start_day,
                "face": self._face,
                "minted": self._minted, "buck_is_token0": self._b0,
                # WP-14: only when set (a kappa-0 cell's meta is unchanged).
                **({"kappa": self.kappa} if self.kappa else {})}

    def telemetry(self, d) -> dict | None:
        if self.proxy is None:
            return None
        rec = self._telemetry_common(d)
        rec["lo"], rec["hi"] = self._pos if self._pos else (0, 0)
        rec["liq"] = int(self._liq)
        rec["side"] = self._side
        rec["funded"] = int(self._funded)
        rec["done"] = self._done
        return rec

    def arb_state(self, d) -> dict | None:
        if self.proxy is None:
            return None
        signed = int(d.buck.functions.signedBalanceOf(self.proxy.address).call())
        lo, hi = self._pos if self._pos else (0, 0)
        return {"cls": self.CTR, "idx": self.idx,
                "cash": int(d.chain.balance_of(d.usdc, self.proxy.address)),
                "held": max(0, signed), "drawn": max(0, -signed),
                "endow": self.budget, "parked": 0,
                "lo": lo, "hi": hi, "liq": int(self._liq),
                "side": self._side, "funded": int(self._funded),
                "positions": self._positions, "repos": self._repositions,
                "converted": self._converted, "withdrawn": self._withdrawn,
                "receipts": 1 if self._pos else 0, "done": self._done}
