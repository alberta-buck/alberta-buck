"""Telemetry schema v2 (alberta_buck/sim/TELEMETRY.md): the ACTION LOG.

Every chain send made while an agent is the ACTOR is appended to that
agent's pending acts -- {"t": tick, "fn": <contract function>, "ok": bool[,
"err": reason]} -- and an agent's own decision notes join the same stream
as {"t": tick, "kind": <verb>, "why": {...}}.  The snapshot drains each
agent's pending acts into frame["ag"][id]["acts"] every frame (state
records keep their stride), so a vector answers "what did this agent do,
and why, on this day".

The loop installs the recorder once (`install(chain)`), sets `chain.actor`
around every agent's act() and the whales' snap(), and keeps
`chain.sim_day` / `chain.sim_tick` current.  Agents opt their reasons in
with `self.note(d, kind, **why)`; the sends are recorded for every agent
without any change to its code.

SIM_ACTS = all (default) | telemetry (only agents with telemetry_static())
| none.
"""

from __future__ import annotations

import os

MODE = os.environ.get("SIM_ACTS", "all")

_ERR_CHARS = 96
_STR_CHARS = 48


def install(chain):
    """Wrap chain.send once so sends are attributed to chain.actor."""
    if getattr(chain, "_acts_installed", False):
        return chain.send
    chain.actor = None
    chain.sim_day = 0
    chain.sim_tick = 0
    inner = chain.send

    def recording_send(*args, **kw):
        fn = args[0] if args else kw.get("contract_fn_call")
        fname = getattr(fn, "fn_name", None) or str(fn)[:40]
        actor = getattr(chain, "actor", None)
        try:
            rcpt = inner(*args, **kw)
        except Exception as e:
            if actor is not None and enabled_for(actor):
                record(actor, {"t": int(getattr(chain, "sim_tick", 0)),
                               "fn": fname, "ok": False,
                               "err": str(e)[:_ERR_CHARS]})
            raise
        if actor is not None and enabled_for(actor):
            record(actor, {"t": int(getattr(chain, "sim_tick", 0)),
                           "fn": fname, "ok": True})
        return rcpt

    chain.send = recording_send
    chain._acts_installed = True
    return recording_send


def enabled_for(agent) -> bool:
    if MODE == "none":
        return False
    if MODE == "telemetry":
        try:
            return agent.telemetry_static() is not None
        except Exception:
            return False
    return True


def record(agent, entry: dict) -> None:
    if MODE == "none":
        return
    agent.__dict__.setdefault("_acts", []).append(entry)


def note(agent, chain, kind: str, **why) -> None:
    """An agent's own decision, with the state it decided on."""
    if not enabled_for(agent):
        return
    record(agent, {"t": int(getattr(chain, "sim_tick", 0)),
                   "kind": str(kind), "why": compact(why)})


def drain(agent) -> list:
    lst = agent.__dict__.get("_acts")
    if not lst:
        return []
    agent.__dict__["_acts"] = []
    return lst


def compact(why: dict) -> dict:
    """Keep the record small: floats to 6 significant digits, strings
    truncated, nested dicts flattened one level, other objects repr'd."""
    out = {}
    for k, v in why.items():
        if isinstance(v, bool) or v is None:
            out[k] = v
        elif isinstance(v, int):
            out[k] = v
        elif isinstance(v, float):
            out[k] = float(f"{v:.6g}")
        elif isinstance(v, str):
            out[k] = v[:_STR_CHARS]
        elif isinstance(v, dict):
            out[k] = {str(a): (float(f"{b:.6g}") if isinstance(b, float) else b)
                      for a, b in list(v.items())[:12]}
        else:
            out[k] = repr(v)[:_STR_CHARS]
    return out
