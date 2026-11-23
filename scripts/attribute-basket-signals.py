"""Attribute traced basket plans and same-transaction executions to discretionary pair work."""

import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
from urllib.parse import quote


SIDES = ("buy", "sell")
CLASSES = ("current", "banked", "indirect")


def load_capture(campaign, manifest, cell):
    path = (campaign / cell["output"]).resolve()
    if not path.is_relative_to(campaign):
        raise ValueError(f"capture path outside campaign: {cell['id']}")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != cell["output_sha256"]:
        raise ValueError(f"capture checksum mismatch: {cell['id']}")
    obj = json.loads(raw)
    tokens, frames = obj["tokens"], obj["frames"]
    if (tokens != manifest["tokens"] or len(frames) != manifest["days"]
            or [f["day"] for f in frames] != list(range(manifest["days"]))):
        raise ValueError(f"capture horizon or TOKENs disagree: {cell['id']}")
    prices = [list(series) for series in zip(*(f["prices"] for f in frames), strict=True)]
    encoded = json.dumps([tokens, prices], separators=(",", ":")).encode()
    if hashlib.sha256(encoded).hexdigest() != cell["price_digest"]:
        raise ValueError(f"price checksum mismatch: {cell['id']}")
    if not any(f["decisions"] for f in frames):
        raise ValueError(f"capture contains no decision telemetry: {cell['id']}")
    return frames


def classify(frames):
    counts = Counter()
    for frame in frames:
        decisions, increases, fills = {}, {}, set()
        for decision in frame["decisions"]:
            key = (decision["tx"], decision["token"])
            if key in decisions:
                raise ValueError(f"duplicate decision {key}")
            decisions[key] = decision
            ask = decision["pairAmount"]
            rate = decision["pairRate"]
            if not math.isfinite(ask) or not math.isfinite(rate) or min(ask, rate) < 0:
                raise ValueError(f"invalid pair evidence {key}")
            if ask > 0 and (not decision["eligible"] or decision["leash"]):
                raise ValueError(f"discretionary request outside gate {key}")
            counts["decisions"] += 1
            counts["eligible"] += bool(decision["eligible"])
            counts["asks"] += ask > 0
            counts["banked_asks"] += ask > 0 and rate == 0

            ask_side = "sell" if decision["gap"] > 0 else "buy"
            changed = increased_directly = False
            for side in SIDES:
                suffix = side.capitalize()
                plan, control = decision["planned" + suffix], decision["planWithoutPair" + suffix]
                if not math.isfinite(plan) or not math.isfinite(control) or min(plan, control) < 0:
                    raise ValueError(f"invalid constrained plan {key}")
                changed |= plan != control
                if plan <= control:
                    counts["decreased"] += plan < control
                    continue
                direct = ask > 0 and side == ask_side
                increased_directly |= direct
                kind = "indirect" if not direct else "current" if rate > 0 else "banked"
                counts[kind + "_plans"] += 1
                increases[(*key, side)] = (kind, control)
            counts["changed"] += changed
            if ask > 0 and not increased_directly:
                # These are diagnostics of overlap, not claims about which
                # individual downstream limit bound the execution.
                bucket = "covered_by_book" if ask <= decision["bookAmount"] else "downstream"
                counts[bucket] += 1

        for trade in frame["trades"]:
            key = (trade["tx"], trade["token"])
            decision = decisions.get(key)
            side, amount = trade["side"], trade["amount"]
            if (decision is None or decision["tick"] != trade["tick"]
                    or decision["index"] >= trade["index"] or side not in SIDES
                    or not math.isfinite(amount) or amount < 0):
                raise ValueError(f"trade lacks valid preceding same-transaction decision {key}")
            event = (*key, side)
            if event in fills:
                raise ValueError(f"duplicate execution {event}")
            fills.add(event)
            if amount > 0 and event in increases:
                kind, control = increases[event]
                counts[kind + "_fills"] += 1
                counts["above_control"] += amount > control
    return counts


def returns(frames, start, end, field):
    opening = Decimal(str(frames[start][field]))
    closing = Decimal(str(frames[end][field]))
    if min(opening, closing) <= 0:
        raise ValueError(f"{field}: positive opening and closing marks required")
    return (closing / opening - 1) * 100


