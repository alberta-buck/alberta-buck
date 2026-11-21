#!/usr/bin/env python3
"""The basket's storage layout: one layout for a shell and every facet it delegatecalls.

The equity basket is a shell (BuckBasketEquity) that delegatecalls its facets (the V3 venue, the
machine's venue and its upkeep), and the desk (BasketDesk) delegatecalls the same venue: each pair
runs on the SAME storage, so every variable a facet declares must sit at the shell's slot, offset and
type. A facet's variable the shell lacks is private state on someone else's storage, and refused.

The shells' layouts are also pinned (scripts/storage-layout.json): removing or moving a field moves
every later one, so a change must be deliberate. Before the release nothing is deployed, so a change
is allowed; after it, a deployed shell's layout is fixed.

    python3 scripts/storage-layout.py --check     # compare with the pin (make storage-layout-check)
    python3 scripts/storage-layout.py --update    # rewrite the pin after a deliberate change

Reads the compiled artifacts under out/ (foundry.toml asks for storageLayout).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "out"
PIN = REPO / "scripts" / "storage-layout.json"

# Each shell, and the facets that run on its storage by delegatecall.
GROUPS = {
    "BuckBasketEquity": ["BuckBasketUniswapV3", "BasketVenueFacet", "BasketUpkeepFacet", "BasketExitFacet"],
    "BasketDesk": ["BuckBasketUniswapV3"],
}


def layout(name: str) -> list[dict]:
    """(label, slot, offset, type, bytes) per variable, in declaration order; types by their
    source label (the AST ids in the type keys change from one compilation to the next)."""
    path = OUT / f"{name}.sol" / f"{name}.json"
    art = json.loads(path.read_text())
    sl = art.get("storageLayout")
    if not sl or "storage" not in sl:
        raise SystemExit(f"{path}: no storageLayout (foundry.toml extra_output; rebuild with forge build)")
    types = sl.get("types") or {}
    rows = []
    for v in sl["storage"]:
        t = types.get(v["type"], {})
        rows.append({"label": v["label"], "slot": int(v["slot"]), "offset": int(v["offset"]),
                     "type": t.get("label", v["type"]), "bytes": int(t.get("numberOfBytes", 0))})
    return rows


def agree(shell: str, facet: str) -> list[str]:
    errors = []
    by_label = {r["label"]: r for r in layout(shell)}
    for r in layout(facet):
        s = by_label.get(r["label"])
        if s is None:
            errors.append(f"{facet}.{r['label']}: not in {shell} (a facet's own state, on the shell's storage)")
        elif (s["slot"], s["offset"], s["type"], s["bytes"]) != (r["slot"], r["offset"], r["type"], r["bytes"]):
            errors.append(f"{facet}.{r['label']}: slot {r['slot']}+{r['offset']} {r['type']}, "
                          f"but {shell} has slot {s['slot']}+{s['offset']} {s['type']}")
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--update", action="store_true")
    a = ap.parse_args(argv)
    errors = []
    for shell, facets in GROUPS.items():
        for facet in facets:
            errors += agree(shell, facet)
    current = {shell: layout(shell) for shell in GROUPS}
    if a.update:
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        PIN.write_text(json.dumps(current, indent=1) + "\n")
        print(f"wrote {PIN.relative_to(REPO)}: " + ", ".join(f"{k} {len(v)} variables" for k, v in current.items()))
        return 0
    pinned = json.loads(PIN.read_text()) if PIN.exists() else {}
    for shell, rows in current.items():
        if pinned.get(shell) != rows:
            old = {r["label"]: r for r in pinned.get(shell, [])}
            new = {r["label"]: r for r in rows}
            moved = sorted(k for k in new if k in old and new[k] != old[k])
            errors.append(f"{shell}: layout differs from the pin -- added {sorted(set(new) - set(old))}, "
                          f"removed {sorted(set(old) - set(new))}, moved {moved[:8]}{'...' if len(moved) > 8 else ''}"
                          " (a deliberate change: make storage-layout-update)")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("storage layout: " + "; ".join(f"{s} and {', '.join(f)} agree" for s, f in GROUPS.items())
          + "; the shells match the pin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
