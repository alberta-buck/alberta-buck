"""Timeline orchestration: deploy, register agents, run days x ticks."""

from __future__ import annotations

import random

from alberta_buck.sim import identity as idmod
from alberta_buck.sim.agents import REGISTRY, MarketMakerWhale
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.deploy import deploy, REDEEMED_TOPIC
import alberta_buck.sim.rebalancer  # noqa: F401  triggers @_register
import alberta_buck.sim.direct_mint  # noqa: F401  triggers @_register
from alberta_buck.sim.direct_mint import BootstrapDMAgent, DirectMintAgent
from alberta_buck.sim.snapshot import Snapshotter

E6 = 10 ** 6
E18 = 10 ** 18


def run(scenario, anvil, out_path=None, verbose=True) -> dict:
    w3 = anvil.w3
    chain = Chain(w3, w3.eth.accounts[0])
    rng = idmod.seeded_rng(scenario.seed)
    prng = random.Random(scenario.seed)

    if verbose:
        print(f"[sim] deploying '{scenario.name}' "
              f"({scenario.days}d x {scenario.ticks_per_day} ticks)...",
              flush=True)
    d = deploy(chain, anvil, scenario, rng)

    # --- build + register the agent population ----------------------- #
    # Reset per-class counters defensively so back-to-back sim runs in
    # the same process don't accumulate stale seq numbers (the
    # bootstrap-token assignment depends on `seq < N`).
    BootstrapDMAgent._counter = 0
    DirectMintAgent._counter = 0
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
        print(f"[sim] registered {len(agents)} EOA identities "
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

    snap = Snapshotter(d, scenario)
    # Capital baselines: computed AFTER bootstrap so the dm baseline
    # includes the bootstrap deposits (otherwise day-0 P&L would jump
    # by the bootstrap principal).
    init_val = snap.agg_value(agents, 0)
    reb_init = snap._agent_value(agents, 0, "BuckBasketRebalancerAgent")
    dm_init = snap._agent_value(agents, 0, "DirectMintAgent")

    ts = w3.eth.get_block("latest")["timestamp"] + 10
    tick_secs = max(60, 86_400 // scenario.ticks_per_day)

    n_tok = len(d.tokens)
    for day in range(scenario.days):
        # One market maker snaps ONE random token at one random tick/day.
        whale_tick = prng.randrange(scenario.ticks_per_day)
        whale_tok = prng.randrange(n_tok)
        for tick in range(scenario.ticks_per_day):
            ts += tick_secs
            anvil.warp_to(ts)
            if tick == whale_tick:
                for wagent in whales:
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

        # Pre-teardown state.
        outstanding_pre = ctr.get("dmOutstandingBuck", 0)
        treasury_pre = ctr.get("treasuryBuck", 0)
        nav_pre = snap._basket_nav()
        print(f"[teardown] pre-state:  outstanding {outstanding_pre/E18:,.2f}  "
              f"treasury {treasury_pre/E18:,.2f}  "
              f"nav {nav_pre/E18:,.2f} BUCK")

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
        print(f"\n[teardown] post-state: outstanding {outstanding_post/E18:,.2f}  "
              f"treasury {treasury_post/E18:,.2f}  "
              f"nav {nav_post/E18:,.2f} BUCK")
        treasury_delta = treasury_post - treasury_pre
        if treasury_delta != 0:
            print(f"[teardown] teardown released "
                  f"{treasury_delta/E18:,.2f} BUCK to treasury")

        # End-of-sim summary: entries, exits, treasury share of remaining NAV.
        print(f"\n[teardown] Lifetime DM activity:")
        print(f"  entries:           {ctr.get('dmEntries', 0)}")
        print(f"  exits:             {ctr.get('dmExits', 0)}")
        print(f"  exit failures:     {ctr.get('dmExitFails', 0)}")
        print(f"  outstanding BUCK:  {outstanding_post/E18:,.2f}")
        print(f"  treasury BUCK:     {treasury_post/E18:,.2f}  (cumulative)")
        if nav_post > 0:
            ts_pct = 100 * (nav_post - outstanding_post) / nav_post
            print(f"  treasury share:    {ts_pct:.2f}% of remaining NAV "
                  f"({nav_post/E18:,.2f} BUCK)")
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
    summary = {
        "json": str(path),
        "days": len(snap.frames),
        "tokens": [t[0] for t in scenario.tokens],
        "track_err": track,
        "cycle_trades": ctr["cycleTrades"],
        "ub_trades": ctr["ubTrades"],
        "direct_trades": ctr["directTrades"],
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
        if ctr.get("cycle_err"):
            print(f"[sim] last cycle exec error: {ctr['cycle_err']}")
    return summary
