"""The SIM SERVER: a pyrevm-backed BUCK world at interactive speed, with
a JS-facing control surface.

The architecture Perry's solution-buck-k-sim slide wants for real: the
swarm (whales, arbs, PID, investors, debtors) runs NATIVELY in Python on
the in-process pyrevm EVM -- the only backend fast enough for
several-days-per-second throughput -- while a browser front-end
subscribes to day frames, adjusts agent populations and tuning live,
and (optionally) attaches its OWN guest agents over a JSON-RPC facade
for full contract-fidelity focal stories (the JS farmer via
anvilSession(url) + join-sim).

Three channels on one asyncio server (websockets + stdlib):

  ws://host:port/frames    every captured day frame as JSON: the
                           snapshot dict (day, basketVal, buckK, supply,
                           spots, per-agent octl states, counters...)
                           plus {"pace": days_per_second}.
  ws://host:port/control   JSON ops applied at the next day boundary:
                             {"op": "population", "cls": ..., "count": N}
                               -- flips arrive/depart days on the
                               pre-built agent pool (the growth
                               machinery IS the population dial)
                             {"op": "knob", "cls": ..., "name": ...,
                              "value": v} -- sets the attribute on every
                               agent of the class (theta, save_rate...)
                             {"op": "pace", "days_per_second": x}
                               -- throttles the loop (0 = flat out)
  POST http://host:port/rpc  a JSON-RPC 2.0 facade over the pyrevm
                           provider: guest agents (the JS eqworld via
                           anvilSession) read state and send txs into
                           the SAME world, serialized with the loop at
                           tick boundaries by the chain lock.

Run:
    python -m alberta_buck.sim.server --experiment \
        alberta_buck/sim/experiments/backdrop.toml --port 8787
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from queue import Queue, Empty

from alberta_buck.sim import experiment as expmod
from alberta_buck.sim.loop import run
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (bytes, bytearray)):
        return "0x" + bytes(o).hex()
    if isinstance(o, int) and abs(o) >= 2 ** 53:
        return str(o)
    return o


class SimServer:
    """Owns the sim thread, the control queue, and the frame fanout."""

    def __init__(self, scenario, basket_impl="prorata"):
        self.scenario = scenario
        self.basket_impl = basket_impl
        self.controls: Queue = Queue()
        self.subscribers: set[asyncio.Queue] = set()
        self.chain_lock = threading.Lock()
        self.pace = 0.0                     # days/second cap; 0 = flat out
        self.provider = None                # pyrevm provider, once built
        self.loop: asyncio.AbstractEventLoop | None = None
        self._last_day_ts = time.monotonic()

    # ---- sim-thread side --------------------------------------------------

    def _on_day_start(self, day, d, agents, ctr):
        if self.pace > 0:                   # interactive throttle
            wait = (1.0 / self.pace) - (time.monotonic() - self._last_day_ts)
            if wait > 0:
                time.sleep(wait)
        self._last_day_ts = time.monotonic()
        while True:
            try:
                op = self.controls.get_nowait()
            except Empty:
                break
            self._apply(op, day, agents)

    def _apply(self, op, day, agents):
        kind = op.get("op")
        if kind == "pace":
            self.pace = float(op.get("days_per_second", 0))
        elif kind == "population":
            cls, want = op.get("cls", ""), int(op.get("count", 0))
            pool = [a for a in agents if type(a).__name__ == cls]
            for i, a in enumerate(pool):
                if i < want:                # activate: arrive now
                    if not getattr(a, "_growth_arrived", False):
                        a.arrive_day = day
                    a.depart_day = None
                else:                       # deactivate: depart now
                    a.depart_day = day
        elif kind == "knob":
            cls, name = op.get("cls", ""), op.get("name", "")
            for a in agents:
                if type(a).__name__ == cls and hasattr(a, name):
                    setattr(a, name, type(getattr(a, name))(op["value"]))

    def _on_frame(self, frame):
        now = time.monotonic()
        payload = json.dumps(_jsonable(
            {**frame, "pace": round(1.0 / max(now - self._last_day_ts, 1e-9), 2)}))
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._fanout, payload)

    def _fanout(self, payload):
        for q in list(self.subscribers):
            if q.qsize() < 100:             # drop frames on slow clients
                q.put_nowait(payload)

    def run_sim(self):
        anvil = PyrevmAnvil()
        anvil.start()
        self.provider = anvil.w3.provider
        try:
            run(self.scenario, anvil, out_path=None, verbose=True,
                basket_impl=self.basket_impl,
                on_day_start=self._on_day_start, on_frame=self._on_frame)
        finally:
            anvil.stop()

    # ---- asyncio side -----------------------------------------------------

    async def ws_handler(self, ws):
        path = ws.request.path if hasattr(ws, "request") else ws.path
        if path.startswith("/control"):
            async for msg in ws:
                try:
                    self.controls.put(json.loads(msg))
                    await ws.send(json.dumps({"ok": True}))
                except Exception as e:
                    await ws.send(json.dumps({"ok": False, "err": str(e)}))
        else:                               # /frames (default)
            q: asyncio.Queue = asyncio.Queue()
            self.subscribers.add(q)
            try:
                while True:
                    await ws.send(await q.get())
            finally:
                self.subscribers.discard(q)

    async def http_handler(self, path, request_headers):
        # websockets' process_request hook: serve POST /rpc as plain HTTP.
        return None                         # v1: RPC facade over ws below

    async def rpc_handler(self, ws):
        """JSON-RPC over a websocket at /rpc: each message is a standard
        {jsonrpc, id, method, params} envelope (or a batch list), answered
        from the pyrevm provider under the chain lock."""
        async for msg in ws:
            req = json.loads(msg)
            batch = req if isinstance(req, list) else [req]
            out = []
            for r in batch:
                with self.chain_lock:
                    try:
                        resp = self.provider.make_request(
                            r["method"], r.get("params", []))
                        out.append({"jsonrpc": "2.0", "id": r.get("id"),
                                    "result": _jsonable(resp.get("result"))})
                    except Exception as e:
                        out.append({"jsonrpc": "2.0", "id": r.get("id"),
                                    "error": {"code": -32000,
                                              "message": str(e)}})
            await ws.send(json.dumps(out if isinstance(req, list) else out[0]))

    async def serve(self, host, port):
        import websockets
        self.loop = asyncio.get_running_loop()
        t = threading.Thread(target=self.run_sim, daemon=True)
        t.start()

        async def route(ws):
            path = ws.request.path if hasattr(ws, "request") else ws.path
            if path.startswith("/rpc"):
                await self.rpc_handler(ws)
            else:
                await self.ws_handler(ws)

        async with websockets.serve(route, host, port):
            print(f"[sim.server] ws://{host}:{port}/frames | /control | /rpc")
            while t.is_alive():
                await asyncio.sleep(0.5)
            print("[sim.server] sim finished")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m alberta_buck.sim.server")
    ap.add_argument("--experiment", required=True, metavar="TOML")
    ap.add_argument("--set", action="append", default=[], dest="sets")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--pace", type=float, default=0.0,
                    help="initial days/second cap (0 = flat out)")
    a = ap.parse_args(argv)
    exp = expmod.load(a.experiment, sets=a.sets)
    sc = expmod.build(exp)
    srv = SimServer(sc, basket_impl=exp.scenario.get("basket", "prorata"))
    srv.pace = a.pace
    asyncio.run(srv.serve(a.host, a.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
