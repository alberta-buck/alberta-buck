"""Read allocation drift and rebalancing activity from matched stock-study closes."""

import json
from math import ceil, log, sqrt
from statistics import median


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


def _gross_share_bp(observation):
    opening = int(observation['opening']['total_shares'])
    return {owner: sum(int(leg[owner]) for leg in observation['flows'].values()) * 10_000 / opening
            for owner in ('other', 'jubilee', 'unattributed')}


def _cash_windows(directory, days):
    """Deduplicate confirmed household settlements by transaction and member."""
    found, incomplete = {}, set()
    with (directory / 'events.jsonl').open('rb') as stream:
        for line in stream:
            if b'"operation":"deposit"' not in line and b'"operation":"redeem"' not in line:
                continue
            event = json.loads(line)
            day, operation = event['day'], event['operation']
            if day not in days or operation not in ('deposit', 'redeem') or event.get('status') != 'confirmed':
                continue
            amounts = event['amounts']
            if amounts.get('in_kind') or not event.get('transaction_hash'):
                incomplete.add(day)
                continue
            key = event['transaction_hash'], event['member'], operation, amounts.get('token_id')
            value = day, int(amounts['buck'])
            if key in found and found[key] != value:
                raise ValueError(f'conflicting settlement event for {key}')
            found[key] = value
    return {day: None if day in incomplete else sum(amount for d, amount in found.values() if d == day)
            for day in days}


def _relative_return(frames, day, horizon, symbol):
    if day + horizon >= len(frames):
        return None
    before, after = ({t['symbol']: t['ref_usd'] for t in frames[d]['market']['tokens']}
                     for d in (day, day + horizon))
    weights = {w['symbol']: w['declared_bp'] for w in frames[day]['basket']['weights'] if w['symbol'] != symbol}
    peer_return = sum(weight * log(after[s] / before[s]) for s, weight in weights.items()) / sum(weights.values())
    return 100 * (log(after[symbol] / before[symbol]) - peer_return)


def _close_urgency(frames, slack, patience):
    """Apply the patience rule once per close to the recorded weight gap."""
    state, closes = {}, {}
    previous_day = frames[0]['day']
    for frame in frames:
        day = frame['day']
        elapsed = min(1, day - previous_day)
        current = {}
        for w in frame['basket']['weights'] or ():
            symbol = w['symbol']
            gap = w['held_bp'] - w['target_bp']
            side = (gap > 0) - (gap < 0)
            old_side, prior = state.get(symbol, (0, 0))
            if side != old_side:
                prior = 0
            current[symbol] = side, min(patience, max(0, prior + elapsed * (abs(gap) - slack)))
        closes[day] = {symbol: urgency for symbol, (_, urgency) in current.items()}
        previous_day, state = day, current
    return closes


