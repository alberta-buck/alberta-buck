"""Timeline orchestration: deploy, register agents, run days x ticks."""

from __future__ import annotations

import random

from alberta_buck.sim import identity as idmod
from alberta_buck.sim.agents import REGISTRY, MarketMakerWhale
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.deploy import deploy, REDEEMED_TOPIC
import alberta_buck.sim.rebalancer  # noqa: F401  triggers @_register
import alberta_buck.sim.direct_mint  # noqa: F401  triggers @_register
from alberta_buck.sim.direct_mint import (
    BootstrapDMAgent, DirectMintAgent, DirectMintBuckAgent,
)
from alberta_buck.sim.snapshot import Snapshotter

E6 = 10 ** 6


def run(scenario, anvil, out_path=None, verbose=True, basket_impl="legacy") -> dict:
    w3 = anvil.w3
    chain = Chain(w3, w3.eth.accounts[0])
    rng = idmod.seeded_rng(scenario.seed)
    prng = random.Random(scenario.seed)

    if verbose:
        print(f"[sim] deploying '{scenario.name}' with {basket_impl} basket "
              f"({scenario.days}d x {scenario.ticks_per_day} ticks)...",
              flush=True)
    d = deploy(chain, anvil, scenario, rng, basket_impl=basket_impl)

    # --- build + register the agent population ----------------------- #
    # Reset per-class counters defensively so back-to-back sim runs in
    # the same process don't accumulate stale seq numbers (the
    # bootstrap-token assignment depends on `seq < N`).
    BootstrapDMAgent._counter = 0
    DirectMintAgent._counter = 0
    DirectMintBuckAgent._counter = 0
    agents, idx = [], 0
    for cls_name, n in scenario.agents.items():
        cls = REGISTRY[cls_name]
        for _ in range(n):
            a = cls(idx); idx += 1
            agents.append(a)
    for a in agents:
        a.setup(d, scenario, rng)
    whales = [a for a in agents if isinstance(a, MarketMakerWhale)]
    arbs = [a for a in agents if a not in whales]
    if verbose:
        eoa_count = sum(1 for a in agents if getattr(a, "is_eoa", True))
        public_agents = len(agents) - eoa_count
        print(f"[sim] registered {eoa_count} EOA identities "
              f"+ {public_agents} public agent contracts "
              f"({len(arbs)} arb, {len(whales)} whale); deploy done.")

    # --- pre-tick bootstrap phase ----------------------------------- #
    # Agents that need to seed on-chain state before any market activity
    # (DM agents seeding empty BuckBasket pools) run their bootstrap()
    # here, so every pool has live liquidity by tick 0.
    ctr = {"directTrades": 0, "cycleTrades": 0, "ubTrades": 0}
    for a in agents:
        a.bootstrap(d, scenario, ctr)
    if verbose and ctr.get("dmEntries", 0) > 0:
        print(f"[sim] bootstrap: {ctr['dmEntries']} DM deposits seeded "
              f"basket pools before tick 0", flush=True)

    # --- experiment: price overlay + scripted interventions ---------- #
    # When an Experiment is attached, wrap the price source so price_shock
    # interventions can overlay multipliers (the whale then re-pins pools to
    # the shocked reference), and stand up the Interventions engine over the
    # LIVE agent/arb lists (population changes mutate them in place).
    exp = getattr(scenario, "experiment", None)
    iv = None
    if exp is not None:
        from alberta_buck.sim.experiment import Interventions, PriceOverlay
        if not isinstance(scenario.prices, PriceOverlay):
            scenario.prices = PriceOverlay(scenario.prices)
        iv = Interventions(exp, d, scenario, agents, arbs, ctr, rng)

    snap = Snapshotter(d, scenario)
    if exp is not None:
        # Live references: iv.applied keeps growing; every checkpoint write
        # (and the final one) embeds the up-to-date log + resolved config.
        snap.meta["experiment"] = exp.resolved()
        if iv is not None:
            snap.meta["interventions_applied"] = iv.applied
    # Capital baselines: computed AFTER bootstrap so the dm baseline
    # includes the bootstrap deposits (otherwise day-0 P&L would jump
    # by the bootstrap principal).
    init_val = snap.agg_value(agents, 0)
    reb_init = snap._agent_value(agents, 0, "BuckBasketRebalancerAgent")
    dm_init = snap._agent_value(
        agents, 0, ("DirectMintAgent", "DirectMintBuckAgent"))

    ts = w3.eth.get_block("latest")["timestamp"] + 10
    # `day_step` advances the calendar (and on-chain clock) more than one day
    # per iteration -- a coarse macro mode so multi-year horizons fit a bounded
    # run.  Default 1 == unchanged.  Each iteration still advances a full
    # `day_step` days of wall-clock time across its inner ticks.
    step = max(1, getattr(scenario, "day_step", 1))
    tick_secs = max(60, (86_400 * step) // scenario.ticks_per_day)

    n_tok = len(d.tokens)
    for day in range(0, scenario.days, step):
        # Scripted interventions fire first, so a price shock scheduled for
        # this day is already visible in refUsd / the whale's snap below.
        if iv is not None:
            iv.apply_due(day)
        # Current day + reference USD prices, for the agents' realized-return
        # accounting (deposit/redeem valuation) and the throughput meter.
        ctr["day"] = day
        ctr["refUsd"] = [scenario.prices.ref(i, day) for i in range(n_tok)]
        # One market-maker intervention per day, but it updates every
        # TOKEN/USDC truth pool.  Updating only one random token let the
        # floating BUCK/USDC gauge be dominated by whichever asset was most
        # recently snapped, making TOKEN/BUCK->USD plots look cross-wired.
        whale_tick = prng.randrange(scenario.ticks_per_day)
        whale_order = list(range(n_tok))
        prng.shuffle(whale_order)
        for tick in range(scenario.ticks_per_day):
            ts += tick_secs
            anvil.warp_to(ts)
            d.chain.clear_balance_cache()
            if tick == whale_tick:
                for wagent in whales:
                    for whale_tok in whale_order:
                        wagent.snap(d, scenario, day, whale_tok, ctr)
            order = arbs[:]
            prng.shuffle(order)
            for a in order:
                a.act(d, scenario, day, tick, ctr)
        try:
            chain.send(d.kctrl.functions.compute())
        except Exception:
            pass
        snap.capture(day, ctr, agents, init_val, reb_init, dm_init)
        # Incremental checkpoint: flush the vector periodically so a long run
        # killed mid-flight still yields usable partial data (and can be
        # plotted).  Cheap relative to a day's on-chain work; final write below
        # still produces the complete vector.
        if out_path and day > 0 and day % 25 == 0:
            snap.write(out_path)
        if verbose and (day % 20 == 0 or day == scenario.days - 1):
            f = snap.frames[-1]
            errs = [abs(f["spotUsdc"][i] - f["refUsd"][i]) / max(1, f["refUsd"][i])
                    for i in range(len(d.tokens))]
            print(f"[sim] day {day:4d}  meanTrackErr={100*sum(errs)/len(errs):.2f}%"
                  f"  cycle={ctr['cycleTrades']} direct={ctr['directTrades']}",
                  flush=True)

    path = snap.write(out_path)

    # --- teardown: force-redeem active DM positions, then report ----- #
    if verbose:
        print("\n[teardown] ======== Force-Redeem Active DM Positions ========")

        # Capture LP group state BEFORE the teardown redemptions
        # (each redeem collects fees and reshuffles the buck group).
        lg_pre = snap._lp_groups()
        cap_pre = dict(snap._lp_cap) if snap._lp_cap else {}

        # Pre-teardown state.  All three quantities are 6-decimal BUCK wei
        # (BUCK uses USDC-compatible decimals); divide by E6 to get whole
        # BUCK for the display.
        outstanding_pre = ctr.get("dmOutstandingBuck", 0)
        treasury_pre = ctr.get("treasuryBuck", 0)
        nav_pre = snap._basket_nav()
        print(f"[teardown] pre-state:  outstanding {outstanding_pre/E6:,.2f}  "
              f"treasury {treasury_pre/E6:,.2f}  "
              f"nav {nav_pre/E6:,.2f} BUCK")

        # Force-redeem every DM agent that entered but never exited.
        # The agent's own _exit() updates ctr (dmExits, dmOutstandingBuck,
        # treasuryBuck) by decoding the new Redeemed event shape.
        active_dm = [a for a in agents
                     if type(a).__name__ == "DirectMintAgent"
                     and getattr(a, "_entered", False)
                     and not getattr(a, "_exited", True)
                     and getattr(a, "_receipt_id", None) is not None]
        print(f"[teardown] forcing redemption of {len(active_dm)} active "
              f"DM positions...")
        forced_ok = 0
        for a in active_dm:
            try:
                a._exit(d, ctr)
                forced_ok += 1
            except Exception as e:
                print(f"  [teardown] dm-{a.idx} exit failed: {e!r}")
        print(f"[teardown] {forced_ok}/{len(active_dm)} forced "
              f"redemptions succeeded")

        # Post-teardown state.
        outstanding_post = ctr.get("dmOutstandingBuck", 0)
        treasury_post = ctr.get("treasuryBuck", 0)
        nav_post = snap._basket_nav()
        print(f"\n[teardown] post-state: outstanding {outstanding_post/E6:,.2f}  "
              f"treasury {treasury_post/E6:,.2f}  "
              f"nav {nav_post/E6:,.2f} BUCK")
        treasury_delta = treasury_post - treasury_pre
        if treasury_delta != 0:
            print(f"[teardown] teardown released "
                  f"{treasury_delta/E6:,.2f} BUCK to treasury")

        # End-of-sim summary: entries, exits, treasury share of NAV.
        #
        # "treasury share" = treasuryBuck / NAV.  treasuryBuck is the
        # cumulative profit BUCK retained by the basket on redemptions
        # (the "sell-high, recycle to buy-low" treasury leg).  NAV is
        # the total LP value of all basket pools, which already includes
        # the TOKEN side that the active LPs still own -- so the prior
        # formula (NAV - outstanding) / NAV was wrong: it counted every
        # active deposit's TOKEN-side principal as "treasury" simply
        # because LP value is ~2x the BUCK obligation, not because the
        # basket actually retained any profit.
        print(f"\n[teardown] Lifetime DM activity:")
        print(f"  entries:           {ctr.get('dmEntries', 0)}")
        print(f"  exits:             {ctr.get('dmExits', 0)}")
        print(f"  exit failures:     {ctr.get('dmExitFails', 0)}")
        print(f"  outstanding BUCK:  {outstanding_post/E6:,.2f}")
        print(f"  treasury BUCK:     {treasury_post/E6:,.2f}  (cumulative)")
        if nav_post > 0:
            ts_pct = 100 * treasury_post / nav_post
            print(f"  treasury share:    {ts_pct:.4f}% of NAV "
                  f"({nav_post/E6:,.2f} BUCK)")
        else:
            print(f"  treasury share:    NAV is 0 (all positions cleared)")

        # -- TOKEN/USDC pools + BUCK/USDC pool: LP group summary ----- #
        print(f"\n[teardown] LP group summary (cumulative fees / day-0 capital):")
        for g, cap0 in cap_pre.items():
            fee, _cap = lg_pre.get(g, (0, 0))
            roi = 100 * fee / cap0 if cap0 else 0
            print(f"  {g:5s}  capital ${cap0/E6:,.0f}  "
                  f"fees ${fee/E6:,.0f}  ROI {roi:+.3f}%")

    # --- summary ----------------------------------------------------- #
    tail = snap.frames[-30:] if len(snap.frames) >= 30 else snap.frames
    track = []
    for i in range(len(d.tokens)):
        e = sum(abs(fr["spotUsdc"][i] - fr["refUsd"][i]) / max(1, fr["refUsd"][i])
                for fr in tail) / len(tail)
        track.append(e)
    verified = all(d.reg.functions.isVerified(a.address).call() for a in agents)
    # Arb throughput vs basket fee income: validate that the DM return scale
    # is consistent with actual BUCK volume crossing the pools, not a trade
    # count.  Each routed cycle crosses ~2 TOKEN/BUCK pool hops at fee_buck,
    # so the basket's gross fee take ~ volume * 2 * fee_buck.
    cycle_vol = ctr.get("cycleVolumeUsdc", 0)
    fee_take = cycle_vol * 2 * d.fee_buck // 1_000_000   # fee_buck is in pip (1e6)
    dm_invested = ctr.get("dmTotalInvested", 0)
    # Realized return: dollar-day-weighted APR over completed deposit->redeem
    # round-trips -- profit per dollar per day, annualised.  This is the honest
    # holder ROI (USD redeemed vs USD deposited), flow-adjusted so new deposits
    # are never mistaken for gains.
    profit_usd = ctr.get("dmProfitUsd", 0)
    dollar_days = ctr.get("dmDollarDays", 0)
    realized_apr = (365.0 * profit_usd / dollar_days) if dollar_days else 0.0
    summary = {
        "json": str(path),
        "days": len(snap.frames),
        "tokens": [t[0] for t in scenario.tokens],
        "track_err": track,
        "cycle_trades": ctr["cycleTrades"],
        "ub_trades": ctr["ubTrades"],
        "direct_trades": ctr["directTrades"],
        "cycle_volume_usdc": cycle_vol,
        "basket_fee_estimate": fee_take,
        "dm_total_invested": dm_invested,
        "dm_realized_apr": realized_apr,
        "dm_round_trips": ctr.get("dmRoundTrips", 0),
        "dm_profit_usd": profit_usd,
        "all_eoa_verified": verified,
        "n_agents": len(agents),
    }
    if verbose:
        print(f"\n[sim] wrote {path}")
        for i, t in enumerate(summary["tokens"]):
            print(f"[sim]   {t:5s} TOKEN/USDC tail tracking err {100*track[i]:.2f}%")
        print(f"[sim] BUCK-routed trades: {ctr['cycleTrades']}  "
              f"(via BUCK/USDC pool: {ctr['ubTrades']}; "
              f"attempts: {ctr.get('cycle_attempt', 0)})  "
              f"whale snaps: {ctr['directTrades']}  "
              f"all EOAs verified: {verified}")
        # Honest holder return: realized dollar-day-weighted APR over completed
        # round-trips, plus the arb throughput + implied basket fee that bounds
        # it (gross BUCK volume crossing the pools, USD-valued).
        rt = ctr.get("dmRoundTrips", 0)
        print(f"[sim] DM realized return: {100*realized_apr:+.2f}% APR  "
              f"({rt} round-trips, ${profit_usd/E6:,.0f} profit on "
              f"${ctr.get('dmDollarDays',0)/E6:,.0f} dollar-days)")
        print(f"[sim] arb throughput: ${cycle_vol/E6:,.0f} routed  "
              f"=> basket fee ~${fee_take/E6:,.0f}")
        if ctr.get("cycle_err"):
            print(f"[sim] last cycle exec error: {ctr['cycle_err']}")
    return summary
