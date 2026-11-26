#!/usr/bin/env python3
"""Summarize verified matched stock-grid pairs and write a dashboard comparison index."""
import argparse
import hashlib
import json
import math
import re
import sys
from statistics import median
import time
from pathlib import Path

from basket_rebalancing_report import rebalance_summary


def load_json(path):
    return json.loads(path.read_text())


def lines(path):
    with path.open() as stream:
        return [json.loads(line) for line in stream if line.strip()]


def validated(campaign, cell, root, tokens):
    raw = (campaign / 'cells' / (cell['id'] + '.json')).read_bytes()
    if not cell.get('sha256') or hashlib.sha256(raw).hexdigest() != cell['sha256']:
        raise ValueError('pair SHA-256 mismatch or missing')
    pair = json.loads(raw)
    if pair.get('fingerprint') != cell['fingerprint'] or not pair.get('matched_prefix'):
        raise ValueError('fingerprint or matched prefix mismatch')
    days, warm = cell['days'], cell['warmup_days']
    if pair.get('days') != days or pair.get('warmup_days') != warm or not 1 <= warm < days:
        raise ValueError('horizon mismatch')
    arms = pair['arms']
    active, maintenance = arms['active'], arms['maintenance']
    if active['opening'] != maintenance['opening']:
        raise ValueError('pre-switch openings differ')
    records = {}
    run_ids = pair.get('run_ids')
    if not isinstance(run_ids, dict) or run_ids != cell.get('run_ids') or set(run_ids) != {'active', 'maintenance'}:
        raise ValueError('pair and campaign run IDs disagree')
    for name, arm in arms.items():
        if name not in ('active', 'maintenance'):
            raise ValueError('unexpected arm')
        data = arm['daily']
        observations = arm['ownership']
        if len(data) != days or len(observations) != days or [x['day'] for x in data] != list(range(days)):
            raise ValueError('pair daily horizon mismatch')
        if [x['day'] for x in observations] != list(range(days)):
            raise ValueError('ownership horizon mismatch')
        tape = [list(col) for col in zip(*(row['prices'] for row in data))]
        digest = hashlib.sha256(json.dumps([tokens, tape], separators=(',', ':')).encode()).hexdigest()
        if digest != cell['price_digest']:
            raise ValueError('price tape mismatch')
        record_id = run_ids[name]
        if not isinstance(record_id, str) or not re.fullmatch(r'[A-Za-z0-9._-]+', record_id):
            raise ValueError('invalid run record ID')
        if arm.get('run_record') and Path(arm['run_record']).name != record_id:
            raise ValueError('arm and pair run IDs disagree')
        directory = root / record_id
        manifest = load_json(directory / 'manifest.json')
        if manifest.get('id') != record_id or manifest.get('status') != 'complete' or manifest.get('frames') != days:
            raise ValueError('run record is missing or incomplete')
        if manifest.get('settings', {}).get('arm') != name or manifest.get('settings', {}).get('warmup_days') != warm:
            raise ValueError('run record arm or treatment boundary mismatch')
        frames = lines(directory / 'frames.jsonl')
        if len(frames) != days or [x['day'] for x in frames] != list(range(days)):
            raise ValueError('run frame horizon mismatch')
        for frame, day, obs in zip(frames, data, observations):
            share = frame.get('basket', {}).get('share_price')
            if share is None or not math.isclose(share, int(day['share']) / 1e18, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError('run frame share mark differs from pair')
            market = frame.get('market', {}).get('tokens') or []
            if (len(market) != len(tokens) or len(day['prices']) != len(tokens)
                    or any(row.get('symbol') != symbol or row.get('ref_usd') is None
                           or not math.isclose(float(row['ref_usd']), float(price) / 1e6,
                                               rel_tol=1e-12, abs_tol=1e-12)
                           for row, symbol, price in zip(market, tokens, day['prices']))):
                raise ValueError('run frame TOKEN prices differ from pair')
            recorded = frame.get('basket', {}).get('ownership')
            expected = {k: v for k, v in obs.items() if k != 'transactions'}
            if recorded != expected:
                raise ValueError('run frame ownership differs from pair')
        records[name] = (record_id, directory, frames)
    if active['daily'][:warm] != maintenance['daily'][:warm] or active['ownership'][:warm] != maintenance['ownership'][:warm]:
        raise ValueError('pre-switch observations differ')
    if [d['prices'] for d in active['daily']] != [d['prices'] for d in maintenance['daily']]:
        raise ValueError('arm price paths differ')
    return pair, records


def optional_float(x):
    return '—' if x is None else f'{x:.3f}%'


def household_cash(directory, warm, days):
    path = directory / 'events.jsonl'
    if not path.exists():
        return None
    found = {}
    try:
        for e in lines(path):
            if not warm <= e.get('day', -1) < days or e.get('status') != 'confirmed' or e.get('operation') not in ('deposit', 'redeem'):
                continue
            a = e['amounts']
            if a.get('in_kind'):
                return None
            tx = e['transaction_hash']
            value = int(a['buck'])
            signature = (e['day'], e.get('member'), e['operation'], value, a.get('token_id'))
            if not tx or value < 0 or (tx in found and found[tx] != signature):
                return None
            found[tx] = signature
        return sum(v[3] for v in found.values()) / 1e6
    except (ValueError, KeyError, TypeError):
        return None


def stock_and_flows(arm, directory, warm, days):
    opening = arm['opening']
    # Ownership windows open at the previous close; do not silently substitute it
    # for the actual pre-switch block when day-start activities changed the book.
    before = arm['ownership'][warm]['opening']
    total = int(opening['total_shares'])
    valid = (total > 0 and int(before['total_shares']) == total
             and int(before['equity_buck']) == int(opening['equity_raw']))
    owners = before.get('receipt_shares', {})
    jubilee = 100 * int(owners['jubilee']) / total if valid and 'jubilee' in owners else None
    household = 100 * int(owners['household']) / total if valid and 'household' in owners else None
    # Gross observed share flow is exact in the capture, while a monetary mark
    # is not a realized cash flow. Report the authenticated household cash subset.
    flows = [d.get('flows') for d in arm['ownership'][warm:days]]
    shares = None
    if all(f and all(k in f.get(kind, {}) for kind in ('minted', 'retired') for k in ('other', 'unattributed')) for f in flows):
        shares = sum(int(f[kind][owner]) for f in flows for kind in ('minted', 'retired') for owner in ('other', 'unattributed'))
    cash = household_cash(directory, warm, days)
    return jubilee, household, shares, cash


def treatment_benchmarks(arm, frames, warm):
    opening, final = arm['opening'], arm['closing']
    first, last = frames[warm - 1], frames[-1]
    if (first.get('block') != opening.get('block')
            or int(arm['daily'][warm - 1]['share']) != int(opening['share_price'])
            or int(arm['daily'][-1]['share']) != int(final['share_price'])):
        return None, None
    start_hold, end_hold = (f['basket'].get('hold_index') for f in (first, last))
    passive = end_hold / start_hold - 1 if start_hold and end_hold else None
    bids = [(f['market'].get('book') or {}).get('buck_bid') for f in (first, last)]
    if any(bid is None or not math.isfinite(bid) or bid <= 0 for bid in bids):
        return passive, None
    share_ratio = int(final['share_price']) / int(opening['share_price'])
    return passive, share_ratio * bids[1] / bids[0] - 1


def regime_summary(groups):
    result = ['## Returns by price regime', '',
              'Each completed pair counts once. These cells share price paths and household seeds. The medians summarize the selected settings.', '',
              '| Regime | Pairs | Median active BUCK | Positive in BUCK | Median active USDC | Positive in USDC | Beat passive in BUCK | Active vs maintenance (wins / ties / losses) |',
              '|---|---:|---:|---:|---:|---:|---:|---:|']
    for regime, group in [*groups.items(), ('All', [x for rows in groups.values() for x in rows])]:
        if not group:
            continue
        active = [a for a, _, _, _ in group]
        usd = [u for _, _, _, u in group if u is not None]
        edges = [a - p for a, _, p, _ in group if p is not None]
        wins = sum(a > m for a, m, _, _ in group)
        ties = sum(a == m for a, m, _, _ in group)
        positive = lambda xs: f'{sum(x > 0 for x in xs)}/{len(xs)}' if xs else '—'
        usd_median = f'{median(usd):+.3%}' if usd else '—'
        result.append(f'| {regime} | {len(group)} | {median(active):+.3%} | {positive(active)} | {usd_median} | {positive(usd)} | {positive(edges)} | {wins} / {ties} / {len(group) - wins - ties} |')
    return result + ['']


def aged_openings(root, manifest):
    if 'aged_study' not in manifest:
        return []
    rows = ['## Verified opening balances', '',
            'The fee column is the increase in net Jubilee demurrage over the elapsed interval. The governed investment of reserve excess precedes the separate contribution. Each row uses the first available arm of a fresh matched pair.', '',
            '| Ledger age (days) | Contribution (BUCK) | Net fee accrued (BUCK) | Excess invested (BUCK) | Opening fund share | Opening fund cash (BUCK) | Passive wallet balance (BUCK) |',
            '|---:|---:|---:|---:|---:|---:|---:|']
    for cell in manifest['cells']:
        for arm, ident in (cell.get('run_ids') or {}).items():
            if not re.fullmatch(re.escape(cell['id']) + r'(?:-try[0-9]+)?-' + re.escape(arm), ident):
                raise ValueError('unexpected aged arm ID')
            path = root / ident / 'opening.json'
            if not path.exists():
                continue
            proof = load_json(path)
            if (proof['fingerprint'] != cell['fingerprint'] or proof['id'] != ident
                    or proof['arm'] != arm or proof['aged_study']['age_days'] != cell['age_days']):
                raise ValueError('aged opening differs from the manifest')
            audit = proof['bootstrap_audit']
            start, finish, after = audit['distributed'], audit['aged'], audit['after']
            if finish['timestamp'] - start['timestamp'] != cell['age_days'] * 86400:
                raise ValueError('opening clock differs from the planned age')
            scale = 10**42  # 10**36 fractional units per raw 6-decimal BUCK
            fee = (finish['net_demurrage_scaled'] - start['net_demurrage_scaled']) / scale
            invested = (audit['aged_investment'] or {}).get('credited_raw', 0) / 10**6
            fraction = int(after['jubilee_shares']) / int(after['total_shares'])
            wallets = sum(after['passive_cash_scaled']) / scale
            rows.append(f"| {cell['age_days']} | {cell['endowment_buck']:,} | {fee:,.2f} | {invested:,.2f} | {fraction:.2%} | {after['jubilee_cash_scaled'] / scale:,.2f} | {wallets:,.2f} |")
            break
    return rows + ['']


def summary(campaign, manifest, root):
    sections, groups, good, errors = {}, {}, [], []
    for cell in manifest['cells']:
        sections.setdefault(cell['regime'], [])
        if cell.get('status') != 'complete':
            errors.append((cell['id'], cell.get('status', 'unknown')))
            continue
        try:
            pair, records = validated(campaign, cell, root, manifest['tokens'])
            warm, days = cell['warmup_days'], cell['days']
            arm_results = {}
            for name in ('active', 'maintenance'):
                arm = pair['arms'][name]
                initial = int(arm['opening']['share_price'])
                final = int(arm['closing']['share_price'])
                if initial <= 0 or final <= 0:
                    raise ValueError('missing pre-switch or final share mark')
                ret = final / initial - 1
                changes = sum(int(row['pair_changes']) for row in arm['daily'][warm:])
                metrics = stock_and_flows(arm, records[name][1], warm, days)
                arm_results[name] = (ret, changes, metrics)
            a, m = arm_results['active'], arm_results['maintenance']
            if a[2][:2] != m[2][:2]:
                raise ValueError('matched treatment-opening ownership differs')
            cash = lambda x: '—' if x is None else f'{x:,.2f}'
            shares = lambda x: '—' if x is None else str(x)
            sections[cell['regime']].append([cell['id'], str(cell['seed']), str(cell['endowment_buck']),
                optional_float(a[2][0]), optional_float(a[2][1]),
                f'{a[0]:+.3%}', f'{m[0]:+.3%}', f'{100*(a[0]-m[0]):+.3f}',
                f'{a[1]} / {m[1]}', f'{shares(a[2][2])} / {shares(m[2][2])}',
                f'{cash(a[2][3])} / {cash(m[2][3])}'])
            passive, active_usdc = treatment_benchmarks(pair['arms']['active'], records['active'][2], warm)
            _, maintenance_usdc = treatment_benchmarks(pair['arms']['maintenance'], records['maintenance'][2], warm)
            sections[cell['regime']][-1].extend(optional_float(100 * x if x is not None else None)
                                                for x in (passive, active_usdc, maintenance_usdc))
            groups.setdefault(cell['regime'], []).append((a[0], m[0], passive, active_usdc))
            good.append((cell, records, pair))
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
            errors.append((cell['id'], str(exc)))
    out = ['# Matched basket stock grid', '',
           'Returns use the exact pre-switch share price and final share price, in each arm’s endogenous BUCK mark before exit costs. Δ is active minus maintenance in percentage points.', '',
           'Opening ownership uses the recorded preceding close only when its total shares and equity exactly match the pre-switch observation; otherwise it is unavailable. Receipt shares approximate TWAP equity ownership before exit costs.', '',
           'Non-Jubilee gross shares sum minted and retired other plus unattributed shares across treatment days; these are raw share units, not BUCK cash. Household gross BUCK is the confirmed cash deposit and redemption subset from run events; missing files, conflicting transactions, or in-kind redemptions are unavailable. Other non-Jubilee owners may be omitted.', '']
    out += ['The USDC share mark multiplies the BUCK share price by the sampled bid for one full-value BUCK at each endpoint. The passive TOKEN benchmark uses the active world’s venue marks in BUCK. Both require a prior close matching the pre-switch block and share price. Actual holder withdrawals can incur additional costs.', '']
    out += aged_openings(root, manifest)
    out += regime_summary(groups)
    for regime, rows in sections.items():
        out += [f'## {regime}', '', '| Cell | Seed | Endowment BUCK | Jubilee at opening | Household at opening | Active ROI | Maintenance ROI | Δ pp | Changed plans A / M | Non-Jubilee gross shares A / M | Household gross BUCK A / M | Active passive ROI (BUCK) | Active ROI (USDC) | Maintenance ROI (USDC) |',
                '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
        out += ['| ' + ' | '.join(row) + ' |' for row in rows]
        if not rows:
            out.append('| No validated pair available | — | — | — | — | — | — | — | — | — | — | — | — | — |')
        out.append('')
    if errors:
        out += ['## Unavailable or invalid', ''] + [f'- `{cell}`: {reason}' for cell, reason in errors] + ['']
    return '\n'.join(out), good


def write_comparison(root, name, manifest, good):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', name):
        raise ValueError('comparison name must contain only letters, numbers, dot, underscore or hyphen')
    verified = {cell['id']: (records, pair) for cell, records, pair in good}
    labels, runs = [], []
    dimensions = [('regime', 'regime'), ('Jubilee endowment (BUCK)', 'endowment_buck'),
                  ('investor rate', 'investor_rate'), ('exit fraction', 'exit_fraction'),
                  ('noise σ', 'noise'), ('seed', 'seed'), ('exit day', 'exit_day'),
                  ('warmup days', 'warmup_days'), ('days', 'days')]
    aged = 'aged_study' in manifest
    if aged:
        dimensions.insert(1, ('Opening ledger age (days)', 'age_days'))

    def coordinates(cell):
        values = {}
        for label, key in dimensions:
            value = cell[key]
            if aged and key == 'regime':
                value = 'history'
            if isinstance(value, (int, float)) and float(value).is_integer():
                value = int(value)
            values[label] = str(value)
        return values

    combinations = [tuple(coordinates(c).values()) for c in manifest['cells']]
    if len(set(combinations)) != len(combinations):
        dimensions.append(('cell', 'id'))
    for cell in manifest['cells']:
        ident = cell['id']
        if cell.get('status') == 'complete' and ident not in verified:
            # A failed checksum or mismatched records must never enter the index.
            continue
        labels.append(ident)
        coords = coordinates(cell)
        if ident in verified:
            records, pair = verified[ident]
            for arm in ('maintenance', 'active'):
                runs.append({'id': records[arm][0], 'values': {**coords, 'arm': arm},
                             'price_digest': cell['price_digest'], 'seed': cell['seed'],
                             'baseline_share_price': int(pair['arms'][arm]['opening']['share_price']) / 1e18,
                             'treatment_day': cell['warmup_days'], 'provisional': False})
        else:
            # Patient arms are sequential. Find the newest started attempt by
            # exact runner ID; a missing sibling stays a named placeholder.
            pattern = re.compile(re.escape(ident) + r'(?:-try(\d+))?-(active|maintenance)$')
            attempts = []
            for path in root.glob(ident + '*'):
                match = pattern.fullmatch(path.name)
                if match and (path / 'manifest.json').is_file():
                    attempts.append(int(match.group(1) or 1))
            attempt = max(attempts, default=1)
            prefix = ident + (f'-try{attempt}' if attempt > 1 else '')
            declared = cell.get('run_ids') or {}
            for arm in ('maintenance', 'active'):
                run_id = declared.get(arm) or prefix + '-' + arm
                runs.append({'id': run_id, 'values': {**coords, 'arm': arm},
                             'provisional': True})
    if not labels:
        return None
    # Prefer a cell with completed arms as the initial held setting.
    reference_id = next((c['id'] for c, _, _ in good), labels[0])
    included = [c for c in manifest['cells'] if c['id'] in labels]
    reference = coordinates(next(c for c in included if c['id'] == reference_id))
    axes = [{'name': 'arm', 'note': 'Matched post-warmup upkeep', 'reference': 'maintenance',
             'values': [{'label': x, 'set': {}} for x in ('maintenance', 'active')]}]
    for label, _ in dimensions:
        values = list(dict.fromkeys(coordinates(c)[label] for c in included))
        if len(values) > 1 or label in ('regime', 'Jubilee endowment (BUCK)'):
            axes.append({'name': label, 'note': 'Select or vary this study input',
                         'reference': reference[label], 'values': [{'label': v, 'set': {}} for v in values]})
    allowed = {axis['name'] for axis in axes}
    for run in runs:
        run['values'] = {k: v for k, v in run['values'].items() if k in allowed}
    body = {'schema': 'buck-comparison/1', 'name': name,
            'question': 'Matched upkeep arms after a common ownership warmup; provisional runs are not yet pair validated',
            'experiment': manifest.get('experiment'), 'days': max(c['days'] for c in manifest['cells']), 'set': {},
            'axes': axes, 'runs': runs}
    from alberta_buck.sim.compare import write_index
    return write_index(root, body)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('campaign', type=Path)
    p.add_argument('--runs', type=Path, help='relocated run record root (default: campaign manifest run_root)')
    p.add_argument('--output', type=Path, help='Markdown output (default: stdout)')
    p.add_argument('--comparison', help='dashboard comparison name (default: campaign directory name + -stock)')
    p.add_argument('--no-comparison', action='store_true')
    p.add_argument('--rebalancing', action='store_true', help='report allocation drift and rebalancing activity')
    p.add_argument('--watch', type=float, metavar='SECONDS', help='refresh summary and index repeatedly (minimum 5 seconds)')
    args = p.parse_args()
    if args.watch is not None and args.watch < 5:
        p.error('--watch interval must be at least 5 seconds')
    while True:
        try:
            manifest = load_json(args.campaign / 'manifest.json')
            if manifest.get('schema') != 1:
                p.error('unsupported campaign schema')
            root = args.runs or Path(manifest['run_root'])
            report, good = (rebalance_summary(args.campaign, manifest, root, validated) if args.rebalancing
                            else summary(args.campaign, manifest, root))
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                temporary = args.output.with_name(args.output.name + '.tmp')
                temporary.write_text(report)
                temporary.replace(args.output)
            else:
                print(report, end='')
            if not args.no_comparison:
                path = write_comparison(root, args.comparison or args.campaign.name + '-stock', manifest, good)
                if path:
                    print(f'Dashboard comparison: {path}', file=sys.stderr)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if args.watch is None:
                raise
            print(f'Waiting for a consistent campaign: {exc}', file=sys.stderr)
        if args.watch is None:
            break
        time.sleep(args.watch)


if __name__ == '__main__':
    main()
