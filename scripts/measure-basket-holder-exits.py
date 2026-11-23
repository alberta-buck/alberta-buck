"""Reconcile completed scheduled investor exits from saved household event ledgers."""

import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import re
from statistics import median


RAW = 10**6
OPERATIONS = {"buy": "surplus", "deposit": "surplus", "redeem": "exit", "sell": "exit"}


def records(path):
    with path.open() as stream:
        for line in stream:
            yield json.loads(line)


def closed_exit(events, final, last_agent):
    before = next(event["accounts"] for event in events if "accounts" in event)
    if int(before["buck_scaled"]) or int(before["issued"]):
        raise ValueError("investor started with BUCK or issued principal")
    if last_agent["day"] < final["day"] or not last_agent["exited"] or int(last_agent["basket"]):
        raise ValueError("recorded exit left a basket receipt")
    by_tx = {}
    totals = Counter()
    receipts = defaultdict(Counter)
    for event in events:
        if event.get("status") != "confirmed" or event.get("operation") == "income":
            continue
        op = event.get("operation")
        if op not in OPERATIONS or event["kind"] != OPERATIONS[op]:
            raise ValueError(f"unsupported confirmed investor operation: {op}")
        if event["seq"] > final["seq"]:
            raise ValueError("investor operation after completed exit")
        tx = event["transaction_hash"]
        amounts = event["amounts"]
        if not tx or tx in by_tx and by_tx[tx] != (op, amounts):
            raise ValueError("missing or conflicting transaction evidence")
        if tx in by_tx:
            continue
        by_tx[tx] = op, amounts
        if op in ("buy", "sell"):
            totals[op] += int(amounts["usdc"])
        else:
            receipt = receipts[amounts["token_id"]]
            if op == "deposit":
                receipt["basis"] += int(amounts["buck"])
                receipt["added"] += int(amounts["shares_added"])
            else:
                if amounts["in_kind"]:
                    raise ValueError("in-kind withdrawal needs separate TOKEN conversion evidence")
                receipt["paid"] += int(amounts["buck"])
                receipt["retired"] += int(amounts["shares_retired"])
    if totals["sell"] != int(final["amounts"]["withdrawn_usdc"]):
        raise ValueError("confirmed BUCK sales disagree with the exit's USDC proceeds")
    if any(r["added"] <= 0 or r["added"] != r["retired"] for r in receipts.values()):
        raise ValueError("receipt share additions and retirements do not close")
    return dict(receipts=len(receipts), basis_buck_raw=sum(r["basis"] for r in receipts.values()),
                payout_buck_raw=sum(r["paid"] for r in receipts.values()), entry_usdc_raw=totals["buy"],
                exit_usdc_raw=totals["sell"], residual_buck_raw=int(last_agent["held_buck"]))


