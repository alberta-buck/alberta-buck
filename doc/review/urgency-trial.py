"""Would urgency that builds up beat the fixed allocation band?

This is the trial that was run before the rule was built into PairRebalancePolicy, kept
as its record and as an independent check on the built rule (the sizing campaign's
patient arms). It runs a policy with no patience, so the basket's band decides.

With a band, a constituent may be rebalanced only while its share is more than a fixed
distance (2 points) from target. This trial replaces that test, per constituent, with

    urgency <- clamp(urgency + (|gap| - slack) x days, 0, patience)     reset when the gap changes side
    may be rebalanced while urgency = patience

on the real contracts, without changing them: before each wheel turn the script reads
the basket's shares, updates the three urgencies itself, and sets the contract's one
band just under the smallest urgent gap (or out of reach when none is urgent). That is
exact unless a constituent that is not urgent has a larger gap than one that is; those
turns are counted as `leaks`. Two rules for what happens after a transfer are tried:

    latch   nothing; the constituent stays eligible until its gap falls under the slack
    fire    the donor and every constituent short of target start again from zero

Every arm runs all actors for 120 days on the lab's chosen sizing, so the result is
comparable with the saved all-actors sizing campaign (maintenance, 2-, 1- and half-point
bands), which this reads for its baselines. `null` drives the 2-point band through the
same machinery and must reproduce that campaign's run.

Run inside the Nix shell with the Python environment active. The baselines come from
    make control-lab-basket-sizing CONTROL_LAB_BASKET_SIZING_STAGE=all \
        CONTROL_LAB_BASKET_SIZING_OUT=doc/control-lab/basket-sizing/all-actors \
        CONTROL_LAB_BASKET_SIZING_SIZINGS=maintenance,first,own-4,own-4-1pt,own-4-half,own-4-p1,own-4-p2,own-4-p4,own-4-p8
and then
    python doc/review/urgency-trial.py /tmp/urgency [jobs]
Each world runs in its own process: the script patches the lab's engine, and a reused
worker would stack one arm's rule on the next.
"""
import json, multiprocessing, sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

TARGETS = [3333, 3333, 3334]
OUT_OF_REACH = 10000                            # a band no share can exceed
SLACK = 25                                      # bp: a quarter point
BASELINE = Path("doc/control-lab/basket-sizing/all-actors/scores.json")
PROFILES = ("three-phase", "two-step", "commodities")
ARMS = {"null": dict(rule="band")}
for rule in ("latch", "fire"):
    for patience in (1, 2, 4, 8):                # point-days
        ARMS[f"{rule}-{patience}"] = dict(rule=rule, patience=100. * patience)