def flow_summary(campaign, manifest, root, validate):
    out = ['# Historical basket flows and missed rebalances', '',
           'Gross share flow is shares minted plus shares retired divided by prior-close shares, '
           'equivalent to marking both at the prior-close share price. Non-Jubilee includes all other '
           'identified receipt holders. Confirmed household cash is a separate measured subset. '
           'These measures exclude market activity that leaves receipt ownership unchanged.', '',
           '| Seed | Extra Jubilee | Arm | Median equity, BUCK | Daily non-Jubilee gross flow P50 / P95, bp | '
           'Large-gap days / without an ask | Of those without an ask, ≤10 bp gross share flow today / last 3 closes |',
           '|---:|---:|---|---:|---:|---:|---:|']
    window_rows, good = [], []
    markouts = {label: {3: [], 15: []} for label in ('No ask, ≤10 bp flow', 'No ask, >10 bp flow', 'At least one ask')}
    machine = manifest['effective_machine']
    slack, patience = (round(10_000 * machine[key]) for key in ('slack', 'patience'))
    for cell in manifest['cells']:
        if cell.get('status') != 'complete' or cell['regime'] != 'history':
            continue
        pair, records = validate(campaign, cell, root, manifest['tokens'])
        good.append((cell, records, pair))
        for arm, (_, directory, frames) in records.items():
            data = pair['arms'][arm]
            warm = cell['warmup_days']
            rows = _days(frames, data['daily'], warm)
            ownership = data['ownership']
            gross = {d['day']: _gross_share_bp(d) for d in ownership[warm:]}
            non_jubilee = [value['other'] for value in gross.values()]
            large = [r for r in rows if abs(r['gap']) >= 100]
            no_ask = [r['day'] for r in large if not r['asks']]
            equity = median(int(o['opening']['equity_buck']) for o in ownership[warm:]) / 1e6
            quiet_day = sum(sum(gross[day].values()) <= 10 for day in no_ask)
            quiet_three = sum(gross.get(day - 2) is not None and
                              sum(sum(gross[d].values()) for d in range(day - 2, day + 1)) <= 10 for day in no_ask)
            label = f"| {cell['seed']} | {cell['endowment_buck']:,} | {arm}"
            out.append(f'{label} | {equity:,.0f} | {median(non_jubilee):.2f} / {_p95(non_jubilee):.2f} | '
                       f'{len(large)} / {len(no_ask)} | {quiet_day} / {quiet_three} |')
            if arm == 'active':
                for row in large:
                    day = row['day']
                    group = 'At least one ask'
                    if not row['asks']:
                        group = 'No ask, ≤10 bp flow' if sum(gross[day].values()) <= 10 else 'No ask, >10 bp flow'
                    for horizon, values in markouts[group].items():
                        relative = _relative_return(frames, day, horizon, row['token'])
                        if relative is not None:
                            values.append(relative if row['gap'] < 0 else -relative)
            windows = {day: symbol for first, last, symbol in ((339, 344, 'CNST'), (842, 847, 'NRGC'))
                       for day in range(first, last + 1) if day in gross}
            if arm != 'active' or not windows:
                continue
            cash = _cash_windows(directory, windows)
            urgency = _close_urgency(frames, slack, patience)
            for day, symbol in windows.items():
                obs = ownership[day]
                weight = next(w for w in frames[day]['basket']['weights'] if w['symbol'] == symbol)
                gap = weight['held_bp'] - weight['target_bp']
                cash_bp = None if cash[day] is None else cash[day] * 10_000 / int(obs['opening']['equity_buck'])
                ratio = '—' if cash_bp is None else f'{cash_bp:.2f}'
                ahead = _relative_return(frames, day, 3, symbol)
                relative = '—' if ahead is None else f'{ahead:+.2f}%'
                signal = data['daily'][day]
                window_rows.append(f'{label} | {day} | {symbol} | {gap:+d} | {urgency[day][symbol]} | {ratio} | '
                                   f"{gross[day]['other']:.2f} / {gross[day]['jubilee']:.2f} / {gross[day]['unattributed']:.2f} | "
                                   f"{signal['pair_asks']} / {signal['pair_changes']} | {relative} |")
    out += ['', '## External-tape markout for the largest gap at each active close', '',
            'A positive markout favors buying an underweight TOKEN or selling an overweight TOKEN '
            'at the close. An ask can concern a different TOKEN. The arms share a tape and the '
            'windows can overlap. These are descriptive marks before any trading costs.', '',
            '| Large-gap group | Favorable by 3 / observations | Median 3-close markout | '
            'Favorable by 15 / observations | Median 15-close markout |', '|---|---:|---:|---:|---:|']
    for label, horizons in markouts.items():
        stats = [f'{sum(x > 0 for x in values)} / {len(values)} | {median(values):+.2f} log-%' if values
                 else '— | —' for values in horizons.values()]
        out.append(f"| {label} | {' | '.join(stats)} |")
    out += ['', '## Replay windows in the active arms', '',
            f'The urgency proxy uses one reported weight gap per day, {slack} bp slack and a '
            f'{patience} bp-day threshold. The actual policy uses intraday observations and gaps '
            'measured against its target book, so this proxy cannot establish eligibility.', '',
            'The last column is the TOKEN’s three-close forward log return minus the declared-weight '
            'average of its peers on the same external tape. These are historical-tape moves. The '
            'saved record has no full decision-time trace or realized round-trip attribution.', '',
            '| Seed | Extra Jubilee | Arm | Close | TOKEN | Held minus target, bp | Close urgency proxy, bp-days | '
            'Gross household cash, bp of equity | '
            'Gross shares non-Jubilee / Jubilee / unattributed, bp | All asks / changed plans | TOKEN vs peers, next 3 |',
            '|---:|---:|---|---:|---|---:|---:|---:|---:|---:|---:|', *window_rows]
    return '\n'.join(out) + '\n', good


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
