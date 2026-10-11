"""Matched sizings of the pair policy's release: how much of an open gap to move, and how soon.

Every arm of a profile shares its feed, opening book and market rules. Only the
PairRebalancePolicy.Sizing differs; the maintenance arm takes no view on prices.
"""
import argparse
import copy
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import math
import multiprocessing
from pathlib import Path

from .basket_research import HISTORICAL
from .block_pairs import PROFILES, simulate_observed
from .book import holdings, reference_value
from .cli import atomic_json, fingerprint
from .model import DEFAULTS, STAGES, dependencies, digest, feed_rows, load_config, own_path, validate

# TOKEN A reprices upward twice and never returns: the case rebalancing loses.
TWO_STEP = dict(name="two-step", perturbations=[
    dict(kind="ramp", tokens=[0], amplitude=.15, start=10, duration=30),
    dict(kind="ramp", tokens=[0], amplitude=.15, start=60, duration=30),
    dict(kind="noise", amplitude=.002, independent=True)])
AVAILABLE = {p["name"]: p for p in [*PROFILES, HISTORICAL, TWO_STEP]}
FLAT = {"flat-noise80", "two-step"}             # no three-phase cycle beneath the perturbations


def half_life(days):
    """The remaining gap halves in so many days once a turn is clear; nothing is fitted to a speed."""
    rate = round(math.log(2) / days, 6)
    return dict(release_speed=0., release_rate=4., closure_gain=0, closure_rate=rate, max_rate=rate)


def patient(point_days, slack_points=.25):
    """Eligibility by urgency: a gap beyond the slack builds it, and so many point-days make it eligible."""
    return dict(slack=slack_points / 100, patience=point_days / 100)


def lean(tilt):
    """How far a target leans against price, held to 15% of the declared weight."""
    return dict(tilt=tilt, lean_cap_bp=1500 if tilt else 0)


# An arm without `pair` is the maintenance control. `pair = {}` is the first experiment's sizing.
# Which gaps may be worked at all is decided by the allocation band or, in a patient arm, by urgency
# that builds with the gap and time. `lab` is the configuration's own rule; each other arm changes
# one thing about it.
LAB = {**half_life(1), **patient(2)}
SIZINGS = {
    "maintenance": dict(title="Maintenance only"),
    "first": dict(title="First experiment: 10%/day, halved at 20 bp/day, no lean", pair={}, basket=lean(0)),
    "lab": dict(title="The lab: half the gap a day, patience 2, tilt 1/2", pair=LAB, basket=lean(.5)),
    "tilt-0": dict(title="No lean", pair=LAB, basket=lean(0)),
    "tilt-quarter": dict(title="Tilt 1/4", pair=LAB, basket=lean(.25)),
    "tilt-1": dict(title="Tilt 1", pair=LAB, basket=lean(1)),
    "half-12h": dict(title="Half the gap in 12 hours", pair={**half_life(.5), **patient(2)}, basket=lean(.5)),
    "half-2d": dict(title="Half the gap in 2 days", pair={**half_life(2), **patient(2)}, basket=lean(.5)),
    "patience-1": dict(title="Patience 1 point-day", pair={**half_life(1), **patient(1)}, basket=lean(.5)),
    "patience-4": dict(title="Patience 4 point-days", pair={**half_life(1), **patient(4)}, basket=lean(.5)),
    "parity-0": dict(title="No lean against parity", pair=LAB, basket={**lean(.5), "parity_gain": 0.}),
    "band-2pt": dict(title="A 2-point band in place of urgency", pair=half_life(1), basket={**lean(.5), "weight_band_bp": 200}),
    "band-2pt-tilt-0": dict(title="A 2-point band, no lean", pair=half_life(1), basket={**lean(0), "weight_band_bp": 200}),
}


def worker(cfg, profile, stage, arm, key, code, path):
    result = simulate_observed(cfg, profile, stage, maintenance_only="pair" not in SIZINGS[arm])
    result.update(cache_key=key, implementation=code, sizing=arm)
    atomic_json(path, result)
    return dict(profile=profile["name"], sizing=arm, cache_key=key, file=Path(path).name, summary=result["summary"])


