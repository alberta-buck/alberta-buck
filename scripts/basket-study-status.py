"""Show campaign progress from manifests, standard live run records and cell logs."""
import argparse
from collections import Counter
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re


def existing_records(runs):
    records = {}
    if runs.is_dir():
        for path in runs.glob("stock-*/manifest.json"):
            record = json.loads(path.read_text())
            records[record["id"]] = record
    return records


def states(campaign, runs=None, *, manifest=None, records=None):
    if manifest is None:
        manifest = json.loads((campaign / "manifest.json").read_text())
    if records is None:
        records = existing_records(runs or Path(manifest.get("run_root", "build/runs")))
    rows = []
    for cell in manifest["cells"]:
        ident = cell["id"]
        status = cell["status"]
        details = []
        for arm in ("active", "maintenance"):
            ids = cell.get("run_ids") or {}
            run_id = ids.get(arm, f"{ident}-{arm}")
            rec = records.get(run_id)
            if rec:
                frames = rec.get("frames", 0)
                details.append(f"{arm} {rec['status']} {frames}/{cell.get('days', '?')} closes")
                if status == "planned":
                    status = "failed" if rec["status"] == "failed" else "running"
        if not details and status in ("running", "failed"):
            log = campaign / cell.get("log", f"cells/{ident}.log")
            if log.exists():
                with log.open("rb") as log_file:
                    log_file.seek(max(0, log.stat().st_size - 4000))
                    tail = log_file.read().decode(errors="replace")
                days = re.findall(r"\[sim\] day\s+(\d+)", tail)
                if status == "failed" and tail.strip():
                    details.append(tail.strip().splitlines()[-1][:180])
                elif days:
                    details.append("last logged day " + days[-1])
        rows.append((ident, status, "; ".join(details)))
    return rows


def inventory(root, runs):
    records = existing_records(runs)
    seen = 0
    for folder, children, names in os.walk(root):
        children[:] = sorted(n for n in children if n not in {
            "cells", "prices", "snapshot", "snapshots", "records", "out", "node_modules",
            ".git", "lib", "agents", "__pycache__"} and not n.endswith("-data"))
        if "manifest.json" not in names:
            continue
        campaign = Path(folder)
        path = campaign / "manifest.json"
        m = json.loads(path.read_text())
        if not isinstance(m, dict) or not isinstance(m.get("cells"), list):
            continue
        rows = states(campaign, manifest=m, records=records)
        counts = Counter(s for _, s, _ in rows)
        updated = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        print(f"{folder}  {counts['complete']}/{len(rows)} complete, "
              f"{counts['running']} running, {counts['failed']} failed  ({updated})")
        seen += 1
    if not seen:
        print(f"No basket study manifests found under {root}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--runs", type=Path)
    p.add_argument("--list", action="store_true", help="discover studies below the supplied directory")
    p.add_argument("--all", action="store_true", help="also list every queued and completed cell")
    a = p.parse_args()
    if a.list:
        inventory(a.directory, a.runs or Path("build/runs"))
        return
    rows = states(a.directory, a.runs)
    counts = Counter(s for _, s, _ in rows)
    print(f"{a.directory}  •  {len(rows)} cells  •  " + ", ".join(f"{counts[s]} {s}" for s in ("complete", "running", "planned", "failed")))
    for ident, status, detail in rows:
        if a.all or status in ("running", "failed"):
            print(f"{status:9} {ident}  {detail}".rstrip())


if __name__ == "__main__":
    main()
