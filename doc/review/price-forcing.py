"""Does the lab press outside prices onto the basket's pools hard enough?

In the lab the outside price of a TOKEN is its TOKEN/USDC pool, which the feed driver pins to
the feed once a step. One arbitrageur, a triangle cycle per TOKEN (TOKEN/BUCK, TOKEN/USDC,
BUCK/USDC), carries that price to the basket's pools. It is capped per cycle and per step, it
stops inside the three pools' fees, and its own trades push the TOKEN/USDC pool off the feed.

`deep` models an outside market too deep to move: after every pass of the arbitrageur the
TOKEN/USDC pools are pinned to the feed again, until a pass finds no cycle. Each profile runs
with the lab's rule and with maintenance only, as the lab has it and deep.

Run inside the Nix shell with the Python environment active:
    python doc/review/price-forcing.py /tmp/forcing [jobs]
"""
import json, math, multiprocessing, sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

PROFILES = ("shock", "three-phase", "commodities")
PASSES = 24                                     # most a deep market is given in one phase


def run(profile_name, deep, maintenance, out):
    from alberta_buck.sim.control_lab import engine
    from alberta_buck.sim.control_lab.block_pairs import PROFILES as LAB, simulate_observed
    from alberta_buck.sim.control_lab.model import load_config, validate
    cfg = load_config("doc/control-lab.toml")
    cfg["feed"]["walk_amplification"] = 0; cfg["bootstrap"]["basket_warmup_days"] = 120
    profile = next(p for p in [*LAB, *cfg["profiles"]] if p["name"] == profile_name)
    validate(cfg)
    drive, arbitrage = engine.World.drive, engine.World.arbitrage

    def remembering_drive(self, target, *, replay=None):
        self.feed_now = target
        return drive(self, target, replay=replay)

    def deep_arbitrage(self, phase="arbitrage"):
        for _ in range(PASSES):
            before = self.counters["arb_cycles"]
            arbitrage(self, phase)
            if self.counters["arb_cycles"] == before:
                break
            drive(self, self.feed_now)          # the outside market does not stay moved

    if deep:
        engine.World.drive, engine.World.arbitrage = remembering_drive, deep_arbitrage
    try:
        result = simulate_observed(cfg, profile, "basket", maintenance_only=maintenance)
    finally:
        engine.World.drive, engine.World.arbitrage = drive, arbitrage
    Path(out, name(profile_name, deep, maintenance)).write_text(json.dumps(dict(frames=result["frames"], summary=result["summary"])))
    return profile_name, deep, maintenance, result["summary"]["seconds"]


def name(profile, deep, maintenance):
    return f"{profile}-{'deep' if deep else 'lab'}-{'maintenance' if maintenance else 'policy'}.json"


def errors(frames):
    """Median and 90th centile of |inside - feed| for a TOKEN's dollar price and for a pair's ratio, log %."""
    own, pair = [], []
    for f in frames[4:]:
        inside = [tb * f["buck_usdc"] for tb in f["token_buck"]]
        own += [abs(100 * math.log(i / t)) for i, t in zip(inside, f["target_usd"])]
        pair += [abs(100 * (math.log(f["token_buck"][a] / f["token_buck"][b]) - math.log(f["target_usd"][a] / f["target_usd"][b])))
                 for a, b in ((0, 1), (0, 2), (1, 2))]
    q = lambda v, p: sorted(v)[int(p * (len(v) - 1))]
    return q(own, .5), q(own, .9), q(pair, .5), q(pair, .9)


def report(out):
    sys.path.insert(0, ".")
    from alberta_buck.sim.control_lab.book import holdings, reference_value
    load = lambda p, deep, m: json.loads(Path(out, name(p, deep, m)).read_text())
    print(f"{'prices':12s} {'market':6s} | inside against the feed: TOKEN median, 90%; pair median, 90% | {'gain':>8s} {'transfers':>9s} {'arb cycles':>10s} {'arb profit':>10s}")
    for p in PROFILES:
        for deep in (False, True):
            policy, still = load(p, deep, False), load(p, deep, True)
            e = errors(policy["frames"])
            gain = reference_value(policy["frames"][-1]) - reference_value(still["frames"][-1])
            transfers = sum(e_["actor"] == "basket" and e_.get("action") == "transfer" for f in policy["frames"] for e_ in f["events"])
            print(f"{p:12s} {'deep' if deep else 'lab':6s} | {e[0]:23.2f}% {e[1]:5.2f}% {e[2]:11.2f}% {e[3]:5.2f}% | {gain:+8,.0f} {transfers:9d}"
                  f" {policy['summary']['arb_cycles']:10d} {policy['summary']['arb_profit_buck']:10,.0f}")
    at = lambda frames, day: next(f for f in frames if f["day"] == day)
    print("\nshock: TOKEN A through the jump at day 45, as the lab has it and with a deep outside market")
    print(f"  {'day':>5s} {'feed':>6s} | lab: inside  A's share  desire A>B  basket             | deep: inside  A's share  desire A>B  basket")
    lab, deep = load("shock", False, False)["frames"], load("shock", True, False)["frames"]
    for f, g in zip(lab, deep):
        if not 44.75 <= f["day"] <= 48:
            continue
        cell = lambda x: (f"{x['token_buck'][0] * x['buck_usdc']:11.1f} {x['basket']['weights'][0] / 100:10.2f} {x['pair_policy']['desire'][0][1]:11.2f}  "
                          + "; ".join(",".join(f"{t['side']} {'ABC'[t['token']]} {t['quote_amount']:,.0f}" for t in e["trades"])
                                      for e in x["events"] if e["actor"] == "basket" and e.get("action") == "transfer").ljust(19))
        print(f"  {f['day']:5.2f} {f['target_usd'][0]:6.1f} | {cell(f)}| {cell(g)}")
    for label, frames in (("lab", lab), ("deep", deep)):
        still = load("shock", label == "deep", True)["frames"]
        sales = [(f["day"], t) for f in frames if 45 <= f["day"] < 55 for e in f["events"] if e["actor"] == "basket" and e.get("action") == "transfer"
                 for t in e["trades"] if t["side"] == "sell" and t["token"] == 0]
        across = lambda fr: reference_value(at(fr, 55.)) - reference_value(at(fr, 44.75))
        liquid = lambda f: holdings(f)[0][0] - f["basket"]["idle"][0]
        print(f"  {label:5s} sales of A in the shock {len(sales)}, {sum(t['quote_amount'] for _, t in sales):,.0f} BUCKs, first at day {sales[0][0] if sales else '-'};"
              f" peak share {max(f['basket']['weights'][0] for f in frames if 45 <= f['day'] < 55) / 100:.2f};"
              f" pools shed {liquid(at(frames, 44.75)) - min(liquid(f) for f in frames if 45 <= f['day'] <= 46.5):.0f} TOKEN A;"
              f" gain across the shock over maintenance {across(frames) - across(still):+,.0f}")


if __name__ == "__main__":
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    jobs = [(p, deep, m, str(out)) for p in PROFILES for deep in (False, True) for m in (False, True)
            if not Path(out, name(p, deep, m)).exists()]
    with ProcessPoolExecutor(max_workers=int(sys.argv[2]) if len(sys.argv) > 2 else 4, max_tasks_per_child=1,
                             mp_context=multiprocessing.get_context("spawn")) as pool:
        for done in as_completed([pool.submit(run, *job) for job in jobs]):
            print(*done.result(), flush=True)
    report(out)
