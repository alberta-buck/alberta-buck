"""Economic/sensor invariants, independent of any fitted sine-wave result."""
import numpy as np
import pytest

from alberta_buck.sim.pair_observer import (
    ObserverConfig, PairObserver, allocation_candidates, contrasts, motion_desire, transition,
)


def test_relative_graph_has_only_n_minus_one_dimensions():
    b = contrasts(6)
    np.testing.assert_allclose(b.T @ b, np.eye(5), atol=1e-14)
    np.testing.assert_allclose(b.T @ np.ones(6), 0, atol=1e-14)
    obs = PairObserver([1.] * 6)
    assert len(obs.pairs) == 15
    assert np.linalg.matrix_rank(obs.edges) == 5
    r = obs.observe(0, {"BUCK": [1, 2, 4, 3, 5, 7], "USDC": [2, 4, 8, 6, 10, 14]})
    levels = {(p["i"], p["j"]): p["spread"] for p in r["pairs"]}
    assert levels[0, 1] + levels[1, 2] == pytest.approx(levels[0, 2], abs=1e-13)


def test_arbitrary_numeraire_motion_cancels_in_state_covariance_and_noise():
    a, b = PairObserver([100, 50, 10]), PairObserver([100, 50, 10])
    rng = np.random.default_rng(42)
    for k in range(100):
        p = np.exp(rng.normal(0, .005, (2, 3))) * [100, 50, 10]
        ra = a.observe(k / 4, dict(zip(a.venues, p)))
        rb = b.observe(k / 4, dict(zip(b.venues, p * np.exp(rng.normal(0, .3, (2, 1))))))
        np.testing.assert_allclose(a.x, b.x, atol=1e-10)
        np.testing.assert_allclose(a.p, b.p, atol=1e-13)
        np.testing.assert_allclose(a.r, b.r, atol=1e-13)
        assert [p["inward_score"] for p in ra["pairs"]] == pytest.approx([p["inward_score"] for p in rb["pairs"]], abs=1e-8)


def test_current_known_impact_offset_is_removed_once_without_erasing_external_motion():
    a, b = PairObserver([100] * 3), PairObserver([100] * 3)
    for k in range(80):
        external = np.array([100 * np.exp(.002 * k), 100, 100])
        own = np.array([-.08, .06, 0]) * np.exp(-max(k - 20, 0) / 10) * (k >= 20)
        a.observe(k / 4, {"BUCK": external, "USDC": external})
        b.observe(k / 4, {"BUCK": external * np.exp(own), "USDC": external}, {"BUCK": own})
        np.testing.assert_allclose(a.x, b.x, atol=1e-11)
    assert a.x[0] != 0  # the external trend was retained


def test_prediction_respects_elapsed_time_and_missing_samples_increase_uncertainty():
    f1, q1 = transition(.4, 2, 1e-6)
    f2, q2 = transition(.6, 2, 1e-6)
    f, q = transition(1, 2, 1e-6)
    np.testing.assert_allclose(f2 @ f1, f, atol=1e-14)
    np.testing.assert_allclose(f2 @ q1 @ f2.T + q2, q, atol=1e-16)
    o = PairObserver([1, 1, 1])
    o.observe(0, {"BUCK": [1, 1, 1]})
    before = np.trace(o.p)
    r = o.observe(5, {})
    assert not r["fresh"] and all(p["inward_score"] == 0 for p in r["pairs"])
    assert np.trace(o.p) > before
    with pytest.raises(ValueError, match="strictly increase"):
        o.observe(5, {"BUCK": [1, 1, 1]})


def test_correlated_venues_do_not_count_as_independent_witnesses():
    p = {"BUCK": [1.01, 1, .99], "USDC": [1.01, 1, .99]}
    independent = PairObserver([1]*3, config=ObserverConfig(venue_correlation_prior=0))
    correlated = PairObserver([1]*3, config=ObserverConfig(venue_correlation_prior=.999))
    single = PairObserver([1]*3, venues=("BUCK",))
    for o in (independent, correlated, single):
        o.observe(0, p)
    assert correlated.p[0, 0] > 1.9 * independent.p[0, 0]
    assert correlated.p[0, 0] == pytest.approx(single.p[0, 0], rel=.002)


def test_future_suffix_cannot_change_prefix_estimates():
    prices = [{"BUCK": [100 + k, 100, 100], "USDC": [100 + k, 100, 100]} for k in range(20)]
    a = PairObserver([100]*3)
    prefix = [a.observe(k / 4, p) for k, p in enumerate(prices[:10])]
    b = PairObserver([100]*3)
    full = [b.observe(k / 4, p) for k, p in enumerate(prices)]
    assert prefix == full[:10]


