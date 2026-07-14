"""The SIM SERVER: pyrevm-backed BUCK worlds at interactive speed, one
independent world PER CLIENT SESSION, with a JS-facing control surface.

The swarm (whales, arbs, PID, investors, debtors, time-arbs) runs
NATIVELY in Python on in-process pyrevm -- the only backend fast enough
for several-days-per-second -- while browser front-ends subscribe to day
frames, adjust populations and tuning live, and (optionally) attach
guest agents over JSON-RPC for full contract-fidelity focal stories
(the JS eqworld via anvilSession + join-sim).

Sessions: every distinct /s/<sid>/... path lazily builds its OWN world
in its own thread (fresh pyrevm EVM), so multiple clients simulate in
parallel.  A session URL may carry --set overrides as query params:
    ws://host:port/s/alice/frames?set=scenario.years=5&set=scenario.seed=7
Bare /frames, /control, /rpc address the session "default".  A session
with no subscribers for IDLE_REAP_S is stopped and discarded.  (All
sessions share the process GIL: N concurrent worlds timeshare the
Python core; scale-out is one process per port behind the tunnel.)

WebSocket channels (websockets):
  /s/<sid>/frames    every captured day frame as JSON + measured pace
  /s/<sid>/control   {"op": "population"|"knob"|"pace", ...} applied at
                     the next day boundary
  /s/<sid>/rpc       JSON-RPC 2.0 over WS

HTTP (port+1, stdlib, CORS *):
  POST /s/<sid>/rpc  JSON-RPC 2.0 -- viem's standard http transport:
                     anvilSession("http://host:port+1/s/<sid>/rpc")

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
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue, Empty

from websockets.exceptions import ConnectionClosed

from alberta_buck.sim import experiment as expmod
from alberta_buck.sim.loop import run
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil

IDLE_REAP_S = 600           # reap a session with no subscribers this long
_BUILD_LOCK = threading.Lock()   # agent classes keep build-time counters


class StopSession(Exception):
    """Raised inside on_day_start to unwind a reaped session's loop."""


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


class Session:
    """One world: its own pyrevm EVM, sim thread, controls, and fanout."""

    def __init__(self, sid, exp_path, sets, basket_impl, loop, pace=0.0):
        self.sid = sid
        self.controls: Queue = Queue()
        self.subscribers: set[asyncio.Queue] = set()
        self.chain_lock = threading.Lock()
        self.pace = pace
        self.provider = None
        self.aloop = loop                   # the asyncio loop, for fanout
        self.done = False
        self._last_day_ts = time.monotonic()
        self._idle_since = time.monotonic()
        exp = expmod.load(exp_path, sets=sets)
        self.scenario_factory = lambda: expmod.build(exp)
        self.basket_impl = basket_impl or exp.scenario.get("basket", "prorata")
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"sim-{sid}")
        self.thread.start()

    # ---- sim-thread side --------------------------------------------------

    def _run(self):
        try:
            with _BUILD_LOCK:
                scenario = self.scenario_factory()
                anvil = PyrevmAnvil()
                anvil.start()
                self.provider = anvil.w3.provider
            try:
                run(scenario, anvil, out_path=None, verbose=False,
                    basket_impl=self.basket_impl,
                    on_day_start=self._on_day_start, on_frame=self._on_frame)
            finally:
                anvil.stop()
        except StopSession:
            print(f"[sim.server] session '{self.sid}' reaped (idle)")
        except Exception as e:
            print(f"[sim.server] session '{self.sid}' died: {e!r}")
            self._fanout_threadsafe(json.dumps({"error": str(e)}))
        finally:
            self.done = True
            self._fanout_threadsafe(json.dumps({"done": True}))

    def _on_day_start(self, day, d, agents, ctr):
        if self.subscribers:
            self._idle_since = time.monotonic()
        elif time.monotonic() - self._idle_since > IDLE_REAP_S:
            raise StopSession()
        if self.pace > 0:
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
                if i < want:
                    if not getattr(a, "_growth_arrived", False):
                        a.arrive_day = day
                    a.depart_day = None
                else:
                    a.depart_day = day
        elif kind == "knob":
            cls, name = op.get("cls", ""), op.get("name", "")
            for a in agents:
                if type(a).__name__ == cls and hasattr(a, name):
                    setattr(a, name, type(getattr(a, name))(op["value"]))

    def _on_frame(self, frame):
        now = time.monotonic()
        pace = round(1.0 / max(now - self._last_day_ts, 1e-9), 2)
        self._fanout_threadsafe(json.dumps(_jsonable(
            {**frame, "pace": pace, "sid": self.sid})))

    def _fanout_threadsafe(self, payload):
        if self.aloop is not None:
            self.aloop.call_soon_threadsafe(self._fanout, payload)

    def _fanout(self, payload):
        for q in list(self.subscribers):
            if q.qsize() < 100:
                q.put_nowait(payload)

    def rpc(self, req):
        """Answer a JSON-RPC envelope (or batch) from the world's provider,
        serialized against the sim loop by the chain lock."""
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
                                "error": {"code": -32000, "message": str(e)}})
        return out if isinstance(req, list) else out[0]


