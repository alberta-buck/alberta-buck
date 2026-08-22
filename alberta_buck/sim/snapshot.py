"""Per-day state capture -> JSON in the existing routing-sim schema.

Schema matches alberta_buck/sim/plot_routing.py so the established plot
renders this unchanged:
  {"tokens":[sym...], "frames":[{day, refUsd[N], spotUsdc[N], spotBuck[N],
   basketVal, buckK, supply, directTrades, cycleTrades, aggPnl}, ...]}
"""

from __future__ import annotations

import json
from pathlib import Path

from web3 import Web3

from alberta_buck.sim.chain import load_artifact

REPO = Path(__file__).resolve().parents[2]
DEFAULT_VECTORS = REPO / "test" / "vectors"
Q96 = 1 << 96
Q128 = 1 << 128
MASK256 = (1 << 256) - 1


def _bal(c, who):
    return c.functions.balanceOf(who).call()


def _implied(d, pool, token_c, dec, quote_c):
    """Implied quote-units per 1 whole token, from live pool reserves."""
    rt = _bal(token_c, pool)
    rq = _bal(quote_c, pool)
    if rt == 0:
        return 0
    return rq * (10 ** dec) // rt


def _pool_value_weights(d) -> list[list[float]]:
    """Per-token [actual_weight, target_weight] from TOKEN/BUCK pools.

    Target weights derive from the basket definition (basketAmount_i *
    poolPrice_i).  Actual weights derive from pool composition (tokenReserve_i
    * poolPrice_i).  Both are normalised so they sum to 1.0.
    """
    N = len(d.tokens)
    # Compute implied BUCK prices (18-dec BUCK per 1 whole token).
    prices = []
    for i in range(N):
        rt = _bal(d.tokens[i], d.pool_buck[i])
        rb = _bal(d.buck, d.pool_buck[i])
        prices.append(rb * (10 ** d.dec[i]) // rt if rt else 0)

    # Target values: basketAmount * initialPriceInBuck * initialPriceInBuck
    # / spotPrice.  When price doubles, target halves → overweight → sell.
    #   base = basketAmount * initialPrice_inBuck (= weight/10000)
    #   targetVal = base * initialPrice_inBuck / spotPrice
    target_val = []
    for i in range(N):
        try:
            c = d.basket.functions.constituents(i).call()
            ba = c[2]       # basketAmount
            ip = c[3]       # initialPriceInBuck
        except Exception:
            ba, ip = 0, 0
        base = ba * ip // (10 ** 18) if ba and ip else 0
        if base and prices[i]:
            target_val.append(base * ip // prices[i])
        else:
            target_val.append(base)
    tv_sum = sum(target_val)

    # Actual values from pool token-side reserves (normalized by token
    # decimals so 8-dec cbBTC is comparable to 18-dec PAXG/AOIL).
    actual_val = []
    for i in range(N):
        rt = _bal(d.tokens[i], d.pool_buck[i])
        actual_val.append(rt * prices[i] // (10 ** d.dec[i])
                          if prices[i] else 0)
    av_sum = sum(actual_val)

    out = []
    for i in range(N):
        tw = target_val[i] / tv_sum if tv_sum else 0.0
        aw = actual_val[i] / av_sum if av_sum else 0.0
        out.append([aw, tw])
    return out


class Snapshotter:
    def __init__(self, d, scenario):
        self.d = d
        self.s = scenario
        self.frames: list[dict] = []
        # Run-level metadata (resolved experiment config, applied
        # interventions, ...) embedded in the written vector.  Callers may
        # store live references here (e.g. a growing intervention log).
        self.meta: dict = {}
        self.tokens = [t[0] for t in scenario.tokens]
        self._pool_abi, _ = load_artifact("UniswapV3Pool")
        # address -> day-0 USDC-micro value per RAW unit helper key
        self._kind = {Web3.to_checksum_address(d.usdc.address): ("usdc", 0),
                      Web3.to_checksum_address(d.buck.address): ("buck", 0)}
        for i, tc in enumerate(d.tokens):
            self._kind[Web3.to_checksum_address(tc.address)] = ("tok", i)
        self._lp_cap: dict | None = None     # group -> capital (USDC, day0)

    # -- LP (Uniswap V3 position) accounting --------------------------- #

    def _raw_usd0(self, addr: str, raw: int) -> int:
        """USDC-micro value at day-0 prices of `raw` units of token `addr`.
        USDC: 1:1.  BUCK: 1:1 (1 BUCK == 1 USDC at t0 by construction).
        TOKEN i: raw * ref0_i / 10**dec_i."""
        kind, i = self._kind[Web3.to_checksum_address(addr)]
        if kind in ("usdc", "buck"):
            return raw
        return raw * self.s.prices.ref(i, 0) // (10 ** self.d.dec[i])

    def _lp_groups(self) -> dict:
        """Per-group (usdc/buck/ub) cumulative LP fee income and deployed
        capital, both in USDC-micro at day-0 prices.  Full-range positions:
        uncollected fee ~= L*(feeGrowthGlobal - feeGrowthInsideLast)/2**128
        + tokensOwed (no poke needed); deployed capital = the L-backed
        redeemable amounts at the *first* observed price."""
        d = self.d
        fees = {"usdc": 0, "buck": 0, "ub": 0}
        cap = {"usdc": 0, "buck": 0, "ub": 0}
        for addr, owner, lo, hi, grp in d.pool_meta:
            p = d.w3.eth.contract(address=addr, abi=self._pool_abi)
            t0 = p.functions.token0().call()
            t1 = p.functions.token1().call()
            key = Web3.solidity_keccak(
                ["address", "int24", "int24"],
                [Web3.to_checksum_address(owner), lo, hi])
            L, fi0, fi1, owed0, owed1 = p.functions.positions(key).call()
            fg0 = p.functions.feeGrowthGlobal0X128().call()
            fg1 = p.functions.feeGrowthGlobal1X128().call()
            unc0 = L * ((fg0 - fi0) & MASK256) // Q128 + owed0
            unc1 = L * ((fg1 - fi1) & MASK256) // Q128 + owed1
            fees[grp] += self._raw_usd0(t0, unc0) + self._raw_usd0(t1, unc1)
            sp = p.functions.slot0().call()[0]
            if sp:
                a0 = L * Q96 // sp           # full-range redeemable amounts
                a1 = L * sp // Q96
                cap[grp] += self._raw_usd0(t0, a0) + self._raw_usd0(t1, a1)
        return {g: (fees[g], cap[g]) for g in fees}

    def agg_value(self, agents, day) -> int:
        """Total agent portfolio in USDC, valued at *day-0* prices so this
        is REALIZED economic P&L (arb edge + token accumulation), not
        mark-to-market noise from the day's GBM-moved CSV reference."""
        d = self.d
        v = 0
        for ag in agents:
            if not getattr(ag, "is_eoa", False) or ag.account is None:
                continue
            v += _bal(d.usdc, ag.address)
            for i, tc in enumerate(d.tokens):
                v += _bal(tc, ag.address) * self.s.prices.ref(i, 0) // (10 ** d.dec[i])
        return v

    def _basket_nav(self) -> int:
        """Total BUCK value of ALL BuckBasket LP positions (token + BUCK
        sides), in 6-decimal BUCK wei (BUCK uses USDC-compatible
        decimals).  Empty pools (no reserves) contribute 0."""
        d = self.d
        total = 0
        for i in range(len(d.tokens)):
            rt = _bal(d.tokens[i], d.pool_buck[i])
            rb = _bal(d.buck, d.pool_buck[i])
            if rt == 0 or rb == 0:
                continue  # empty pool
            p = rb * (10 ** d.dec[i]) // rt   # BUCK per whole token (18d)
            total += rt * p // (10 ** d.dec[i])  # token side value in BUCK
            total += rb                          # BUCK side value
        return total

    def _agent_value(self, agents, day, cls_name) -> int:
        """Portfolio value of matching agents, including the value of any
        BuckBasket LP deposits (receipt NFTs)."""
        d = self.d
        names = {cls_name} if isinstance(cls_name, str) else set(cls_name)
        v = 0
        for ag in agents:
            if type(ag).__name__ not in names:
                continue
            # USDC is the numeraire and 6-dec like the valuation itself, so it
            # enters at par.  Omitting it made this a measure of *deployment*
            # rather than wealth: an agent converting USDC into TOKEN moved
            # value from an uncounted bucket into a counted one, and the
            # difference was booked as gain.
            #
            # This alone does not make `directMintPnl` a return.  The DM
            # agents call `_buy_token_from_usdc`, which mints the USDC it
            # spends inside the same call, so they never hold a USDC balance
            # for this line to find -- the value is conjured at the purchase
            # site, not lost at the valuation site.  For a sound return use
            # the realized round-trip accounting (`dmProfitUsd` over
            # `dmDollarDays`), which compares redeem proceeds against what was
            # actually deposited and is immune to both.
            v += _bal(d.usdc, ag.address)
            for i, tc in enumerate(d.tokens):
                v += _bal(tc, ag.address) * self.s.prices.ref(i, 0) // (10 ** d.dec[i])
            # Include the BuckBasket deposit at the depositor's *own* economic
            # stake -- what they contributed and can redeem -- NOT the doubled
            # LP position.  For a TOKEN deposit the depositor owns the TOKEN
            # side; the paired BUCK was system-minted seigniorage that is burned
            # on redeem and is never the depositor's wealth (counting it roughly
            # doubled DM value and manufactured phantom ROI).  For a BUCK-side
            # deposit (tokenPrincipal == 0) the contributed BUCK is their stake
            # (1 BUCK == 1 USDC at t=0; both 6-dec).
            di = ag.deposit_info(d) if hasattr(ag, "deposit_info") else None
            if di is not None:
                tok_idx, ptok, pbuck = di
                if ptok > 0:
                    v += ptok * self.s.prices.ref(tok_idx, 0) // (10 ** d.dec[tok_idx])
                else:
                    v += pbuck
        return v

    def capture(self, day, ctr, agents, init_val,
                rebal_init_val: int | None = None,
                dm_init_val: int | None = None) -> None:
        d = self.d
        # Per-agent telemetry (TELEMETRY.md, v1).  Static facts once into
        # meta; per-frame records under frame["ag"] for agents due at this
        # day (day % TELEMETRY_STRIDE == 0).  Agents opt in by implementing
        # telemetry()/telemetry_static(); the default None costs nothing.
        if "telemetry" not in self.meta:
            metas = []
            for ag in agents:
                try:
                    st = ag.telemetry_static()
                except Exception:
                    st = None
                if st is not None:
                    cls = type(ag).__name__
                    metas.append({
                        "id": f"{cls}#{ag.idx}", "cls": cls, "idx": ag.idx,
                        "stride": max(1, int(getattr(
                            ag, "TELEMETRY_STRIDE", 1))),
                        "knobs": st})
            if metas:
                self.meta["telemetry"] = {
                    "version": 1, "units": "usd6", "agents": metas}
        ag_t = {}
        for ag in agents:
            stride = max(1, int(getattr(ag, "TELEMETRY_STRIDE", 1)))
            if day % stride:
                continue
            try:
                rec = ag.telemetry(d)
            except Exception:
                rec = None
            if rec:
                ag_t[f"{type(ag).__name__}#{ag.idx}"] = rec
        ref, su, sb = [], [], []
        for i, tc in enumerate(d.tokens):
            ref.append(self.s.prices.ref(i, day))
            su.append(_implied(d, d.pool_usdc[i], tc, d.dec[i], d.usdc))
            sb.append(_implied(d, d.pool_buck[i], tc, d.dec[i], d.buck))
        # Floating BUCK/USDC pool: implied USDC-micro per 1 BUCK.
        buck_usd = 0
        if getattr(d, "pool_ub", ""):
            ru = _bal(d.usdc, d.pool_ub)
            rb = _bal(d.buck, d.pool_ub)
            if rb:
                buck_usd = ru * 1_000_000 // rb
        try:
            bv = int(d.basket.functions.basketValueInBuck().call())
        except Exception:
            bv = 0
        try:
            bk = int(d.kctrl.functions.buckK().call())
        except Exception:
            bk = 0
        # PID internals (ppm error, ppm*s integral, ppm dError) -- additive
        # observability for the equilibrium experiment; 0 if unavailable.
        try:
            pid_p = int(d.kctrl.functions.P().call())
            pid_i = int(d.kctrl.functions.I().call())
            pid_d = int(d.kctrl.functions.D().call())
        except Exception:
            pid_p = pid_i = pid_d = 0
        # Idle-BUCK held by savers (sum over any SaverAgent proxies present).
        saver_hold = 0
        for ag in agents:
            if type(ag).__name__ == "SaverAgent" and getattr(ag, "proxy", None):
                try:
                    saver_hold += _bal(d.buck, ag.address)
                except Exception:
                    pass
        # Excursion-arb population state: BUCK inventory + par-marked P&L
        # (nw - nw0) summed over ExcursionArbAgent and its subclasses.
        exc_held = exc_pnl = 0
        for ag in agents:
            if (type(ag).__name__.startswith("Excursion")
                    and getattr(ag, "proxy", None)):
                try:
                    exc_held += _bal(d.buck, ag.address)
                    exc_pnl += ag._nw(d) - ag._nw0
                except Exception:
                    pass
        # Differential-mode private rebalancers: P&L vs buy-and-hold of
        # the initial inventory (raw BUCK), summed over the class.
        crb_pnl = crb_n = 0
        for ag in agents:
            if type(ag).__name__ == "CommodityRebalArbAgent":
                try:
                    rec = ag.telemetry(d)
                    if rec:
                        crb_pnl += rec.get("pnl", 0)
                        crb_n += 1
                except Exception:
                    pass
        # Borrower issuance-channel state (equilibrium scenario): summed
        # K-scaled limit / drawn / funding-reserve accounts across the
        # FatCreditBorrower population, plus the cumulative flow counters.
        # This is the observability that shows WHY issuance lives or dies
        # (e.g. the reserve throttle clamping the channel shut).
        fat = {"limit": 0, "drawn": 0, "reserve_held": 0, "reserve_req": 0,
               "pending": 0}
        for ag in agents:
            cs = ag.channel_state(d) if hasattr(ag, "channel_state") else None
            if cs:
                for k in fat:
                    fat[k] += cs.get(k, 0)
        # Optimal-control debtors: per-agent net worth + counterfactual (the
        # strategy-comparison observability; a handful of small dicts/frame).
        octl = []
        for ag in agents:
            if hasattr(ag, "octl_state"):
                st = ag.octl_state(d)
                if st:
                    octl.append(st)
        # Discount-BUCK time arbs (holder/basketeer A/B): per-agent
        # inventory so PnL and parked positions chart per frame.
        arbs2 = []
        for ag in agents:
            if hasattr(ag, "arb_state"):
                st = ag.arb_state(d)
                if st:
                    arbs2.append(st)
        # Regime knobs (mean over each class present) -- track how the
        # periodic regime shocks move the population's primary knobs.  Read
        # off the live agent objects; guarded so non-equilibrium runs are 0.
        uts = [getattr(ag, "util_target", None) for ag in agents
               if type(ag).__name__ == "FatCreditBorrowerAgent"]
        uts = [u for u in uts if u is not None]
        regime_util = sum(uts) / len(uts) if uts else 0.0
        brs = [getattr(ag, "base_rate", None) for ag in agents
               if type(ag).__name__ == "SaverAgent"]
        brs = [b for b in brs if b is not None]
        regime_saver = sum(brs) / len(brs) if brs else 0.0
        lg = self._lp_groups()
        if self._lp_cap is None:                       # freeze capital basis
            self._lp_cap = {g: lg[g][1] for g in lg}
        lp = {g: [lg[g][0], self._lp_cap[g]] for g in lg}
        # Raw token + BUCK balances in each TOKEN/BUCK pool.
        pool_bal = []
        for i, tc in enumerate(d.tokens):
            pool_bal.append([_bal(tc, d.pool_buck[i]),
                             _bal(d.buck, d.pool_buck[i])])
        # Basket target vs actual pool value weights (for rebalancing plot).
        pool_weights = _pool_value_weights(d)

        # Rebalancer P&L (if any rebalancers are present).
        reb_pnl = 0
        if rebal_init_val is not None:
            reb_pnl = self._agent_value(
                agents, day, "BuckBasketRebalancerAgent") - rebal_init_val

        # Direct-mint agent P&L.
        dm_pnl = 0
        if dm_init_val is not None:
            dm_pnl = self._agent_value(
                agents, day, ("DirectMintAgent", "DirectMintBuckAgent")
            ) - dm_init_val

        # Basket NAV (total BUCK value of all BuckBasket LP) and
        # outstanding DM liability (sum of buckPrincipal across active
        # deposits).  Treasury BUCK tracks retained profit from redemptions.
        nav = self._basket_nav()
        out_buck = ctr.get("dmOutstandingBuck", 0)
        treas_buck = ctr.get("treasuryBuck", 0)
        treas_frac = treas_buck / nav if nav > 0 else 0.0

        self.frames.append({
            "invested": init_val,                      # arb capital (USDC,d0)
            "lp": lp,                                  # group: [feeUsd, capUsd]
            "day": day,
            "refUsd": ref,
            "spotUsdc": su,
            "spotBuck": sb,
            "basketVal": bv,
            "buckK": bk,
            "pid_p": pid_p,
            "pid_i": pid_i,
            "pid_d": pid_d,
            "saver_hold": saver_hold,
            "buck_usd": buck_usd,                      # BUCK/USDC spot (micro)
            "regime_util": regime_util,                # mean borrower util_target
            "regime_saver": regime_saver,              # mean saver base_rate (USDC)
            "saver_buys": ctr.get("saverBuys", 0),     # cumulative dip buys
            "saver_sells": ctr.get("saverSells", 0),   # cumulative rip sells
            "regime_events": ctr.get("regimeEvents", 0),   # cumulative shocks
            # Borrower issuance channel: summed live state + cumulative flows.
            "fat_limit": fat["limit"],
            "fat_drawn": fat["drawn"],
            "fat_reserve_held": fat["reserve_held"],
            "fat_reserve_req": fat["reserve_req"],
            "fat_pending": fat["pending"],
            "fat_issued": ctr.get("fatIssued", 0),         # cum BUCK sold
            "fat_retired": ctr.get("fatRetired", 0),       # cum BUCK bought back
            "fat_burned": ctr.get("fatBurned", 0),         # cum BUCK burned
            "fat_prefund": ctr.get("fatPreFundBought", 0), # cum reserve buys
            "fat_throttled": ctr.get("fatThrottled", 0),   # cum throttle hits
            "fat_released": ctr.get("fatReleased", 0),     # cum reserve released
            "octl": octl,                                  # per-debtor states
            "arb2": arbs2,                                 # discount-arb states
            "dba_bought": ctr.get("dbaBought", 0),
            "dba_sold": ctr.get("dbaSold", 0),
            "dba_spent": ctr.get("dbaSpent", 0),
            "dba_recv": ctr.get("dbaRecv", 0),
            "dbb_bought": ctr.get("dbbBought", 0),
            "dbb_sold": ctr.get("dbbSold", 0),
            "dbb_spent": ctr.get("dbbSpent", 0),
            "dbb_recv": ctr.get("dbbRecv", 0),
            "dbb_parked": ctr.get("dbbParked", 0),
            "dbb_harvests": ctr.get("dbbHarvests", 0),
            # BuckIssuerArbAgent: the supply side.  Without these the agent
            # is invisible -- counters live in `ctr` and a frame that does
            # not copy them reads 0 forever, which is exactly how the first
            # two smoke runs looked like a dead agent.
            "bia_drawn": ctr.get("biaDrawn", 0),
            "bia_retired": ctr.get("biaRetired", 0),
            "bia_bought": ctr.get("biaBought", 0),
            "bia_sold": ctr.get("biaSold", 0),
            "bia_throttled": ctr.get("biaThrottled", 0),
            # BuckPoolInvestorAgent.  Snapshotted at birth this time: a
            # counter that lives only in `ctr` reads 0 forever, which has
            # already produced two confident wrong readings on this branch.
            "bpi_minted": ctr.get("bpiMinted", 0),
            "bpi_positions": ctr.get("bpiPositions", 0),
            "bpi_repositions": ctr.get("bpiRepositions", 0),
            # MonetaryOpsAgent: the four quadrants, and the bounds that
            # stopped each of the three runaways.  moNoBook counts the times
            # Q2 wanted to retire and had no drawn line to retire against --
            # the one thing an agent structurally cannot do that the basket
            # can, so it is the measure of what phases 3/4 would add.
            "mo_dev_bp": ctr.get("moDevBp", 0),
            "mo_q1": ctr.get("moQ1", 0),
            "mo_q2": ctr.get("moQ2", 0),
            "mo_q3": ctr.get("moQ3", 0),
            "mo_q4": ctr.get("moQ4", 0),
            "mo_bought": ctr.get("moBought", 0),
            "mo_sold": ctr.get("moSold", 0),
            "mo_issued": ctr.get("moIssued", 0),
            "mo_retired": ctr.get("moRetired", 0),
            "mo_burned": ctr.get("moBurned", 0),
            "mo_opened": ctr.get("moOpened", 0),
            "mo_pos_limit": ctr.get("moPosLimit", 0),
            "mo_cum_limit": ctr.get("moCumLimit", 0),
            "mo_no_book": ctr.get("moNoBook", 0),
            "mo_throttled": ctr.get("moThrottled", 0),
            "mo_funded": ctr.get("moFunded", 0),
            # BuckBasketOps, driven by MonetaryKeeperAgent.  Distinct mk*
            # prefix from the mo* agent prototype above: both write the same
            # ctr dict and merging them would silently double-count.
            "mk_q1": ctr.get("mkQ1", 0),
            "mk_q2": ctr.get("mkQ2", 0),
            "mk_q3": ctr.get("mkQ3", 0),
            "mk_q4": ctr.get("mkQ4", 0),
            "mk_ops": ctr.get("mkOps", 0),
            "mk_idle": ctr.get("mkIdle", 0),
            "mk_bound": ctr.get("mkBound", 0),
            "mk_no_advice": ctr.get("mkNoAdvice", 0),
            "mk_done": ctr.get("mkDone", 0),
            "mk_no_value": ctr.get("mkNoValue", 0),
            "mk_tok_held": list(ctr.get("mkTokHeld", [])),
            "mk_tok_value": ctr.get("mkNavBuck", 0),
            # BuckBasketFence.  fk_footprint vs fk_budget is the whole test:
            # if the footprint stops tracking the budget down, the K-scaling
            # is not biting.
            "fk_struck": ctr.get("fkStruck", 0),
            "fk_minted": ctr.get("fkMinted", 0),
            "fk_burned": ctr.get("fkBurned", 0),
            "fk_nav": ctr.get("fkNav", 0),
            "fk_shares": ctr.get("fkShares", 0),
            "fk_footprint": ctr.get("fkFootprint", 0),
            "fk_budget": ctr.get("fkBudget", 0),
            "fk_failed": ctr.get("fkFailed", 0),
            "fk_err": ctr.get("fk_err", ""),
            "mk_slippage": ctr.get("mkSlippage", 0),
            "mk_no_director": ctr.get("mkNoDirector", 0),
            "mk_other_err": ctr.get("mkOtherErr", 0),
            # Book state, read from chain each operation (gauges, not counters).
            "mk_outstanding": ctr.get("mkOutstanding", 0),
            "mk_buck_held": ctr.get("mkBuckHeld", 0),
            # What the desk's inventory has taken out of the deviation K sees.
            "mk_offset": ctr.get("mkOffset", 0),
            "mk_err": ctr.get("mk_err", ""),
            "mo_why": dict(ctr.get("moWhy", {})),
            # The last exception each proxy agent swallowed.  These were set
            # into `ctr` from the start and copied nowhere, so a smoke run
            # showed BuckPoolInvestorAgent minting $4.7M and opening ZERO
            # positions with no visible reason -- the fourth time on this
            # branch that an unplumbed counter turned a loud failure into a
            # silent one.
            "mo_err": ctr.get("mo_err", ""),
            "bpi_err": ctr.get("bpi_err", ""),
            "bia_err": ctr.get("bia_err", ""),
            "bcd_err": ctr.get("bcd_err", ""),
            # WHY they were refused, not just how often.  A bare count let a
            # wrong explanation stand unchallenged for two runs.
            "bia_why": dict(ctr.get("biaWhy", {})),
            "bcd_why": dict(ctr.get("bcdWhy", {})),
            "bcd_deploys": ctr.get("bcdDeploys", 0),
            "bcd_throttled": ctr.get("bcdThrottled", 0),
            "bcd_saved": ctr.get("bcdSaved", 0),
            "bcd_deploys": ctr.get("bcdDeploys", 0),
            "bcd_atomic_refis": ctr.get("bcdAtomicRefis", 0),
            "bcd_atomic_declined": ctr.get("bcdAtomicDeclined", 0),
            "growth_arrivals": ctr.get("growthArrivals", 0),
            "growth_departures": ctr.get("growthDepartures", 0),
            # Endogenous-origination arrivals (arrive_mode "endog"): how many
            # debtors / basket depositors the price-responsive hazard clocks
            # have brought online so far.
            "endog_debtor_arrivals": ctr.get("endogDebtorArrivals", 0),
            "endog_depositor_arrivals": ctr.get("endogDepositorArrivals", 0),
            # Excursion-arb + whale-raid observability.
            "exc_entries": ctr.get("excursionEntries", 0),
            "exc_exits": ctr.get("excursionExits", 0),
            "exc_pnl": exc_pnl,
            "exc_held": exc_held,
            "raid_phase": ctr.get("raidPhase", 0),
            "raid_pnl": ctr.get("raidPnl", 0),
            "raid_side": ctr.get("raidSide", ""),
            # Directional quadrant volumes of the excursion population
            # (cumulative usd6): [absorb, retire, supply, issue].
            "exc_q": [ctr.get("excQ1", 0), ctr.get("excQ2", 0),
                      ctr.get("excQ3", 0), ctr.get("excQ4", 0)],
            "crb_pnl": crb_pnl,
            "crb_trades": ctr.get("crbTrades", 0),
            "neighbors_retired": ctr.get("neighborsRetired", 0),
            "iv_events": ctr.get("ivEvents", 0),           # cum interventions
            "supply": int(d.buck.functions.totalSupply().call()),
            "directTrades": ctr["directTrades"],
            "cycleTrades": ctr["cycleTrades"],
            "ubTrades": ctr.get("ubTrades", 0),
            "buckUsd": buck_usd,
            "aggPnl": self.agg_value(agents, day) - init_val,
            "poolBal": pool_bal,
            "poolWeights": pool_weights,
            "rebalancerPnl": reb_pnl,
            "rebalanceTrades": ctr.get("rebalanceTrades", 0),
            "directorPokes": ctr.get("directorPokes", 0),
            "directorTrades": ctr.get("directorTrades", 0),
            "directMintPnl": dm_pnl,
            "dmEntries": ctr.get("dmEntries", 0),
            "dmExits": ctr.get("dmExits", 0),
            "dmExitFails": ctr.get("dmExitFails", 0),
            "dmTotalInvested": ctr.get("dmTotalInvested", 0),
            # Realized round-trip accounting: profit booked only when a
            # deposit is actually redeemed, against the capital-days it was
            # deployed for.  Already maintained by _DMBase._record_roundtrip
            # and printed at teardown; carried per-frame so the plots can show
            # a return that does not depend on how an agent's idle wealth is
            # valued.
            "dmProfitUsd": ctr.get("dmProfitUsd", 0),
            "dmDollarDays": ctr.get("dmDollarDays", 0),
            "dmRoundTrips": ctr.get("dmRoundTrips", 0),
            "basketNav": nav,
            "dmOutstanding": out_buck,
            "treasuryBuck": treas_buck,
            "treasuryShare": treas_frac,
        })
        if ag_t:
            self.frames[-1]["ag"] = ag_t

    def write(self, path=None) -> Path:
        p = Path(path) if path else (
            DEFAULT_VECTORS / f"{self.s.name}-sim.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        out = {"tokens": self.tokens, "decimals": self.d.dec,
               "frames": self.frames}
        if self.meta:
            out["meta"] = self.meta
        # Atomic: the incremental checkpoint rewrites this file every 25
        # days while the run continues, and a reader plotting a run in
        # flight would otherwise be able to catch a half-written file.
        # Write beside it and rename, which is atomic within a filesystem.
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(out))
        tmp.replace(p)
        return p
