"""Paired disturbances, full selector coverage and selective reuse."""
import copy
import math
from pathlib import Path

import pytest

from alberta_buck.sim.control_lab.campaign import load_grid, plan
from alberta_buck.sim.control_lab.model import DEFAULTS, STAGES, feed_rows, history_rows, load_config

ROOT = Path(__file__).resolve().parents[2]


def test_walk_starts_at_zero_and_scales_the_same_path_without_seeding_history():
    cfg = copy.deepcopy(DEFAULTS)
    profile = cfg["profiles"][0]
    baseline, history = feed_rows(cfg, profile), history_rows(cfg)
    cfg["feed"]["walk_amplification"] = .5
    half = feed_rows(cfg, profile)
    cfg["feed"]["walk_amplification"] = 1
    full = feed_rows(cfg, profile)
    assert baseline[0] == half[0] == full[0]
    assert history_rows(cfg) == history
    assert full == feed_rows(cfg, profile)
    for b, h, f in zip(baseline, half, full):
        for bv, hv, fv in zip(b["target_usd"], h["target_usd"], f["target_usd"]):
            assert 2 * math.log(hv / bv) == pytest.approx(math.log(fv / bv), abs=4e-8)
    assert full[-1] != baseline[-1]
    cfg["seed"] += 1
    assert feed_rows(cfg, profile)[-1] != full[-1]


def test_full_cartesian_grid_reuses_only_inactive_controls():
    cfg = load_config(ROOT / "doc/control-lab.toml")
    grid = load_grid(ROOT / "doc/control-lab-campaign.toml")
    manifest, runs = plan(cfg, grid, {"sha256": "test"})
    assert manifest["visible_cases"] == 891
    assert len(runs) == manifest["unique_runs"] == 279
    assert len(manifest["cases"]) == len(STAGES) == 11
    assert all(len(cases) == 81 for cases in manifest["cases"].values())
    used = {k for cases in manifest["cases"].values() for k in cases.values()}
    assert used == runs.keys()
    for stage, cases in manifest["cases"].items():
        fixed = cases["three-phase:0:medium:medium"]
        assert (cases["three-phase:0:high:medium"] != fixed) == bool(STAGES[stage].get("demand"))
        assert (cases["three-phase:0:medium:high"] != fixed) == bool(STAGES[stage].get("supply"))
        assert cases["three-phase:0.5:medium:medium"] != fixed
        assert cases["mixed:0:medium:medium"] != fixed
    changed, _ = plan(cfg, grid, {"sha256": "changed"})
    assert not used.intersection(k for cases in changed["cases"].values() for k in cases.values())


def test_grid_rejects_invalid_feed_or_rate_before_any_workers_start():
    cfg = load_config(ROOT / "doc/control-lab.toml")
    grid = load_grid(ROOT / "doc/control-lab-campaign.toml")
    grid["flows"]["high"] = cfg["demand"]["max_per_day"] + 1
    with pytest.raises(ValueError, match="desired flow"):
        plan(cfg, grid, {})


def test_zero_walk_has_the_original_three_phase_prices_and_walks_are_independent():
    cfg = copy.deepcopy(DEFAULTS)
    rows = feed_rows(cfg, cfg["profiles"][0])
    for r in rows:
        expected = [round(100 * (1 + .12 * math.sin(2 * math.pi * r['day'] / 30 + i * 2 * math.pi / 3)), 6)
                    for i in range(3)]
        assert r['target_usd'] == expected
    cfg['feed']['walk_amplification'] = 1
    walked = feed_rows(cfg, cfg['profiles'][0])
    endpoints = [math.log(walked[-1]['target_usd'][i] / rows[-1]['target_usd'][i]) for i in range(3)]
    assert len(set(endpoints)) == 3