class SimServer:
    def __init__(self, exp_path, basket_impl=None, pace=0.0):
        self.exp_path = exp_path
        self.basket_impl = basket_impl
        self.pace = pace
        self.sessions: dict[str, Session] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def session(self, sid, query="") -> Session:
        with self._lock:
            s = self.sessions.get(sid)
            if s is None or s.done:
                sets = urllib.parse.parse_qs(query).get("set", [])
                print(f"[sim.server] building session '{sid}' "
                      f"(sets={sets or '-'})...")
                s = Session(sid, self.exp_path, sets, self.basket_impl,
                            self.loop, pace=self.pace)
                self.sessions[sid] = s
            return s

    @staticmethod
    def parse_path(raw):
        """/s/<sid>/<chan>?... | /<chan>?... -> (sid, chan, query).
        A leading /http segment is stripped: the cloudflare tunnel routes
        path ^/http/ to the HTTP JSON-RPC listener and everything else to
        the websocket listener, so one hostname serves both."""
        url = urllib.parse.urlparse(raw)
        parts = [p for p in url.path.split("/") if p]
        if parts and parts[0] == "http":
            parts = parts[1:]
        if len(parts) >= 3 and parts[0] == "s":
            return parts[1], parts[2], url.query
        return "default", (parts[-1] if parts else "frames"), url.query

    # ---- websocket side ---------------------------------------------------

    async def route(self, ws):
        raw = ws.request.path if hasattr(ws, "request") else ws.path
        sid, chan, query = self.parse_path(raw)
        s = self.session(sid, query)
        if chan == "control":
            async for msg in ws:
                try:
                    s.controls.put(json.loads(msg))
                    await ws.send(json.dumps({"ok": True}))
                except Exception as e:
                    await ws.send(json.dumps({"ok": False, "err": str(e)}))
        elif chan == "rpc":
            async for msg in ws:
                req = json.loads(msg)
                resp = await asyncio.to_thread(s.rpc, req)
                await ws.send(json.dumps(resp))
        else:                               # frames
            q: asyncio.Queue = asyncio.Queue()
            s.subscribers.add(q)
            try:
                while True:
                    await ws.send(await q.get())
            except ConnectionClosed:
                pass                        # browser navigated away: normal
            finally:
                s.subscribers.discard(q)

    async def serve(self, host, port):
        import websockets
        self.loop = asyncio.get_running_loop()
        httpd = ThreadingHTTPServer((host, port + 1), _make_rpc_handler(self))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        async with websockets.serve(self.route, host, port):
            print(f"[sim.server] ws://{host}:{port}/s/<sid>/"
                  f"{{frames|control|rpc}}  http://{host}:{port + 1}"
                  f"/s/<sid>/rpc (POST)")
            while True:
                await asyncio.sleep(3600)


def _make_rpc_handler(server: SimServer):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):           # quiet
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_POST(self):
            sid, chan, query = server.parse_path(self.path)
            if chan != "rpc":
                self.send_response(404)
                self._cors()
                self.end_headers()
                return
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            s = server.session(sid, query)
            body = json.dumps(s.rpc(req)).encode()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m alberta_buck.sim.server")
    ap.add_argument("--experiment", required=True, metavar="TOML")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8797)   # 8787 is BUCKy's
    ap.add_argument("--pace", type=float, default=0.0,
                    help="initial days/second cap per session (0 = flat out)")
    ap.add_argument("--basket", default=None)
    a = ap.parse_args(argv)
    srv = SimServer(a.experiment, basket_impl=a.basket, pace=a.pace)
    asyncio.run(srv.serve(a.host, a.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
