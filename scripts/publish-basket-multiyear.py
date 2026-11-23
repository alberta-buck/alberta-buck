"""Publish matched historical price-regime comparisons from retained captures."""

import argparse
from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
import mmap
from pathlib import Path
import re
import sys


HERE = Path(__file__).resolve()
HISTORY = Path("build/sim/signal-history-730-datasim")
ANCHORS = Path("build/sim/basket-anchor-grid-mac")
REFERENCE = Path("build/sim/basket-multiyear/five-year-reference.json")
REFERENCE_ARMS = ("econ-F-s11", "econ-F-s13")
ARMS = ("maintenance", "production")
TAPES = {"pinned-bridge": "Pinned bridge · Mac",
         "straight-bridge": "Linear bridge · Mac",
         "carry-forward": "Carry-forward · Mac",
         "history": "Historical bridge · datasim"}


def load_importer():
    spec = importlib.util.spec_from_file_location("research_import", HERE.with_name("import-basket-research.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_pairs(campaign, regimes, runs, importer):
    manifest = json.loads((campaign / "manifest.json").read_bytes())
    matched = {}
    for regime in regimes:
        matched[regime] = {}
        for arm in ARMS:
            cells = list(importer.select_cells(manifest, [("regime", regime), ("arm", arm), ("seed", "41404")]))
            if len(cells) != 1:
                raise ValueError(f"{campaign}: expected one completed {regime}/{arm} capture")
            cell, = cells
            place = importer.import_capture(campaign, manifest, cell, runs)
            capture = json.loads((campaign / cell["output"]).read_bytes())
            matched[regime][arm] = (place.name, capture["frames"])
        left, right = (v[1] for v in matched[regime].values())
        if len(left) != 730 or [f["prices"] for f in left] != [f["prices"] for f in right]:
            raise ValueError(f"{regime}: control and production tapes differ")
    return matched, manifest.get("price_calendar")


def save_matched_index(runs, name, groups, end_day, note):
    from alberta_buck.sim import compare

    axis = lambda name, note, labels: dict(name=name, note=note, reference=labels[0],
                                           values=[dict(label=label, set={name: label}) for label in labels])
    index = dict(schema=compare.SCHEMA, name=name, question=note, days=end_day + 1, end_day=end_day,
                 experiment="matched runs of economy.toml, seed 41404",
                 axes=[axis("upkeep", "Vary policy within one identical exogenous price tape", list(ARMS)),
                       axis("price tape", "Change the matched price treatment", [TAPES[t] for t in groups])],
                 runs=[dict(id=run_id, values={"price tape": TAPES[tape], "upkeep": arm})
                       for tape, pair in groups.items() for arm, (run_id, _) in pair.items()])
    return compare.write_index(runs, index)


def extract_reference(checkout):
    """Keep exact passive marks from the original snapshots with the compact source run records."""
    records = {}
    # These two field names occur exactly once per old snapshot frame. Compare
    # the recorded share price against JSONL to validate the extracted sequence.
    match = re.compile(rb'"(eq_share_price|eq_hold)"\s*:\s*([0-9.eE+\-]+)')
    for arm in REFERENCE_ARMS:
        directory = checkout / "build/runs" / arm
        manifest = json.loads((directory / "manifest.json").read_bytes())
        frames = [json.loads(line) for line in (directory / "frames.jsonl").read_text().splitlines()]
        path = checkout / manifest["outputs"]["vector"]
        with path.open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as vector:
            holds, shares = [], []
            for field in match.finditer(vector):
                if field[1] == b"eq_hold":
                    holds.append(float(field[2]))
                else:
                    shares.append(int(field[2]))
        if (manifest["status"] != "complete" or len(frames) != len(holds) or len(holds) != len(shares)
                or any(frame["basket"]["share_price"] != share / 1e18
                       for frame, share in zip(frames, shares, strict=True))):
            raise ValueError(f"{arm}: source snapshots do not match record")
        for frame, hold, share in zip(frames, holds, shares, strict=True):
            frame["basket"]["hold_index"] = hold
            frame["basket"]["share_price_raw"] = str(share)
        records[arm] = dict(manifest=manifest, frames=frames)
    return records


def save_references(runs, bundle):
    from alberta_buck.sim import compare

    records = json.loads(bundle.read_bytes())
    match = []
    for arm in REFERENCE_ARMS:
        record = records[arm]
        manifest, frames = record["manifest"], record["frames"]
        if [f["day"] for f in frames] != list(range(1825)) or manifest["status"] != "complete":
            raise ValueError(f"{arm}: expected complete five-year closes")
        config = deepcopy(manifest["experiment"])
        config["scenario"].pop("seed")
        match.append((manifest["revision"], config, [[t["ref_usd"] for t in f["market"]["tokens"]] for f in frames]))
    if match[0] != match[1]:
        raise ValueError("five-year seed references differ in configuration, revision or input prices")
    selections = []
    for arm in REFERENCE_ARMS:
        record = records[arm]
        manifest, frames = record["manifest"], record["frames"]
        run_id = "reference-5y-" + arm
        directory = runs / run_id
        source = dict(bundle=str(bundle), original_id=arm, original_host=manifest["host"],
                      original_vector=manifest["outputs"]["vector"], active_reference_only=True)
        if directory.exists() and json.loads((directory / "manifest.json").read_bytes()).get("source") != source:
            raise FileExistsError(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "frames.jsonl").write_text("".join(json.dumps(f, separators=(",", ":")) + "\n" for f in frames))
        for ledger in ("agents", "events"):
            (directory / (ledger + ".jsonl")).touch()
        manifest = {**manifest, "id": run_id, "name": run_id, "source": source,
                    "capabilities": dict(live=False, control=None, agents=False, events=False)}
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
        selections.append(dict(id=run_id, values={"household seed": str(manifest["scenario"]["seed"])}))
    labels = [s["values"]["household seed"] for s in selections]
    index = dict(schema=compare.SCHEMA, name="multiyear-active-reference-seeds",
                 question="Older active-only baseline. Identical historical price forcing and policy, "
                          "different household seeds. This suite predates current household and upkeep changes",
                 experiment="archived economy.toml at 4d7497aa", days=1825, end_day=1824,
                 axes=[dict(name="household seed", note="Neither arm is maintenance", reference=labels[0],
                            values=[dict(label=seed, set={"scenario.seed": seed}) for seed in labels])],
                 runs=selections)
    path = compare.write_index(runs, index)
    return path, records


def roi(first, last, field):
    return (Decimal(str(last[field])) / Decimal(str(first[field])) - 1) * 100


def report(path, groups, end_days, references):
    lines = ["# Multi-year basket price regimes", "",
             "Share marks are in BUCK and exclude individual exit costs. The passive TOKEN index prices the initial "
             "declared weights at each world's LOW marks. Its BUCK denominator and venue marks can differ between "
             "arms on the same external price tape. Policy effects below are matched active minus maintenance share returns.",
             "", "| Tape | Close interval | Active share | Maintenance share | Δ share (pp) | Active passive | Maintenance passive |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for end in end_days:
        for tape, pair in groups.items():
            if end != 729 and tape == "history":
                continue
            a, b = pair["production"][1], pair["maintenance"][1]
            ap, bp = (roi(f[0], f[end], "share") for f in (a, b))
            ah, bh = (roi(f[0], f[end], "hold") for f in (a, b))
            lines.append(f"| {TAPES[tape]} | 0–{end} | {ap:+.6f}% | {bp:+.6f}% | {ap-bp:+.6f} | {ah:+.6f}% | {bh:+.6f}% |")
    lines.extend(["", "The last common simulated availability date for the three Mac treatments is day 699, "
                  "October 1, 2022. The pinned Mac tape and historical datasim tape match exactly. "
                  "Their duplicate returns cross-check platform replay and do not add an independent price path."])
    if references:
        lines.extend(["", "## Older five-year active references", "",
                      "These active-only runs use identical exogenous prices, older policy at `4d7497aa` and two household seeds. "
                      "They have no strict-maintenance counterfactual and are outside the matched two-year comparison.", "",
                      "| Household seed | Close interval | Share return | Passive return | Share − passive (pp) |",
                      "|---|---:|---:|---:|---:|"])
        for arm in REFERENCE_ARMS:
            r = references[arm]
            rows = [f["basket"] for f in r["frames"]]
            share = roi(rows[0], rows[-1], "share_price_raw")
            hold = roi(rows[0], rows[-1], "hold_index")
            lines.append(f"| {r['manifest']['scenario']['seed']} | 0–1824 | {share:+.6f}% | {hold:+.6f}% | {share-hold:+.6f} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--history", type=Path, default=HISTORY)
    p.add_argument("--anchors", type=Path, default=ANCHORS)
    p.add_argument("--runs", type=Path, default=Path("build/runs"))
    p.add_argument("--reference-bundle", type=Path, default=REFERENCE)
    p.add_argument("--report", type=Path, default=Path("build/sim/basket-multiyear/returns.md"))
    p.add_argument("--extract-five-year", type=Path, metavar="CHECKOUT",
                   help="Write a compact reference bundle to stdout from the original datasim checkout")
    args = p.parse_args()
    if args.extract_five_year:
        json.dump(extract_reference(args.extract_five_year), sys.stdout, separators=(",", ":"))
        return
    importer = load_importer()
    history, _ = read_pairs(args.history.resolve(), ("history",), args.runs, importer)
    anchors, calendar = read_pairs(args.anchors.resolve(), tuple(TAPES)[:3], args.runs, importer)
    last_anchor = calendar["last_common_anchor_day"]
    if len({tuple(pair["production"][1][last_anchor]["prices"]) for pair in anchors.values()}) != 1:
        raise ValueError("declared last common anchor has different external prices")
    if any(history["history"][arm][1] != anchors["pinned-bridge"][arm][1] for arm in ARMS):
        raise ValueError("Mac pinned bridge replay does not match datasim history")
    groups = {**anchors, **history}
    print(save_matched_index(args.runs, "multiyear-price-regimes", groups, 729,
          "730 closes for each matched price tape. The day-729 external close differs across anchor treatments. "
          "Pinned Mac and historical datasim repeat the same tape"))
    print(save_matched_index(args.runs, "multiyear-common-anchors", anchors, last_anchor,
          "Through the last common external anchor, day 699. The availability lag is an assumption"))
    references = None
    if args.reference_bundle.is_file():
        path, references = save_references(args.runs, args.reference_bundle)
        print(path)
    report(args.report, groups, (last_anchor, 729), references)
    print(args.report)


if __name__ == "__main__":
    main()
