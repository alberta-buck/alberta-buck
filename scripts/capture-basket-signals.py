"""Replay an economy with read-only daily pair observations and authenticated basket trades."""
import argparse
import itertools
import json
import math
from pathlib import Path

from alberta_buck.sim import experiment
from alberta_buck.sim import identity
from alberta_buck.sim.loop import run
from alberta_buck.sim.prices import Prices
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--experiment", default="alberta_buck/sim/experiments/economy.toml")
    parser.add_argument("--days", type=int, default=36)
    parser.add_argument("--set", dest="settings", action="append", default=[])
    parser.add_argument("--price-file", type=Path, action="append", default=[],
                        help="Close-price CSV, repeated once per TOKEN in scenario order")
    parser.add_argument("--code-size-limit", type=int, help="Local EVM limit for instrumented research facets")
    parser.add_argument("--identity-cache", type=Path, help="Isolated simulation registration cache")
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be positive")
    if args.code_size_limit is not None and args.code_size_limit <= 0:
        parser.error("--code-size-limit must be positive")
    if args.identity_cache is not None:
        identity._CACHE_PATH = args.identity_cache.resolve()
    exp = experiment.load(args.experiment, sets=args.settings)
    exp.scenario["days"] = args.days
    scenario = experiment.build(exp)
    if args.price_file:
        if len(args.price_file) != len(scenario.tokens):
            parser.error("provide one --price-file per scenario TOKEN")
        scenario.csv_files = [str(path.resolve()) for path in args.price_file]
        scenario.prices = Prices(scenario.csv_files)
        if scenario.prices.days < args.days or any(
                price <= 0 for series in scenario.prices.series for price in series):
            parser.error("--price-file series must contain enough positive daily prices")
        scenario.days = args.days
    rows = []
    trades = []
    decisions = []
    deployment = None
    rate_unit = 1e4 * math.log1p(0.0001) * 86400 / 1e18

    def start(day, d, agents, counters):
        nonlocal deployment
        if deployment is not None:
            return
        deployment = d
        send = d.chain.send
        event = d.basket.events.Responded()
        topic = d.w3.keccak(text=event.signature)
        tracing = any(e.get("name") == "SignalDecision" for e in d.equity_upkeep.abi)
        decision_event = d.equity_upkeep.events.SignalDecision() if tracing else None
        decision_topic = d.w3.keccak(text=decision_event.signature) if tracing else None

        def record(*args, **kwargs):
            receipt = send(*args, **kwargs)
            for log in receipt["logs"] if receipt else ():
                if log["address"] != d.basket.address or not log["topics"]:
                    continue
                sig = log["topics"][0]
                if sig != topic and sig != decision_topic:
                    continue
                position = dict(tick=d.chain.sim_tick, tx=receipt["transactionHash"].hex(),
                                index=log["logIndex"])
                if sig == decision_topic:
                    args = decision_event.process_log(log)["args"]
                    detail = dict(args["detail"])
                    for field in ("gap", "pairRate"):
                        detail[field] /= 1e18
                    detail["urgency"] /= 86400e18
                    for field in ("pairAmount", "bookAmount", "ceilingAmount", "buyStepCap", "sellStepCap",
                                  "depthCap", "grain", "plannedBuy", "plannedSell",
                                  "planWithoutPairBuy", "planWithoutPairSell"):
                        detail[field] /= 1e6
                    decisions.append(dict(token=args["token"], **position, **detail))
                else:
                    values = event.process_log(log)["args"]
                    action = values["action"]
                    if action in (1, 2):
                        side = "sell" if action == 1 else "buy"
                        amount = values["proceeds" if action == 1 else "buySpent"] / 1e6
                        trades.append(dict(side=side, token=values[side], amount=amount, **position))
            return receipt

        d.chain.send = record

    def capture(frame):
        d = deployment
        signals, policy = d.pair_signals.functions, d.pair_policy.functions
        pairs = []
        for i, j in itertools.combinations(range(len(d.tokens)), 2):
            level, rate, low, high, _, _, _, mask = signals.pair(i, j).call()
            pairs.append(dict(tokens=[i, j], level=level / 1e18 * math.log1p(.0001),
                              rate=rate * rate_unit, low=low * rate_unit, high=high * rate_unit,
                              ready=mask, forward=policy.desire(i, j).call() / 1e18,
                              reverse=policy.desire(j, i).call() / 1e18))
        rows.append(dict(day=frame["day"], prices=frame["refUsd"], pairs=pairs, trades=trades.copy(),
                         decisions=decisions.copy(),
                         weights=frame["eq_response_w"], targets=frame["eq_lean"],
                         share=frame["eq_share_price"], hold=frame["eq_hold"],
                         sells=frame.get("wh_eq_sell_n", 0), buys=frame.get("wh_eq_buy_n", 0)))
        trades.clear()
        decisions.clear()

    backend_options = {} if args.code_size_limit is None else {"code_size_limit": args.code_size_limit}
    with PyrevmAnvil(**backend_options) as backend:
        run(scenario, backend, on_day_start=start, on_frame=capture)
    assert len(rows) == args.days
    for side in ("buy", "sell"):
        count = sum(t["side"] == side for r in rows for t in r["trades"])
        assert count == rows[-1][side + "s"], (side, count, rows[-1][side + "s"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dict(tokens=[token[0] for token in scenario.tokens], config=exp.resolved(),
                                           frames=rows), separators=(",", ":")))


if __name__ == "__main__":
    main()