def run(profile_name, arm, out):
    from alberta_buck.sim.control_lab import engine
    from alberta_buck.sim.control_lab.basket_sizing import FLAT, TWO_STEP, score
    from alberta_buck.sim.control_lab.block_pairs import PROFILES as LAB, simulate_observed
    from alberta_buck.sim.control_lab.model import load_config, own_path, validate
    spec = ARMS[arm]
    cfg = load_config("doc/control-lab.toml")
    cfg["feed"]["walk_amplification"] = 0; cfg["bootstrap"]["basket_warmup_days"] = 120
    cfg["pair"].update(slack=0., patience=0.)      # a band policy: the script, not the contract, keeps the count
    profiles = {p["name"]: p for p in [*LAB, TWO_STEP, *(p for p in cfg["profiles"] if own_path(p))]}
    if profile_name in FLAT:
        cfg["feed"]["amplitude"] = 0
    validate(cfg)
    urgency, side, trace = [0.] * 3, [0] * 3, []
    original_run = engine.World.run

    def world_run(self, rows):
        d, observe, days = self.d, self.before_policy, 1 / self.cfg["ticks_per_day"]

        def before_policy():
            fired = {int(p["donor"]) for p in self.pair_plans}          # the turn before this one
            observe()
            weights = [int(w) for w in d.basket.functions.responseWeightsBp().call()]
            gap = [weights[i] - TARGETS[i] for i in range(3)]
            if spec["rule"] == "band":
                band, urgent, leak = 200, [abs(g) > 200 for g in gap], False
            else:
                for i in range(3):
                    now = (gap[i] > 0) - (gap[i] < 0)
                    if now != side[i] or (spec["rule"] == "fire" and fired and (i in fired or gap[i] < 0)):
                        urgency[i] = 0.
                    side[i] = now
                    urgency[i] = min(spec["patience"], max(0., urgency[i] + (abs(gap[i]) - SLACK) * days))
                urgent = [u >= spec["patience"] for u in urgency]
                band = max(0, min(abs(gap[i]) for i in range(3) if urgent[i]) - 2) if any(urgent) else OUT_OF_REACH
                leak = any(not urgent[i] and abs(gap[i]) >= band for i in range(3))
            params = list(d.basket.functions.eq().call())
            if params[6] != band or spec["rule"] == "band":
                params[6] = band
                self.chain.send(d.basket.functions.setEquityParams(tuple(params)), sender=d.gov)
            trace.append(dict(gap=gap, urgency=[round(u, 2) for u in urgency], urgent=urgent, band=band, leak=leak))

        self.before_policy = before_policy
        return original_run(self, rows)

    engine.World.run = world_run
    try:
        result = simulate_observed(cfg, profiles[profile_name], "all")
    finally:
        engine.World.run = original_run            # a reused worker must not stack one arm's rule on the next
    turns = len(trace)
    s = score(result)
    s.update(profile=profile_name, arm=arm, seconds=result["summary"]["seconds"], turns=turns,
             open_share=sum(any(t["urgent"]) for t in trace) / turns, leaks=sum(t["leak"] for t in trace),
             gas=result["summary"]["gas"], fallbacks=result["summary"].get("fallbacks"))
    Path(out, f"{profile_name}-{arm}.json").write_text(json.dumps(dict(score=s, trace=trace)))
    return s


def report(out):
    base = json.loads(BASELINE.read_text())
    rows = {p: {} for p in PROFILES}
    for f in sorted(Path(out).glob("*.json")):
        s = json.loads(f.read_text())["score"]
        rows[s["profile"]][s["arm"]] = s
    for profile, arms in rows.items():
        still = base[profile]["maintenance"]["reference"]
        print(f"\n{profile}: gain at reference over maintenance")
        print(f"  {'rule':14s} {'gain':>9s} {'transfers':>9s} {'per transfer':>12s} {'moved':>10s} {'open':>6s} {'leaks':>5s}")
        shown = [(f"band {name}", base[profile][key], None) for name, key in
                 (("2 points", "own-4"), ("1 point", "own-4-1pt"), ("half point", "own-4-half"))]
        shown += [(f"built p{n}", base[profile][f"own-4-p{n}"], None) for n in (1, 2, 4, 8) if f"own-4-p{n}" in base[profile]]
        shown += [(arm, arms[arm], arms[arm]) for arm in ARMS if arm in arms]
        for name, s, mine in shown:
            gain = s["reference"] - still
            each = f"{gain / s['transfers']:+12,.0f}" if s["transfers"] else f"{'':12s}"
            tail = f"{100 * mine['open_share']:5.0f}% {mine['leaks']:5d}" if mine else ""
            print(f"  {name:14s} {gain:+9,.0f} {s['transfers']:9d} {each} {s['sold']:10,.0f} {tail}")


if __name__ == "__main__":
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    jobs = [(p, a, str(out)) for p in PROFILES for a in ARMS
            if (a != "null" or p == "three-phase") and not (out / f"{p}-{a}.json").exists()]
    with ProcessPoolExecutor(max_workers=int(sys.argv[2]) if len(sys.argv) > 2 else 4, max_tasks_per_child=1,
                             mp_context=multiprocessing.get_context("spawn")) as pool:
        for done in as_completed([pool.submit(run, *job) for job in jobs]):
            s = done.result()
            print(s["profile"], s["arm"], s["seconds"], flush=True)
    report(out)
