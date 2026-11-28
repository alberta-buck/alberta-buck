"""Book confirmed direct wheel revenue and account transfers from transaction logs."""

from __future__ import annotations


class WheelRevenue:
    def __init__(self, world):
        self.basket = world.basket.address
        self.wheel = world.wheel.address
        self.decimals = world.dec
        self.wheel_bytes = bytes.fromhex(self.wheel[2:])
        self.rows = []
        self.decoders = {}
        for contract, name in (
            (world.basket, "BasketArbitrage"),
            (world.basket, "PriorArbFeesCollected"),
            (world.basket, "WheelFeesCollected"),
            (world.wheel, "Ticked"),
            (world.wheel, "Funded"),
            (world.buck, "NetValueTransfer"),
        ):
            event = getattr(contract.events, name, None)
            if event is not None:
                key = (contract.address.lower(), world.w3.keccak(text=event.signature))
                self.decoders[key] = (name, event.process_log)

    def book(self, receipt):
        if receipt["status"] != 1:
            return
        transfers = {}
        for log in receipt["logs"]:
            topics = log["topics"]
            match = self.decoders.get((log["address"].lower(), topics[0])) if topics else None
            if match is None:
                continue
            name, decode = match
            if name == "NetValueTransfer" and all(topic[-20:] != self.wheel_bytes for topic in topics[1:]):
                continue
            a = decode(log)["args"]
            if name == "NetValueTransfer":
                key = (a["from"], a["to"], a["nominal"])
                transfers.setdefault(key, []).append(a["netScaled"])
                continue
            if name == "BasketArbitrage":
                row = dict(kind="arb", to=self.basket, route=a["route"], direction=a["direction"],
                           input_nominal=a["input"], cash=a["cashReturn"], lp=a["lpReturn"],
                           lp_fees=a["lpFees"], total=a["totalReturn"])
            elif name == "PriorArbFeesCollected":
                row = dict(kind="prior_arb_fees", to=self.basket, route=a["route"], total=a["fees"])
            elif name == "WheelFeesCollected":
                row = dict(kind="pool_fees", to=self.basket, pool=a["pool"], buck=a["buckNet"],
                           token_amount=a["tokenAmount"], token_decimals=self.decimals[a["pool"]],
                           token_mark=a["tokenMarkBuck"],
                           total=a["buckNet"] + a["tokenMarkBuck"])
            else:
                if name == "Ticked":
                    if not a["reservePay"]:
                        continue
                    source, to = self.wheel, a["caller"]
                    row = dict(kind="keeper_payout", nominal=a["reservePay"], work=a["work"])
                else:
                    source, to = a["from"], self.wheel
                    row = dict(kind="reserve_funding", nominal=a["taken"], refused=a["refused"])
                amounts = transfers.get((source, to, row["nominal"]), [])
                row.update(source=source, to=to, net_scaled=amounts.pop() if amounts else (
                    None if row["nominal"] else 0))
            self.rows.append(dict(**row, block=receipt["blockNumber"], tx="0x" + bytes(
                receipt["transactionHash"]).hex(), log_index=log["logIndex"]))

    def close_day(self):
        rows, self.rows = self.rows, []
        summary = dict(arb_cash=0, arb_lp=0, arb_lp_fees=0, arb_total=0, pool_fees=0,
                       prior_arb_fees=0, keeper_payout=0, keeper_net_scaled=0,
                       reserve_funding=0, reserve_net_scaled=0)
        for row in rows:
            kind = row["kind"]
            if kind == "arb":
                for field in ("cash", "lp", "lp_fees", "total"):
                    summary[f"arb_{field}"] += row[field]
            elif kind in ("pool_fees", "prior_arb_fees"):
                summary[kind] += row["total"]
            else:
                summary[kind] += row["nominal"]
                key = "keeper_net_scaled" if kind == "keeper_payout" else "reserve_net_scaled"
                if summary[key] is not None:
                    net = row["net_scaled"]
                    summary[key] = None if net is None else summary[key] + net
        summary["basket_total"] = summary["arb_total"] + summary["pool_fees"] + summary["prior_arb_fees"]
        return summary, rows
