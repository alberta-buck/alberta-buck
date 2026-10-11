"""The synthetic commodity profile: trend, lags, bounded uncorrelated oscillations."""
import copy
import math
from pathlib import Path

import pytest

from alberta_buck.sim.control_lab import commodities
from alberta_buck.sim.control_lab.campaign import load_grid, plan
from alberta_buck.sim.control_lab.commodities import commodity_inputs
from alberta_buck.sim.control_lab.model import DEFAULTS, STAGES, dependencies, feed_rows, load_config, own_path

ROOT = Path(__file__).resolve().parents[2]
PROFILE = next(p for p in load_config(ROOT / "doc/control-lab.toml")["profiles"] if p["name"] == "commodities")


def quiet(**changes):
    """The committed profile without per-sample noise, so samples can be compared exactly."""
    profile = copy.deepcopy(PROFILE)
    profile["commodities"].update({"noise": 0., **changes})
    return profile


def pearson(a, b):
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(
        sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))


def test_usd_price_is_the_real_price_times_the_inflation_level():
    cfg = copy.deepcopy(DEFAULTS)
    data = commodity_inputs(cfg, PROFILE)
    rows = data["rows"]
    assert len(rows) == 481 and rows[0]["inflation_usd"] == 1
    assert feed_rows(cfg, PROFILE) == rows
    for r in rows[::37]:
        assert r["target_usd"] == pytest.approx([p * r["inflation_usd"] for p in r["real_usd"]], abs=2e-6)
    # Eight years at 3% and two at 9%: the level every TOKEN ends near.
    assert rows[-1]["inflation_usd"] == pytest.approx(1.03 ** 8 * 1.09 ** 2, rel=1e-8)
    assert data["provenance"]["ending_inflation_usd"] == rows[-1]["inflation_usd"]
    for i, token in enumerate(PROFILE["commodities"]["tokens"]):
        ending = math.log(rows[-1]["target_usd"][i] / 100 / rows[-1]["inflation_usd"])
        assert token["floor"] - .08 < ending < token["ceiling"] + .08


def test_more_future_does_not_move_the_opening_price_level():
    cfg = copy.deepcopy(DEFAULTS)
    longer = copy.deepcopy(cfg)
    longer["days"] = 240
    # More future does not rewrite the trend or the lags; only the decorrelation
    # constants, taken over the live window, may move the oscillations slightly.
    assert commodity_inputs(cfg, quiet())["rows"][0]["inflation_usd"] == 1
    assert commodity_inputs(longer, quiet())["rows"][0]["inflation_usd"] == 1


def test_the_path_does_not_depend_on_the_sampling_rate():
    coarse, fine = copy.deepcopy(DEFAULTS), copy.deepcopy(DEFAULTS)
    fine["ticks_per_day"] = 24
    a, b = commodity_inputs(coarse, quiet())["rows"], commodity_inputs(fine, quiet())["rows"]
    assert len(b) == 2881
    for k, row in enumerate(a):
        assert b[6 * k]["day"] == row["day"] and b[6 * k]["target_usd"] == row["target_usd"]
    noisy = commodity_inputs(coarse, PROFILE)["rows"]
    ratios = [math.log(n["target_usd"][0] / q["target_usd"][0]) for n, q in zip(noisy, a)]
    assert math.sqrt(sum(x * x for x in ratios) / len(ratios)) == pytest.approx(.0015, rel=.15)


def test_inflation_level_and_lag_gap_are_the_closed_forms():
    segments = commodities._inflation([[0, 3.], [36, 9.], [60, 3.]], 1.)
    slow, fast = math.log(1.03) / 12, math.log(1.09) / 12
    assert commodities._level(segments, -10) == pytest.approx(-10 * slow)
    assert commodities._level(segments, 36) == pytest.approx(36 * slow)
    assert commodities._level(segments, 48) == pytest.approx(36 * slow + 12 * fast)
    assert commodities._level(segments, 120) == pytest.approx(96 * slow + 24 * fast)
    for lag in (3., 16.):
        assert commodities._lag_gap(segments, lag, 36) == 0             # steady before the burst
        rest = -lag * (fast - slow)
        during = commodities._lag_gap(segments, lag, 48)
        assert during == pytest.approx(rest * (1 - math.exp(-12 / lag)))
        at_end = rest * (1 - math.exp(-24 / lag))
        assert commodities._lag_gap(segments, lag, 60) == pytest.approx(at_end)
        assert commodities._lag_gap(segments, lag, 90) == pytest.approx(at_end * math.exp(-30 / lag))
    # The slow TOKEN falls further behind and takes longer to return.
    assert commodities._lag_gap(segments, 16., 60) < commodities._lag_gap(segments, 3., 60) < 0
    assert abs(commodities._lag_gap(segments, 16., 90)) > 10 * abs(commodities._lag_gap(segments, 3., 90))
    assert commodities._lag_gap(segments, 0., 50) == 0
    # Half the compression: the same months arrive in twice the days.
    assert commodities._level(commodities._inflation([[0, 3.], [72, 9.]], .5), 40) == pytest.approx(36 * slow + 4 * fast)


