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
    s.history, s.rpc_clients, s.rpc_waiting = [], 0, 0
    s._rpc_count, s._rpc_ts, s._sv, s.scenario = threading.Lock(), 0.0, None, None
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


def test_a_step_begun_in_a_pause_runs_exactly_its_days():
    """Paused at day D's start, "+1 day" runs day D and pauses at D+1's."""
    s = session()
    s.subscribers = {object()}
    s.paused = True
    s.controls.put({"op": "step", "days": 1})
    s._wait_while_paused(10, [])             # day 10 runs
    assert not s.paused
    s._advance_step()                        # day 11's start: paused again
    assert s.paused
    s.controls.put({"op": "step", "days": 3})
    s._wait_while_paused(11, [])             # days 11, 12, 13 run
    for _ in range(2):
        s._advance_step()
        assert not s.paused
    s._advance_step()                        # day 14's start
    assert s.paused


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


def test_a_batch_is_answered_under_one_hold_and_status_needs_no_chain():
    """A page's approve + deposit + their receipts land together: one hold of
    the chain lock for the whole batch.  sim_status touches no chain, so it
    is answered even while a day holds the lock."""
    s = session()
    held = []

    class Provider:
        def make_request(self, method, params):
            held.append(s.chain_lock.locked())
            return {"result": method}

    s.provider = Provider()
    out = s.rpc([{"jsonrpc": "2.0", "id": i, "method": m}
                 for i, m in enumerate(["eth_sendRawTransaction", "eth_getTransactionReceipt"])])
    assert [r["result"] for r in out] == ["eth_sendRawTransaction", "eth_getTransactionReceipt"]
    assert held == [True, True] and not s.chain_lock.locked()
    s.chain_lock.acquire()                       # a day is running
    s.day = 4
    out = s.rpc({"jsonrpc": "2.0", "id": 9, "method": "sim_status"})
    assert out["result"]["day"] == 4
    s.chain_lock.release()


def test_provider_errors_reach_the_client():
    """A revert's code and data pass through (the page decodes the basket's
    custom errors from them)."""
    s = session()

    class Provider:
        def make_request(self, method, params):
            return {"error": {"code": 3, "message": "execution reverted", "data": "0x1234"}}

    s.provider = Provider()
    out = s.rpc({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{}]})
    assert out["error"] == {"code": 3, "message": "execution reverted", "data": "0x1234"}


def test_the_next_day_waits_for_queued_rpc():
    """Between days the sim thread lets a queued envelope through before it
    takes the chain: a released lock is not handed to its waiter, and the
    visitor's deposit would otherwise starve behind a running world."""
    s = session()
    order = []

    class Provider:
        def make_request(self, method, params):
            order.append("rpc")
            return {"result": "0x1"}

    s.provider = Provider()
    s.chain_lock.acquire()                       # the day running
    t = threading.Thread(target=s.rpc, args=({"jsonrpc": "2.0", "id": 1,
                                              "method": "eth_blockNumber"},))
    t.start()
    while not s.rpc_waiting:
        time.sleep(0.001)
    s.chain_lock.release()                       # the frame
    s._yield_to_rpc()                            # the next day's start
    order.append("day")
    t.join()
    assert order == ["rpc", "day"]


def test_frames_are_kept_for_a_reloaded_pages_replay():
    s = session()
    s._fanout("full-1", "lite-1")
    s._fanout(json.dumps({"done": True}))        # not a frame
    s._fanout("full-2", "lite-2")
    assert s.history == ["lite-1", "lite-2"]


def test_the_savings_block_rides_in_every_frame():
    s = session()
    s._sv = {"O": 200, "S": 20, "P": 180, "B": 230, "T": 0, "D": 1.2778}
    full, lite = s._payloads({"day": 1})
    assert json.loads(lite)["sv"]["D"] == 1.2778 and json.loads(full)["sv"]["B"] == 230


def test_signed_transactions_carry_their_real_hash_and_need_the_next_nonce():
    """eth_sendRawTransaction answers keccak(raw) -- a page names the receipt
    in the same batch -- and refuses a nonce that is not the sender's next
    (a captured transaction cannot be replayed)."""
    from eth_account import Account
    from eth_utils import keccak
    from alberta_buck.sim.pyrevm_backend import PyrevmAnvil

    anvil = PyrevmAnvil().start()
    acct = Account.create()
    tx = {"to": "0x" + "22" * 20, "value": 0, "gas": 100_000, "gasPrice": 0,
          "nonce": 0, "chainId": anvil.chain_id, "data": b""}
    raw = acct.sign_transaction(tx).raw_transaction
    p = anvil.w3.provider
    out = p.make_request("eth_sendRawTransaction", ["0x" + raw.hex()])
    assert out["result"] == "0x" + keccak(raw).hex()
    assert p.make_request("eth_getTransactionReceipt", [out["result"]])["result"]["status"] == "0x1"
    again = p.make_request("eth_sendRawTransaction", ["0x" + raw.hex()])
    assert "nonce 0 is not the sender's next (1)" in again["error"]["message"]
    eip1559 = {k: v for k, v in tx.items() if k != "gasPrice"}
    typed = acct.sign_transaction({**eip1559, "nonce": 1, "type": 2,
                                   "maxFeePerGas": 0, "maxPriorityFeePerGas": 0})
    out = p.make_request("eth_sendRawTransaction", ["0x" + typed.raw_transaction.hex()])
    assert out["result"] == "0x" + keccak(typed.raw_transaction).hex()


def test_a_reset_unwinds_the_day():
    """"reset" rebuilds the world from its first day, in place: applied at a
    day's start, it unwinds the run like a reaped session, to be rebuilt."""
    import pytest
    from alberta_buck.sim.server import ResetSession
    s = session()
    with pytest.raises(ResetSession):
        s._apply({"op": "reset"}, 12, [])


def test_a_reset_clears_the_world_and_tells_its_watchers():
    import asyncio
    s = session()
    s._enrolled = {"0xabc": ("args", 1)}
    s.history = ['{"day": 1}', '{"day": 2}']
    s.day, s.d, s.agents, s._info, s._sv = 7, object(), [], {"basket": "0x1"}, {"D": 1.1}
    s.paused, s.step_left = True, 2
    s.controls.put({"op": "shock"})
    s.chain_lock.acquire()
    s._holding = True
    full, lite = asyncio.Queue(), asyncio.Queue()
    s.subscribers, s.lite = {full, lite}, {lite}
    s._restarting()
    assert (s.day, s.d, s.agents, s._info, s._sv) == (None, None, None, None, None)
    assert s._enrolled == {} and not s.paused and s.step_left is None
    assert s.controls.empty(), "ops queued for the old world do not reach the new one"
    assert not s._holding and s.chain_lock.acquire(blocking=False), "the chain is let go"
    s._reset_fanout()                    # the asyncio side (no loop in a test)
    assert s.history == [], "a reloaded page replays only the new world"
    for q in (full, lite):
        assert json.loads(q.get_nowait()) == {"reset": True}
