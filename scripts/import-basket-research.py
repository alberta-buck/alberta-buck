"""Open completed basket research captures as partial read-only dashboard runs.

The default imports the two-year historical control pair. For a shorter or
archived campaign, use --campaign PATH and one or more --filter FIELD=GLOB.
The complete pair diagnostics remain in each campaign's offline viewer.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from fnmatch import fnmatchcase
import hashlib
import json
import math
from pathlib import Path
import re

from alberta_buck.sim.runrecord import FILES, RunRecorder, frame_record


ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "build/sim/signal-history-730-datasim"
FILTER_FIELDS = ("id", "group", "regime", "seed", "noise_sigma", "arm", "response", "onset", "speed")


class CaptureRecorder(RunRecorder):
    def frame(self, flat):
        row = frame_record(flat, self.manifest["tokens"])
        row["health"] = None  # the capture did not sample faults or household activity
        self._append("frames", [row])
        self.days += 1

    def empty_ledgers(self):
        # The API's indexed readers expect a file. Capabilities still say
        # that the archived data cannot answer household or event questions.
        for key in ("agents", "events"):
            path = self.dir / FILES[key]
            if not path.exists():
                path.touch()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def select_cells(manifest, filters):
    seen = set()
    for cell in manifest["cells"]:
        if cell["status"] != "complete" or any(
            not fnmatchcase(str(cell.get(key)), pattern) for key, pattern in filters
        ):
            continue
        key = tuple(cell.get(field) for field in ("group", "price_digest", "arm", "response", "onset", "speed"))
        if key not in seen:
            seen.add(key)
            yield cell


def projected_frames(rows, tokens, declared):
    volumes, counts = Counter(), Counter()
    for day, row in enumerate(rows):
        prices = row["prices"]
        if (row["day"] != day or len(prices) != len(tokens)
                or any(type(p) is not int or p <= 0 for p in prices)
                or type(row["share"]) is not int or row["share"] <= 0
                or not math.isfinite(row["hold"]) or row["hold"] <= 0):
            raise ValueError(f"invalid close on day {day}")
        for side in ("buy", "sell"):
            orders = [t for t in row["trades"] if t["side"] == side]
            counts[side] += len(orders)
            if counts[side] != row[side + "s"]:
                raise ValueError(f"inconsistent cumulative {side} count on day {day}")
            for trade in orders:
                amount = Decimal(str(trade["amount"])) * 1_000_000
                if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
                    raise ValueError(f"{side} trade has invalid BUCK amount on day {day}")
                volumes[side] += int(amount)
        yield dict(day=day, refUsd=prices, spotBuck=[None] * len(tokens), spotUsdc=[None] * len(tokens),
                   eq_share_price=row["share"], eq_hold=row["hold"],
                   eq_w=row["weights"], eq_lean=row["targets"], eq_decl=declared,
                   wh_eq_buy=volumes["buy"], wh_eq_sell=volumes["sell"],
                   wh_eq_buy_n=counts["buy"], wh_eq_sell_n=counts["sell"])


def import_capture(campaign, manifest, cell, root):
    capture_path = (campaign / cell["output"]).resolve()
    if not capture_path.is_relative_to(campaign):
        raise ValueError("capture lies outside its campaign directory")
    raw = capture_path.read_bytes()
    sha = digest(raw)
    if sha != cell["output_sha256"]:
        raise ValueError(f"{cell['id']}: capture checksum mismatch")
    capture = json.loads(raw)
    tokens, rows = capture["tokens"], capture["frames"]
    if tokens != manifest["tokens"] or len(rows) != manifest["days"] or not rows:
        raise ValueError(f"{cell['id']}: capture disagrees with campaign horizon or TOKENs")
    config = capture["config"]
    if config["scenario"]["seed"] != cell["seed"]:
        raise ValueError(f"{cell['id']}: capture seed disagrees with campaign")
    columns = [list(row) for row in zip(*(frame["prices"] for frame in rows), strict=True)]
    if digest(json.dumps([tokens, columns], separators=(",", ":")).encode()) != cell["price_digest"]:
        raise ValueError(f"{cell['id']}: price tape checksum mismatch")
    declared = manifest.get("token_weights_bp")
    frames = list(projected_frames(rows, tokens, [declared[s] for s in tokens] if declared else None))

    label = f"{campaign.name}-{cell['id']}"
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", label)[:64].rstrip("-")
    run_id = f"partial-{slug}-{sha[:10]}"
    rec = CaptureRecorder(root, label, run_id=run_id, kind="research-capture", engine=manifest["backend"])
    old = rec.dir / "manifest.json"
    if old.exists():
        prior = json.loads(old.read_text())
        if (prior.get("source", {}).get("sha256") == sha and prior.get("status") == "complete"
                and prior.get("frames") == len(frames)):
            rec.empty_ledgers()
            return rec.dir
        raise FileExistsError(f"cannot replace existing record {rec.dir}")

    viewer = campaign / "index.html"
    source = dict(capture=str(capture_path), sha256=sha, price_sha256=cell["price_digest"],
                  campaign=str(campaign / "manifest.json"), cell=cell["id"],
                  viewer=str(viewer) if viewer.is_file() else None,
                  partial=True, available=["reference prices", "share price", "passive TOKEN index",
                                           "basket weights", "executed TOKEN flow"])
    written = datetime.fromtimestamp(capture_path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    rec.open(meta={"experiment": config}, tokens=tokens, source=source,
             settings={name: cell.get(name) for name in FILTER_FIELDS})
    try:
        rec.manifest.update(revision=None, started=written, imported=rec.manifest["started"],
                            capabilities=dict(live=False, control=None, agents=False, events=False))
        rec.manifest["scenario"].update(name=f"Partial research capture · {cell['regime']} · {cell['response']}",
                                        seed=cell["seed"], days=len(frames),
                                        day_step=config["scenario"]["day_step"],
                                        ticks_per_day=config["scenario"]["ticks_per_day"],
                                        basket=config["scenario"]["basket"])
        for frame in frames:
            rec.frame(frame)
        rec.empty_ledgers()
        rec.close(outputs=dict(capture=str(capture_path)))
        rec.manifest["finished"] = written
        rec._write_manifest()
    except (Exception, KeyboardInterrupt) as error:
        rec.close(status="failed", error=str(error))
        raise
    return rec.dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, default=DEFAULT)
    parser.add_argument("--runs", type=Path, help="dashboard runs directory (default $SIM_RUNS or build/runs)")
    parser.add_argument("--filter", "--filters", action="append", default=[], help="Exact or glob FIELD=VALUE")
    parser.add_argument("--list", action="store_true", help="Show the matching cells without importing")
    args = parser.parse_args()
    campaign = args.campaign.resolve()
    filters = []
    for expr in args.filter:
        key, sep, pattern = expr.partition("=")
        if not sep or key not in FILTER_FIELDS or not pattern:
            parser.error("use FIELD=GLOB for " + ", ".join(FILTER_FIELDS))
        filters.append((key, pattern))
    manifest = json.loads((campaign / "manifest.json").read_text())
    if manifest.get("schema_version") != 1:
        parser.error("unsupported campaign schema")
    if not filters:
        if campaign == DEFAULT.resolve():
            cells = [c for c in select_cells(manifest, [("regime", "history"), ("seed", "41404")])
                     if c["arm"] in ("production", "maintenance")]
            if len(cells) != 2 or manifest["days"] != 730:
                parser.error("expected two completed 730-day historical references")
        elif args.list:
            cells = list(select_cells(manifest, filters))
        else:
            parser.error("select archive or shorter campaign cells with --filter")
    else:
        cells = list(select_cells(manifest, filters))
    if not cells:
        parser.error("no complete cells match")
    if args.list:
        for cell in cells:
            print(cell["id"])
        return
    for cell in cells:
        place = import_capture(campaign, manifest, cell, args.runs)
        print(f"{place}  /?run={place.name}&tab=basket")


if __name__ == "__main__":
    main()
