"""What the basket does with a TOKEN that jumps 30% for ten days.

The lab's `shock` profile steps TOKEN A up by 30% of its base from day 45 to day 55, on top of
the three-phase cycle. This runs it with the basket alone under the lab's rule and with one
thing changed at a time (the lean, the pace, urgency), then prints:

  1. for each rule, what it sold of TOKEN A while the shock lasted and what it earned across it;
  2. for the lab's own rule, every turn of the shock: the pair signals, the policy's desire, the
     TOKEN A the basket holds (in liquidity and in its wallet), and A's share of the book beside
     the share the same TOKEN would have had in a wallet.

Before the lean and the steady release were built the basket sold the jumped TOKEN once, for
about 10,500 BUCKs. "band, no lean" is the nearest arm to that rule that the contracts still have.

Run inside the Nix shell with the Python environment active:
    python doc/review/shock-response.py /tmp/shock
"""
import json, math, multiprocessing, sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path


LAB = "lab"
ARMS = {                                        # None: maintenance only; otherwise what to change in the lab's rule
    "maintenance":      None,
    LAB:                dict(),
    "no lean":          dict(basket=dict(tilt=0., lean_cap_bp=0)),
    "tilt 1":           dict(basket=dict(tilt=1.)),
    "half in 12 hours": dict(pair=dict(closure_rate=1.386294, max_rate=1.386294)),
    "2-point band":     dict(pair=dict(slack=0., patience=0.)),
    "band, no lean":    dict(pair=dict(slack=0., patience=0.), basket=dict(tilt=0., lean_cap_bp=0)),
}
BEFORE, AFTER = 44.75, 55.                      # the last frame before the jump, the first after the drop


def run(arm, out):
    from alberta_buck.sim.control_lab.block_pairs import simulate_observed
    from alberta_buck.sim.control_lab.model import load_config, validate
    cfg = load_config("doc/control-lab.toml")
    cfg["feed"]["walk_amplification"] = 0; cfg["bootstrap"]["basket_warmup_days"] = 120
    profile = next(p for p in cfg["profiles"] if p["name"] == "shock")
    spec = ARMS[arm]
    for section, changes in (spec or {}).items():
        cfg[section].update(changes)
    validate(cfg)
    result = simulate_observed(cfg, profile, "basket", maintenance_only=spec is None)
    Path(out, f"{arm.replace(' ', '-').replace('/', '-').replace(',', '')}.json").write_text(json.dumps(result["frames"]))
    return arm, result["summary"]["seconds"]


def frames(out, arm):
    return json.loads(Path(out, f"{arm.replace(' ', '-').replace('/', '-').replace(',', '')}.json").read_text())


def at(run, day):
    return next(f for f in run if f["day"] == day)


