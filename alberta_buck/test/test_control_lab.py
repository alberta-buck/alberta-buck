"""Causal controls, deterministic feeds, and real-EVM lab wiring."""
import copy
from pathlib import Path

import pytest

from alberta_buck.sim.control_lab.model import DEFAULTS, dependencies, digest, feed_rows, history_rows, load_config
from alberta_buck.sim.signal_history import historical_state


CONFIG = Path(__file__).resolve().parents[2] / "doc/control-lab.toml"


def test_three_phases_have_constant_common_mode():
    cfg = copy.deepcopy(DEFAULTS)
    rows = feed_rows(cfg, cfg["profiles"][0])
    assert len(rows) == 481
    assert rows[0]["day"] == 0 and rows[-1]["day"] == 120
    assert max(abs(sum(r["target_usd"]) - 300) for r in rows) < 2e-6
    assert max(r["target_usd"][0] for r in rows) > 111


def test_selective_ramp_and_seeded_noise():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["feed"]["amplitude"] = 0
    profile = dict(name="ramp", perturbations=[dict(kind="ramp", tokens=[1], start=2, duration=2, amplitude=.2)])
    rows = feed_rows(cfg, profile)
    assert rows[0]["target_usd"] == [100., 100., 100.]
    assert rows[16]["target_usd"] == [100., 120., 100.]
    assert rows[-1]["target_usd"] == [100., 120., 100.]
    profile["perturbations"].append(dict(kind="noise", amplitude=.002, independent=True))
    assert feed_rows(cfg, profile) == feed_rows(cfg, profile)
    changed = copy.deepcopy(cfg)
    changed["seed"] += 1
    assert feed_rows(cfg, profile) != feed_rows(changed, profile)


def test_impossible_feed_is_rejected_instead_of_clipped():
    cfg = copy.deepcopy(DEFAULTS)
    with pytest.raises(ValueError, match="nonpositive"):
        feed_rows(cfg, dict(name="bad", perturbations=[dict(kind="step", amplitude=-2)]))


def test_cache_invalidation_tracks_enabled_mechanisms():
    cfg = load_config(CONFIG)
    profile = cfg["profiles"][1]
    changed = copy.deepcopy(cfg)
    changed["desk"]["gain_bp"] += 100
    for stage in ("feeds", "monitor", "linked", "basket", "k", "demand", "supply", "both"):
        assert digest(dependencies(cfg, profile, stage)) == digest(dependencies(changed, profile, stage))
    for stage in ("desk", "all"):
        assert digest(dependencies(cfg, profile, stage)) != digest(dependencies(changed, profile, stage))
    changed = copy.deepcopy(cfg)
    changed["demand"]["budget_usdc"] += 1
    assert dependencies(cfg, profile, "supply") == dependencies(changed, profile, "supply")
    assert dependencies(cfg, profile, "both") != dependencies(changed, profile, "both")


def test_history_excludes_all_experiment_perturbations_and_future_prices():
    cfg = load_config(CONFIG)
    before = history_rows(cfg)
    cfg["profiles"] = [dict(name="extreme", perturbations=[dict(kind="ramp", amplitude=1000)])]
    cfg["days"] = 1
    cfg["seed"] = 999
    assert history_rows(cfg) == before
    assert [x["day"] for x in before] == list(range(-1280, 0))
    levels, _, metadata = historical_state(before, cfg["feed"]["base"], [18] * 3,
        [True, False, True], [10**18] * 3)
    # Long anchors stay near the known mean, not +/- 10% first-phase prices.
    import math
    means = metadata["mean_relative_ticks"]
    assert max(abs(levels[i][-1] / 1e18 - means[i]) * math.log(1.0001) for i in range(3)) < .002
    assert any(levels[i][0] != levels[i][-1] for i in range(3))
    with pytest.raises(ValueError, match="ending at day -1"):
        historical_state(before[1:] + [dict(day=0, prices=[100] * 3)], [100] * 3,
                         [18] * 3, [True] * 3, [10**18] * 3)


@pytest.fixture(scope="module")
def evm_controls():
    pytest.importorskip("pyrevm")
    pytest.importorskip("web3")
    from alberta_buck.sim.control_lab.engine import simulate
    cfg = load_config(CONFIG)
    cfg["days"] = 3
    # Day-zero prices now settle before sampling. Exercise rejection with a
    # genuine live disturbance instead of relying on a bootstrap discontinuity.
    profile = dict(name="step", perturbations=[dict(kind="step", start=1, duration=10, amplitude=.12)])
    return {stage: simulate(cfg, profile, stage) for stage in ("monitor", "monitor-k", "linked", "k", "all")}


def test_sensor_replay_has_no_arbitrage_or_policy(evm_controls):
    r = evm_controls["monitor"]
    assert r["summary"]["arb_cycles"] == r["summary"]["desk_ops"] == 0
    assert all(f["k"] == .75 for f in r["frames"])
    for frame in r["frames"]:
        assert frame["signal"]["fresh"]
        assert frame["signal"]["history_ready"]
        assert frame["token_buck"] == pytest.approx(frame["target_usd"], rel=1e-9)
    assert any(f["signal"]["invalid_after_move"] for f in r["frames"])
    assert r["history"]["rows"][-1]["day"] == -1
    assert r["frames"][0]["signal"]["consecutive_days"] == 1
    assert any(abs(x) > 100 for x in r["frames"][0]["signal"]["excursion"])


