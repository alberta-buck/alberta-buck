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


def test_common_inflation_and_elapsed_schedule_do_not_create_trades():
    seconds = np.arange(5) * 86_400
    prices = np.broadcast_to(np.arange(5)[:, None] * .01, (5, 5))
    arms = np.array([[0., .02, 0., 0.], [.5, .02, .75, 1.]])
    for schedule in ([300], [300, 1800, 14_400], [172_800]):
        marks, stats = study.replay(seconds, prices, arms, 300, schedule, 0, 0, 30, 2, 30)
        np.testing.assert_allclose(marks, 1., atol=1e-14)
        assert np.all(stats[:, 0] == 0)
