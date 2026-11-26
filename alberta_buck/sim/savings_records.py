"""Adapt confirmed WebEVM Savings observations to the shared run recorder."""
from types import SimpleNamespace

from alberta_buck.sim.runrecord import RunRecorder


def record_frame(frame):
    sv = frame["sv"]
    return {
        "day": frame["day"], "net": {"block": frame["block"]},
        "buckK": frame["buckK"], "basketVal": frame["basketVal"],
        "buck_usd": frame["buck_usd"], "supply": int(frame["supply"]),
        "refUsd": frame["refUsd"], "spotUsdc": frame["spotUsdc"], "spotBuck": frame["spotBuck"],
        "eq_lien": int(sv["L"]), "eq_equity": int(sv["E"]), "eq_share_price": sv["sp"],
        "eq_liquidity": int(sv["Q"]), "eq_headroom": int(sv["H"]), "eq_unpaid_relief": int(sv["relief"]),
        "eq_hold": frame.get("eq_hold"), "eq_w": frame.get("eq_w"), "eq_lean": frame.get("eq_lean"),
        "eq_decl": frame["eq_decl"],
        "households": {key: int(value) for key, value in (frame["households"] or {}).items()
                       if key in ("nw", "hypo", "drawn", "basket", "basket_mark", "cash", "mortgage",
                                  "active", "exited")},
        "octl": [{**agent, **{key: int(agent[key]) for key in ("drawn", "limit", "basket", "basket_mark",
                                                               "cash", "mortgage", "nw", "nw_buck", "hypo",
                                                               "held_buck") if agent.get(key) is not None}}
                 for agent in frame["agents"]],
        "household_events": frame["events"],
        **{key: int(value) for key, value in frame.items() if key.startswith("wh_eq_")
           and value is not None},
    }


def write_record(root, run_id, scenario, spec, result):
    recorder = RunRecorder(root, "Economy · WebEVM", run_id=run_id, engine="webevm")
    meta = result["record_meta"]
    observed = SimpleNamespace(seed=scenario.seed, days=scenario.days, day_step=scenario.day_step,
                               ticks_per_day=scenario.ticks_per_day, agents=meta["cast"])
    recorder.open(scenario=observed, tokens=[token[0] for token in scenario.tokens],
                  meta={"experiment": scenario.experiment.resolved(),
                        "run": {"basket": "equity-machine"},
                        "buck_contract": {"roles": meta["contracts"], "accounting": "net"}},
                  source={"driver": "SavingsWorld", "price_digest": spec["price_digest"]})
    try:
        for frame in result["record_frames"]:
            recorder.frame(record_frame(frame))
    except BaseException as exc:
        recorder.close("failed", error=exc)
        raise
    recorder.close()
    return recorder.id
