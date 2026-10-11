"""Fast, causal sensor experiments beside existing EMA machinery.

python -m alberta_buck.sim.pair_observer_lab --jobs 4
python -m alberta_buck.sim.pair_observer_lab --render-only

No EVM, agents, trades, or ROI claims. JSON is the source of every figure.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
import hashlib
import gzip
import json
from pathlib import Path
import time

import numpy as np

from .pair_observer import ObserverConfig, ResponseConfig, PairObserver, allocation_candidates
from .pair_spectrum import historical_spectra
from .rebalance_policy import _EmaLadder, synth_prices, SYNTH_BASE, SYMS
from .control_lab.model import baseline_prices

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = (5, 10, 20, 40, 80, 160, 1280)    # the retired EMA ladder, kept here as the comparison
OUT = ROOT / "doc/control-lab/pair-observer"
CASES = ("three-phase", "changing-cycles", "numeraire-only", "own-impact",
         "impact-mismatch", "venue-dislocation", "persistent-trend",
         "shared-noise", "missing-venue", "genuine-repricing", "lagged-commodities")


def inputs(case, days, seed, noise, warmup=1280, ticks=4):
    """Plant-only truths and separate observable snapshots.

    Noise scales quote observations, not underlying commodity dynamics. The
    legacy lagged-commodity generator retains its own OU fluctuations.
    Prehistory contains baseline dynamics/noise, never the selected shock.
    """
    if case not in CASES or days < 2 or noise < 0:
        raise ValueError("invalid experiment")
    t = np.arange(-warmup * ticks, days * ticks + 1) / ticks
    means = np.array([100., 100., 100.])
    names = ["A", "B", "C"]
    if case == "lagged-commodities":
        names = list(SYMS)
        means = np.array([SYNTH_BASE[s] for s in names])
        history = np.asarray(synth_prices(seed, (warmup + days + 2) / 365))
        logp = np.array([np.interp(t + warmup, np.arange(len(history)), np.log(history[:, i]))
                         for i in range(len(names))]).T
    elif case == "changing-cycles":
        # Analytic phase integrates linearly changing frequency; amplitude
        # also changes. Prehistory continues the initial frequency/amplitude.
        start = np.array([18., 45., 80.])
        end = np.array([55., 22., 120.])
        slope = (1 / end - 1 / start) / days
        age = np.maximum(t, 0)[:, None]
        phase = 2 * np.pi * (t[:, None] / start + .5 * slope * age**2) + np.arange(3) * 2 * np.pi / 3
        amp = np.array([.06, .14, .10]) + age / days * np.array([.12, -.07, .10])
        logp = np.log(means) + np.log1p(amp * np.sin(phase))
    elif case == "numeraire-only":
        logp = np.tile(np.log(means), (len(t), 1))
    elif case == "persistent-trend":
        drift = np.maximum(t, 0)[:, None] * np.array([.0015, -.0008, .0002])
        logp = np.log(means) + drift
    else:
        cfg = {"feed": {"base": means.tolist(), "amplitude": .12, "period_days": 30}}
        logp = np.log([baseline_prices(cfg, day) for day in t])
    if case == "genuine-repricing":
        logp[:, 0] += .10 * (t >= 45)
    n = len(means)
    # Separate seeded streams preserve a path prefix if the run is extended.
    shared = np.random.default_rng(np.random.SeedSequence([seed, 0])).standard_normal((len(t), n))
    independent = np.random.default_rng(np.random.SeedSequence([seed, 1])).standard_normal((len(t), 2, n))
    rho = .9 if case == "shared-noise" else .25
    error = noise * (np.sqrt(rho) * shared[:, None, :] + np.sqrt(1 - rho) * independent)
    # Baselines can vary arbitrarily; same-venue ratios cancel each exactly.
    bases = np.zeros((len(t), 2))
    live = np.maximum(t, 0)
    if case == "numeraire-only":
        bases[:, 0] = .15 * np.sin(live / 17) + .001 * live
        bases[:, 1] = np.log(1 - .20 * (live >= 40))
    actual_impact = np.zeros((len(t), 2, n))
    predicted = np.zeros_like(actual_impact)
    uncertainty = np.zeros_like(actual_impact)
    if case in ("own-impact", "impact-mismatch"):
        # Fee-free constant-product marginal moves from known trade/reserve
        # ratios: sell A, buy B. Relaxation is a testable assumed decay model.
        kick = np.zeros(n)
        kick[:2] = [-2 * np.log1p(.03), 2 * np.log1p(.025)]
        age = np.maximum(t - 45, 0)
        on = (t >= 45)[:, None]
        actual_impact[:, 0, :] = on * np.exp(-age[:, None] / 2) * kick
        if case == "own-impact":
            predicted[:] = actual_impact  # ideal known-input ablation
        else:
            predicted[:, 0, :] = on * .7 * np.exp(-age[:, None] / 4) * kick
            uncertainty[:, 0, :] = np.abs(predicted[:, 0, :]) * .5
    if case == "venue-dislocation":
        age = np.maximum(t - 45, 0)
        actual_impact[:, 0, 0] = (t >= 45) * .08 * np.exp(-age / 3)
        # No known own transaction: no offset is supplied to the observer.
    logs = logp[:, None, :] - bases[:, :, None] + error + actual_impact
    observed = np.exp(logs)
    available = np.ones((len(t), 2), dtype=bool)
    if case == "missing-venue":
        available[(t >= 40) & (t < 50), 1] = False
        available[(t >= 60) & (t < 63), :] = False
    return dict(t=t, names=names, means=means, truth_log=logp, observed=observed,
                available=available, impact=predicted, impact_std=uncertainty,
                actual_impact=actual_impact, bases=bases, rho=rho)


def evaluate_run(case, days, seed, noise):
    data = inputs(case, days, seed, noise)
    config = ObserverConfig()  # fixed across cases, noise settings and seeds
    fused = PairObserver(data["means"], config=config)
    single = PairObserver(data["means"], venues=("BUCK",), config=config)
    raw = PairObserver(data["means"], config=config)
    historical = (data["t"] < 0) & (data["t"] % 1 == 0)
    past_logs = np.mean(np.log(data["observed"][historical]) - data["impact"][historical], axis=1)
    spectra = historical_spectra(past_logs, fused.pairs)
    cycle_configs = {pair: ResponseConfig(**s["response"]) for pair, s in zip(fused.pairs, spectra["pairs"])}
    n = len(data["means"])
    # Reuse the legacy differential EMA ladder at its actual daily cadence.
    # Initialize to known historical means, then replay 1280 full past days.
    emas = [_EmaLadder(WINDOWS) for _ in range(n)]
    for leg in emas:
        leg.update(0.)
    matched_level = np.zeros((n, len(WINDOWS)))
    matched_velocity = np.zeros_like(matched_level)
    matched_time = None
    ema_last_day = None
    records = []
    truth = (data["truth_log"] - np.log(data["means"])) @ fused.b @ fused.edges.T
    velocity = np.gradient(truth, .25, axis=0)
    acceleration = np.gradient(velocity, .25, axis=0)
    for k, day in enumerate(data["t"]):
        # EMA uses the established 1280 daily samples. The new observer gets
        # 128 days at its LIVE cadence, avoiding a bootstrap cadence artifact.
        if day < -128 and not float(day).is_integer():
            continue
        prices = {v: data["observed"][k, q] for q, v in enumerate(fused.venues) if data["available"][k, q]}
        if prices:
            corrected = [np.log(p / data["means"]) - data["impact"][k, q]
                         for q, v in enumerate(fused.venues) if (p := prices.get(v)) is not None]
            z = np.mean(corrected, axis=0)
            z -= z.mean()
            dt = 1. if matched_time is None else day - matched_time
            alpha = 1 - (1 - 2 / (np.asarray(WINDOWS) + 1))**dt
            change = (z[:, None] - matched_level) * alpha
            matched_level += change
            matched_velocity = change / dt
            matched_time = day
        if float(day).is_integer() and "BUCK" in prices:
            lp = np.log(prices["BUCK"] / data["means"])
            lp -= lp.mean()
            for leg, value in zip(emas, lp):
                leg.update(float(value))
            ema_last_day = day
        if day < -128:
            continue
        impact = dict(zip(fused.venues, data["impact"][k]))
        impact_std = dict(zip(fused.venues, data["impact_std"][k]))
        result = fused.observe(float(day), prices, impact, impact_std)
        alone = single.observe(float(day), prices, impact, impact_std)
        uncorrected = raw.observe(float(day), prices)
        if day < 0:
            continue
        pairs_ema = []
        for i, j in fused.pairs:
            # Current production comparator uses daily increments, not the
            # older ladder's strided velocities; preserve all seven channels.
            levels = [a.ma(s) - b.ma(s) for s in range(len(WINDOWS)) for a, b in [(emas[i], emas[j])]]
            velocities = [(emas[i].hist[s][-1] - emas[i].hist[s][-2])
                          - (emas[j].hist[s][-1] - emas[j].hist[s][-2]) for s in range(len(WINDOWS))]
            e = uncorrected["routes"].get("BUCK", [0.] * len(fused.pairs))[len(pairs_ema)] - levels[-1]
            votes = sum(v * e < 0 for v in velocities[:-1])
            pairs_ema.append(dict(level=levels, velocity=velocities, votes=votes,
                matched_level=(matched_level[i] - matched_level[j]).tolist(),
                matched_velocity=(matched_velocity[i] - matched_velocity[j]).tolist()))
        # Fixed initial token quantities, solely to illustrate eligible
        # directions. No candidate is executed or fed back into these weights.
        marked = np.exp(fused.b @ fused.x[:fused.m])
        weights = marked / marked.sum()
        proposals = allocation_candidates(result, weights, np.full(n, 1/n))
        cycle_proposals = allocation_candidates(result, weights, np.full(n, 1/n), pair_configs=cycle_configs)
        records.append(dict(day=float(day), truth=truth[k].tolist(),
            truth_velocity=velocity[k].tolist(), truth_acceleration=acceleration[k].tolist(),
            prices={v: p.tolist() for v, p in prices.items()},
            base_values=np.exp(data["bases"][k]).tolist(),
            impact={v: u.tolist() for v, u in impact.items()},
            impact_std={v: u.tolist() for v, u in impact_std.items()},
            actual_impact=data["actual_impact"][k].tolist(), weights=weights.tolist(),
            fused=result, buck_only=alone, uncorrected=uncorrected, ema=pairs_ema,
            ema_fresh=bool(ema_last_day is not None and day - ema_last_day <= 1),
            matched_ema_fresh=bool(matched_time is not None and day - matched_time <= 1),
            candidates=proposals, cycle_candidates=cycle_proposals))
    result = dict(schema=1, case=case, days=days, seed=seed, noise=noise,
        names=data["names"], means=data["means"].tolist(), pairs=fused.pairs,
        config=asdict(config), history_days=dict(ema=1280, observer=128), noise_cross_venue_rho=data["rho"],
        observations="synchronous 6-hour snapshots; daily EMA comparator",
        reference="known baseline means; normalized log ratios, not equal TOKEN unit prices",
        response=asdict(ResponseConfig()), spectra=spectra, records=records)
    result["metrics"] = metrics(result)
    return result


def metrics(result):
    # Fixed scoring interval across cases. All live samples remain in JSON;
    # no evaluation samples fit observer hyperparameters.
    rows = [r for r in result["records"] if r["day"] >= 10]
    truth = np.array([r["truth_velocity"] for r in rows])
    spread = np.array([r["truth"] for r in rows])
    out = {}
    for variant in ("fused", "buck_only", "uncorrected", "ema5", "ema_matched"):
        if variant.startswith("ema"):
            field = "velocity" if variant == "ema5" else "matched_velocity"
            estimate = np.array([[p[field][0] for p in r["ema"]] for r in rows])
            fresh = np.array([r["ema_fresh" if variant == "ema5" else "matched_ema_fresh"] for r in rows])[:, None]
        else:
            estimate = np.array([[p["velocity"] for p in r[variant]["pairs"]] for r in rows])
            fresh = np.array([r[variant]["fresh"] for r in rows])[:, None]
        valid = np.broadcast_to(fresh, truth.shape)
        active = valid & (np.abs(truth) > .001)
        out[variant] = dict(velocity_rmse=float(np.sqrt(np.mean((estimate[valid] - truth[valid])**2))),
            direction_accuracy=float(np.mean((estimate * truth > 0)[active])) if np.any(active) else None)
        if not variant.startswith("ema"):
            sigma = np.array([[p["sigma"][1] for p in r[variant]["pairs"]] for r in rows])
            out[variant]["velocity_2sigma_coverage"] = float(np.mean((np.abs(estimate - truth) <= 2*sigma)[valid]))
            score = np.array([[p["inward_score"] for p in r[variant]["pairs"]] for r in rows])
            firing = valid & (score > .5)
            correct = (spread * truth < 0) & (np.abs(spread) > .002)
            out[variant]["strong_closing_samples"] = int(firing.sum())
            out[variant]["false_closing_fraction"] = float(np.mean(~correct[firing])) if np.any(firing) else None
    out["max_route_disagreement_nis"] = max((r["fused"]["disagreement_nis"] or 0) for r in rows)
    for field in ("candidates", "cycle_candidates"):
        total = departing = slowing = 0.
        lookup = {tuple(pair): k for k, pair in enumerate(result["pairs"])}
        for r in rows:
            for p in r[field]:
                i, j = p["sell"], p["buy"]
                k = lookup[min(i, j), max(i, j)]
                sign = 1 if i < j else -1
                v = sign * r["truth_velocity"][k]
                a = sign * r["truth_acceleration"][k]
                total += p["nav_fraction"]
                if v > 0:
                    departing += p["nav_fraction"]
                    if a < 0:
                        slowing += p["nav_fraction"]
        out[field] = dict(candidate_nav_sum=total,
            share_while_departing=departing / total if total else None,
            share_while_departing_and_slowing=slowing / total if total else None)
    return out


def fingerprint():
    paths = [Path(__file__), Path(__file__).with_name("pair_observer.py"),
             Path(__file__).with_name("pair_spectrum.py"),
             Path(__file__).with_name("rebalance_policy.py"),
             Path(__file__).parent / "control_lab/model.py"]
    return hashlib.sha256(b"".join(p.read_bytes() for p in paths)).hexdigest()


def worker(task):
    case, days, seed, noise, filename = task
    result = evaluate_run(case, days, seed, noise)
    with gzip.open(filename, "wt", compresslevel=6) as f:
        json.dump(result, f, separators=(",", ":"), allow_nan=False)
        f.write("\n")
    return dict(case=case, noise=noise, seed=seed, file=Path(filename).name, metrics=result["metrics"])


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument("--cases", default=",".join(CASES))
    p.add_argument("--seeds", default="41404,41405,41406")
    p.add_argument("--noise", default="0,.002,.008")
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--force", action="store_true")
    p.add_argument("--render-only", action="store_true")
    p.add_argument("--no-render", action="store_true")
    args = p.parse_args(argv)
    if args.render_only:
        from .pair_observer_render import render
        render(args.out)
        return
    args.out.mkdir(parents=True, exist_ok=True)
    cases = args.cases.split(",")
    noises, seeds = [float(x) for x in args.noise.split(",")], [int(x) for x in args.seeds.split(",")]
    if args.days < 11 or args.jobs < 1 or any(c not in CASES for c in cases) or any(not np.isfinite(x) or x < 0 for x in noises):
        p.error("valid cases, finite nonnegative noise, days >= 11, jobs >= 1 required")
    source = fingerprint()
    tasks, entries = [], []
    start = time.monotonic()
    for case in cases:
        for noise in noises:
            for seed in seeds:
                key = hashlib.sha256(json.dumps([source, case, args.days, seed, noise]).encode()).hexdigest()[:16]
                filename = args.out / f"{case}-{key}.json.gz"
                if filename.exists() and not args.force:
                    with gzip.open(filename, "rt") as f:
                        r = json.load(f)
                    entries.append(dict(case=case, noise=noise, seed=seed, file=filename.name, metrics=r["metrics"]))
                else:
                    tasks.append((case, args.days, seed, noise, str(filename)))
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        for entry in pool.map(worker, tasks):
            entries.append(entry)
            print(f"{entry['case']} noise={entry['noise']} seed={entry['seed']} done", flush=True)
    entries.sort(key=lambda e: (e["case"], e["noise"], e["seed"]))
    manifest = dict(schema=1, source_sha256=source, days=args.days, cases=cases,
                    noise=noises, seeds=seeds, entries=entries)
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"{len(entries)} experiments; {len(tasks)} computed in {time.monotonic()-start:.1f}s", flush=True)
    if not args.no_render:
        from .pair_observer_render import render
        render(args.out)


if __name__ == "__main__":
    main()
