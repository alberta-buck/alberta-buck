#!/usr/bin/env python3
"""Summarize matched first-to-last TWAP share returns from a basket signal grid."""

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
from statistics import mean


def returns(campaign, manifest, cell):
    """Validate the artifact and read the same share mark used by the grid viewer."""
    raw = (campaign / cell["output"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != cell["output_sha256"]:
        raise ValueError("capture checksum mismatch")
    capture = json.loads(raw)
    frames = capture["frames"]
    tokens = capture["tokens"]
    if tokens != manifest["tokens"] or len(frames) != manifest["days"]:
        raise ValueError("capture TOKENs or horizon disagree with manifest")
    if any(row["day"] != day or len(row["prices"]) != len(tokens)
           for day, row in enumerate(frames)):
        raise ValueError("inconsistent capture days or prices")
    prices = [list(series) for series in zip(*(row["prices"] for row in frames))]
    price_json = json.dumps([tokens, prices], separators=(",", ":")).encode()
    if hashlib.sha256(price_json).hexdigest() != cell["price_digest"]:
        raise ValueError("captured price tape disagrees with manifest")
    shares = [row["share"] for row in frames]
    if not shares or any(not isinstance(value, (float, int)) or not math.isfinite(value)
                         or value <= 0 for value in shares):
        raise ValueError("missing or invalid TWAP share mark")
    return shares[-1] / shares[0] - 1


def index_campaign(campaign):
    manifest = json.loads((campaign / "manifest.json").read_text())
    if manifest["schema_version"] != 1:
        raise ValueError("unsupported signal-grid manifest schema")
    groups = defaultdict(dict)
    errors = []
    for cell in manifest["cells"]:
        group = groups[cell["group"]]
        arm = cell["arm"]
        key = ((cell["response"], cell["onset"], cell["speed"])
               if arm == "candidate" else arm)
        if key in group:
            raise ValueError(f"duplicate arm in {cell['group']}: {key}")
        result = None
        if cell["status"] == "complete":
            try:
                result = returns(campaign, manifest, cell)
            except (OSError, KeyError, ValueError, TypeError) as exc:
                errors.append((cell["id"], str(exc)))
        else:
            errors.append((cell["id"], cell["status"]))
        group[key] = (cell, result)
    return manifest, groups, errors


def matched(groups):
    summaries = defaultdict(lambda: {"planned": 0, "paths": defaultdict(list)})
    unmatched = []
    for group_id, group in groups.items():
        for key, (cell, candidate) in group.items():
            if cell["arm"] != "candidate":
                continue
            summary = summaries[(cell["regime"], cell["noise_sigma"], *key)]
            summary["planned"] += 1
            names = ("production", "maintenance")
            controls = [group.get(name) for name in names]
            missing = [name for name, control in zip(names, controls)
                       if control is None or control[1] is None]
            if candidate is None:
                missing.insert(0, "candidate")
            if missing:
                unmatched.append((group_id, "/".join(key), ", ".join(missing)))
                continue
            path = cell["price_digest"]
            if any(any(control[0][field] != cell[field]
                       for field in ("regime", "noise_sigma", "seed", "price_digest"))
                   for control in controls):
                unmatched.append((group_id, "/".join(key), "different control inputs"))
                continue
            production, maintenance = [control[1] for control in controls]
            summary["paths"][path].append((candidate, production, maintenance,
                                            candidate - production, candidate - maintenance))
    return summaries, unmatched


def render(campaign, manifest, groups, errors):
    summaries, unmatched = matched(groups)
    out = [f"# Matched basket share returns · {campaign.name}", "",
           f"{manifest['days']} daily closes. Returns use the first and last BUCK TWAP share "
           "marks before individual exit costs. Deltas are percentage points.", "",
           "## Matched controls", "",
           "Each row compares production and maintenance on the same seed and verified price tape.", "",
           "| Regime | Daily noise | Seed | Production ROI | Maintenance ROI | Δ production − maintenance (pp) | Sign |",
           "|---|---:|---:|---:|---:|---:|---|"]
    for group_id, group in sorted(groups.items()):
        production, maintenance = group.get("production"), group.get("maintenance")
        control = production or maintenance
        if control is None:
            continue
        cell = control[0]
        noise = "archive" if cell.get("noise_sigma") is None else f"{cell['noise_sigma']:g}"
        same = production and maintenance and all(
            production[0].get(field) == maintenance[0].get(field)
            for field in ("regime", "noise_sigma", "seed", "price_digest"))
        if same and production[1] is not None and maintenance[1] is not None:
            prod, maint = production[1], maintenance[1]
            delta = prod - maint
            values = [f"{prod:+.3%}", f"{maint:+.3%}", f"{100 * delta:+.3f}",
                      "positive" if delta > 0 else "negative" if delta < 0 else "zero"]
        else:
            values = ["—"] * 4
            unmatched.append((group_id, "production/maintenance", "missing, invalid, or different inputs"))
        out.append("| " + " | ".join(map(str, [cell['regime'], noise, cell['seed'], *values])) + " |")
    if summaries:
        out.extend(["", "## Candidate comparisons", "",
           "Each comparison uses the candidate and both controls from its own seed and price tape. "
           "The averages give each distinct price tape equal weight after averaging matched seeds "
           "on that tape. Seeds sharing a zero-noise tape still vary the economy. This is a "
           "descriptive comparison of the measured tapes.", "",
           "| Regime | Daily noise | Law | Onset | Speed | Matched / planned | Distinct tapes | "
           "Candidate ROI | Production ROI | Maintenance ROI | Δ production (pp) | Δ maintenance (pp) |",
           "|---|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|"])
    for (regime, noise, law, onset, speed), summary in sorted(summaries.items(), key=lambda item: tuple(str(x) for x in item[0])):
        paths = summary["paths"]
        count = sum(len(instances) for instances in paths.values())
        path_means = [[mean(values) for values in zip(*instances)] for instances in paths.values()]
        averages = [mean(values) for values in zip(*path_means)]
        values = ([f"{value:+.3%}" for value in averages[:3]]
                  + [f"{100 * value:+.3f}" for value in averages[3:]]) if count else ["—"] * 5
        fields = [regime, "archive" if noise is None else f"{noise:g}", law, onset, speed, f"{count}/{summary['planned']}",
                  str(len(paths)), *values]
        out.append("| " + " | ".join(fields) + " |")
    if unmatched or errors:
        out.extend(["", "## Incomplete comparisons", ""])
        for group, candidate, issue in unmatched:
            out.append(f"- `{group}` · `{candidate}` · {issue}")
        if errors:
            out.extend(["", "### Cell status and validation", ""])
            for cell, error in errors:
                out.append(f"- `{cell}` · {error}")
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path, help="Directory containing manifest.json and captures")
    parser.add_argument("--output", type=Path, help="Write Markdown to this path")
    args = parser.parse_args()
    try:
        report = render(args.campaign, *index_campaign(args.campaign))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report)
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
