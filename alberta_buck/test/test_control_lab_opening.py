"""Opening phase and production pair-basket wiring, through the real runner."""
import copy
import json
from pathlib import Path

import pytest

from alberta_buck.sim.control_lab.cli import worker
from alberta_buck.sim.control_lab.interactive import plot_data
from alberta_buck.sim.control_lab.model import STAGES, load_config

CONFIG = Path(__file__).resolve().parents[2] / "doc/control-lab.toml"


@pytest.fixture(scope="module")
def opening_runs(tmp_path_factory):
    pytest.importorskip("pyrevm")
    root = tmp_path_factory.mktemp("opening")
    cfg = load_config(CONFIG)
    cfg["days"] = 3
    results = {}
    for stage in ("linked", "basket", "all"):
        path = root / f"{stage}.json"
        worker(cfg, cfg["profiles"][0], stage, stage, {}, path)
        results[stage] = json.loads(path.read_text())
    # A day-zero disturbance is an opening price, not historical EMA data.
    profile = dict(name="opening-shock", perturbations=[
        dict(kind="step", tokens=[0], start=0, duration=2, amplitude=.12)])
    path = root / "shock.json"
    worker(cfg, profile, "basket", "shock", {}, path)
    results["shock"] = json.loads(path.read_text())
    return results


def test_funded_bootstrap_restores_both_routes_before_arbitrage(opening_runs):
    for result in opening_runs.values():
        boot, first = result["bootstrap"], result["frames"][0]
        assert boot["basket_prelude"]["days"] == 120
        assert boot["opening_twap_settled"]
        assert boot["opening_target_usd"] == first["target_usd"]
        for route in ("token_buck", "token_usdc"):
            assert boot["prices"][route] == pytest.approx(first["target_usd"], rel=1e-9)
        # Later live actors may move these prices; inactive opening controls
        # must not spend days arbitraging a synthetic 1 -> initial-phase jump.
        if result["stage"] != "all":
            assert first["cumulative"]["arb_cycles"] == 0
            assert first["token_buck"] == pytest.approx(first["target_usd"], rel=1e-9)
        assert boot["reference_at_means"] == pytest.approx(1.)
    assert opening_runs["shock"]["history"] == opening_runs["basket"]["history"]
    assert opening_runs["shock"]["bootstrap"]["opening_target_usd"][0] == 112


def test_prelude_does_not_seed_a_fictitious_pair_velocity(opening_runs):
    for result in opening_runs.values():
        frames = result["frames"]
        # The observation wheel and pair response can each call observe in
        # separate pyrevm blocks at this timestamp; neither has an interval yet.
        assert 1 <= frames[0]["pair_signal"]["epoch"] <= 2
        assert frames[0]["pair_signal"]["ready_mask"] == 0
        assert all(v["samples"] == 0 for v in frames[0]["pair_signal"]["venues"])
        assert all(p["rate_bp_day"] == 0 for p in frames[0]["pair_signal"]["pairs"])
        assert all(f["pair_signal"]["ready_mask"] == 0 for f in frames if f["day"] < 2)
        assert frames[-1]["pair_signal"]["ready_mask"] == 3


def test_walkthrough_uses_pair_response_and_collapses_new_sensor_panes(opening_runs):
    stages = list(STAGES)
    assert stages[stages.index("linked") + 1] == "basket"
    assert all(STAGES[s].get("basket") for s in stages[stages.index("basket"):])
    assert opening_runs["linked"]["bootstrap"] == opening_runs["basket"]["bootstrap"]
    for stage in ("linked", "basket", "all"):
        result = opening_runs[stage]
        assert (result.get("basket_mode") == "pair") == (stage != "linked")
        assert ("pair_policy" in result["frames"][0]) == (stage != "linked")
        panes = {p["id"]: p for p in plot_data(result)["panes"]}
        assert {"pair-quality", "pair-level-0", "pair-rate-1", "pair-error-2"} <= panes.keys()
        assert all(p["group"] == "monitor-detail" for k, p in panes.items() if k.startswith("pair-"))
        assert any(line["label"] == "Route rate spread" for line in panes["pair-rate-0"]["lines"])
        assert panes["price"]["group"] is None
        if stage != "linked":
            assert {"pair-intent", "pair-impact", "allocation"} <= panes.keys()
    standalone = copy.deepcopy(opening_runs["basket"])
    standalone.pop("sensor_details")
    assert all(p["group"] is None for p in plot_data(standalone)["panes"] if p["id"].startswith("pair-"))
