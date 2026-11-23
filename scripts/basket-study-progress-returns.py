"""Read a fixed-close return snapshot from a live stock study and its run records."""

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from math import log
from pathlib import Path
import re
from statistics import median

from alberta_buck.sim.prices import PRICE_SCALE
from alberta_buck.sim.runrecord import read_lines


ARMS = ("active", "maintenance")
CELL_KEYS = ("regime", "noise", "seed", "endowment_buck", "investor_rate", "exit_fraction",
             "exit_day", "warmup_days", "days", "price_digest")


def digest(data):
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def tape_hash(tokens, prices):
    return digest([tokens, [list(column) for column in zip(*prices, strict=True)]])


def available_tape(campaign, cell, tokens):
    if any(Path(name).name != name for name in cell["files"]):
        raise ValueError("unexpected price file path")
    paths = [campaign / "prices" / f for f in cell["files"]]
    if not all(p.is_file() for p in paths):
        return None
    columns = []
    for path in paths:
        with path.open(newline="") as stream:
            column = [int(row["close_usd_micro"]) // PRICE_SCALE for row in csv.DictReader(stream)]
        if len(column) != cell["days"]:
            raise ValueError("input price tape has the wrong horizon")
        columns.append(column)
    prices = list(zip(*columns, strict=True))
    if tape_hash(tokens, prices) != cell["price_digest"]:
        raise ValueError("local price tape SHA-256 disagrees with the campaign")
    return prices


def observed_prices(frame, tokens):
    market = frame["market"]["tokens"]
    if [row["symbol"] for row in market] != tokens:
        raise ValueError("recorded price TOKEN order differs")
    prices = [Decimal(str(row["ref_usd"])) * 1_000_000 for row in market]
    if any(not p.is_finite() or p <= 0 or p != p.to_integral_value() for p in prices):
        raise ValueError("invalid recorded reference price")
    return tuple(int(p) for p in prices)


def read_arm(runs, cell, study, arm, day, expected_tape):
    prefix = cell.get("run_prefix") or cell["id"]
    if (not re.fullmatch(r"stock-[A-Za-z0-9._-]+", prefix)
            or not re.fullmatch(re.escape(cell["id"]) + r"(?:-try[0-9]+)?", prefix)):
        raise ValueError("unexpected run prefix")
    ids = cell.get("run_ids") or {}
    run_id = ids.get(arm, prefix + "-" + arm)
    if run_id != prefix + "-" + arm:
        raise ValueError("campaign arm ID disagrees with run prefix")
    directory = runs / run_id
    if not (directory / "manifest.json").is_file():
        return None, "absent"
    record = json.loads((directory / "manifest.json").read_bytes())
    scenario, config = record["scenario"], record["experiment"]
    if (record["id"] != run_id or record["tokens"] != study["tokens"]
            or scenario["days"] != cell["days"] or scenario["seed"] != cell["seed"]
            or scenario["day_step"] != 1 or config.get("source") != study["experiment"]):
        raise ValueError("run ID, seed, TOKENs, daily horizon or frozen experiment disagree")
    schedule = config.get("interventions", [])
    expected = (dict(day=cell["warmup_days"], action="set_population", investor_rate=cell["investor_rate"]),
                dict(day=cell["exit_day"], action="set_population", exit_now=cell["exit_fraction"]))
    if not all(item in schedule for item in expected):
        raise ValueError("recorded investor treatments disagree")
    settings = record.get("settings") or {}
    if settings and (settings.get("arm") != arm or settings.get("warmup_days") != cell["warmup_days"]):
        raise ValueError("recorded arm or warmup differs")
    metadata = settings.get("stock_study")
    if metadata and metadata.get("price_digest") != cell["price_digest"]:
        raise ValueError("recorded price SHA-256 disagrees with the campaign")

    frames, _ = read_lines(directory / "frames.jsonl")
    if len(frames) > cell["days"] or any(f["day"] != i for i, f in enumerate(frames)):
        raise ValueError("run frame days are not consecutive")
    if len(frames) <= day:
        return None, f"day {len(frames) - 1}" if frames else "no closes"
    prices = [observed_prices(frame, study["tokens"]) for frame in frames]
    verified = False
    if len(frames) == cell["days"]:
        if tape_hash(study["tokens"], prices) != cell["price_digest"]:
            raise ValueError("recorded price tape SHA-256 differs")
        verified = True
    if expected_tape is not None:
        if prices != expected_tape[:len(prices)]:
            raise ValueError("record price rows differ from the SHA-verified input tape")
        verified = True
    summary = marks(frames[cell["warmup_days"] - 1], frames[day])
    return (summary, prices[:day + 1], verified), record["status"]


def positive(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def marks(first, last):
    baskets = first.get("basket") or {}, last.get("basket") or {}
    shares = [positive(b.get("share_price")) for b in baskets]
    holds = [positive(b.get("hold_index")) for b in baskets]
    bids = [positive((f["market"].get("book") or {}).get("buck_bid")) for f in (first, last)]
    share = shares[1] / shares[0] if all(shares) else None
    passive = holds[1] / holds[0] - 1 if all(holds) else None
    usdc = share * bids[1] / bids[0] - 1 if share is not None and all(bids) else None
    return share - 1 if share is not None else None, usdc, passive


def opportunity(prices, first, last):
    """Summarize aligned half-cycle moves across TOKENs in one reference tape."""
    window = 15
    previous = None
    magnitudes = []
    reversals = comparisons = 0
    for end in range(first + window, last + 1, window):
        moves = [log(b / a) for a, b in zip(prices[end - window], prices[end], strict=True)]
        total = sum(moves)
        relative = [move - (total - move) / (len(moves) - 1) for move in moves]
        magnitudes.extend(map(abs, relative))
        if previous is not None:
            reversals += sum(a * b < 0 for a, b in zip(previous, relative, strict=True))
            comparisons += len(relative)
        previous = relative
    return (
        (last - first) // window,
        f"{100 * median(magnitudes):.3f}" if magnitudes else "—",
        f"{reversals}/{comparisons}" if comparisons else "—",
    )


def opportunity_table(tape_cases, seen_prices, day):
    lines = [
        "", "## Price-tape reversal opportunity", "",
        "Each window is a nonoverlapping 15-day reference-price log move from the last warmup close. "
        "Each TOKEN is measured relative to the geometric mean of the other TOKENs. "
        "Median magnitude pools all absolute relative moves in log points (100 × natural log). "
        "The reversal count divides adjacent opposite-sign moves by all possible adjacent comparisons "
        "across TOKENs, including zero moves in the denominator. A trailing window shorter than 15 days is "
        "excluded. This half-cycle diagnostic describes the price paths.", "",
        "| Price regime(s) | Tape SHA-256 prefix | Closes | Full windows | Median magnitude (log points) | Reversals |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for (price_digest, first), regimes in tape_cases.items():
        prices = seen_prices.get(price_digest)
        measures = opportunity(prices, first, day) if prices is not None else ("—", "—", "—")
        fields = [", ".join(regimes), price_digest[:10], f"{first}–{day}", *measures]
        lines.append("| " + " | ".join(map(str, fields)) + " |")
    return lines


def render(campaign, runs, day):
    study = json.loads((campaign / "manifest.json").read_bytes())
    if study.get("schema") != 1 or not study["cells"]:
        raise ValueError("expected a stock-grid campaign manifest")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Basket study progress at close {day}", "", f"Snapshot taken {now}.", "",
             "Each return starts at the last warmup close listed in the table. The exact pre-switch block can "
             "include later day-start activity. The requested day is the zero-based simulation close. "
             "Returns are cumulative and precede individual exit costs.", "",
             "BUCK uses the TWAP share mark. Sampled USDC multiplies that mark by the sampled BUCK bid at both "
             "closes. Passive is the in-world LOW-mark initial-weight TOKEN index, in BUCK. "
             "A dash means that observation was unavailable at this close.", "",
             "| Price regime | Opening Jubilee endowment (BUCK) | Investor arrival setting (/yr) | Scheduled exit | Baseline day | Active BUCK | Active USDC | Active passive | Maintenance BUCK | Maintenance USDC | Maintenance passive | Maintenance |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    seen_prices = {}
    tape_cases = {}
    active_count = maintenance_count = verified_count = 0
    issues = []
    fmt = lambda value: f"{value:+.3%}" if value is not None else "—"
    for cell in study["cells"]:
        warm = cell["warmup_days"]
        if not 1 <= warm <= day < cell["days"]:
            raise ValueError(f"{cell['id']}: close {day} is outside treatment")
        key = {field: cell[field] for field in CELL_KEYS}
        if "opening_cash_buck" in cell:
            key["opening_cash_buck"] = cell["opening_cash_buck"]
        if digest([key, study["runtime"]]) != cell["fingerprint"] or not cell["id"].endswith(cell["fingerprint"][:10]):
            raise ValueError(f"{cell['id']}: campaign fingerprint disagrees")
        price_digest = cell["price_digest"]
        tape_cases.setdefault((price_digest, warm - 1), {})[cell["regime"]] = None
        expected_tape = available_tape(campaign, cell, study["tokens"])
        if expected_tape is not None:
            seen_prices.setdefault(price_digest, expected_tape[:day + 1])
        values, notes = [], {}
        for arm in ARMS:
            try:
                result, notes[arm] = read_arm(runs, cell, study, arm, day, expected_tape)
                if result is None:
                    values.extend(["—"] * 3)
                    continue
                returns, prices, verified = result
                previously_seen = seen_prices.setdefault(cell["price_digest"], prices)
                if prices != previously_seen:
                    raise ValueError("matched tape has a different recorded price prefix")
                values.extend(map(fmt, returns))
                verified_count += verified
                active_count += arm == "active"
                maintenance_count += arm == "maintenance"
            except (OSError, KeyError, TypeError, ValueError, InvalidOperation) as error:
                issues.append(f"- `{cell['id']}`, {arm}: {error}")
                notes[arm] = "invalid"
                values.extend(["—"] * 3)
        maint = "same close" if notes["maintenance"] in ("running", "complete") else notes["maintenance"]
        fields = [cell["regime"], f"{cell['endowment_buck']:,.0f}", f"{cell['investor_rate']:g}",
                  f"{cell['exit_fraction']:.0%} on day {cell['exit_day']}", str(warm - 1), *values, maint]
        lines.append("| " + " | ".join(fields) + " |")
    lines.extend(["", f"Available at close {day}: {active_count}/{len(study['cells'])} active and "
                  f"{maintenance_count}/{len(study['cells'])} maintenance. Matched prefixes agree among available "
                  f"arms sharing each tape. Full tape SHA-256 checked for {verified_count} arms with complete "
                  "frames or local input CSVs. Partial arms are checked against their campaign identity and "
                  "other available prefixes."])
    lines.extend(opportunity_table(tape_cases, seen_prices, day))
    if issues:
        lines.extend(["", "## Unavailable or inconsistent records", "", *issues])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--runs", type=Path, default=Path("build/runs"))
    parser.add_argument("--day", type=int, required=True, help="Zero-based simulation close")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = render(args.campaign, args.runs, args.day)
    if args.output is None:
        print(report, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report)
        print(args.output)


if __name__ == "__main__":
    main()
