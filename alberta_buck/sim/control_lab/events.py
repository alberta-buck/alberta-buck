"""Executed operations from receipts, separate from sensor advice and LP work."""
from __future__ import annotations


class ExecutionRecorder:
    def __init__(self, world):
        from eth_utils import event_abi_to_log_topic
        from alberta_buck.sim.chain import load_artifact
        self.world = world
        self.decoders = {}
        for contract, event in [("UniswapV3Pool", "Swap"), ("EquityDesk", "MonetaryOperation"),
                                ("BuckBasketEquityResponse", "Responded"), ("BasketMachineWheel", "Cycled")]:
            abi, _ = load_artifact(contract)
            spec = next(a for a in abi if a.get("type") == "event" and a["name"] == event)
            self.decoders[bytes(event_abi_to_log_topic(spec))] = spec
        d = world.d
        self.actors = {d.desk.address.lower(): "desk", d.basket.address.lower(): "basket",
                       world.arb.address.lower(): "arb"}
        self.pools = {}
        for quote, addresses in [("BUCK", d.pool_buck), ("USDC", d.pool_usdc)]:
            for i, address in enumerate(addresses):
                self.pools[address.lower()] = (i, quote, world.pairs[address][0] == d.tokens[i].address)

    def capture(self, receipt, phase):
        from web3._utils.events import get_event_data
        w = self.world
        pending = {actor: [] for actor in self.actors.values()}
        operations = []
        for log in receipt["logs"]:
            spec = self.decoders.get(bytes(log["topics"][0])) if log["topics"] else None
            address = log["address"].lower()
            if spec is None or address not in self.pools and address not in self.actors:
                continue
            args = get_event_data(w.d.w3.codec, spec, log)["args"]
            if spec["name"] == "Swap":
                actor = self.actors.get(args["recipient"].lower())
                if actor is None or address not in self.pools:
                    continue
                token, quote, token0 = self.pools[address]
                dt, dq = (int(args["amount0"]), int(args["amount1"])) if token0 else (int(args["amount1"]), int(args["amount0"]))
                if dt == 0 or dq == 0:
                    continue
                assert dt * dq < 0, "swap token and quote amounts must have opposite signs"
                pending[actor].append(dict(token=token, quote=quote, side="buy" if dt < 0 else "sell",
                    token_amount=abs(dt) / 1e18, quote_amount=abs(dq) / 1e6,
                    token_raw=str(abs(dt)), quote_raw=str(abs(dq)),
                    price_quote=abs(dq / dt) * 1e12, log_index=int(log["logIndex"])))
                continue
            actor = self.actors[address]
            operation = dict(actor=actor, phase=phase,
                transaction=w.transactions - w.bootstrap_transactions,
                log_index=int(log["logIndex"]), trades=pending[actor])
            pending[actor] = []
            if spec["name"] == "Cycled":
                operation.update(action="cycle", token=int(args["k"]), direction=int(args["dir"]),
                                 input_buck=int(args["amountIn"]) / 1e6, profit_buck=int(args["profit"]) / 1e6)
            elif spec["name"] == "MonetaryOperation":
                quadrant = int(args["quadrant"])
                operation.update(action={1: "absorb", 2: "retire", 3: "supply", 4: "issue"}[quadrant],
                                 buck_moved=int(args["buckMoved"]) / 1e6)
            else:
                operation.update(action={0: "mark", 1: "sell", 2: "buy", 3: "transfer", 4: "pair"}[int(args["action"])],
                    sell_token=int(args["sell"]) if int(args["sell"]) < 3 else None,
                    buy_token=int(args["buy"]) if int(args["buy"]) < 3 else None,
                    proceeds_buck=int(args["proceeds"]) / 1e6, spent_buck=int(args["buySpent"]) / 1e6)
            operations.append(operation)
        assert not any(pending.values()), "executed swap missing its operation event"
        return operations


def execution_counts(frames):
    events = [e for f in frames for e in f.get("events", [])]
    counts = {"recorded": all("events" in f for f in frames),
              "arb_cycles": sum(e["actor"] == "arb" for e in events),
              "desk_ops": sum(e["actor"] == "desk" for e in events),
              "basket_responses": sum(e["actor"] == "basket" for e in events),
              "basket_pairs": sum(e["actor"] == "basket" and e["action"] == "pair" for e in events)}
    for actor in ("desk", "basket", "arb"):
        for side in ("buy", "sell"):
            counts[f"{actor}_{side}s"] = sum(t["side"] == side for e in events if e["actor"] == actor for t in e["trades"])
    return counts