def table(rows):
    lines = [
        "| Cell | Decisions | Eligible | Asks (banked) | Changed (smaller legs) | Increases C / B / I | Same-tx fills C / B / I | Above no-pair amount | Book overlap | Later filters | Share | Passive |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, counts, share, hold in rows:
        plan = " / ".join(str(counts[k + "_plans"]) for k in CLASSES)
        fills = " / ".join(str(counts[k + "_fills"]) for k in CLASSES)
        lines.append(f"| `{name}` | {counts['decisions']} | {counts['eligible']} | "
                     f"{counts['asks']} ({counts['banked_asks']}) | {counts['changed']} ({counts['decreased']}) | {plan} | {fills} | "
                     f"{counts['above_control']} | {counts['covered_by_book']} | {counts['downstream']} | "
                     f"{share:+.6f}% | {hold:+.6f}% |")
    return lines


def render(campaign, manifest, captures, markout):
    end = manifest["days"] - 1
    common = (manifest.get("price_calendar") or {}).get("last_common_anchor_day")
    periods = [(0, end, 0)]
    if isinstance(common, int) and 0 < common < end:
        periods.extend([(0, common, 0), (common + 1, end, common)])
    lines = [
        f"# Discretionary basket decisions · {campaign.name}", "",
        "Each decision is one TOKEN in one response transaction. Eligibility is the captured mask. An ask is a "
        "positive raw pair amount before caps. Zero-rate asks can spend previously accrued allowance, including "
        "allowance from book sizing. They are counted once and reported separately from current signal asks.", "",
        "Changed counts TOKEN decisions whose constrained buy or sell differs from the no-pair plan on the same book. "
        "Increases and fills count individual sides. C is a current-rate ask on that side, B a zero-rate banked ask "
        "on that side, and I an increase without an own ask on that side, such as released room for a different TOKEN. "
        "Decreases can also change a plan. Book-only orders identical to the no-pair plan cannot enter the fill counts.", "",
        "A fill requires a positive Responded amount on the increased side after its decision in the same transaction "
        "and tick. Above no-pair amount is a stronger comparison with the baseline planned amount. The logged sale "
        "amount is the marked plan on successful execution. These counts do not measure executed incremental cash "
        "or replicate a separate no-pair world.", "",
        "Book overlap counts asks without a direct increase whose raw amount was no larger than concurrent planned "
        "book movement. Later filters counts the remaining unconverted asks. The trace alone cannot isolate the "
        "individual cap, dust threshold or credit interaction.", "",
        "Returns use the stated close interval. Share is the BUCK TWAP mark before holder exit costs. The passive "
        "TOKEN index uses each world's LOW marks with initial declared weights, so it can differ between matched "
        "arms even when external prices agree. Compare paired share returns to measure the observed policy difference.",
    ]
    missing = len(manifest["cells"]) - len(captures)
    if missing:
        lines.extend(["", f"Omitted {missing} incomplete or failed cells."])
    if markout:
        lines.extend(["", f"[Forward relative-price markouts]({markout}). These use subsequent reference-price "
                      "changes for unique TOKEN/side/day observations, before costs. Bridges are generated prices."])
    for first, last, baseline in periods:
        lines.extend(["", f"## Decisions on days {first}–{last}, returns from close {baseline} to {last}", ""])
        rows = [(cell["id"], classify(frames[first:last + 1]), returns(frames, baseline, last, "share"),
                 returns(frames, baseline, last, "hold")) for cell, frames in captures]
        lines.extend(table(rows))
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markout", type=Path, help="Link an existing forward-price markout CSV")
    args = parser.parse_args()
    campaign = args.campaign.resolve()
    manifest = json.loads((campaign / "manifest.json").read_bytes())
    captures = [(c, load_capture(campaign, manifest, c)) for c in manifest["cells"] if c["status"] == "complete"]
    if not captures:
        parser.error("no completed captures")
    markout = None
    if args.markout:
        if not args.markout.is_file():
            parser.error("markout file does not exist")
        markout = quote(os.path.relpath(args.markout, args.output.parent))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(campaign, manifest, captures, markout))
    print(args.output)


if __name__ == "__main__":
    main()
