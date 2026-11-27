import math

import numpy as np

from alberta_buck.sim import pair_price_study as study


def test_bridge_preserves_anchors_at_each_noise_level():
    seconds = np.array([0, 3600, 7200])
    logs = np.arange(15, dtype=float).reshape(3, 5) / 100
    flat = list(study.bridge_chunks(seconds, logs, 12, 0, 7))
    noisy = list(study.bridge_chunks(seconds, logs, 12, 150, 7))
    again = list(study.bridge_chunks(seconds, logs, 12, 150, 7))
    for index in range(2):
        np.testing.assert_array_equal(logs[index + 1], noisy[index][-1])
        np.testing.assert_array_equal(noisy[index], again[index])
        assert not np.array_equal(noisy[index][:-1], flat[index][:-1])


def test_urgency_uses_time_and_resets_on_a_side_change():
    whole, side = study.urgency_step(0., 1, .0125, 1., .0025, .02)
    partial = 0.
    for dt in (.125, .5, .375):
        partial, _ = study.urgency_step(partial, 1, .0125, dt, .0025, .02)
    assert math.isclose(whole, partial)
    assert study.urgency_step(whole, side, -.0125, .5, .0025, .02) == (.005, -1)
    assert study.urgency_step(0., side, .0125, 3., .0025, .02) == (whole, side)


def test_exponential_release_composes_across_uneven_calls():
    rate = .4
    hours = [12 / 3600, 1 / 60, .5, 4]
    allowance = study.allowance_step(0., rate, sum(hours) / 24)
    remaining = math.prod(math.exp(-study.allowance_step(0., rate, dt / 24)) for dt in hours)
    assert math.isclose(math.exp(-allowance), remaining)
    assert study.allowance_step(0., rate, 3.) == rate


def test_outward_release_tapers_with_acceleration_and_grows_with_separation():
    params = dict(rate=.06, allowance=.03, drift_fraction=.5, cap=.69)
    assert study.early_rate(.12, change=.10, **params) == 0
    assert study.early_rate(.30, change=.10, **params) > 0
    assert study.early_rate(.30, change=.02, **params) > study.early_rate(.30, change=.10, **params)
    assert study.early_rate(.30, change=.02, **(params | {"drift_fraction": 0})) == 0


def test_small_stall_release_requires_no_clear_outward_motion():
    stall = study.settling_rate(0., 0., .01, .69)
    assert 0 < stall < .69
    assert study.settling_rate(.003, .001, .01, .69) < stall
    assert study.settling_rate(0., .003, .01, .69) == 0
    assert study.settling_rate(.02, 0., .01, .69) == 0


def test_common_inflation_and_elapsed_schedule_do_not_create_trades():
    seconds = np.arange(5) * 86_400
    prices = np.broadcast_to(np.arange(5)[:, None] * .01, (5, 5))
    arms = np.array([[0., .02, 0., 0., 0., 0., 0., 0.], [.5, .02, .75, 1., 0., 0., 0., 0.]])
    for schedule in ([300], [300, 1800, 14_400], [172_800]):
        marks, stats, _ = study.replay(seconds, prices, arms, 300, schedule, 0, 0, 30, 2, 30)
        np.testing.assert_allclose(marks, 1., atol=1e-14)
        assert np.all(stats[:, 0] == 0)


def test_spread_braking_requires_a_clearer_turn_at_narrow_separation():
    def result(excess, change, wide_threshold=.05):
        return study.spread_braking_rate(study.SEPARATION + excess, .05, change,
                                        .02, .10, wide_threshold, 4., .69)

    assert result(.002, -.004) == 0.  # weak braking near the reference
    assert result(.12, -.004) > 0.  # the same braking far from the reference
    assert result(.002, -.035) > 0.  # clear inward acceleration near the reference
    assert result(.12, .002) == 0.  # mild-braking arm requires inward change
    assert result(.12, .002, -.25) > 0.  # signed-threshold arm tolerates weak outward acceleration
    assert result(.12, .008, -.25) == 0.


def test_full_history_recovers_block_observer_without_intermediate_wheel_calls():
    seconds = np.arange(8) * 86_400
    logs = np.zeros((len(seconds), len(study.WEIGHTS)))
    logs[:, 1] = .08 * np.cos(np.arange(len(seconds)))
    arms = np.array([[0., 0., 0., 0., 0., 0., 0., 0.]])
    # Seven days end on the same boundary for both schedules.
    fine, fine_stats, sampled = study.replay(seconds, logs, arms, 300, [300], 50, 7, 30, 2, 30)
    uneven, uneven_stats, restored = study.replay(seconds, logs, arms, 300, [3600, 21_600], 50, 7, 30, 2, 30,
                                                  history=21_600)
    _, _, shortened = study.replay(seconds, logs, arms, 300, [3600, 21_600], 50, 7, 30, 2, 30,
                                   history=3600)
    np.testing.assert_allclose(restored["levels"], sampled["levels"], atol=1e-13)
    np.testing.assert_allclose(restored["rates"], sampled["rates"], atol=1e-13)
    assert restored["observer_updates"] == sampled["observer_updates"]
    assert shortened["observer_updates"] < restored["observer_updates"]
    assert shortened["truncated_windows"] == restored["wheel_calls"] // 2
    assert restored["truncated_windows"] == 0
    assert restored["wheel_calls"] < sampled["wheel_calls"]
    np.testing.assert_allclose(uneven, fine, atol=1e-13)
    assert fine_stats[0, 0] == uneven_stats[0, 0] == 0


def test_history_urgency_reconstructs_a_side_change_without_extra_executions():
    hours = np.arange(7 * 24 + 1)
    seconds = hours * 3600
    logs = np.zeros((len(hours), len(study.WEIGHTS)))
    # Sustained spread with very brief excursions toward the anchor inside
    # wheel gaps. Holdings never hit the declared-weight leash.
    logs[1:, 1] = np.where(hours[1:] % 7 == 5, .02, -.18)
    arms = np.array([[0., .02, 0., 0., 0., 0., 0., 0.]])
    def run(fill):
        return study.replay(seconds, logs, arms, 300, [3600, 21_600], 0, 0, 30, 2, 30,
                            history=21_600, history_step=3600, history_urgency=fill)
    _, baseline, observer = run(False)
    _, backfill, same_observer = run(True)
    assert observer == same_observer
    assert baseline[0, 3] > backfill[0, 3]