def report(out):
    sys.path.insert(0, ".")
    from alberta_buck.sim.control_lab.book import holdings, reference_value
    runs = {a: frames(out, a) for a in ARMS}
    still = reference_value(at(runs["maintenance"], AFTER)) - reference_value(at(runs["maintenance"], BEFORE))
    print(f"TOKEN A sold by transfer from day 45 to day 55, and the gain across the shock (day {BEFORE} to {AFTER}) over maintenance")
    print(f"  {'rule':16s} {'sales':>5s} {'BUCKs':>8s} {'TOKEN A':>8s} {'first':>6s} {'last':>6s} {'gain':>8s}   TOKEN A held before / at the end of the shock")
    for arm, run_ in runs.items():
        sales = [(f["day"], t) for f in run_ if 45 <= f["day"] < 55 for e in f["events"]
                 if e["actor"] == "basket" and e.get("action") == "transfer" for t in e["trades"] if t["side"] == "sell" and t["token"] == 0]
        gain = reference_value(at(run_, AFTER)) - reference_value(at(run_, BEFORE)) - still
        first = f"{sales[0][0]:6.2f} {sales[-1][0]:6.2f}" if sales else f"{'':13s}"
        print(f"  {arm:16s} {len(sales):5d} {sum(t['quote_amount'] for _, t in sales):8,.0f} {sum(t['token_amount'] for _, t in sales):8.1f} {first} {gain:+8,.0f}"
              f"   {holdings(at(run_, BEFORE))[0][0]:7.1f} / {holdings(at(run_, 54.75))[0][0]:7.1f}")
    lab = runs[LAB]
    start = holdings(at(lab, BEFORE))[0]
    print(f"\n{LAB}, turn by turn. A/B and A/C: log price above the accepted reference, %, and its rate, % a day, with the allowance's ends.")
    print("  day   | A/B level  rate   low   high | A/C level  rate   low   high | desire A>B A>C | urgency A/B/C  | TOKEN A: held  liquidity wallet |"
          " A's share  in a wallet | basket")
    for f in lab:
        if not 44.5 <= f["day"] <= 56.5:
            continue
        ab, ac = (next(p for p in f["pair_signal"]["pairs"] if p["tokens"] == [0, b]) for b in (1, 2))
        tokens = holdings(f)[0]
        wallet = [q * p for q, p in zip(start, f["token_buck"])]
        events = "; ".join(e.get("action", "") + ": " + ", ".join(f"{t['side']} {'ABC'[t['token']]} {t['quote_amount']:,.0f}" for t in e["trades"])
                           for e in f["events"] if e["actor"] == "basket" and e["trades"])
        print(f"  {f['day']:5.2f} | {100 * ab['level_log']:8.2f} {ab['rate_bp_day'] / 100:6.2f} {ab['low_bp_day'] / 100:6.2f} {ab['high_bp_day'] / 100:6.2f} |"
              f" {100 * ac['level_log']:8.2f} {ac['rate_bp_day'] / 100:6.2f} {ac['low_bp_day'] / 100:6.2f} {ac['high_bp_day'] / 100:6.2f} |"
              f"       {f['pair_policy']['desire'][0][1]:5.2f} {f['pair_policy']['desire'][0][2]:4.2f} | "
              + "/".join(f"{u:4.2f}" for u in f["pair_policy"]["urgency_point_days"])
              + f" | {tokens[0]:14.1f} {tokens[0] - f['basket']['idle'][0]:9.1f} {f['basket']['idle'][0]:6.1f} |"
              f" {f['basket']['weights'][0] / 100:9.2f} {100 * wallet[0] / sum(wallet):11.2f}  | {events}")
    a0, a1 = at(lab, BEFORE), at(lab, 46.5)
    shed = (holdings(a0)[0][0] - a0["basket"]["idle"][0]) - (holdings(a1)[0][0] - a1["basket"]["idle"][0])
    print(f"\nOn the way up the liquidity shed {shed:.1f} TOKEN A at a mean of {math.sqrt(a0['token_buck'][0] * a1['token_buck'][0]):.1f} BUCKs,"
          f" between {a0['token_buck'][0]:.1f} and {a1['token_buck'][0]:.1f}.")
    for name, day in (("before the jump", BEFORE), ("after the jump", 45.), ("before the drop", 54.75), ("after the drop", AFTER)):
        print(f"  value at reference {name:16s} maintenance {reference_value(at(runs['maintenance'], day)):11,.0f}   {LAB} {reference_value(at(lab, day)):11,.0f}")


if __name__ == "__main__":
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    jobs = [a for a in ARMS if not Path(out, f"{a.replace(' ', '-').replace('/', '-').replace(',', '')}.json").exists()]
    with ProcessPoolExecutor(max_workers=int(sys.argv[2]) if len(sys.argv) > 2 else 4, max_tasks_per_child=1,
                             mp_context=multiprocessing.get_context("spawn")) as pool:
        for done in as_completed([pool.submit(run, a, str(out)) for a in jobs]):
            print(*done.result(), flush=True)
    report(out)
