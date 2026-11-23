"""Measure subsequent relative price changes for executed signal-increased orders."""

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean


def increased_orders(frames):
    """Each observation is a TOKEN, side and day; repeated wheel turns count once."""
    orders = set()
    for frame in frames:
        decisions = {}
        for event in sorted(frame["decisions"] + frame["trades"], key=lambda e: (e["tx"], e["index"])):
            key = event["tx"], event["token"]
            if "gap" in event:
                decisions[key] = event
                continue
            decision = decisions.get(key)
            if decision is None or decision["tick"] != event["tick"]:
                raise ValueError("execution lacks a preceding decision in its transaction")
            side = event["side"].capitalize()
            if (decision["planned" + side] > decision["planWithoutPair" + side]
                    and event["amount"] > 0):
                orders.add((frame["day"], event["token"], event["side"]))
    return sorted(orders)


def timing(capture, horizons):
    frames = capture["frames"]
    prices = [f["prices"] for f in frames]
    n = len(capture["tokens"])
    if n < 2 or any(f["day"] != i or len(f["prices"]) != n for i, f in enumerate(frames)):
        raise ValueError("incomplete daily TOKEN prices")
    by_side = defaultdict(list)
    for day, token, side in increased_orders(frames):
        by_side[side].append((day, token))
    for side in ("buy", "sell"):
        orders = by_side[side]
        for horizon in horizons:
            aligned = []
            for day, token in orders:
                if day + horizon >= len(prices):
                    continue
                growth = [math.log(b / a) for a, b in zip(prices[day], prices[day + horizon])]
                other = mean(g for j, g in enumerate(growth) if j != token)
                relative = growth[token] - other
                aligned.append(relative if side == "buy" else -relative)
            favorable = sum(value > 1e-12 for value in aligned)
            flat = sum(abs(value) <= 1e-12 for value in aligned)
            yield [side, horizon, len(orders), len(aligned), favorable, flat,
                   round(mean(aligned) * 10000, 3) if aligned else ""]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--horizon", action="append", type=int, help="Forward days, defaults to 1, 3 and 7")
    args = parser.parse_args()
    horizons = sorted(set(args.horizon or [1, 3, 7]))
    if min(horizons) < 1:
        parser.error("horizons must be positive")
    manifest = json.loads((args.campaign / "manifest.json").read_text())
    rows = []
    for cell in manifest["cells"]:
        if cell["status"] != "complete":
            continue
        data = (args.campaign / cell["output"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != cell["output_sha256"]:
            raise ValueError(f"checksum mismatch for {cell['id']}")
        for row in timing(json.loads(data), horizons):
            rows.append([cell["id"], cell["price_digest"], *row])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as output:
        csv_out = csv.writer(output)
        csv_out.writerow(["cell", "price_digest", "side", "forward_days", "token_side_days",
                          "with_forward_prices", "favorable", "flat", "mean_signed_relative_log_bp"])
        csv_out.writerows(rows)


if __name__ == "__main__":
    main()
