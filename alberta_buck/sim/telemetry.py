"""Read per-agent telemetry out of a sim vector (schema: TELEMETRY.md).

One entry point for analysis code, plots, and parity tests, so nothing
else needs to know the frame layout:

    from alberta_buck.sim.telemetry import load
    t = load("test/vectors/rebalancing-sim-eq-...-excursion.json")
    t["version"]                        # 1, or 0 for a pre-telemetry vector
    a = t["agents"]["ExcursionArbAgent#0"]
    a["meta"]                           # {"cls", "idx", "stride", "knobs"}
    a["days"]                           # [0, 1, 2, ...] sample days
    a["series"]["nw"]                   # par-marked net worth per sample
    a["series"]["u"]                    # any field the class emitted

Fields absent from a given sample day are filled with None so every
series is index-aligned with `days`.  Old vectors (no telemetry) load
gracefully: {"version": 0, "agents": {}}.
"""

from __future__ import annotations

import json
from pathlib import Path


def load(path) -> dict:
    """Parse `path` (a sim vector) into {"version", "units", "agents"}.

    agents: {id: {"meta": {...}, "days": [...], "series": {field: [...]}}}
    """
    doc = json.loads(Path(path).read_text())
    meta = (doc.get("meta") or {}).get("telemetry") or {}
    out = {"version": int(meta.get("version", 0)),
           "units": meta.get("units", "usd6"), "agents": {}}
    agents = out["agents"]
    for m in meta.get("agents", []):
        agents[m["id"]] = {"meta": {k: v for k, v in m.items()
                                    if k != "id"},
                           "days": [], "series": {}}
    for fr in doc.get("frames", []):
        ag = fr.get("ag")
        if not ag:
            continue
        day = fr.get("day", 0)
        for aid, rec in ag.items():
            a = agents.setdefault(   # tolerate records absent from meta
                aid, {"meta": {}, "days": [], "series": {}})
            n = len(a["days"])
            a["days"].append(day)
            for k, v in rec.items():
                s = a["series"].setdefault(k, [None] * n)
                s.append(v)
            for k, s in a["series"].items():
                if len(s) <= n:      # field missing this sample: align
                    s.append(None)
    return out