def test_linear_motion_and_acceleration_are_observable_with_psd_covariance():
    o = PairObserver([100]*3)
    for k in range(200):
        t = k / 4
        prices = np.exp([.004*t + .0001*t*t, 0, 0]) * 100
        r = o.observe(t, {"BUCK": prices, "USDC": prices})
        assert np.linalg.eigvalsh(o.p).min() >= -1e-15
        assert np.linalg.eigvalsh(o.r).min() >= o.config.noise_floor**2 * .999
    pair = r["pairs"][0]
    assert pair["velocity"] == pytest.approx(.004 + .0002*t, abs=1e-5)
    assert pair["acceleration"] == pytest.approx(.0002, abs=1e-5)
    assert pair["regime"] == "departing"


def test_pair_candidates_obey_actual_book_direction_and_shared_leg_capacity():
    snap = dict(fresh=True, pairs=[dict(i=i, j=j, velocity=v, acceleration=0, sigma=[.001]*3)
                                 for i, j, v in [(0, 1, -.03), (0, 2, -.03), (1, 2, .03)]])
    proposals = allocation_candidates(snap, [.5, .25, .25], [1/3]*3)
    assert len(proposals) == 2
    assert all(p["sell"] == 0 for p in proposals)
    assert sum(p["nav_fraction"] for p in proposals) <= .5 - 1/3 - .002 + 1e-12
    # Opposite actual allocation overrides any historical price spread sign.
    snap["pairs"][0]["velocity"] = .03
    proposals = allocation_candidates(snap, [.2, .5, .3], [1/3]*3)
    assert any(p["sell"] == 1 and p["buy"] == 0 for p in proposals)
    assert allocation_candidates({**snap, "fresh": False}, [.5, .25, .25], [1/3]*3) == []


def test_cessation_releases_before_reversal_and_closure_accelerates_participation():
    speeds = [.02, .004, .002, .001, 0, -.002, -.01]
    rates = [motion_desire(v, .001, .0001, .0001)["rate"] for v in speeds]
    assert rates[0] == 0
    assert all(a <= b for a, b in zip(rates, rates[1:]))
    assert rates[2] > 0  # still departing, but braking: no zero-crossing gate
    assert rates[-1] > 5 * rates[4]
    assert motion_desire(.002, 0, .0001, .0001)["rate"] == 0  # steady departure
    assert motion_desire(0, 0, .0001, .0001)["rate"] > 0  # settled plateau


def test_destinations_share_sale_by_shortfall_not_duplicate_pair_pressure():
    snap = dict(fresh=True, pairs=[dict(i=i, j=j, velocity=v, acceleration=0, sigma=[.0001]*3)
                                 for i, j, v in [(0, 1, -.02), (0, 2, -.005), (1, 2, 0)]])
    weights = [.6, .25, .15]
    p = allocation_candidates(snap, weights, [1/3]*3)
    assert len(p) == 2
    assert p[0]["nav_fraction"] / p[1]["nav_fraction"] == pytest.approx((1/3-.25-.002)/(1/3-.15-.002))
    assert sum(v["nav_fraction"] for v in p) < .6-1/3-.002


def test_spectral_parameters_resolve_slow_and_fast_history_without_future():
    from alberta_buck.sim.pair_spectrum import spectrum
    t = np.arange(1280)
    fast = spectrum(.1*np.sin(2*np.pi*t/20))
    slow = spectrum(.1*np.sin(2*np.pi*t/180))
    assert fast["period_days"] == pytest.approx(20, rel=.02)
    assert slow["period_days"] == pytest.approx(180, rel=.05)
    assert fast["response"]["release_rate"] > 8 * slow["response"]["release_rate"]
    assert fast["response"]["release_speed"] > 8 * slow["response"]["release_speed"]
    assert not spectrum(np.zeros(1280))["resolved"]
    assert not spectrum(np.random.default_rng(1).normal(0, .001, 1280))["resolved"]


def test_longer_run_preserves_noise_history_and_existing_path_prefix():
    from alberta_buck.sim.pair_observer_lab import inputs
    short = inputs("three-phase", 12, 42, .008, warmup=64)
    long = inputs("three-phase", 20, 42, .008, warmup=64)
    np.testing.assert_array_equal(short["observed"], long["observed"][:len(short["t"])])


def test_full_numeraire_only_run_has_no_relative_motion_or_allocation_requests():
    import json
    from alberta_buck.sim.pair_observer_lab import evaluate_run
    r = evaluate_run("numeraire-only", 50, 42, 0)
    json.dumps(r, allow_nan=False)  # all telemetry is portable JSON
    assert all(not row["candidates"] and not row["cycle_candidates"] for row in r["records"])
    assert max(abs(p["velocity"]) for row in r["records"] for p in row["fused"]["pairs"]) < 1e-12
    assert all(not p["resolved"] for p in r["spectra"]["pairs"])


@pytest.mark.parametrize("prices", [{"BUCK": [1, 2]}, {"BUCK": [1, 0, 2]}, {"BUCK": [1, float('nan'), 2]}])
def test_malformed_observation_rejected_before_state_mutation(prices):
    o = PairObserver([1]*3)
    with pytest.raises(ValueError):
        o.observe(0, prices)
    assert o.last_time is None and np.all(o.x == 0)
