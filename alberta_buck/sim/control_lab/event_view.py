"""Presentation of executed TOKEN/BUCK fills; never infer trades from signals."""
from collections import defaultdict

from .events import execution_counts

MARKERS = {
    "desk-buy": ("Desk TOKEN buys", "#14783e", "up"),
    "desk-sell": ("Desk TOKEN sells", "#b52b40", "down"),
    "basket-buy": ("Basket TOKEN buys", "#098c9e", "up"),
    "basket-sell": ("Basket TOKEN sells", "#d46c17", "down"),
    "arb-buy": ("Arb TOKEN buys", "#7155b6", "up"),
    "arb-sell": ("Arb TOKEN sells", "#c24c99", "down"),
    "basket-pair": ("Basket LP pairings", "#647580", "diamond"),
}
MARKER_NOTE = ("Triangles mark executed TOKEN/BUCK buys (up) and sells (down), grouped by token, side and observation. "
               "Their height is the volume-weighted fill price; USD conversion uses end-of-observation BUCK/USDC. "
               "Grey diamonds mark LP pairing at the observed spot, not a swap. Hover a marker or inspect the execution log. "
               "Arbitrage markers show the TOKEN/BUCK leg; the opposite TOKEN/USDC leg is recorded in the log.")


def price_markers(result, native=False):
    groups = {key: dict(id=key, label=label, color=color, shape=shape, points=[])
              for key, (label, color, shape) in MARKERS.items()}
    for index, frame in enumerate(result["frames"]):
        buckets = defaultdict(list)
        for operation in frame.get("events", []):
            for trade in operation["trades"]:
                if trade["quote"] == "BUCK":
                    buckets[operation["actor"] + "-" + trade["side"], trade["token"]].append(trade)
            if operation["actor"] == "basket" and operation["action"] in ("pair", "sell-pair"):
                token = operation["buy_token"]
                buckets["basket-pair", token].append(dict(token_amount=0, quote_amount=operation["spent_buck"]))
        for (kind, token), trades in buckets.items():
            quantity = sum(t["token_amount"] for t in trades)
            quote = sum(t["quote_amount"] for t in trades)
            price = quote / quantity if quantity else frame["token_buck"][token]
            value = price / result["config"]["feed"]["base"][token]
            if not native:
                value *= frame["buck_usdc"]
            groups[kind]["points"].append(dict(index=index, token=token, value=value, count=len(trades),
                                                token_amount=quantity, buck_amount=quote))
    return [group for group in groups.values() if group["points"]]


def execution_log(result):
    # Exact integer amounts remain in raw JSON. The viewer only needs human units.
    return [{"index": index, **{k: v for k, v in e.items() if k not in ("trades", "log_index")},
             "trades": [{k: v for k, v in t.items() if k not in ("token_raw", "quote_raw", "log_index")}
                        for t in e["trades"]]}
            for index, frame in enumerate(result["frames"]) for e in frame.get("events", [])]
