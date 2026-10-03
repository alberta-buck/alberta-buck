"""Replacement wiring on the EVM, with the simulator's real agents/pools."""
from dataclasses import replace

import pytest

pytest.importorskip("web3")
pytest.importorskip("pyrevm")

from alberta_buck.sim.loop import run
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
from alberta_buck.sim.scenario import ROUTING


def test_replacement_publishes_and_runs_through_the_simulator(monkeypatch, tmp_path):
    monkeypatch.setenv("SIM_EQUITY_MACHINE", "1")
    scenario = replace(ROUTING, days=2, ticks_per_day=4)
    frames = []
    deployment = []
    with PyrevmAnvil() as evm:
        run(scenario, evm, out_path=str(tmp_path / "machine.json"), verbose=False,
            basket_impl="equity-ops", on_frame=frames.append,
            on_day_start=lambda day, d, agents, ctr: deployment.append(d))
        d = deployment[-1]
        assert d.wheel.functions.responseMode().call()
        assert d.wheel.functions.opsBasket().call() == d.desk.address
        assert d.wheel.functions.sampleSource().call() == d.monetary_policy.address
        assert d.desk.functions.executionGuard().call() == d.monetary_policy.address
        assert d.kctrl.functions.dTMax().call() == 3600
        assert len(frames) == 2
        assert all(frame["eq_signal_fresh"] for frame in frames)
        assert all(sum(frame["eq_w"]) == 10000 for frame in frames)
        assert frames[-1]["desk_budget"] <= frames[0]["desk_budget"]
        assert frames[-1]["eq_equity"] > 0
