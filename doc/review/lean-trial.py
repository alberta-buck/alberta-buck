"""How far should a target lean against price?

The basket's liquidity positions sell a TOKEN as its price rises, so a position's share moves
half as far as a wallet's. The shell's tilt leans each target the other way: a TOKEN's target
is its declared weight times (reference price / price) to the power tilt, shared out over the
book and held within the lean cap. The policy still decides when (a turn, a closing ratio) and
how fast.

    tilt 0     restore the declared weights and no more
    tilt 1/2   the lab's setting: ask for the half that liquidity damps
    tilt 1     the older design's lean

Every arm runs the basket alone for 120 days beside a maintenance run of the same profile.

Run inside the Nix shell with the Python environment active:
    python doc/review/lean-trial.py /tmp/lean [jobs]
"""
import json, multiprocessing, sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

PROFILES = ("shock", "three-phase", "noise80", "two-step", "commodities")
TILTS = (0., .25, .5, 1.)


def run(profile_name, tilt, out):
    from alberta_buck.sim.control_lab.basket_sizing import FLAT, TWO_STEP
    from alberta_buck.sim.control_lab.block_pairs import PROFILES as LAB, simulate_observed
    from alberta_buck.sim.control_lab.model import load_config, validate
    cfg = load_config("doc/control-lab.toml")
    cfg["feed"]["walk_amplification"] = 0; cfg["bootstrap"]["basket_warmup_days"] = 120
    if profile_name in FLAT:
        cfg["feed"]["amplitude"] = 0
    profile = next(p for p in [*LAB, TWO_STEP, *cfg["profiles"]] if p["name"] == profile_name)
    if tilt is not None:
        cfg["basket"].update(tilt=tilt, lean_cap_bp=1500 if tilt else 0)
    validate(cfg)
    result = simulate_observed(cfg, profile, "basket", maintenance_only=tilt is None)
    Path(out, name(profile_name, tilt)).write_text(json.dumps(result["frames"]))
    return profile_name, tilt, result["summary"]["seconds"]


def name(profile, tilt):
    return f"{profile}-{'maintenance' if tilt is None else f'tilt{tilt:g}'}.json"


def report(out):
    sys.path.insert(0, ".")
    from alberta_buck.sim.control_lab.book import holdings, reference_value
    at = lambda frames, day: next(f for f in frames if f["day"] == day)
    print(f"{'prices':12s} {'tilt':>5s} {'gain':>9s} {'transfers':>9s} {'per transfer':>12s} {'moved':>9s} {'lowest':>9s}   shares at the end")
    for p in PROFILES:
        still = json.loads(Path(out, name(p, None)).read_text())
        for tilt in TILTS:
            frames = json.loads(Path(out, name(p, tilt)).read_text())
            gain = reference_value(frames[-1]) - reference_value(still[-1])
            moved = [sum(t["quote_amount"] for t in e["trades"] if t["side"] == "sell") for f in frames for e in f["events"]
                     if e["actor"] == "basket" and e.get("action") == "transfer"]
            lowest = min(reference_value(f) - reference_value(g) for f, g in zip(frames, still))
            each = f"{gain / len(moved):+12,.0f}" if moved else f"{'':12s}"
            print(f"{p:12s} {tilt:5g} {gain:+9,.0f} {len(moved):9d} {each} {sum(moved):9,.0f} {lowest:+9,.0f}   "
                  + "/".join(f"{w / 100:.1f}" for w in frames[-1]["basket"]["weights"]))
    still = json.loads(Path(out, name("shock", None)).read_text())
    across = lambda fr: reference_value(at(fr, 55.)) - reference_value(at(fr, 44.75))
    print("\nshock: TOKEN A sold by transfer from day 45 to day 55")
    print(f"  {'tilt':>5s} {'sales':>5s} {'BUCKs':>8s} {'first':>6s} {'last':>6s} {'gain across the shock':>21s}   TOKEN A held before / at the end   A's share at the end of the shock")
    for tilt in TILTS:
        frames = json.loads(Path(out, name("shock", tilt)).read_text())
        sales = [(f["day"], t) for f in frames if 45 <= f["day"] < 55 for e in f["events"] if e["actor"] == "basket" and e.get("action") == "transfer"
                 for t in e["trades"] if t["side"] == "sell" and t["token"] == 0]
        days = (f"{sales[0][0]:6.2f} {sales[-1][0]:6.2f}" if sales else f"{'':13s}")
        print(f"  {tilt:5g} {len(sales):5d} {sum(t['quote_amount'] for _, t in sales):8,.0f} {days} {across(frames) - across(still):+21,.0f}"
              f"   {holdings(at(frames, 44.75))[0][0]:7.1f} / {holdings(at(frames, 54.75))[0][0]:7.1f}                  {at(frames, 54.75)['basket']['weights'][0] / 100:.2f}")


if __name__ == "__main__":
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    jobs = [(p, t, str(out)) for p in PROFILES for t in (None, *TILTS) if not Path(out, name(p, t)).exists()]
    with ProcessPoolExecutor(max_workers=int(sys.argv[2]) if len(sys.argv) > 2 else 4, max_tasks_per_child=1,
                             mp_context=multiprocessing.get_context("spawn")) as pool:
        for done in as_completed([pool.submit(run, *job) for job in jobs]):
            print(*done.result(), flush=True)
    report(out)