def score(result):
    """One run's ending values, its sales and purchases, and its value at each cycle boundary."""
    frames = result["frames"]
    first, last = frames[0], frames[-1]
    trades = sold = bought = 0
    for f in frames:
        for e in f["events"]:
            if e["actor"] == "basket" and e.get("action") in ("sell", "buy"):
                trades += 1
                sold += sum(t["quote_amount"] for t in e["trades"] if t["side"] == "sell")
                bought += sum(t["quote_amount"] for t in e["trades"] if t["side"] == "buy")
    targets = [3333, 3333, 3334]
    error = sum(sum(abs(w - t) for w, t in zip(f["basket"]["weights"], targets)) / 3 for f in frames) / len(frames)
    cycle = round(result["config"]["feed"]["period_days"] * result["config"]["ticks_per_day"])
    tokens, buck = holdings(last)
    return dict(reference=reference_value(last), opened=reference_value(first),
                untouched=reference_value(last, holdings(first)),
                usd=last["basket"]["equity"] * last["buck_usdc"], buck=last["basket"]["equity"],
                buck_usdc=last["buck_usdc"], reference_usd=last["reference"]["target_usd"],
                trades=trades, sold=sold, bought=bought, error_pp=error / 100,
                tokens=sum(tokens), net_buck=buck, arb=last["cumulative"]["arb_profit_buck"],
                mean_abs_parity=result["summary"]["mean_abs_parity_error"], end_k=result["summary"]["end_k"],
                cycles=[reference_value(f) for f in frames[::cycle]])


