"""The sim server's session controls (alberta_buck/sim/server.py): the
savings demonstration's pause / step / shock / chain ops, lite frames, the
chain lock that keeps RPC between days, and the server-side RPC methods.
A Session is exercised without building a world."""
import json
import threading
import time
from queue import Queue

from alberta_buck.sim.server import Session


def session():
    s = Session.__new__(Session)
    s.sid, s.controls, s.subscribers, s.lite = "t", Queue(), set(), set()
    s.chain_lock = threading.Lock()
    s.pace, s.paused, s.step_left, s.done = 0.0, False, None, False
    s.day, s.d, s.agents, s._info, s._holding = None, None, None, None, False
    s.aloop, s.provider = None, None
    s._last_day_ts = s._idle_since = time.monotonic()
    return s


class ShockAgent:            # the server finds the cast's shock agent by name
    def __init__(self):
        self.armed = []

    def arm(self, side, usd, days):
        self.armed.append((side, usd, days))


class Wheel:
    def __init__(self):
        self.chain = None

    def set_chain(self, name):
        self.chain = name


def test_pause_resume_step():
    s = session()
    s._apply({"op": "pause"}, 0, [])
    assert s.paused
    s._apply({"op": "resume"}, 0, [])
    assert not s.paused and s.step_left is None
    s._apply({"op": "step", "days": 3}, 0, [])
    assert not s.paused and s.step_left == 3
    for _ in range(3):
        s._advance_step()
        assert not s.paused
    s._advance_step()                    # the fourth day's start: paused
    assert s.paused and s.step_left is None


def test_shock_arms_the_shock_agent_and_chain_toggles_the_wheel():
    s = session()
    shock, wheel = ShockAgent(), Wheel()
    s._apply({"op": "shock", "side": "buy", "usd": 5e6, "days": 2}, 0, [wheel, shock])
    s._apply({"op": "chain", "name": "l1"}, 0, [wheel, shock])
    assert shock.armed == [("buy", 5e6, 2)]
    assert wheel.chain == "l1"


def test_lite_frames_drop_the_per_agent_telemetry():
    s = session()
    full, lite = s._payloads({"day": 3, "basketVal": 1, "ag": {"x": 1}, "octl": [1],
                              "wh_sol_cycles": 2})
    f, l = json.loads(full), json.loads(lite)
    assert "ag" in f and "octl" in f
    assert "ag" not in l and "octl" not in l
    assert l["wh_sol_cycles"] == 2 and l["day"] == 3 and l["sid"] == "t"


def test_the_frame_releases_the_days_chain_lock():
    s = session()
    s.chain_lock.acquire()
    s._holding = True
    s._on_frame({"day": 0})
    assert not s._holding
    assert s.chain_lock.acquire(blocking=False)   # free for RPC between days


def test_server_side_rpc_methods():
    s = session()
    s.day, s.paused = 7, True
    out = s.rpc({"jsonrpc": "2.0", "id": 1, "method": "sim_status"})
    assert out["result"]["day"] == 7 and out["result"]["paused"] is True
    out = s.rpc({"jsonrpc": "2.0", "id": 2, "method": "sim_info"})
    assert out["result"] == {}                   # no world built yet


def test_public_rpc_refuses_unsigned_transactions():
    """A public server forwards reads and SIGNED transactions only: the pyrevm
    provider would execute eth_sendTransaction for any `from`."""
    s = session()
    s.public = True
    calls = []

    class Provider:
        def make_request(self, method, params):
            calls.append(method)
            return {"result": "0x1"}

    s.provider = Provider()
    out = s.rpc({"jsonrpc": "2.0", "id": 1, "method": "eth_sendTransaction",
                 "params": [{"from": "0x" + "11" * 20, "to": "0x" + "22" * 20}]})
    assert "error" in out and calls == []
    out = s.rpc({"jsonrpc": "2.0", "id": 2, "method": "eth_blockNumber"})
    assert out["result"] == "0x1" and calls == ["eth_blockNumber"]


def test_public_session_overrides_are_the_pages_toggles_only():
    from alberta_buck.sim.server import PUBLIC_SETS
    sets = ["scenario.prices=revert", "scenario.agents.DirectMintAgent=10000",
            "agents.BasketWheelAgent.chain=l1"]
    kept = [x for x in sets if x.startswith(PUBLIC_SETS)]
    assert kept == ["scenario.prices=revert", "agents.BasketWheelAgent.chain=l1"]