def test_oscillations_have_the_requested_spread_and_crossing_time():
    for index, token in enumerate(PROFILE["commodities"]["tokens"]):
        waves = commodities._oscillation(41404, index, token, 10)
        assert sum(share * share for _, _, share in waves) == pytest.approx(1)
        rms = math.sqrt(sum(share * share * w * w for w, _, share in waves))
        assert math.pi / rms == pytest.approx(token["crossing_months"])
        months = [k / 4 for k in range(40_000)]                          # ten thousand months
        shape = [commodities._shape(waves, m) for m in months]
        assert math.sqrt(sum(x * x for x in shape) / len(shape)) == pytest.approx(1, rel=.02)
        crossings = sum(1 for a, b in zip(shape, shape[1:]) if (a < 0) != (b < 0))
        assert 10_000 / crossings == pytest.approx(token["crossing_months"], rel=.25)
    assert commodities._oscillation(41404, 0, token, 10) != commodities._oscillation(41405, 0, token, 10)


def test_the_three_oscillations_are_uncorrelated_over_the_live_window():
    tokens = PROFILE["commodities"]["tokens"]
    for seed in (41404, 41405, 7):
        waves = [commodities._oscillation(seed, i, t, 10) for i, t in enumerate(tokens)]
        months = [k / 4 for k in range(481)]
        raw = [[commodities._shape(w, m) for m in months] for w in waves]
        mix = commodities._unmix(raw)
        free = [dict(t, floor=None, ceiling=None) for t in tokens]
        mixed = list(zip(*[commodities._departures(free, waves, mix, m) for m in months]))
        for a, b in ((0, 1), (0, 2), (1, 2)):
            assert pearson(mixed[a], mixed[b]) == pytest.approx(0, abs=1e-9)
        assert mixed[0] == pytest.approx([tokens[0]["spread"] * x for x in raw[0]])   # the first is left alone
    # Unmixed, the committed seed's first two would be far from uncorrelated.
    waves = [commodities._oscillation(41404, i, t, 10) for i, t in enumerate(tokens)]
    raw = [[commodities._shape(w, k / 4) for k in range(481)] for w in waves]
    assert abs(pearson(raw[0], raw[1])) > .3


def test_bounds_squash_softly_with_the_floor_the_tighter():
    token = dict(spread=.5, floor=-.1, ceiling=.3)
    waves = [(1., 0., 1.)]
    values = [commodities._departures([token], [waves], ([[]], [0.]), m / 10)[0] for m in range(200)]
    assert -.1 < min(values) < -.095 and .25 < max(values) < .3
    small = commodities._departures([dict(spread=.001, floor=-.1, ceiling=.3)], [waves], ([[]], [0.]), 0.)[0]
    assert small == pytest.approx(.001 * math.sqrt(2), rel=1e-3)        # untouched well inside the bounds


def test_seed_moves_the_oscillations_and_leaves_the_trend():
    a, b = copy.deepcopy(DEFAULTS), copy.deepcopy(DEFAULTS)
    b["seed"] += 1
    ra, rb = commodity_inputs(a, PROFILE)["rows"], commodity_inputs(b, PROFILE)["rows"]
    assert [r["inflation_usd"] for r in ra] == [r["inflation_usd"] for r in rb]
    assert ra[200]["target_usd"] != rb[200]["target_usd"]
    assert commodity_inputs(a, PROFILE) == commodity_inputs(copy.deepcopy(DEFAULTS), PROFILE)


def test_a_complete_path_refuses_other_disturbances_and_bad_parameters():
    cfg = copy.deepcopy(DEFAULTS)
    assert own_path(PROFILE) and not own_path(cfg["profiles"][0]) and not own_path(None)
    cfg["feed"]["walk_amplification"] = .5
    with pytest.raises(ValueError, match="complete path"):
        feed_rows(cfg, PROFILE)
    cfg = copy.deepcopy(DEFAULTS)
    with pytest.raises(ValueError, match="complete path"):
        feed_rows(cfg, dict(PROFILE, perturbations=[dict(kind="noise")]))
    for change in (dict(inflation=[[5, 3.]]), dict(inflation=[[0, 3.], [0, 4.]]), dict(months_per_day=0), dict(noise=.2),
                   dict(tokens=PROFILE["commodities"]["tokens"][:2]),
                   dict(tokens=[dict(PROFILE["commodities"]["tokens"][0], floor=.1)] * 3),
                   dict(tokens=[dict(PROFILE["commodities"]["tokens"][0], crossing_months=0)] * 3)):
        with pytest.raises(ValueError):
            commodity_inputs(cfg, quiet(**change))


def test_profile_parameters_are_part_of_every_stage_key():
    cfg = copy.deepcopy(DEFAULTS)
    changed = quiet(inflation=[[0, 2.]])
    for stage in STAGES:
        assert dependencies(cfg, PROFILE, stage) != dependencies(cfg, changed, stage)


def test_grid_offers_the_commodity_profile_without_a_walk():
    cfg, grid = load_config(ROOT / "doc/control-lab.toml"), load_grid(ROOT / "doc/control-lab-campaign.toml")
    manifest, runs = plan(cfg, grid, {"sha256": "test"})
    for stage, cases in manifest["cases"].items():
        mine = [k for k in cases if k.startswith("commodities:")]
        assert len(mine) == 9 and all(k.split(":")[1] == "0" for k in mine)
        assert (cases["commodities:0:high:medium"] != cases["commodities:0:medium:medium"]) == bool(STAGES[stage].get("demand"))
    assert sum(1 for r in runs.values() if r["profile"]["name"] == "commodities") == 31