@pytest.mark.parametrize("venue", ["token_buck", "token_usdc"])
def test_spot_dislocation_does_not_disable_profitable_arbitrage(venue):
    pytest.importorskip("pyrevm")
    from alberta_buck.sim.control_lab.engine import World, isolated_environment
    from alberta_buck.sim.signal_history import initialize_history
    from alberta_buck.sim.router import sqrt_price_x96
    cfg = load_config(CONFIG)
    with isolated_environment(cfg):
        world = World(cfg, "linked")
        try:
            world.warp(world.start)
            initialize_history(world.chain, world.signals, world.d.basket,
                world.d.gov, history_rows(cfg), cfg["feed"]["base"])
            world.chain.send(world.signals.functions.observe())
            world.warp(world.start + 1800)
            d = world.d
            pool = d.pool_buck[0] if venue == "token_buck" else d.pool_usdc[0]
            quote = d.buck if venue == "token_buck" else d.usdc
            target = sqrt_price_x96(d.tokens[0].address, 10**18, quote.address, 140 * 10**6)
            current = world.pools[pool].functions.slot0().call()[0]
            world.chain.send(d.simlp.functions.swap(pool, d.simlp.address, target < current,
                (1 << 127) - 1, target, *world.pairs[pool]))
            world.chain.send(world.signals.functions.observe())
            if venue == "token_buck":
                assert not world.signals.functions.fresh().call()
            # Even an unavailable/stale sensor must not block a spot cycle.
            world.arbitrage()
            assert world.counters["arb_cycles"] > 0
            assert world.counters["arb_profit_buck"] > 0
            assert d.buck.functions.signedBalanceOf(world.arb.address).call() > 0
        finally:
            world.close()


def test_k_without_actuators_does_not_move_pool_prices(evm_controls):
    a, b = evm_controls["monitor"], evm_controls["monitor-k"]
    assert a["summary"]["arb_cycles"] == b["summary"]["arb_cycles"] == 0
    assert any(f["k"] != .75 for f in b["frames"])
    for fa, fb in zip(a["frames"], b["frames"]):
        for key in ("token_buck", "token_usdc", "buck_usdc"):
            assert fa[key] == fb[key]


@pytest.mark.parametrize("amplitude", [0., .12])
def test_unperturbed_replay_has_only_bounded_oracle_precision_error(amplitude):
    pytest.importorskip("pyrevm")
    from alberta_buck.sim.control_lab.engine import simulate
    cfg = load_config(CONFIG)
    cfg["days"] = 30
    cfg["feed"]["amplitude"] = amplitude
    r = simulate(cfg, cfg["profiles"][0], "monitor-k")
    assert r["bootstrap"]["reference_at_means"] == pytest.approx(1., abs=1e-15)
    assert r["summary"]["arb_cycles"] == 0
    for f in r["frames"]:
        # Declared integer-bp weights differ by 1 bp between constituents;
        # the resulting common-mode ripple is at most 0.12 bp here.
        assert abs(f["reference"]["target_usd"] - 1) <= .00001201
        assert abs(f["bvib"] - f["reference"]["spot_buck"]) < .000101
        assert abs(f["k"] - .75) < .00025
        assert f["buck_usdc"] == 1.


def test_phase_ready_start_has_no_fabricated_arbitrage_and_reconciles_custody():
    pytest.importorskip("pyrevm")
    from alberta_buck.sim.control_lab.engine import simulate
    cfg = load_config(CONFIG)
    cfg["days"] = 3
    r = simulate(cfg, cfg["profiles"][0], "linked")
    first = r["frames"][0]
    assert first["cumulative"]["arb_cycles"] == 0
    assert first["buck_usdc"] == 1.
    assert not first["signal"]["invalid_after_move"]
    assert first["token_buck"] == pytest.approx(first["target_usd"], rel=1e-9)
    initial = r["bootstrap"]["market"]["pool_buck"]
    for f in r["frames"]:
        assert f["market"]["arb_held_buck"] == pytest.approx(f["cumulative"]["arb_profit_buck"], abs=1e-6)
        assert f["market"]["pool_buck"] + f["market"]["arb_held_buck"] == pytest.approx(initial, abs=1e-5)


@pytest.mark.parametrize("stage,quote", [("demand", 1.01), ("supply", .99)])
def test_private_intent_trades_through_a_modestly_adverse_price(stage, quote):
    pytest.importorskip("pyrevm")
    from alberta_buck.sim.control_lab.engine import World, isolated_environment
    from alberta_buck.sim.router import sqrt_price_x96
    cfg = load_config(CONFIG)
    with isolated_environment(cfg):
        w = World(cfg, stage)
        try:
            d = w.d
            current = w.pools[d.pool_ub].functions.slot0().call()[0]
            target = sqrt_price_x96(d.buck.address, 10**6, d.usdc.address, round(quote * 10**6))
            w.chain.send(d.simlp.functions.swap(d.pool_ub, d.simlp.address, target < current,
                (1 << 127) - 1, target, *w.pairs[d.pool_ub]))
            w.step = 0
            w.agents()
            amount = w.counters["demand_usdc" if stage == "demand" else "supply_buck"]
            assert 0 < amount < cfg[stage]["rate_at_par_per_day"] / cfg["ticks_per_day"]
            assert w.counters[stage + "_trades"] == 1
        finally:
            w.close()


def test_private_response_and_desk_accounting_are_real(evm_controls):
    r = evm_controls["all"]
    assert r["summary"]["demand_usdc"] > 0
    budgets = [f["desk"]["budget"] for f in r["frames"]]
    assert all(a >= b >= 0 for a, b in zip(budgets, budgets[1:]))
    for f in r["frames"]:
        assert f["agents"]["demand_cash"] + f["cumulative"]["demand_usdc"] == pytest.approx(1_000_000)
        assert f["agents"]["demand_held"] >= 0
        assert f["signal"]["fresh"]
    assert r["frames"][-1]["gas"] > 0
