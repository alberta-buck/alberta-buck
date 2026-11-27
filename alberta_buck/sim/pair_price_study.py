"""Raw-price study of PairSignals and PairRebalancePolicy, with a fixed-capital book."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import itertools
import json
import math
from pathlib import Path

import numpy as np

from .basket_machine import DEFAULTS, LOG_TICK, OBSERVER

try:
    from numba import njit
except ImportError:
    def njit(function):
        return function


SYMBOLS = ("LABR", "CNST", "FOOD", "NRGC", "PAXG")
WEIGHTS = np.array([2553, 2340, 2128, 2128, 851]) / 10_000
DAY = 86_400
SEPARATION = 100 * LOG_TICK
RESPONSE, ERROR_TIME, MAX_GAP, WARMUP, FLOOR = OBSERVER
FLOOR = FLOOR / 10**18 * LOG_TICK * DAY


@njit
def target_weights(out, relative_logs, weights, tilt, cap):
    total = 0.
    for i in range(len(out)):
        out[i] = weights[i] * math.exp(-tilt * relative_logs[i])
        total += out[i]
    for i in range(len(out)):
        out[i] = min(weights[i] * (1 + cap), max(weights[i] * (1 - cap), out[i] / total))


@njit
def urgency_step(previous, old_side, gap, dt, slack, patience):
    side = 1 if gap > 0 else -1 if gap < 0 else 0
    if side != old_side:
        previous = 0.
    return min(patience, max(0., previous + (abs(gap) - slack) * min(dt, 1.))), side


@njit
def allowance_step(previous, rate, dt):
    return min(4., previous + rate * min(dt, 1.))


@njit
def motion_rate(rate, change, allowance, release_rate, cap):
    braking = min(1., max(0., -change) / (allowance + FLOOR))
    if abs(rate) <= FLOOR:
        braking = .2
    scale = allowance + FLOOR
    release = release_rate * scale / (scale + max(0., rate)) * braking
    closure = cap * min(1., max(0., -(rate + allowance)) / scale)
    return min(cap, release + closure)


@njit
def early_rate(separation, rate, change, allowance, drift_fraction, cap):
    """Additional small release. The acceleration penalty falls as the excess spread widens."""
    drift = RESPONSE / DAY * max(0., rate + allowance + .5 * max(0., change))
    budget = (separation - SEPARATION) * drift_fraction
    return .1 * cap * max(0., 1. - drift / budget) if budget > 0 else 0.


def bridge_chunks(seconds, logs, step, noise_bp, seed):
    """Seeded log Brownian bridges with the supplied anchor endpoints and no extra drift."""
    rng = np.random.default_rng(seed)
    for k in range(len(seconds) - 1):
        span = int(seconds[k + 1] - seconds[k])
        blocks = span // step
        fraction = np.arange(1, blocks + 1)[:, None] / blocks
        chunk = logs[k] + fraction * (logs[k + 1] - logs[k])
        if noise_bp and blocks > 1:
            walk = np.cumsum(rng.standard_normal(chunk.shape), axis=0)
            chunk += (walk - fraction * walk[-1]) * (noise_bp / 10_000 * math.sqrt(step / DAY))
        chunk[-1] = logs[k + 1]
        yield chunk


def long_anchors():
    from .quotes.source import QuoteSource
    names = ["LABR_US", "CNST", "FOOD_US", "NRGC", "XAU"]
    source = QuoteSource(tokens=names)
    anchors = [dict(source.anchors(name)) for name in names]
    dates = sorted(set.intersection(*(set(column) for column in anchors)))
    return dates, np.array([[column[day] for column in anchors] for day in dates])


def read_anchors(path):
    """CSV with an ISO timestamp column and the five basket symbols."""
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    dates = [datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) for row in rows]
    dates = [d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d for d in dates]
    return dates, np.array([[float(row[name]) for name in SYMBOLS] for row in rows])


def synthetic(tape, days, step):
    sample = np.arange(0, days * DAY + step, step)
    time = sample / DAY
    stagger = np.arange(len(SYMBOLS))
    if tape == "cycle":
        logs = .10 * np.sin(time[:, None] * 2 * np.pi / (27 + 13 * stagger) + stagger)
    else:
        logs = np.zeros((len(sample), len(SYMBOLS)))
        logs[:, 1] = -.22 * np.clip((time - 25) / 12, 0, 1)
        if tape == "temporary":
            logs[:, 1] += .22 * np.clip((time - 58) / 18, 0, 1)
    return sample, logs


# One observer per path, reused by all parameter arms. Columns in book state are
# holdings, urgency, gap sign, allowance and the previous observed gap.
@njit
def simulate_chunk(prices, origin, prev, mark_prev, books, cash, stats, track, errors, hour_open, roughness,
                   weights, arms, first, step, schedule, clock, integral, quotes, twap_sum, previous_gap, cost, grain, depth):
    response = RESPONSE / DAY
    hour = 3600 // step
    twap_span = 600 / step
    rough_gain = 1 - math.exp(-1 / (24 * 30))
    n = len(weights)
    residual = np.empty(n)
    changes = np.empty(n)
    live = np.empty(n)
    factors = np.empty(n)
    targets = np.empty(n)
    plans = np.empty(n)
    discretionary = np.empty(n, dtype=np.bool_)
    rates = np.empty(n)
    raw_early = np.empty(n)
    for offset in range(len(prices)):
        index = first + offset
        current = prices[offset]
        slot = (index - 1) % len(quotes)
        for i in range(n):
            integral[i] += prev[i]
            twap_sum[i] += prev[i] - quotes[slot, i]
            quotes[slot, i] = prev[i]
            prev[i] = current[i]
        if index % hour == 0:
            p = 0
            for i in range(n):
                for j in range(i + 1, n):
                    move = (current[i] - hour_open[i]) - (current[j] - hour_open[j])
                    roughness[p] += rough_gain * (abs(move) * math.sqrt(24) - roughness[p])
                    p += 1
            hour_open[:] = current
        if index != clock[0]:
            continue

        # The raw price is held until its next update. Average the interval
        # exactly, then track successive interval midpoints as PairSignals does.
        span = index - clock[1]
        center = np.sum(integral) / (span * n)
        seed = clock[2] == 0
        reset = span * step > MAX_GAP
        obs_dt = (span + clock[2]) * step / (2 * DAY)
        k = obs_dt / (response + obs_dt)
        beta = k * k
        alpha = 2 * k - beta
        error_gain = obs_dt * DAY / (ERROR_TIME + obs_dt * DAY)
        for i in range(n):
            oldest = quotes[(slot + 1) % len(quotes), i]
            excess = oldest * (len(quotes) - twap_span) if index >= len(quotes) else 0.
            live[i] = (twap_sum[i] - excess) / min(index, twap_span) - origin[i]
            observation = integral[i] / span - center
            residual[i] = 0. if seed or reset else observation - track[0, i] - track[1, i] * obs_dt
            changes[i] = beta * residual[i] / obs_dt
            if seed or reset:
                track[0, i], track[1, i] = observation, 0.
            else:
                track[0, i] += track[1, i] * obs_dt + alpha * residual[i]
                track[1, i] += changes[i]
            integral[i] = 0.
        p = 0
        for i in range(n):
            for j in range(i + 1, n):
                if seed or reset:
                    errors[p, 0], errors[p, 1] = 0., 0.
                else:
                    errors[p, 0] += error_gain * (abs(residual[i] - residual[j]) - errors[p, 0])
                    errors[p, 1] += error_gain * (abs(changes[i] - changes[j]) / obs_dt - errors[p, 1])
                p += 1
        if seed or reset:
            clock[3] = 2 * index - span
            clock[4] = 0
        clock[2] = 0 if reset else span
        if not reset:
            clock[4] += 1
        ready = clock[4] >= 3 and (2 * index - span - clock[3]) * step >= 2 * WARMUP
        book_dt = min(1., span * step / DAY)
        clock[1] = index
        clock[5] = (clock[5] + 1) % len(schedule)
        clock[0] += schedule[clock[5]]

        movement = 0.
        for i in range(n):
            movement += weights[i] * (current[i] - mark_prev[i])
        for i in range(n):
            # Price elasticity of a full-range LP mark in the basket numeraire.
            factors[i] = math.exp(.5 * (current[i] - mark_prev[i] - movement))
        for m in range(len(arms)):
            tilt, patience, drift_fraction, adaptive = arms[m]
            holdings = books[m, 0]
            book = 0.
            for i in range(n):
                holdings[i] *= factors[i]
                book += holdings[i]
            target_weights(targets, live, weights, tilt, DEFAULT_CAP)
            maximum = CAP
            rates[:] = 0.
            raw_early[:] = 0.
            if ready and patience > 0:
                p = 0
                for i in range(n):
                    for j in range(i + 1, n):
                        anchor = origin[i] - origin[j]
                        filtered = track[0, i] - track[0, j] - anchor
                        spot = current[i] - current[j] - anchor
                        sign = 1. if filtered > 0 else -1.
                        separation = min(sign * filtered, sign * spot)
                        if separation > SEPARATION:
                            speed = sign * (track[1, i] - track[1, j])
                            change = sign * (changes[i] - changes[j]) * response / obs_dt
                            allowance = (FLOOR + errors[p, 0] / response
                                         + errors[p, 1] * (2 * response + obs_dt / 2)
                                         + adaptive * roughness[p])
                            base = motion_rate(speed, change, allowance, RELEASE, maximum)
                            early = early_rate(separation, speed, change, allowance, drift_fraction, maximum)
                            desire = max(base, early)
                            # Two columns would duplicate the peer scan. A pair
                            # can direct a sale on the high side and a buy on the low side.
                            if sign * (holdings[i] - targets[i] * book) > 0:
                                rates[i] += weights[j] * desire
                                raw_early[i] += weights[j] * max(0., early - base)
                            if -sign * (holdings[j] - targets[j] * book) > 0:
                                rates[j] += weights[i] * desire
                                raw_early[j] += weights[i] * max(0., early - base)
                        p += 1
            for i in range(n):
                gap = holdings[i] / book - targets[i]
                old_side = books[m, 2, i]
                next_urgency, side = urgency_step(books[m, 1, i], old_side, gap, book_dt, SLACK, patience)
                if previous_gap:
                    if side != old_side:
                        next_urgency = 0.
                    else:
                        next_urgency, _ = urgency_step(books[m, 1, i], old_side, books[m, 4, i],
                                                       book_dt, SLACK, patience)
                books[m, 1, i], books[m, 2, i], books[m, 4, i] = next_urgency, side, gap
                leash = abs(holdings[i] / book - weights[i]) > LEASH * weights[i]
                rate = rates[i] / (1 - weights[i])
                eligible = ready and patience > 0 and next_urgency >= patience
                amount = 0.
                accrued = 0.
                if leash:
                    amount = abs(gap) * book
                elif eligible:
                    stats[m, 3] += 1
                    if rate > 0:
                        stats[m, 4] += 1
                    if raw_early[i] > 0:
                        stats[m, 5] += 1
                    # Contract carries allowance to this response at the requested rate.
                    accrued = allowance_step(books[m, 3, i], rate, book_dt)
                    amount = abs(gap) * book * -math.expm1(-accrued)
                books[m, 3, i] = accrued if rate > 0 else 0.
                step_cap = min(holdings[i] if gap > 0 else targets[i] * book, book) * STEP_CAP
                amount = min(amount, step_cap, depth * book)
                plans[i] = -side * amount if amount > 2 * grain * book else 0.
                discretionary[i] = eligible and not leash
            buys = 0.
            for i in range(n):
                amount = plans[i]
                if amount < 0:
                    cash[m] -= amount * (1 - cost)
                    holdings[i] += amount
                    books[m, 3, i] = 0.
                    stats[m, 0] += 1
                    if discretionary[i]:
                        stats[m, 8] += 1
                    stats[m, 1] -= amount
                    stats[m, 2] -= amount * cost
                else:
                    buys += amount
            scale = min(1., cash[m] / (buys * (1 + cost))) if buys > 0 else 0.
            for i in range(n):
                amount = plans[i] * scale
                if amount > 2 * grain * book:
                    cash[m] -= amount * (1 + cost)
                    holdings[i] += amount
                    books[m, 3, i] = 0.
                    stats[m, 0] += 1
                    if discretionary[i]:
                        stats[m, 8] += 1
                    stats[m, 1] += amount
                    stats[m, 2] += amount * cost
            cash[m] = max(0., cash[m])
            equity = cash[m] + np.sum(holdings)
            stats[m, 6] = max(equity, stats[m, 6])
            stats[m, 7] = max(stats[m, 7], 1 - equity / stats[m, 6])
        mark_prev[:] = current


DEFAULT_CAP = DEFAULTS["lean_cap_bp"] / 10_000
SLACK = DEFAULTS["slack"]
LEASH = DEFAULTS["leash_bp"] / 10_000
CAP = math.log(2) / DEFAULTS["half_life_days"]
RELEASE = DEFAULTS["release_rate"]
STEP_CAP = .05


def replay(seconds, log_prices, arms, step, decision, noise_bp, seed, cost_bp, grain_bp, depth_bp,
           previous_gap=False):
    n = len(WEIGHTS)
    origin = log_prices[0]
    prev, mark_prev = origin.copy(), origin.copy()
    schedule = np.array(decision, dtype=np.int64) // step
    # Next call, last endpoint, previous span, seeded midpoint, samples, schedule position.
    clock = np.array([schedule[0], 0, 0, 0, 0, 0], dtype=np.int64)
    integral = np.zeros(n)
    quotes = np.zeros((math.ceil(600 / step), n))
    twap_sum = np.zeros(n)
    hour_open = origin.copy()
    track = np.zeros((2, n))
    errors = np.zeros((n * (n - 1) // 2, 2))
    roughness = np.zeros(len(errors))
    books = np.zeros((len(arms), 5, n))
    books[:, 0, :] = .95 * WEIGHTS
    cash = np.full(len(arms), .05)
    stats = np.zeros((len(arms), 9))
    stats[:, 6] = 1.
    first = 1
    for prices in bridge_chunks(seconds, log_prices, step, noise_bp, seed):
        simulate_chunk(prices, origin, prev, mark_prev, books, cash, stats, track, errors, hour_open, roughness,
                       WEIGHTS, arms, first, step, schedule, clock, integral, quotes, twap_sum, previous_gap,
                       cost_bp / 10_000, grain_bp / 10_000, depth_bp / 10_000)
        first += len(prices)
    end_move = log_prices[-1] - mark_prev
    factors = np.exp(.5 * (end_move - WEIGHTS @ end_move))
    marked = cash + books[:, 0, :] @ factors
    stats[:, 7] = np.maximum(stats[:, 7], 1 - marked / stats[:, 6])
    return marked, stats


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("output", type=Path)
    p.add_argument("--tape", choices=("long", "cycle", "temporary", "permanent"), default="long")
    p.add_argument("--csv", type=Path, help="raw external CSV with timestamp and five symbol columns")
    p.add_argument("--days", type=int, help="use the trailing N days of anchors or a synthetic duration")
    p.add_argument("--step", type=int, default=12, help="price observation cadence, seconds")
    p.add_argument("--decision", type=int, nargs="+", help="repeating wheel-call intervals in seconds, defaults to one call per price step")
    p.add_argument("--noise-bp", type=float, nargs="+", default=[0., 50., 150.], help="daily log volatility of bridges")
    p.add_argument("--seed", type=int, nargs="+", default=[41404])
    p.add_argument("--tilt", type=float, nargs="+", default=[0., DEFAULTS["tilt"]])
    p.add_argument("--patience", type=float, nargs="+", default=[100., 200.], help="basis-point-days")
    p.add_argument("--outward", type=float, nargs="+", default=[0., .5, .75],
                   help="projected drift budget as a multiple of excess separation, zero disables early release")
    p.add_argument("--adaptive", type=float, nargs="+", default=[0., 1.], help="additional slow noise allowance weight")
    p.add_argument("--previous-gap", action="store_true", help="accrue using the previous observed gap")
    p.add_argument("--cost-bp", type=float, default=30.)
    p.add_argument("--grain-bp", type=float, default=2., help="trade grain as basis points of TOKEN book")
    p.add_argument("--depth-bp", type=float, default=30., help="per-constituent fill cap in basis points of TOKEN book")
    args = p.parse_args(argv)
    decision = args.decision or [args.step]
    if args.step <= 0 or 3600 % args.step or any(d < args.step or d % args.step for d in decision):
        p.error("step must divide an hour and wheel intervals must be positive multiples of step")
    if args.days is not None and args.days <= 0:
        p.error("days must be positive")
    if min(args.patience) <= 0 or not all(
            math.isfinite(x) and x >= 0 for x in
            [*args.noise_bp, *args.tilt, *args.patience, *args.outward, *args.adaptive,
             args.cost_bp, args.grain_bp, args.depth_bp]):
        p.error("study parameters must be finite and nonnegative, with positive patience")
    if max(args.tilt) > 1 or args.cost_bp >= 10_000:
        p.error("tilt must be at most one and cost must be less than 10000 bp")
    if args.csv or args.tape == "long":
        dates, prices = read_anchors(args.csv) if args.csv else long_anchors()
        if args.days is not None and dates:
            cut = len(dates) - 1
            while cut > 0 and (dates[-1] - dates[cut]).total_seconds() < args.days * DAY:
                cut -= 1
            dates, prices = dates[cut:], prices[cut:]
        if len(dates) < 2 or not np.all(np.isfinite(prices) & (prices > 0)):
            p.error("provide at least two strictly positive price observations")
        seconds = np.array([(d - dates[0]).total_seconds() for d in dates])
        if np.any(np.diff(seconds) <= 0) or np.any(seconds % args.step):
            p.error("timestamps must increase on the requested observation grid")
        seconds, logs = seconds.astype(np.int64), np.log(prices)
        source = {"kind": "external-csv" if args.csv else "monthly-usd-proxies",
                  "file": str(args.csv) if args.csv else None, "first": dates[0].isoformat(),
                  "last": dates[-1].isoformat(), "anchors": len(dates)}
    else:
        seconds, logs = synthetic(args.tape, args.days or 120, max(args.step, 3600))
        source = {"kind": args.tape, "anchors": len(seconds)}
    production = (DEFAULTS["tilt"], DEFAULTS["patience"], 0., 0.)
    base = (DEFAULTS["tilt"], 0., 0., 0.)
    treatments = [(t, p / 10_000, d, a) for t, p, d, a in
                  itertools.product(args.tilt, args.patience, args.outward, args.adaptive)]
    controls = [(tilt, 0., 0., 0.) for tilt in args.tilt]
    arms = np.array(list(dict.fromkeys([base, production, *controls, *treatments])))
    maintenance = {arm[0]: i for i, arm in enumerate(arms) if arm[1] == 0.}
    rows = []
    groups = [(noise, seed) for noise in args.noise_bp for seed in (args.seed if noise else args.seed[:1])]
    for noise, seed in groups:
        marks, stats = replay(seconds, logs, arms, args.step, decision, noise, seed,
                              args.cost_bp, args.grain_bp, args.depth_bp, args.previous_gap)
        for index, (arm, mark, stat) in enumerate(zip(arms, marks, stats, strict=True)):
            rows.append(dict(arm=("maintenance" if arm[1] == 0. else "production" if index == 1 else "candidate"),
                             tilt=arm[0], patience_bp_days=arm[1] * 10_000,
                             outward_drift_fraction=arm[2], adaptive_noise=arm[3], noise_bp=noise, seed=seed,
                             net_return_pct=100 * (mark - 1), vs_maintenance_pp=100 * (mark - marks[maintenance[arm[0]]]),
                             vs_production_pp=100 * (mark - marks[1]), trades=int(stat[0]), discretionary_fills=int(stat[8]),
                             turnover_pct=100 * stat[1], cost_pct=100 * stat[2],
                             eligible=int(stat[3]), positive_rate=int(stat[4]), extra_early_rate=int(stat[5]),
                             max_drawdown_pct=100 * stat[7]))
        print(f"{source['kind']}, {noise:g} bp, seed {seed}: {len(arms)} arms", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dict(schema=1, source=source, price_step_seconds=args.step,
                                          decision_seconds=decision, previous_gap=args.previous_gap, observations=int(seconds[-1] // args.step),
                                          fee_bp=args.cost_bp, grain_bp=args.grain_bp, depth_bp=args.depth_bp,
                                          rows=rows), indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
