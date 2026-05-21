"""Timeline orchestration: deploy, register agents, run days x ticks."""

from __future__ import annotations

import random

from alberta_buck.sim import identity as idmod
from alberta_buck.sim.agents import REGISTRY, MarketMakerWhale
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.deploy import deploy, REDEEMED_TOPIC
import alberta_buck.sim.rebalancer  # noqa: F401  triggers @_register
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
              f"({scenario.days}d x {scenario.ticks_per_day} ticks)...")
    d = deploy(chain, anvil, scenario, rng)

    # --- build + register the agent population ----------------------- #
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

    snap = Snapshotter(d, scenario)
    init_val = snap.agg_value(agents, 0)
    # Rebalancer initial capital (day-0 prices) for separate P&L tracking.
    reb_init = snap._agent_value(agents, 0, "BuckBasketRebalancerAgent")
    ctr = {"directTrades": 0, "cycleTrades": 0, "ubTrades": 0}

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
        snap.capture(day, ctr, agents, init_val, reb_init)
        if verbose and (day % 20 == 0 or day == scenario.days - 1):
            f = snap.frames[-1]
            errs = [abs(f["spotUsdc"][i] - f["refUsd"][i]) / max(1, f["refUsd"][i])
                    for i in range(len(d.tokens))]
            print(f"[sim] day {day:4d}  meanTrackErr={100*sum(errs)/len(errs):.2f}%"
                  f"  cycle={ctr['cycleTrades']} direct={ctr['directTrades']}")

    path = snap.write(out_path)

    # --- teardown: redeem TOKEN/BUCK pools + pool ROI report ----------- #
    if verbose:
        from eth_abi import decode as eth_abi_decode

        print("\n[teardown] ======== Pool Teardown ========")

        # Capture LP group state BEFORE redeeming TOKEN/BUCK pools
        # (redemption collects fees, zeroing the buck group position).
        lg_pre = snap._lp_groups()
        cap_pre = dict(snap._lp_cap) if snap._lp_cap else {}

        # -- TOKEN/BUCK pools: redeem each via BuckBasket ------------ #
        for i, tc in enumerate(d.tokens):
            sym = scenario.tokens[i][0]
            rid = d.pool_receipts.get(i)
            pb = d.pool_buck[i]
            if rid is None:
                print(f"\n[teardown] {sym}/BUCK pool {pb[:10]}...  "
                      f"NO receipt ID — cannot redeem")
                continue

            # Pre-redeem pool state.
            tok_pre = tc.functions.balanceOf(pb).call()
            buck_pre = d.buck.functions.balanceOf(pb).call()
            implied_pre = (buck_pre * (10 ** d.dec[i]) // tok_pre
                           if tok_pre else 0)
            # Initial deposit details.
            dep = d.basket.functions.deposits(rid).call()
            principal_tok = dep[1]
            principal_buck = dep[2]

            print(f"\n[teardown] {sym}/BUCK pool {pb[:10]}...  "
                  f"receiptId={rid}")
            print(f"           pre-redeem: {tok_pre/(10**d.dec[i]):,.6g} {sym}  "
                  f"{buck_pre/E18:,.2f} BUCK  "
                  f"implied {implied_pre/E18:,.2f} BUCK/{sym}")
            print(f"           initial deposit: "
                  f"{principal_tok/(10**d.dec[i]):,.6g} {sym}  "
                  f"{principal_buck/E18:,.2f} BUCK")

            try:
                rcpt = chain.send(d.basket.functions.redeem(rid, 0))
                for log in rcpt["logs"]:
                    if log["topics"][0] == REDEEMED_TOPIC:
                        toUserT, halfProfitT, burnedB, halfProfitB, _liq = \
                            eth_abi_decode(
                                ["uint256", "uint256", "uint256",
                                 "uint256", "uint128"],
                                log["data"])
                        user_tok = toUserT + halfProfitT
                        profit_tok = user_tok - principal_tok if user_tok > principal_tok else 0
                        total_profit_tok = profit_tok  # token-side profit
                        # halfProfitB is user's BUCK profit; 2x is total BUCK profit
                        total_profit_buck = 2 * halfProfitB
                        print(f"           redeemed:")
                        print(f"             token to user: "
                              f"{user_tok/(10**d.dec[i]):,.6g} {sym}  "
                              f"(principal {toUserT/(10**d.dec[i]):,.6g}"
                              f" + profit {halfProfitT/(10**d.dec[i]):,.6g})")
                        print(f"             BUCK profit to user: "
                              f"{halfProfitB/E18:,.2f} BUCK")
                        print(f"             BUCK burned: {burnedB/E18:,.2f}")
                        print(f"             treasury retained: "
                              f"{halfProfitT/(10**d.dec[i]):,.6g} {sym}")
                        roi_tok = (100 * profit_tok / principal_tok
                                   if principal_tok else 0)
                        print(f"             token profit: "
                              f"{profit_tok/(10**d.dec[i]):,.6g} {sym}  "
                              f"ROI {roi_tok:+.3f}%")
                        break
                else:
                    print(f"           WARNING: no Redeemed event found")
            except Exception as e:
                print(f"           redeem FAILED: {e}")

            # Post-redeem pool state.
            tok_post = tc.functions.balanceOf(pb).call()
            buck_post = d.buck.functions.balanceOf(pb).call()
            implied_post = (buck_post * (10 ** d.dec[i]) // tok_post
                            if tok_post else 0)
            print(f"           post-redeem: {tok_post/(10**d.dec[i]):,.6g} {sym}  "
                  f"{buck_post/E18:,.2f} BUCK  "
                  f"implied {implied_post/E18:,.2f} BUCK/{sym}")

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
