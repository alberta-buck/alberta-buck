"""Replacement wiring on the EVM, with the simulator's real agents/pools."""
from contextlib import contextmanager
from dataclasses import replace
import math

import pytest

pytest.importorskip("web3")
pytest.importorskip("pyrevm")

from alberta_buck.sim import basket_machine
from alberta_buck.sim import experiment as expmod
from alberta_buck.sim.loop import run
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
from alberta_buck.sim.scenario import ROUTING, Scenario, keep_tokens


@contextmanager
def simulated(tmp_path, basket_impl, machine=None):
    scenario = replace(ROUTING, days=2, ticks_per_day=4)
    if machine is not None:
        scenario.experiment = expmod.load(None, sets=[f"machine.{key}={value}" for key, value in machine.items()])
    frames, deployment = [], []
    with PyrevmAnvil() as evm:
        run(scenario, evm, out_path=str(tmp_path / "machine.json"), verbose=False,
            basket_impl=basket_impl, on_frame=frames.append,
            on_day_start=lambda day, d, agents, ctr: deployment.append(d))
        yield deployment[-1], frames


def test_replacement_publishes_and_runs_through_the_simulator(monkeypatch, tmp_path):
    monkeypatch.setenv("SIM_EQUITY_MACHINE", "1")
    with simulated(tmp_path, "equity-ops") as (d, frames):
        assert d.wheel.functions.responseMode().call()
        assert d.wheel.functions.opsBasket().call() == d.desk.address
        assert d.wheel.functions.sampleSource().call() == d.monetary_policy.address
        assert d.desk.functions.executionGuard().call() == d.monetary_policy.address
        assert d.kctrl.functions.dTMax().call() == 21600           # the world's step: four ticks a day
        assert len(frames) == 2
        assert all(frame["eq_signal_fresh"] for frame in frames)
        assert all(sum(frame["eq_w"]) == 10000 for frame in frames)
        assert frames[-1]["desk_budget"] <= frames[0]["desk_budget"]
        assert frames[-1]["eq_equity"] > 0


def test_an_experiment_selects_and_sizes_the_machine(tmp_path):
    with simulated(tmp_path, "equity-machine", {"tilt": .25, "lean_cap_bp": 1000, "interval": 900}) as (d, frames):
        assert d.equity_machine and d.basket_impl == "equity-ops"
        assert d.wheel.functions.responseMode().call()
        assert tuple(d.basket.functions.response().call()) == (3000, 900, 2500, 1000)
        assert d.kctrl.functions.dTMax().call() == 21600
        last = frames[-1]
        assert last["eq_signal_fresh"] and last["desk_side"] in (-1, 0, 1) and last["desk_over_days"] >= 0
        # The targets the frame reports are the facet's: they share the book, within the lean cap.
        assert abs(sum(last["eq_lean"]) - 10000) <= len(last["eq_lean"])
        assert all(abs(lean - declared) <= declared * 1000 // 10000 + 1
                   for lean, declared in zip(last["eq_lean"], last["eq_decl"]))
        assert last["eq_lean"] == basket_machine.targets_bp(d)
        # Each response is booked by what it did, with the BUCKs it moved.
        booked = {k: v for k, v in last.items() if k.startswith("wh_eq_") and "response" not in k}
        names = {name for name, _ in basket_machine.ACTIONS.values()}
        assert last.get("wh_eq_place_n", 0) > 0, booked
        assert all(last.get(f"wh_eq_{name}", 0) > 0 for name in names if last.get(f"wh_eq_{name}_n", 0))
        # The credit is marked net: every TOKEN is paired, and the line is still there.
        assert d.basket.functions.netMark().call()
        assert last["eq_lien"] > 0 and last["eq_held_buck"] > 0 and last["eq_liquidity"] > 0
        assert abs(last["eq_lien"] - last["eq_held_buck"]) < last["eq_equity"] // 2


def test_machine_settings_are_the_experiments_then_the_environments_then_the_labs(monkeypatch):
    half = round(math.log(2) * 10**18)
    assert basket_machine.sizing(basket_machine.settings()) == (0, 4 * 10**18, 0, half, half, 10**18, 25 * 10**14, 2 * 10**16)
    assert basket_machine.sizing(basket_machine.settings({"half_life_days": .5}))[3] == round(math.log(2) * 2 * 10**18)
    monkeypatch.setenv("SIM_MACHINE_TILT", "1")
    monkeypatch.setenv("SIM_OPS_PERSIST", "7")
    assert basket_machine.settings()["tilt"] == 1 and basket_machine.settings()["desk_persist_days"] == 7
    assert basket_machine.settings({"tilt": .25})["tilt"] == .25
    with pytest.raises(ValueError, match="unknown"):
        basket_machine.settings({"tilte": .25})


def test_a_scenario_narrowed_to_fewer_tokens_shares_the_departed_weight():
    files = ["paxg.csv", "cbbtc.csv", "aoil.csv", "paxg.csv", "aoil.csv", "cbbtc.csv"]
    six = Scenario(name="six", csv_files=files, agents={"TokenAccumulatorAgent": 6, "MarketMakerWhale": 1},
                   tokens=[("LABR", "", 18, 2400), ("CNST", "", 18, 2200), ("FOOD", "", 18, 2000),
                           ("NRGC", "", 18, 2000), ("PAXG", "", 18, 800), ("cbBTC", "", 8, 600)])
    five = keep_tokens(six, ["PAXG", "LABR", "CNST", "FOOD", "NRGC"])
    assert [(t[0], t[3]) for t in five.tokens] == [("LABR", 2553), ("CNST", 2340), ("FOOD", 2128), ("NRGC", 2128),
                                                   ("PAXG", 851)]
    assert five.csv_files == files[:5] and five.agents == {"TokenAccumulatorAgent": 5, "MarketMakerWhale": 1}
    with pytest.raises(ValueError, match="scenario.tokens"):
        keep_tokens(five, ["WETH"])
