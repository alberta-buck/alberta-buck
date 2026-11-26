"""Read allocation drift and rebalancing activity from matched stock-study closes."""

from math import ceil, sqrt


def _p95(values):
    return sorted(values)[ceil(0.95 * len(values)) - 1]


def _ranges(days):
    ranges = []
    for day in days:
        if ranges and day == ranges[-1][1] + 1:
            ranges[-1][1] = day
        else:
            ranges.append([day, day])
    return ', '.join(str(lo) if lo == hi else f'{lo}–{hi}' for lo, hi in ranges) or '—'


def _drift(weights, factors):
    weighted = [weight * factor for weight, factor in zip(weights, factors, strict=True)]
    total, declared = sum(weighted), sum(weights)
    return 100 * max(abs(value / total / (weight / declared) - 1)
                     for value, weight in zip(weighted, weights, strict=True))


def _days(frames, signals, warm):
    opening = frames[warm - 1]
    reference = {t['symbol']: t['ref_usd'] for t in opening['market']['tokens']}
    declared = {w['symbol']: w['declared_bp'] for w in opening['basket']['weights']}
    counters = ('buys', 'sells')
    previous = [int(opening['basket']['wheel'][key] or 0) for key in counters]
    rows = []
    for frame, signal in zip(frames[warm:], signals[warm:], strict=True):
        basket = frame['basket']
        weights = basket['weights']
        largest = max(weights, key=lambda w: abs(w['held_bp'] - w['target_bp']))
        current = [int(basket['wheel'][key] or 0) for key in counters]
        buys, sells = [after - before for after, before in zip(current, previous, strict=True)]
        if buys < 0 or sells < 0:
            raise ValueError('wheel counters decreased during treatment')
        previous = current
        prices = {t['symbol']: t['ref_usd'] for t in frame['market']['tokens']}
        price_factors = [prices[symbol] / reference[symbol] for symbol in declared]
        rows.append({
            'day': frame['day'], 'buys': buys, 'sells': sells,
            'decisions': signal['decision_count'], 'asks': signal['pair_asks'], 'changes': signal['pair_changes'],
            'token': largest['symbol'], 'gap': largest['held_bp'] - largest['target_bp'],
            'leash': 100 * max(abs(w['held_bp'] - w['declared_bp']) / w['declared_bp'] for w in weights),
            'relative_prices': 100 * (max(price_factors) / min(price_factors) - 1),
            'fixed_tokens': _drift(declared.values(), price_factors),
            'full_range_proxy': _drift(declared.values(), [sqrt(p) for p in price_factors]),
        })
    return rows


def rebalance_summary(campaign, manifest, root, validate):
    out = ['# Basket execution and allocation drift', '',
           f'Campaign: `{campaign.name}`.', '',
           'Includes validated completed pairs. Treatment begins after the common warmup. '
           'A decision is one constituent considered in one response. An ask is a positive pair-policy '
           'amount. A change is a different final plan from the same-state plan without the pair rate.', '',
           'Wheel buys and sells are deltas of the cumulative `Responded` counters. A sale can include a '
           'liquidity withdrawal without a swap. The saved daily records cannot attribute every '
           'confirmed fill or refusal to its decision. Maintenance allows book sizing and safeguards.', '',
           '| Tape | Seed | Jubilee BUCK | Arm | Decisions | Asks | Changed plans | Days changed | Wheel buys / sells |',
           '|---|---:|---:|---|---:|---:|---:|---:|---:|']
    good, missing, active = [], [], []
    for cell in manifest['cells']:
        if cell.get('status') != 'complete':
            missing.append((cell['id'], cell.get('status', 'unknown')))
            continue
        try:
            pair, records = validate(campaign, cell, root, manifest['tokens'])
            arms = {name: _days(record[2], pair['arms'][name]['daily'], cell['warmup_days'])
                    for name, record in records.items()}
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
            missing.append((cell['id'], str(exc)))
            continue
        good.append((cell, records, pair))
        label = f"{cell['regime']} | {cell['seed']} | {cell['endowment_buck']:,}"
        for name, rows in arms.items():
            decisions, asks, changes, buys, sells = [sum(d[key] for d in rows)
                                                   for key in ('decisions', 'asks', 'changes', 'buys', 'sells')]
            live_days = [d['day'] for d in rows if d['changes']]
            out.append(f'| {label} | {name} | {decisions:,} | {asks:,} | {changes:,} | {len(live_days)} | {buys:,} / {sells:,} |')
            if name == 'active':
                active.append((cell, label, rows, live_days))
    out += ['', '## Closing weights in the active arms', '',
            'Gap means the largest absolute held-minus-target weight at a daily close in basis points of '
            'allocation. It approximates the direction of imbalance but differs from the policy’s '
            'intraday exposure gap divided by its target book size. Leash drift is the largest relative '
            'departure from a declared weight. The fixed-TOKEN and full-range proxies use the same '
            'declared starting weights and daily USD tape. The former scales exposures with price '
            'and the latter with its square root. Both omit funding and market execution.', '',
            '| Tape | Seed | Jubilee BUCK | Gap P95 / max (bp) | Days ≥100 bp with zero asks / all ≥100 bp | '
            'Leash drift max | Fixed-TOKEN drift max | Full-range proxy max | Relative price spread max |',
            '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for _, label, rows, _ in active:
        gaps = [abs(d['gap']) for d in rows]
        large = [d for d in rows if abs(d['gap']) >= 100]
        peaks = [max(d[key] for d in rows) for key in ('leash', 'fixed_tokens', 'full_range_proxy', 'relative_prices')]
        out.append(f'| {label} | {_p95(gaps)} / {max(gaps)} | {sum(not d["asks"] for d in large)} / {len(large)} | '
                   + ' | '.join(f'{p:.1f}%' for p in peaks) + ' |')
    out += ['', '## When plans changed on the historical tape', '',
            'These are zero-based simulation closes. The largest no-ask gap is an example to replay. '
            'The close and its lack of an ask do not identify the intraday gate that held.', '',
            '| Tape | Seed | Jubilee BUCK | Changed-plan closes | Largest no-ask close |',
            '|---|---:|---:|---|---|']
    for cell, label, rows, live_days in active:
        if cell['regime'] != 'history':
            continue
        peak = max((d for d in rows if not d['asks']), key=lambda d: abs(d['gap']), default=None)
        example = (f"day {peak['day']}, {peak['token']} {peak['gap']:+d} bp; "
                   f"{peak['buys']} wheel buys / {peak['sells']} sells" if peak else '—')
        out.append(f'| {label} | {_ranges(live_days)} | {example} |')
    if missing:
        out += ['', '## Unavailable or invalid', ''] + [f'- `{cell}`: {reason}' for cell, reason in missing]
    return '\n'.join(out) + '\n', good