def exit_rows(campaign, runs=None):
    manifest = json.loads((campaign / "manifest.json").read_text())
    runs = runs or Path(manifest["run_root"])
    for cell in manifest["cells"]:
        if cell["status"] != "complete" or not cell["exit_fraction"]:
            continue
        for arm, run_id in cell["run_ids"].items():
            run = runs / run_id
            record = json.loads((run / "manifest.json").read_text())
            if record["status"] != "complete" or record["frames"] != cell["days"]:
                raise ValueError(f"{run_id}: incomplete run record")
            files = record["files"]
            scheduled = [event for event in records(run / files["interventions"])
                         if event.get("ok") and event.get("exit_now") == cell["exit_fraction"]
                         and event.get("applied_day") == cell["exit_day"]]
            if len(scheduled) != 1:
                raise ValueError(f"{run_id}: missing scheduled withdrawal evidence")
            selection = re.fullmatch(r"(\d+) of \d+ investors withdraw", scheduled[0]["note"])
            if not selection:
                raise ValueError(f"{run_id}: missing withdrawal selection count")
            people = defaultdict(list)
            for event in records(run / files["events"]):
                member = event.get("member") or ""
                if "/investor/" in member:
                    people[member].append(event)
            last_mark = {}
            for agent in records(run / files["agents"]):
                if agent.get("role") == "investor":
                    last_mark[agent["member"]] = agent
            selected = 0
            for member, events in people.items():
                if not any(e.get("kind") == "exit" and e.get("status") == "started" and
                           e["day"] == cell["exit_day"] for e in events):
                    continue
                selected += 1
                exits = [e for e in events if e.get("kind") == "exit" and e.get("outcome") == "exited"]
                if len(exits) > 1:
                    raise ValueError(f"{run_id}/{member}: more than one exit")
                row = dict(cell=cell["id"], regime=cell["regime"], noise=cell["noise"],
                           endowment_buck=cell["endowment_buck"], investor_rate=cell["investor_rate"],
                           arm=arm, member=member,
                           scheduled_day=cell["exit_day"], completed_day=None)
                if exits:
                    try:
                        row.update(closed_exit(events, exits[0], last_mark[member]), completed_day=exits[0]["day"])
                    except (KeyError, ValueError) as error:
                        raise ValueError(f"{run_id}/{member}: {error}") from error
                yield row
            if selected != int(selection[1]):
                raise ValueError(f"{run_id}: {selected} starts on scheduled day, expected {selection[1]}")


def describe(rows, heading, *, funded_only=False):
    complete = [r for r in rows if r["completed_day"] is not None]
    bought = [r for r in complete if r["entry_usdc_raw"]]
    holders = [r for r in bought if r["receipts"]]
    groups = [("closed funded receipt holders", holders)]
    if not funded_only:
        groups.insert(0, ("all completed conversion cycles", bought))
    for label, group in groups:
        print(f"{heading} · {label} · {len(group)}/{len(rows)} selected investors")
        if group:
            incoming = sum(r["entry_usdc_raw"] for r in group)
            outgoing = sum(r["exit_usdc_raw"] for r in group)
            median_roi = median(r["exit_usdc_raw"] / r["entry_usdc_raw"] - 1 for r in group)
            print(f"  USDC spent {incoming/RAW:,.2f}, sale proceeds {outgoing/RAW:,.2f}, "
                  f"aggregate realized ROI {outgoing/incoming-1:+.4%}, median investor {median_roi:+.4%}")
    if holders:
        basis = sum(r["basis_buck_raw"] for r in holders)
        paid = sum(r["payout_buck_raw"] for r in holders)
        print(f"  Closed receipt basis {basis/RAW:,.2f} BUCK, cash payout {paid/RAW:,.2f} BUCK, "
              f"receipt-leg return {paid/basis-1:+.4%}")
    if complete:
        print(f"  Completed exits {len(complete)}, max residual {max(r['residual_buck_raw'] for r in complete)/RAW:.6f} BUCK")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--runs", type=Path, help="Directory containing relocated standard run records")
    parser.add_argument("--output", type=Path, help="Optional per-investor CSV, with raw money in units of 1e-6")
    args = parser.parse_args()
    try:
        rows = list(exit_rows(args.campaign, args.runs))
    except (KeyError, OSError, ValueError) as error:
        parser.error(str(error))
    if not rows:
        parser.error("no completed scheduled-withdrawal pairs")
    print("Realized USDC cycles include exit sale of wallet BUCK. Individual NFT returns use only "
          "their BUCK basis and payouts. Unspent USDC and unsold BUCK are excluded from the return.")
    for arm in ("active", "maintenance"):
        describe([r for r in rows if r["arm"] == arm], arm)
    paired_holders = Counter((r["cell"], r["member"]) for r in rows if r.get("receipts"))
    for arm in ("active", "maintenance"):
        describe([r for r in rows if r["arm"] == arm and paired_holders[r["cell"], r["member"]] == 2],
                 arm + " matched funded investors", funded_only=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fields = list(dict.fromkeys(key for row in rows for key in row))
        with args.output.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    main()
