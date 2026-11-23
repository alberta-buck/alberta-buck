"""Compare dated household basket flows, marked ownership and price-driven weight drift."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from statistics import mean, median


RAW = 10**6
Q = 10**36
DEFAULT_WINDOWS = ((1, 30), (31, 364), (365, 729))


def jsonl(path):
    with path.open() as stream:
        for line in stream:
            yield json.loads(line)


def receipt_flows(path):
    """Confirmed household cash legs, excluding quotes, market legs and retries."""
    flows = defaultdict(Counter)
    recorded = {}
    for event in jsonl(path):
        operation = event.get("operation")
        if event.get("status") != "confirmed" or operation not in ("deposit", "redeem"):
            continue
        amount = event["amounts"]
        if amount.get("in_kind"):
            raise ValueError("an in-kind redemption needs an execution mark for its TOKENs")
        shares = int(amount["shares_added" if operation == "deposit" else "shares_retired"])
        tx = event["transaction_hash"]
        if not tx or shares <= 0:
            raise ValueError("confirmed receipt flow lacks transaction or share evidence")
        day = event["day"]
        value = int(amount["buck"])
        signature = (day, event["member"], operation, value, shares, amount["token_id"])
        if tx in recorded:
            if recorded[tx] != signature:
                raise ValueError(f"conflicting evidence for receipt flow {tx}")
            continue
        recorded[tx] = signature
        flows[day][operation] += value
    return flows


def holder_marks(path):
    marks = defaultdict(Counter)
    for row in jsonl(path):
        mark = row["basket_mark"]
        if mark is None and row["basket"]:
            raise ValueError(f"missing receipt mark on day {row['day']}")
        marks[row["day"]][row["role"]] += mark or 0
    return marks


def relative_price_drift(previous, current, exposure):
    """One-way amount needed to restore prior weights after external price moves."""
    a, b = previous["market"]["tokens"], current["market"]["tokens"]
    holdings = previous["basket"]["weights"]
    symbols = [row["symbol"] for row in a]
    if symbols != [row["symbol"] for row in b] or symbols != [row["symbol"] for row in holdings]:
        raise ValueError("TOKEN ordering changed between prices and weights")
    raw_weights = [row["held_bp"] for row in holdings]
    total = sum(raw_weights)
    if total == 0:
        raise ValueError(f"missing weights on day {previous['day']}")
    weights = [weight / total for weight in raw_weights]
    returns = [after["ref_usd"] / before["ref_usd"] for before, after in zip(a, b)]
    index = sum(weight * ratio for weight, ratio in zip(weights, returns))
    return exposure / 2 * sum(weight * abs(ratio - index) for weight, ratio in zip(weights, returns))


def read_world(record, vector_path):
    manifest = json.loads((record / "manifest.json").read_bytes())
    if manifest["status"] != "complete":
        raise ValueError(f"{record} is incomplete")
    days = list(jsonl(record / manifest["files"]["frames"]))
    path = vector_path or Path(manifest["outputs"]["vector"])
    vector = json.loads(path.read_bytes())["frames"]
    if len(days) != manifest["frames"] or len(days) != len(vector):
        raise ValueError("record, manifest and full vector have different lengths")
    marks = holder_marks(record / manifest["files"]["agents"])
    flows = receipt_flows(record / manifest["files"]["events"])
    if any(day < 0 or day >= len(days) for day in flows):
        raise ValueError("receipt flow outside completed observation window")
    archived_deposits = vector[-1].get("bcd_invested")
    if archived_deposits is not None and sum(row["deposit"] for row in flows.values()) != archived_deposits:
        raise ValueError("confirmed deposits fail to reconcile to the vector's deposit counter")
    samples = []
    for i, (day, row) in enumerate(zip(days, vector)):
        book = day["basket"]
        equity, share = book["equity"], row["eq_share_price"]
        if day["day"] != i or row["day"] != i or row["eq_equity"] != equity or equity <= 0 or share <= 0:
            raise ValueError(f"daily close {i} disagrees with vector or lacks a positive TWAP mark")
        household = day["households"]["basket_mark"]
        if household != sum(marks[i].values()):
            raise ValueError(f"household marks do not reconcile at day {i}")
        fund = row["net"]["jubilee"]["position"]
        if day["jubilee"]["position_mark"] != fund["mark"]:
            raise ValueError(f"fund mark does not reconcile at day {i}")
        exposure = book.get("token_exposure")
        if exposure is None:
            # TWAP gross = TWAP equity + lien - net cash - accrued relief.
            cash = int(row["net"]["carrying"]["basket"]["net_scaled"]) // Q
            exposure = equity + book["lien"] - cash - book["unpaid_relief"] - book["held_buck"]
        samples.append(dict(equity=equity, share=share, exposure=exposure, household=household,
                            investor=marks[i]["investor"], fund=fund["mark"], fund_basis=fund["basis"]))
    bootstrap = sum(position["mark"] for position in vector[-1].get("basket_positions", [])
                    if position["agent"].startswith("BootstrapDMAgent"))
    return manifest, days, samples, flows, bootstrap


def describe(record, vector_path, requested_windows):
    manifest, days, samples, flows, bootstrap = read_world(record, vector_path)
    last_day = len(days) - 1
    windows = requested_windows or DEFAULT_WINDOWS
    revision = manifest["revision"]
    state = " (dirty record)" if revision.get("dirty") else ""
    method = ("recorded LOW TOKEN exposure" if "token_exposure" in days[0]["basket"]
              else "TWAP gross less recorded LOW LP BUCK held, a mixed-mark exposure estimate")
    first = samples[0]
    out = [f"## {record.name} · {revision['commit']}{state}", "",
           f"Day 0 opens this analysis after bootstrap. Its closing equity is "
           f"{first['equity'] / RAW:,.2f} BUCK and its confirmed household deposits are "
           f"{flows[0]['deposit'] / RAW:,.2f} BUCK. The price-drift calculation uses {method}.",
           "",
           "| Days | Household gross median | Household gross mean | Household net mean | "
           "Price drift median | Price drift mean | Drift > gross days | Drift > gross on flow days | "
           "Abs. inferred net shares mean |",
           "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for lo, hi in windows:
        if not 1 <= lo <= hi <= last_day:
            raise ValueError(f"days {lo}–{hi} exceed the available prior-close window 1–{last_day}")
        gross, net, drift, shares = [], [], [], []
        for day in range(lo, hi + 1):
            prev, now = samples[day - 1], samples[day]
            opening = prev["equity"]
            deposit, redeem = flows[day]["deposit"], flows[day]["redeem"]
            gross.append((deposit + redeem) / opening)
            net.append((deposit - redeem) / opening)
            drift.append(relative_price_drift(days[day - 1], days[day], prev["exposure"]) / opening)
            shares.append(abs(now["equity"] * prev["share"] / (opening * now["share"]) - 1))
        above = sum(a > b for a, b in zip(drift, gross))
        flow_above = sum(a > b > 0 for a, b in zip(drift, gross))
        on_flow = sum(amount > 0 for amount in gross)
        out.append(f"| {lo}–{hi} | {median(gross):.4%} | {mean(gross):.4%} | {mean(net):+.4%} | "
                   f"{median(drift):.4%} | {mean(drift):.4%} | {above}/{len(drift)} | {flow_above}/{on_flow} | "
                   f"{mean(shares):.4%} |")
    out.extend(["", "| Day close | Basket equity, BUCK | Investor mark | Whole household mark | Jubilee mark |",
                "|---|---:|---:|---:|---:|"])
    for day in sorted({0, last_day, *(end for _, end in windows), *(lo for lo, _ in windows if lo > 1)}):
        row = samples[day]
        equity = row["equity"]
        ownership = [f"{row[key] / RAW:,.2f} ({row[key] / equity:.4%})"
                     for key in ("investor", "household", "fund")]
        out.append(f"| {day} | {equity / RAW:,.2f} | " + " | ".join(ownership) + " |")
    final = samples[-1]
    if bootstrap:
        residual = final["equity"] - final["household"] - final["fund"] - bootstrap
        out.extend(["", f"Final bootstrap receipts mark at {bootstrap / RAW:,.2f} BUCK. "
                    f"Unassigned equity after those, all households and Jubilee is {residual / RAW:,.2f} BUCK."])
    out.extend(["", f"Jubilee receipt cost basis changed from {first['fund_basis'] / RAW:,.2f} to "
                f"{final['fund_basis'] / RAW:,.2f} BUCK across day closes. The records contain no gross "
                "Jubilee investment and sale ledger."])
    return "\n".join(out)


def window(value):
    try:
        start, end = value.split(":")
        return int(start), int(end)
    except ValueError as error:
        raise argparse.ArgumentTypeError("window must be START:END") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=Path, nargs="+", help="Run record directory with manifest and JSONL logs")
    parser.add_argument("--vector", type=Path, action="append",
                        help="Relocated full vector, repeated in the same order as the records")
    parser.add_argument("--window", type=window, action="append", help="Inclusive simulated days, repeatable")
    parser.add_argument("--output", type=Path, help="Write Markdown to this path")
    args = parser.parse_args()
    paths = args.vector or [None] * len(args.records)
    if len(paths) != len(args.records):
        parser.error("provide one --vector per run or use the paths in the run manifests")
    intro = ("# Basket stock, receipt flows and price drift\n\n"
             "Flow is confirmed household BUCK credited on deposits or paid on redemptions, "
             "deduplicated by transaction. Gross adds both directions and net subtracts redemptions. "
             "Each day's flow and fixed-quantity price drift use the prior close's TWAP equity. "
             "Drift applies external USD TOKEN price ratios to the prior TOKEN weights and exposure "
             "and estimates the one-way notional required to restore those weights. "
             "Total shares are inferred from equity divided by TWAP share price at each close. "
             "The absolute change measures net shares relative to the previous close. "
             "Ownership percentages use same-close TWAP receipt and equity marks before exit costs.")
    try:
        sections = [describe(record, path, args.window) for record, path in zip(args.records, paths)]
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError) as error:
        parser.error(str(error))
    text = "\n\n".join([intro, *sections]) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