def test_plot_payload_preserves_series_units_and_gaps():
    # Deliberately distinct dollar/native prices catch an incorrect conversion;
    # an unavailable published observation must become a gap, not a zero.
    from alberta_buck.sim.control_lab.interactive import plot_data
    frame = dict(day=0, target_usd=[100, 110, 90], token_usdc=[101, 111, 91],
        token_buck=[98, 108, 88], buck_usdc=1.02, bvib=.99,
        signal=dict(raw=.99, fresh=True, excursion=[1, 2, 3], votes=[2, 4, 6],
                    invalid_after_move=False, relative=[10, 20, 30], ema=[[0] * 7] * 3),
        reference=dict(market_usd=1.01), k=.76, p_contribution=.001, i_contribution=.009,
        market=dict(arb_held_buck=10, pool_minus_virtual_buck=20, active_buck=1000),
        desk=dict(capital=1_200_000, budget=1_000_000, account=-500),
        cumulative=dict(demand_usdc=2500, supply_buck=2490),
        agents=dict(demand_cash=997500, supply_headroom=740000),
        basket=dict(weights=[3333,3333,3334], equity=1_010_000, account=-600_000))
    result = dict(frames=[frame, copy.deepcopy(frame)], flags=STAGES['all'], config=DEFAULTS,
        history=dict(mean_relative_ticks=[0, 0, 0]), bootstrap=dict(market=frame['market']),
        summary={}, stage='all', profile=dict(name='three-phase'), cache_key='test')
    result['frames'][1]['day'] = .25
    result['frames'][1]['signal']['fresh'] = False
    result['frames'][1]['basket'].update(equity=980_000, account=-750_000)
    result['frames'][1]['buck_usdc'] = 1.05
    data = plot_data(result)
    panes = {p['id']: p for p in data['panes']}
    assert data['days'] == [0, .25]
    assert all(len(line['values']) == 2 for pane in data['panes'] for line in pane['lines'])
    assert panes['price']['lines'][0]['values'][0] == pytest.approx(
        result['frames'][0]['token_buck'][0] * result['frames'][0]['buck_usdc'] / result['config']['feed']['base'][0])
    assert panes['reference']['lines'][1]['values'][1] is None
    assert panes['actors']['lines'][0]['values'][1] == pytest.approx(result['frames'][1]['cumulative']['demand_usdc'] / 1000)
    assert {'ema-0','ema-1','ema-2','votes','k','desk','custody'} <= panes.keys()
    assert len(panes['ema-0']['lines']) == 8
    # Net equity already deducts the lien. Keep the original investment
    # fixed, and convert each net mark at its own exchange rate. A native
    # loss can coexist with a dollar gain as BUCK appreciates.
    assert panes['basket-value']['unit'] == 'BUCK / USD'
    assert panes['basket-value']['lines'][0]['values'] == [1_010_000, 980_000]
    assert panes['basket-value']['lines'][1]['values'] == [1_030_200, 1_029_000]
    assert panes['basket-value']['lines'][2]['values'] == [1_000_000, 1_000_000]
    assert data['basket_performance']['gain_buck'] == -20_000
    assert data['basket_performance']['return_pct'] == pytest.approx(-2)
    assert data['basket_performance']['initial_usd'] == 1_000_000
    assert data['basket_performance']['final_net_usd'] == pytest.approx(1_029_000)
    assert data['basket_performance']['gain_usd'] == pytest.approx(29_000)
    assert data['basket_performance']['return_usd_pct'] == pytest.approx(2.9)
    assert panes['basket-return']['unit'] == 'Percent'
    assert panes['basket-return']['lines'][0]['values'] == pytest.approx([1, -2])
    assert panes['basket-return']['lines'][1]['values'] == pytest.approx([3.02, 2.9])
    assert panes['basket-return']['lines'][2]['values'] == [0, 0]


def test_cached_campaign_keeps_original_execution_time_and_rejects_partial_json(tmp_path):
    import json
    from alberta_buck.sim.control_lab.campaign import cached_entry, run_campaign
    cfg = copy.deepcopy(DEFAULTS)
    cfg['days'] = 1
    grid = dict(profiles=['three-phase'], walk=[0], flows=dict(low=2500, medium=5000, high=10000))
    manifest, runs = plan(cfg, grid, {'sha256':'test'}, ['feeds'])
    key, spec = next(iter(runs.items()))
    path = tmp_path / spec['file']
    path.write_text('{"incomplete":')
    assert cached_entry(path, key, 1, 4) is None
    result = dict(cache_key=key, frames=[dict(day=n/4) for n in range(5)],
                  profile=spec['profile'], stage='feeds', summary=dict(seconds=10))
    path.write_text(json.dumps(result))
    assert cached_entry(path, key, 1, 4)['cache_key'] == key
    assert cached_entry(path, key, 120, 4) is None
    original = {**manifest, 'status':'complete', 'elapsed_seconds':123.4,
                'started_at':'2026-10-03T00:00:00+00:00', 'finished_at':'2026-10-03T00:02:03+00:00'}
    (tmp_path / 'manifest.json').write_text(json.dumps(original))
    run_campaign(manifest, runs, tmp_path, jobs=4)
    final = json.loads((tmp_path / 'manifest.json').read_text())
    assert final['elapsed_seconds'] == 123.4
    assert final['started_at'] == original['started_at']
    assert final['status'] == 'complete' and final['cached_runs'] == 1
