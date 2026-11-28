from types import SimpleNamespace

from web3 import Web3

from alberta_buck.sim import runrecord
from alberta_buck.sim.wheel_revenue import WheelRevenue


def test_direct_revenue_uses_confirmed_addresses_and_does_not_count_cycle_fees_twice(tmp_path):
    basket, wheel, buck, keeper = ["0x" + f"{i:02x}" * 20 for i in range(1, 5)]

    def contract(address, *names):
        return SimpleNamespace(address=address, events=SimpleNamespace(**{
            name: SimpleNamespace(signature=f"{name}()", process_log=lambda log: {"args": log["args"]})
            for name in names
        }))

    world = SimpleNamespace(
        w3=Web3, dec=[18],
        basket=contract(basket, "BasketArbitrage", "PriorArbFeesCollected", "WheelFeesCollected"),
        wheel=contract(wheel, "Funded", "Ticked"),
        buck=contract(buck, "NetValueTransfer"),
    )
    ledger = WheelRevenue(world)

    def log(address, name, **args):
        topics = [Web3.keccak(text=f"{name}()")]
        if name in ("NetValueTransfer",):
            topics += [bytes.fromhex(args[a][2:]).rjust(32, b"\0") for a in ("from", "to")]
        return dict(address=address, topics=topics, args=args)

    q = 10**36
    logs = [
        log(keeper, "BasketArbitrage"),  # identical topic from an unrelated address
        log(basket, "PriorArbFeesCollected", route=2, fees=4),
        log(basket, "BasketArbitrage", route=2, direction=1, input=100, cashReturn=-5,
            lpReturn=19, lpFees=12, totalReturn=14),
        log(basket, "WheelFeesCollected", pool=0, buckNet=3, tokenAmount=10**18, tokenMarkBuck=4),
        log(buck, "NetValueTransfer", **{"from": wheel, "to": keeper, "nominal": 8, "netScaled": 7*q + 2}),
        log(wheel, "Ticked", reservePay=8, caller=keeper, work=2),
        log(buck, "NetValueTransfer", **{"from": keeper, "to": wheel, "nominal": 30, "netScaled": 29*q}),
        log(wheel, "Funded", **{"from": keeper, "taken": 30, "refused": 10}),
    ]
    for index, event in enumerate(logs):
        event["logIndex"] = index
    receipt = dict(status=0, logs=logs, blockNumber=17, transactionHash=bytes(32))
    ledger.book(receipt)
    ledger.book({**receipt, "status": 1})
    totals, rows = ledger.close_day()
    assert totals["basket_total"] == 25
    assert totals["arb_lp_fees"] == 12
    assert totals["keeper_net_scaled"] == 7*q + 2
    assert totals["reserve_net_scaled"] == 29*q
    assert [(r["kind"], r["to"], r["log_index"]) for r in rows] == [
        ("prior_arb_fees", basket, 1), ("arb", basket, 2), ("pool_fees", basket, 3),
        ("keeper_payout", keeper, 5), ("reserve_funding", wheel, 7),
    ]
    assert ledger.close_day()[1] == []
    recorder = runrecord.RunRecorder(tmp_path, run_id="revenue").open()
    recorder.frame(dict(day=0, wheel_revenue=totals, wheel_operations=rows))
    recorder.close()
    values, _ = runrecord.read_lines(recorder.dir / "wheel.jsonl")
    assert values[3]["net_scaled"] == str(7*q + 2)
    assert all(v["day"] == 0 for v in values)
    frames, _ = runrecord.read_lines(recorder.dir / "frames.jsonl")
    assert frames[0]["wheel_revenue"]["basket_total"] == 25
