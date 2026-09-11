"""Telemetry v2: the action recorder (alberta_buck/sim/acts.py)."""

import pytest

from alberta_buck.sim import acts
from alberta_buck.sim.agents import Agent


class _Fn:
    def __init__(self, name):
        self.fn_name = name


class _Chain:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def send(self, fn, *a, **k):
        self.calls += 1
        if self.fail:
            raise RuntimeError("tx reverted: swap :: Slippage()")
        return {"status": 1}


class _Deploy:
    def __init__(self, chain):
        self.chain = chain


def test_send_is_attributed_to_the_actor(monkeypatch):
    monkeypatch.setattr(acts, "MODE", "all")
    ch = _Chain()
    acts.install(ch)
    a = Agent(7)
    ch.actor = a
    ch.sim_tick = 2
    ch.send(_Fn("swap"))
    ch.actor = None
    ch.send(_Fn("compute"))          # no actor: not recorded
    got = acts.drain(a)
    assert got == [{"t": 2, "fn": "swap", "ok": True}]
    assert acts.drain(a) == []       # drained
    assert ch.calls == 2


def test_failed_send_is_recorded_and_reraised(monkeypatch):
    monkeypatch.setattr(acts, "MODE", "all")
    ch = _Chain(fail=True)
    acts.install(ch)
    a = Agent(1)
    ch.actor = a
    with pytest.raises(RuntimeError):
        ch.send(_Fn("swap"))
    (e,) = acts.drain(a)
    assert e["ok"] is False and e["fn"] == "swap" and "Slippage" in e["err"]


def test_note_records_why_compactly(monkeypatch):
    monkeypatch.setattr(acts, "MODE", "all")
    ch = _Chain()
    acts.install(ch)
    ch.sim_tick = 3
    a = Agent(4)
    a.note(_Deploy(ch), "buy", bvib=1.0123456789, amt=1_000_000, tag="x" * 100,
           nested={"k": 0.123456789}, obj=object())
    (n,) = acts.drain(a)
    assert n["t"] == 3 and n["kind"] == "buy"
    assert n["why"]["bvib"] == 1.01235 and n["why"]["amt"] == 1_000_000
    assert len(n["why"]["tag"]) == 48
    assert n["why"]["nested"] == {"k": 0.123457}
    assert n["why"]["obj"].startswith("<object")


def test_install_is_idempotent(monkeypatch):
    monkeypatch.setattr(acts, "MODE", "all")
    ch = _Chain()
    first = acts.install(ch)
    second = acts.install(ch)
    assert first is second is ch.send


def test_mode_none_records_nothing(monkeypatch):
    monkeypatch.setattr(acts, "MODE", "none")
    ch = _Chain()
    acts.install(ch)
    a = Agent(2)
    ch.actor = a
    ch.send(_Fn("swap"))
    a.note(_Deploy(ch), "buy", x=1)
    assert acts.drain(a) == []


def test_mode_telemetry_records_only_opted_in_agents(monkeypatch):
    monkeypatch.setattr(acts, "MODE", "telemetry")
    ch = _Chain()
    acts.install(ch)

    class Opted(Agent):
        def telemetry_static(self):
            return {"k": 1}

    plain, opted = Agent(1), Opted(2)
    for ag in (plain, opted):
        ch.actor = ag
        ch.send(_Fn("swap"))
    assert acts.drain(plain) == []
    assert len(acts.drain(opted)) == 1
