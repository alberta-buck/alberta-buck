"""Check campaign completeness, provenance and accounting from saved JSON."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .model import STAGES, dependencies, digest


def audit(root, partial=False):
    root = Path(root)
    manifest = json.loads((root / ('progress.json' if partial else 'manifest.json')).read_text())
    cases, entries = manifest['cases'], manifest['entries']
    required = {k for choices in cases.values() for k in choices.values()}
    assert len(required) == manifest['unique_runs'], 'distinct-case count mismatch'
    assert sum(map(len, cases.values())) == manifest['visible_cases'], 'Cartesian count mismatch'
    assert set(entries) <= required, 'unexpected cached entry'
    if not partial:
        assert manifest['status'] == 'complete', 'campaign is not complete'
        assert set(entries) == required, 'missing results'
    from .campaign import plan
    expected, _ = plan(manifest['requested_config'], manifest['grid'], manifest['implementation'], list(manifest['stages']))
    assert expected['cases'] == cases, 'selectors do not resolve to their active inputs'
    feeds, market_paths, samples, transactions, summary = {}, {}, 0, 0, []
    recorded_runs = recorded_cycles = recorded_desk_ops = recorded_basket_swaps = 0
    for key, entry in entries.items():
        result = json.loads((root / entry['file']).read_text())
        cfg, flags, frames = result['config'], result['flags'], result['frames']
        context = (entry['profile'], entry['stage'], key[:12])
        assert result['backend'] == 'pyrevm', context
        assert flags == STAGES[result['stage']], context
        assert bool(frames[0].get('pair_signal')) == bool(flags.get('monitor')), context
        if flags.get('basket'):
            assert result.get('basket_mode') == 'pair' and 'pair_policy' in frames[0], context
        opening = result['bootstrap']
        assert opening['opening_twap_settled'], context
        assert opening['opening_target_usd'] == frames[0]['target_usd'], context
        for i, price in enumerate(opening['opening_target_usd']):
            for route in ('token_buck', 'token_usdc'):
                assert math.isclose(opening['prices'][route][i], price, rel_tol=1e-6), ('opening price', context)
        assert result['cache_key'] == key, context
        deps = dependencies(cfg, result['profile'], result['stage'])
        assert deps == result['dependencies'], context
        assert digest(dict(schema=1, implementation=manifest['implementation'], inputs=deps)) == key, context
        assert result['implementation'] == manifest['implementation'], context
        assert len(frames) == cfg['days'] * cfg['ticks_per_day'] + 1, context
        assert result['summary']['samples'] == len(frames), context
        assert [f['day'] for f in frames] == [n / cfg['ticks_per_day'] for n in range(len(frames))], context
        assert math.isclose(result['bootstrap']['reference_at_means'], 1, rel_tol=0, abs_tol=1e-12), context
        feed_key = digest(dict(feed=cfg['feed'], profile=result['profile'], seed=cfg['seed'], days=cfg['days'], tpd=cfg['ticks_per_day']))
        target = [f['target_usd'] for f in frames]
        if feed_key in feeds:
            assert feeds[feed_key] == target, ('unpaired feeds', context)
        else:
            feeds[feed_key] = target
        if result['stage'] in ('monitor', 'monitor-k', 'linked', 'k'):
            market_paths[feed_key, result['stage']] = [
                [*f['token_usdc'], *f['token_buck'], f['buck_usdc']] for f in frames]
        previous_budget = math.inf
        event_cycles = event_desk = 0
        for f in frames:
            assert all(math.isfinite(v) and v > 0 for v in [f['bvib'], f['buck_usdc'], *f['token_usdc'], *f['token_buck'], *f['target_usd']]), context
            assert cfg['controller']['kmin'] - 1e-12 <= f['k'] <= cfg['controller']['kmax'] + 1e-12, context
            if not flags.get('k'):
                assert f['k'] == cfg['controller']['k0'], context
            for flag, counter in [('arb','arb_cycles'), ('desk','desk_ops'), ('demand','demand_usdc'), ('supply','supply_buck')]:
                if not flags.get(flag):
                    assert f['cumulative'][counter] == 0, (flag, context)
            assert f['agents']['demand_cash'] >= 0 and f['agents']['supply_headroom'] >= 0, context
            assert math.isclose(f['agents']['demand_cash'] + f['cumulative']['demand_usdc'], cfg['demand']['budget_usdc'], rel_tol=0, abs_tol=1e-5), context
            assert math.isclose(f['market']['arb_held_buck'], f['cumulative']['arb_profit_buck'], rel_tol=0, abs_tol=1e-5), context
            assert 0 <= f['desk']['budget'] <= previous_budget, context
            previous_budget = f['desk']['budget']
            if flags.get('basket'):
                assert sum(f['basket']['weights']) == 10000, context
            if 'events' in frames[0]:
                assert 'events' in f, ('missing execution records', context)
                for e in f['events']:
                    assert flags.get(e['actor']), ('inactive actor executed', e['actor'], context)
                    assert e['phase'] in ('after-feed', 'after-agents', 'policy', 'after-policy'), context
                    assert 0 < e['transaction'] <= f['transactions'], context
                    event_cycles += e['actor'] == 'arb'
                    event_desk += e['actor'] == 'desk'
                    if e['actor'] == 'arb':
                        assert len(e['trades']) == 2 and e['profit_buck'] > 0, context
                        assert {t['token'] for t in e['trades']} == {e['token']}, context
                        assert {t['quote'] for t in e['trades']} == {'BUCK', 'USDC'}, context
                        assert {t['side'] for t in e['trades']} == {'buy', 'sell'}, context
                    if e['actor'] == 'basket':
                        recorded_basket_swaps += len(e['trades'])
                        if e['action'] in ('pair', 'mark'):
                            assert not e['trades'], ('LP pairing or valuation mislabeled as a trade', context)
                    for t in e['trades']:
                        assert t['side'] in ('buy', 'sell') and 0 <= t['token'] < 3, context
                        assert int(t['token_raw']) > 0 and int(t['quote_raw']) > 0, context
                        assert math.isclose(t['token_amount'] * t['price_quote'], t['quote_amount'], rel_tol=1e-12), context
                assert event_cycles == f['cumulative']['arb_cycles'], ('missing arb event', context)
                assert event_desk == f['cumulative']['desk_ops'], ('missing desk event', context)
        if 'events' in frames[0]:
            recorded_runs += 1
            recorded_cycles += event_cycles
            recorded_desk_ops += event_desk
        samples += len(frames)
        transactions += result['summary']['transactions']
        summary.append(dict(profile=entry['profile'], stage=entry['stage'],
            walk=cfg['feed']['walk_amplification'], buyer=cfg['demand']['rate_at_par_per_day'] if flags.get('demand') else None,
            seller=cfg['supply']['rate_at_par_per_day'] if flags.get('supply') else None,
            mean_bvib=sum(f['bvib'] for f in frames) / len(frames), **result['summary']))
    control_pairs = 0
    for feed_key in feeds:
        for fixed, moving in [('monitor', 'monitor-k')]:
            if (feed_key, fixed) in market_paths and (feed_key, moving) in market_paths:
                assert market_paths[feed_key, fixed] == market_paths[feed_key, moving], ('K alone changed prices', fixed, moving, feed_key)
                control_pairs += 1
    report = dict(status='partial' if partial else 'complete', verified_runs=len(entries), required_runs=len(required),
                  paired_price_paths=len(feeds), samples=samples, transactions=transactions,
                  identical_fixed_vs_moving_k_pairs=control_pairs,
                  raw_bytes=sum((root / e['file']).stat().st_size for e in entries.values()),
                  source_sha256=manifest['implementation']['sha256'], summaries=summary)
    report['executions'] = dict(recorded_runs=recorded_runs, arb_cycles=recorded_cycles,
                                desk_operations=recorded_desk_ops, basket_swaps=recorded_basket_swaps)
    return report


def main(argv=None):
    from .cli import atomic_json
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', default='doc/control-lab/interactive')
    ap.add_argument('--partial', action='store_true', help='audit the running progress manifest')
    args = ap.parse_args(argv)
    report = audit(args.out, args.partial)
    atomic_json(Path(args.out) / ('audit-partial.json' if args.partial else 'audit.json'), report)
    print(json.dumps({k:v for k,v in report.items() if k != 'summaries'}, indent=2))


if __name__ == '__main__':
    main()