def render(root):
    root = Path(root)
    m = json.loads((root / "manifest.json").read_text())
    scores = {name: {} for name in m["profiles"]}
    for e in m["entries"].values():
        scores[e["profile"]][e["sizing"]] = score(json.loads((root / e["file"]).read_text()))
    atomic_json(root / "scores.json", scores)
    arms = [a for a in SIZINGS if a in m["sizings"]]
    windows = len(next(iter(scores.values()))["maintenance"]["cycles"]) - 1
    cfg = m["requested_config"]
    report = ["#+title: Sizing the pair basket's release", "#+options: toc:2 num:nil", "", m["method"], "",
        "* The sizings", "",
        "Rates are fractions of the remaining eligible gap per day (0.693 halves it each day); speeds are log "
        "returns per day. The braking release is =release_rate × release × braking=, where =release= halves at "
        "=release_speed= of outward motion and =braking= compares the rate's change with its allowance. With "
        "=release_speed = 0= the allowance itself is the scale. Closure adds =closure_gain= per unit of confirmed "
        "closing speed, or =closure_rate= times its confidence, and =max_rate= caps the sum. A donor moves "
        "1 - exp(-rate × time) of its gap, however often it is asked. Which gaps may be worked at all is decided "
        "by the allocation band or, in a patient arm, by urgency: a constituent counts its distance from target "
        "beyond the slack over time, is eligible once the count reaches the patience, and stays so until its gap "
        "is back inside the slack. A target leans against price by the tilt, up to the cap. Every other limit "
        "(step, pool depth, minimum size, credit) is unchanged.", "",
        "| Sizing | release_rate | release_speed | closure_gain | closure_rate | max_rate | Tilt | Allocation band, bp | "
        "Slack, points | Patience, point-days |",
        "|--------+--------------+---------------+--------------+--------------+----------+------+---------------------+"
        "---------------+----------------------|"]
    for arm in arms:
        if "pair" in SIZINGS[arm]:
            p = {**DEFAULTS["pair"], **SIZINGS[arm]["pair"]}
            basket = {**cfg["basket"], **SIZINGS[arm].get("basket", {})}
            gate = f"| | {100 * p['slack']:g} | {100 * p['patience']:g}" if p["patience"] else f"| {basket['weight_band_bp']} | |"
            report.append(f"| {arm} | {p['release_rate']:g} | {p['release_speed']:g} | {p['closure_gain']} | "
                          f"{p['closure_rate']:g} | {p['max_rate']:g} | {basket['tilt']:g} {gate} |")
    report += ["", "The band arms are compared with the same maintenance control. Maintenance pairs what waits "
        "and keeps the leash and the ceiling, and works no gap on the policy's word."]
    report += ["", "* Ending value against the maintenance control", "",
        "/At reference/ values TOKEN at the external feed and net BUCK at the reference basket's cost, so the "
        "stage's own BUCK/USDC drift does not enter. USD converts the contract's BUCK equity at the ending "
        "BUCK/USDC spot. All three are marks: caller gas and liquidation are not charged. "
        "Differences are against the maintenance arm of the same profile. Where BUCK trades far from the "
        "reference basket's cost, the reference mark favours whichever arm ends with more net BUCK when BUCK is "
        "cheap and penalizes it when BUCK is dear; read the three units together.", "",
        "| Profile | Sizing | Trades | TOKEN sold, BUCK | At reference | vs maintenance | Per trade | "
        "USD vs maintenance | BUCK vs maintenance | BUCK/USDC | Allocation error, pp |",
        "|---------+--------+--------+------------------+--------------+----------------+-----------+"
        "--------------------+---------------------+-----------+----------------------|"]
    for profile, group in scores.items():
        base = group["maintenance"]
        for arm in arms:
            s = group[arm]
            gain = s["reference"] - base["reference"]
            each = f"{gain / s['trades']:+,.0f}" if s["trades"] else ""
            report.append(f"| {profile} | {arm} | {s['trades']} | {s['sold']:,.0f} | {s['reference']:,.0f} | "
                          f"{gain:+,.0f} | {each} | {s['usd'] - base['usd']:+,.0f} | "
                          f"{s['buck'] - base['buck']:+,.0f} | {s['buck_usdc']:.4f} | {s['error_pp']:.3f} |")
    report += ["", "* What holding still would have done", "",
        "The opening book, never touched, revalued at each profile's ending feed and reference. "
        "Maintenance earns LP fees and relief on top of it; a sizing must be judged against maintenance.", "",
        "| Profile | Opening value | Untouched at the end | Maintenance at the end | Reference basket, USD |",
        "|---------+---------------+----------------------+------------------------+-----------------------|"]
    for profile, group in scores.items():
        s = group["maintenance"]
        report.append(f"| {profile} | {s['opened']:,.0f} | {s['untouched']:,.0f} | {s['reference']:,.0f} | "
                      f"{s['reference_usd']:.4f} |")
    period = cfg["feed"]["period_days"]
    report += ["", "* Is the gain repeated, cycle after cycle?", "",
        f"Gain at reference over the maintenance arm within each {period:g}-day window. A one-time repositioning "
        "would appear in the first window only.", "",
        "| Profile | Sizing | " + " | ".join(f"Days {round(i * period)}–{round((i + 1) * period)}"
                                             for i in range(windows)) + " |",
        "|---------+--------+" + "+".join("-----------" for _ in range(windows)) + "|"]
    for profile, group in scores.items():
        base = group["maintenance"]["cycles"]
        for arm in arms[1:]:
            c = group[arm]["cycles"]
            report.append(f"| {profile} | {arm} | " + " | ".join(
                f"{(c[i + 1] - c[i]) - (base[i + 1] - base[i]):+,.0f}" for i in range(len(c) - 1)) + " |")
    report += ["", "* The book each arm ends with", "",
        "Every TOKEN is paired with issued BUCKs from the first turn. Net BUCK is what the book has let out (below "
        "zero) or holds beyond its lien (above): sales raise it and purchases lower it.", "",
        "| Profile | Sizing | TOKEN held | Net BUCK | Mean abs parity error, % | Ending K | Arb profit, BUCK |",
        "|---------+--------+------------+----------+--------------------------+----------+------------------|"]
    for profile, group in scores.items():
        for arm in arms:
            s = group[arm]
            report.append(f"| {profile} | {arm} | {s['tokens']:,.0f} | {s['net_buck']:+,.0f} | "
                          f"{100 * s['mean_abs_parity']:.3f} | {s['end_k']:.4f} | {s['arb']:,.0f} |")
    (root / "results.org").write_text("\n".join(report) + "\n")
    print(f"Rendered {len(m['entries'])} EVM runs -> {root / 'results.org'}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="doc/control-lab.toml")
    ap.add_argument("--out", default="doc/control-lab/basket-sizing/current")
    ap.add_argument("--profiles", default="three-phase,noise20,noise80,flat-noise80,historical-bridge,two-step,commodities")
    ap.add_argument("--sizings", default=",".join(SIZINGS))
    ap.add_argument("--stage", choices=("basket", "k", "all"), default="basket")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--ticks-per-day", type=int)
    ap.add_argument("--render-only", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    if args.render_only:
        return render(root)
    if args.jobs < 1 or args.days < 1:
        ap.error("jobs and days must be positive")
    names, arms = args.profiles.split(","), args.sizings.split(",")
    if len(set(arms)) != len(arms) or any(a not in SIZINGS for a in arms) or "maintenance" not in arms:
        ap.error("sizings must be distinct, known, and include the maintenance control")
    from alberta_buck.sim.chain import load_artifact
    cfg = load_config(args.config)
    # The configuration's complete price paths (the commodity profile) join this campaign's own.
    available = {**{p["name"]: p for p in cfg["profiles"] if own_path(p)}, **AVAILABLE}
    if len(set(names)) != len(names) or any(n not in available for n in names):
        ap.error("unknown or duplicate profile")
    cfg["days"] = args.days
    cfg["feed"]["walk_amplification"] = 0
    cfg["bootstrap"]["basket_warmup_days"] = 120
    if args.ticks_per_day:
        cfg["ticks_per_day"] = args.ticks_per_day
    code = {**fingerprint(), "basket_sizing": digest(dict(source=Path(__file__).read_text(),
        book=Path(__file__).with_name("book.py").read_text(), control=load_artifact("BasketUpkeepMaintenanceControl")))}
    m = dict(schema=1, kind="basket-sizing", status="running", requested_config=cfg, implementation=code,
        market_stage=args.stage, flags=STAGES[args.stage], profiles=names, sizings={a: SIZINGS[a]["title"] for a in arms},
        entries={},
        method=f"{args.days}-day matched real-EVM runs at stage {args.stage}, {cfg['ticks_per_day']} market steps a day: "
               "one maintenance control and several sizings of the pair policy's release for each price profile. "
               "Same 120-day mean-price prelude and opening prices in every arm; arbitrage income stays external.")
    pending = []
    for name in names:
        profile = available[name]
        for arm in arms:
            selected = copy.deepcopy(cfg)
            selected["pair"] = {**DEFAULTS["pair"], **SIZINGS[arm].get("pair", {})}
            selected["basket"].update(SIZINGS[arm].get("basket", {}))
            if name in FLAT:
                selected["feed"]["amplitude"] = 0
            validate(selected)
            feed_rows(selected, profile)
            key = digest(dict(inputs=dependencies(selected, profile, args.stage), arm=arm, implementation=code))
            path = root / f"{name}-{arm}-{key[:16]}.json"
            if path.exists():
                r = json.loads(path.read_text())
                assert r["cache_key"] == key and len(r["frames"]) == args.days * cfg["ticks_per_day"] + 1
                m["entries"][key] = dict(profile=name, sizing=arm, cache_key=key, file=path.name, summary=r["summary"])
            else:
                pending.append((selected, profile, args.stage, arm, key, code, str(path)))
    atomic_json(root / "progress.json", m)
    with ProcessPoolExecutor(max_workers=args.jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
        for done in as_completed([pool.submit(worker, *job) for job in pending]):
            e = done.result()
            m["entries"][e["cache_key"]] = e
            atomic_json(root / "progress.json", m)
            print(e["profile"], e["sizing"], e["summary"]["seconds"], flush=True)
    m["status"] = "complete"
    atomic_json(root / "manifest.json", m)
    render(root)


if __name__ == "__main__":
    main()
