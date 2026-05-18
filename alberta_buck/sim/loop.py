"""Timeline orchestration: deploy, register agents, run days x ticks."""

from __future__ import annotations

import random

from alberta_buck.sim import identity as idmod
from alberta_buck.sim.agents import REGISTRY, MarketMakerWhale
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.deploy import deploy
from alberta_buck.sim.snapshot import Snapshotter


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
    ctr = {"directTrades": 0, "cycleTrades": 0}

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
        snap.capture(day, ctr, agents, init_val)
        if verbose and (day % 20 == 0 or day == scenario.days - 1):
            f = snap.frames[-1]
            errs = [abs(f["spotUsdc"][i] - f["refUsd"][i]) / max(1, f["refUsd"][i])
                    for i in range(len(d.tokens))]
            print(f"[sim] day {day:4d}  meanTrackErr={100*sum(errs)/len(errs):.2f}%"
                  f"  cycle={ctr['cycleTrades']} direct={ctr['directTrades']}")

    path = snap.write(out_path)

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
        "direct_trades": ctr["directTrades"],
        "all_eoa_verified": verified,
        "n_agents": len(agents),
    }
    if verbose:
        print(f"[sim] wrote {path}")
        for i, t in enumerate(summary["tokens"]):
            print(f"[sim]   {t:5s} TOKEN/USDC tail tracking err {100*track[i]:.2f}%")
        print(f"[sim] BUCK-routed trades: {ctr['cycleTrades']}  "
              f"(attempts: {ctr.get('cycle_attempt', 0)})  "
              f"whale snaps: {ctr['directTrades']}  "
              f"all EOAs verified: {verified}")
        if ctr.get("cycle_err"):
            print(f"[sim] last cycle exec error: {ctr['cycle_err']}")
    return summary
