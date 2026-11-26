"""Replay identical opening economies, then compare active and strict-maintenance upkeep.

The existing day-zero cash investors supply the opening sleeve using their
ordinary opening USD budgets and income. Run with OUTPUT.json --days 8
--warmup-days 3 --set scenario.prices=three-phase --record-root build/runs.
Opening lots are marked in BUCK before exit costs. Top-ups and sales within
one receipt leave the surviving original lots unidentifiable.
"""

import argparse
from functools import partial
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from alberta_buck.sim import experiment, identity
from alberta_buck.sim.chain import load_artifact
from alberta_buck.sim.loop import run
from alberta_buck.sim.prices import Prices
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
from alberta_buck.sim.runrecord import RunRecorder
from buck_core import mark_net_basket_positions


ROOT = Path(__file__).resolve().parents[1]
FACETS = {"active": "BasketUpkeepSignalTrace", "maintenance": "BasketUpkeepSignalTraceMaintenance"}
DEFAULT_EXPERIMENT = ROOT / "alberta_buck/sim/experiments/economy.toml"


def ownership_module():
    path = ROOT / "scripts/capture-basket-ownership.py"
    spec = importlib.util.spec_from_file_location("capture_basket_ownership", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_cohort(d, cohort):
    """Read identity, open receipts and current BUCK/USDC at one block."""
    block = d.w3.eth.block_number
    read = lambda fn: fn.call(block_identifier=block)
    members = {}
    for member, agent in cohort.items():
        owner = agent.address
        ids = sorted(agent._basket_receipts)
        mark = mark_net_basket_positions(d.chain, d.basket, d.buck, owner, ids, block_number=block)
        members[member] = dict(owner=owner, exited=agent.exited, cash_usdc_raw=read(d.usdc.functions.balanceOf(owner)),
                               cash_buck_raw=read(d.buck.functions.netBalanceOf(owner)),
                               positions={str(p.token_id): dict(shares=str(p.shares), basis_raw=p.basis, mark_raw=p.mark)
                                          for p in mark.positions})
    net_demurrage = read(d.buck.functions.fundFlows())[1]
    return dict(jubilee_net_demurrage_scaled=net_demurrage[0] * 10**36 + net_demurrage[1],
                jubilee_claim_seconds=read(d.buck.functions.claimSeconds()),
                block=block, share_price=str(read(d.basket.functions.sharePrice())),
                total_shares=str(read(d.basket.functions.totalShares())), equity_raw=read(d.basket.functions.equity()),
                total_issued_raw=read(d.buck.functions.totalIssued()), members=members)


def terminal_sleeve(opening, closing):
    """Counterfactual fixed-share mark and upper bound for opening shares still in the original receipts."""
    def mark(shares):
        total = int(closing["total_shares"])
        return shares * closing["equity_raw"] // total if total else 0

    by_member = {}
    for member, booked in opening["members"].items():
        current = closing["members"][member]
        original_mark = remaining_mark = remaining_shares = 0
        for receipt_id, lot in booked["positions"].items():
            held = current["positions"].get(receipt_id, {})
            opening_shares = int(lot["shares"])
            remaining = min(opening_shares, int(held.get("shares", 0)))
            original_mark += mark(opening_shares)
            remaining_mark += mark(remaining)
            remaining_shares += remaining
        by_member[member] = dict(counterfactual_hold_mark_raw=original_mark,
                                 remaining_opening_shares_upper=str(remaining_shares),
                                 remaining_opening_mark_upper_raw=remaining_mark, exited=current["exited"])
    return by_member


def install_treatment(d, arm, peer_window=None):
    # Both arms deploy the same two facets in the same order, then send one
    # governance transaction. Only the chosen facet's address differs.
    active = d.equity_upkeep.functions
    constructor = (active.pairPolicy().call(), active.parity().call(),
                   active.parityGain().call(), active.parityGainDear().call())
    facets = {}
    for name, contract in FACETS.items():
        args = constructor
        if name == "active" and peer_window is not None:
            contract = "BasketUpkeepPeerTrace"
            first, last = peer_window
            args = (*constructor, d.household_epoch + first * 86400, d.household_epoch + (last + 1) * 86400)
        abi, code = load_artifact(contract, libraries=d.upkeep_libraries)
        facets[name] = d.chain.deploy(contract, *args, abi=abi, bytecode=code)
    chosen = facets[arm]
    receipt = d.chain.send(d.basket.functions.setEquityWheel(chosen.address),
                           sender=d.basket.functions.governance().call())
    if d.basket.functions.equityWheel().call() != chosen.address:
        raise ValueError("governance switch failed")
    d.equity_upkeep = chosen
    return dict(deployed={name: f.address for name, f in facets.items()}, selected=chosen.address,
                block=int(receipt["blockNumber"]))


def decision_stage(decision, arm):
    detail = decision["detail"]
    if not detail["gap"]:
        return "no_gap"
    if not detail["depthCap"]:
        return "no_depth_for_swap"
    if detail["leash"]:
        return "safeguard"
    if arm == "maintenance":
        return "maintenance"
    if not detail["eligible"]:
        return "urgency_or_band"
    if not detail["pairRate"]:
        return "no_directional_rate"
    if not detail["pairAmount"]:
        return "no_released_amount"
    if (detail["plannedBuy"], detail["plannedSell"]) == (detail["planWithoutPairBuy"], detail["planWithoutPairSell"]):
        return "no_plan_change"
    side = "sell" if detail["gap"] > 0 else "buy"
    return ("changed_plan_with_swap" if any(f["side"] == side and f["swap"] for f in decision["fills"])
            else "changed_plan_without_swap")


def replay(arm, *, experiment_path, days, warmup_days, settings, price_files, on_day_start, on_frame, recorder,
           trace_days=None, trace_peers=False):
    exp = experiment.load(experiment_path, sets=settings)
    if exp.cfg.get("machine", {}).get("upkeep", "BasketUpkeepFacet") != "BasketUpkeepFacet":
        raise ValueError("the shared prefix requires ordinary BasketUpkeepFacet upkeep")
    exp.scenario["days"] = days
    scenario = experiment.build(exp)
    if price_files:
        if len(price_files) != len(scenario.tokens):
            raise ValueError("provide one price CSV per TOKEN in scenario order")
        scenario.csv_files = [str(Path(path).resolve()) for path in price_files]
        scenario.prices = Prices(scenario.csv_files)
        if scenario.prices.days < days or any(price <= 0 for column in scenario.prices.series for price in column):
            raise ValueError("price CSVs must contain enough positive daily prices")
        scenario.days = days
    if scenario.day_step != 1 or scenario.days != days or scenario.rng_mode != "keyed":
        raise ValueError("use a keyed daily economy with prices for the entire horizon")
    ownership_script = ownership_module()
    owners_start, owners_frame, ownership = ownership_script.ownership_recorder()
    cohort, daily, decisions, result = {}, [], [], {}
    deployment = None
    day_now = -1
    if trace_days is not None:
        result["decision_trace"] = []

    def start(day, d, agents, counters):
        nonlocal deployment, day_now
        deployment = d
        day_now = day
        owners_start(day, d, agents, counters)
        if day == 0:
            cohort.update({a.member: a for a in agents if getattr(a, "investor", False)
                           and a.arrive_day == 0 and not a.lifecycle})
            if not cohort:
                raise ValueError("no day-zero working cash investors")
        if on_day_start is not None:
            on_day_start(day, d, agents, counters)
        if day != warmup_days:
            return
        result["opening"] = read_cohort(d, cohort)
        if not any(m["positions"] for m in result["opening"]["members"].values()):
            raise ValueError("the treatment starts before any tracked investor obtained a receipt")
        result["switch"] = install_treatment(d, arm, trace_days if trace_peers else None)
        if recorder is not None:
            recorder.intervene(day, "set_upkeep_facet", {"arm": arm, **result["switch"]},
                              source="experiment", replay=False)
        decoder = d.equity_upkeep.events.SignalDecision()
        topic = d.w3.keccak(text=decoder.signature)
        send = d.chain.send
        if trace_days is not None:
            from alberta_buck.sim.chain import load_artifact
            pool_abi, _ = load_artifact("UniswapV3Pool")
            pools = {address: (i, d.w3.eth.contract(address=address, abi=pool_abi).events.Swap())
                     for i, address in enumerate(d.pool_buck)}
            responded = d.basket.events.Responded()
            work = d.basket.events.WheelWork()
            response_topic, work_topic = (d.w3.keccak(text=e.signature) for e in (responded, work))
            swap_topic = d.w3.keccak(text=next(iter(pools.values()))[1].signature)
            peer_event = d.equity_upkeep.events.SignalPeer() if trace_peers and arm == "active" else None
            peer_topic = d.w3.keccak(text=peer_event.signature) if peer_event is not None else None

        def record_trace(receipt):
            group, swaps, timestamp = [], {}, None

            def flush(response_log):
                nonlocal timestamp
                if group:
                    if timestamp is None:
                        timestamp = d.w3.eth.get_block(receipt["blockNumber"])["timestamp"]
                    for decision in group:
                        decision["stage"] = decision_stage(decision, arm)
                    result["decision_trace"].append(dict(day=day_now, tick=d.chain.sim_tick,
                        block=receipt["blockNumber"], timestamp=timestamp, tx=receipt["transactionHash"].hex(),
                        response_log=response_log, decisions=group))

            for log in receipt["logs"]:
                if not log["topics"]:
                    continue
                sig = log["topics"][0]
                if sig == swap_topic and log["address"] in pools:
                    token, swap = pools[log["address"]]
                    values = swap.process_log(log)["args"]
                    if values["sender"] == d.basket.address and values["recipient"] == d.basket.address:
                        swaps[token] = {"amount0": str(values["amount0"]), "amount1": str(values["amount1"]),
                                        "log": log["logIndex"]}
                if log["address"] != d.basket.address:
                    continue
                if sig == topic:
                    args = decoder.process_log(log)["args"]
                    group.append({"token": args["token"], "detail": dict(args["detail"]), "fills": []})
                elif sig == peer_topic:
                    peer = dict(peer_event.process_log(log)["args"])
                    for decision in group:
                        if decision["token"] == peer["constituent"]:
                            decision.setdefault("peers", []).append(peer)
                elif sig == response_topic:
                    values = responded.process_log(log)["args"]
                    if values["action"] in (1, 2):
                        side = "sell" if values["action"] == 1 else "buy"
                        token = values[side]
                        swap = swaps.pop(token, None)
                        for decision in group:
                            if decision["token"] == token:
                                decision["fills"].append({"side": side, "swap": swap,
                                                          "buck": values["proceeds"] if side == "sell"
                                                          else values["buySpent"]})
                elif sig == work_topic and work.process_log(log)["args"]["kind"] == 2 and group:
                    flush(log["logIndex"])
                    group, swaps = [], {}
            flush(None)

        def record(*args, **kwargs):
            receipt = send(*args, **kwargs)
            if receipt is not None and trace_days is not None and trace_days[0] <= day_now <= trace_days[1]:
                record_trace(receipt)
            for log in receipt["logs"] if receipt else ():
                if log["address"] != d.basket.address or not log["topics"] or log["topics"][0] != topic:
                    continue
                detail = decoder.process_log(log)["args"]["detail"]
                decisions.append((int(detail["pairAmount"]), (
                    detail["plannedBuy"] != detail["planWithoutPairBuy"]
                    or detail["plannedSell"] != detail["planWithoutPairSell"])))
            return receipt

        d.chain.send = record

    def capture(frame):
        owners_frame(frame)
        daily.append(dict(day=frame["day"], prices=frame["refUsd"], share=frame["eq_share_price"],
                          hold=frame["eq_hold"], total_issued=frame["net"]["total_issued"],
                          jubilee=frame["net"]["jubilee"],
                          decision_count=len(decisions), pair_asks=sum(amount > 0 for amount, _ in decisions),
                          pair_changes=sum(changed for _, changed in decisions)))
        if arm == "maintenance" and any(amount or changed for amount, changed in decisions):
            raise ValueError("strict maintenance requested a discretionary gap trade")
        decisions.clear()
        if frame["day"] + 1 == days:
            result["closing"] = read_cohort(deployment, cohort)
        if on_frame is not None:
            on_frame(arm, frame)

    previous_cache = identity._CACHE_PATH
    run_recorder, capture_frame = (ownership_script.dashboard_recorder(recorder, capture, ownership)
                                   if recorder is not None else (None, capture))
    try:
        with TemporaryDirectory(prefix="basket-patient-identity-") as cache, PyrevmAnvil(code_size_limit=65536) as backend:
            identity._CACHE_PATH = Path(cache) / "registry.json"
            run(scenario, backend, on_day_start=start, on_frame=capture_frame, recorder=run_recorder)
    finally:
        identity._CACHE_PATH = previous_cache

    if len(daily) != days or sum(row["decision_count"] for row in daily[warmup_days:]) == 0:
        raise ValueError("capture missed the requested horizon or post-switch decisions")
    if trace_days is not None:
        captured = {day: 0 for day in range(trace_days[0], trace_days[1] + 1)}
        for response in result["decision_trace"]:
            captured[response["day"]] += len(response["decisions"])
        if any(daily[day]["decision_count"] != count for day, count in captured.items()):
            raise ValueError("window trace disagrees with the daily decision counters")
    return {**result, "opening_lots_at_close": terminal_sleeve(result["opening"], result["closing"]),
            "daily": daily, "ownership": ownership, "config": exp.resolved(), "price_files": scenario.csv_files,
            "run_record": str(recorder.dir) if recorder is not None else None}


def run_pair(*, experiment_path=DEFAULT_EXPERIMENT, days, warmup_days, settings=(), price_files=(),
             on_day_start=None, on_frame=None, record_root=None, run_id=None, trace_days=None, trace_peers=False):
    """Run sequential arms. Hooks are called on the normal sim clock in each arm."""
    if not 1 <= warmup_days < days:
        raise ValueError("need at least one warmup day and one treatment day")
    if run_id is not None and record_root is None:
        raise ValueError("run_id requires record_root")
    if trace_days is not None and not warmup_days <= trace_days[0] <= trace_days[1] < days:
        raise ValueError("trace days must fall inside treatment")
    if trace_peers and trace_days is None:
        raise ValueError("peer traces require a decision trace window")
    run_arm = partial(replay, experiment_path=experiment_path, days=days, warmup_days=warmup_days,
                      settings=settings, price_files=price_files, on_day_start=on_day_start, on_frame=on_frame,
                      trace_days=trace_days, trace_peers=trace_peers)
    recorders = {}
    try:
        arms = {}
        for arm in FACETS:
            recorder = None
            if record_root is not None:
                arm_id = f"{run_id}-{arm}" if run_id else None
                recorder = recorders[arm] = RunRecorder(record_root, f"patient-{arm}", run_id=arm_id)
            arms[arm] = run_arm(arm, recorder=recorder)
        active, maintenance = arms.values()
        if (active["opening"] != maintenance["opening"]
                or active["daily"][:warmup_days] != maintenance["daily"][:warmup_days]
                or active["ownership"][:warmup_days] != maintenance["ownership"][:warmup_days]
                or active["switch"]["deployed"] != maintenance["switch"]["deployed"]
                or active["switch"]["block"] != maintenance["switch"]["block"]):
            raise ValueError("matched arms diverged before the upkeep switch")
        if [row["prices"] for row in active["daily"]] != [row["prices"] for row in maintenance["daily"]]:
            raise ValueError("matched arms used different external price tapes")
    except (Exception, KeyboardInterrupt) as error:
        for recorder in recorders.values():
            if recorder.manifest:
                recorder.close(status="failed", error=str(error))
        raise
    for arm, recorder in recorders.items():
        recorder.manifest["settings"] = dict(arm=arm, warmup_days=warmup_days, matched_prefix=True,
                                            switch=arms[arm]["switch"], price_files=arms[arm]["price_files"])
        recorder.close()
    return dict(schema_version=1, warmup_days=warmup_days, days=days, matched_prefix=True, arms=arms)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--experiment", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--days", type=int, default=36)
    parser.add_argument("--warmup-days", type=int, default=7)
    parser.add_argument("--set", dest="settings", action="append", default=[])
    parser.add_argument("--price-file", action="append", type=Path, default=[],
                        help="Daily close CSV, repeated once per TOKEN in scenario order")
    parser.add_argument("--record-root", type=Path, help="Directory of dashboard run records")
    parser.add_argument("--run-id", help="Base dashboard ID, suffixed with each arm")
    parser.add_argument("--trace-days", type=int, nargs=2, metavar=("FIRST", "LAST"),
                        help="capture decision-time plans and confirmed pool swaps for these treatment closes")
    parser.add_argument("--trace-peers", action="store_true", help="include ordered peer gates for eligible decisions")
    parser.add_argument("--opening-jubilee", type=int, default=0,
                        help="existing SimLP BUCK invested in the Jubilee receipt before day zero trades")
    args = parser.parse_args()
    if args.opening_jubilee < 0:
        parser.error("--opening-jubilee must be nonnegative")
    endowment = []

    def opening(day, deployment, _agents, _counters):
        if day == 0:
            from basket_jubilee_endowment import apply_opening_endowment
            endowment.append(apply_opening_endowment(deployment, args.opening_jubilee))

    data = run_pair(experiment_path=args.experiment, days=args.days, warmup_days=args.warmup_days,
                    settings=args.settings, price_files=args.price_file, record_root=args.record_root, run_id=args.run_id,
                    trace_days=args.trace_days, trace_peers=args.trace_peers,
                    on_day_start=opening if args.opening_jubilee else None)
    if args.opening_jubilee:
        if len(endowment) != 2 or endowment[0] != endowment[1]:
            raise ValueError("opening Jubilee funding differed between arms")
        data["opening_jubilee"] = endowment[0]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
