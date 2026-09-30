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
                     and the savings facility's block "sv" (the depositors'
                     book, read from the basket at the frame); ?lite=1
                     drops the per-agent telemetry (ag, octl, arb2, lp, mx)
                     for a browser client, and &replay=1 first sends every
                     lite frame the world has made (a reloaded page redraws
                     its charts)
  /s/<sid>/control   {"op": ..., ...} applied at the next day boundary:
                     "pace" {days_per_second}, "population" {cls, count},
                     "knob" {cls, name, value}, "pause", "resume",
                     "step" {days}, "shock" {side buy|sell, usd, days}
                     (arms the cast's ShockAgent), "chain" {name l1|l2}
                     (the work wheel's caller gas profile), "reset"
                     (rebuild the world from its first day, in place: the
                     same link and watchers, who receive {"reset": true})
  /s/<sid>/rpc       JSON-RPC 2.0 over WS; besides the chain's own methods,
                     "sim_info" (the world's addresses and constituents),
                     "sim_status" (day, paused, pace) and "sim_enroll"
                     [address] (register the address as an identity with
                     the world's issuer and approve the basket to pay it
                     BUCK: an equity basket pays its exits in BUCK) are
                     answered by the server

PUBLIC mode (--public, for serving behind the tunnel): the pyrevm provider
executes eth_sendTransaction for ANY `from` with no signature, so a public
server must not forward it -- a visitor could act as governance or as any
agent.  --public answers only reads, receipts, SIGNED raw transactions
(the sender is recovered from the signature: a visitor acts only as
itself) and sim_info / sim_status; everything else is refused.  Session
ids should be unguessable (the page draws a random one): anyone holding
one can drive that world.

The chain is serialized against the world: the sim thread holds the
session's chain lock from the end of each day's start (after any pause)
to its frame, so RPC -- a visitor's deposit, say -- lands only BETWEEN
days or while the world is paused, never inside an agent's day.  A JSON-RPC
batch is answered under ONE hold of the lock (a page's approve + deposit +
their receipts land together), and between days the sim thread lets queued
RPC through before it takes the chain again -- and, while a page holds an
rpc channel open, waits a moment after each frame for the reads that frame
prompts -- so a running world never starves its visitor.

HTTP (port+1, stdlib, CORS *):
  POST /s/<sid>/rpc  JSON-RPC 2.0 -- viem's standard http transport:
                     anvilSession("http://host:port+1/s/<sid>/rpc")

STATIC (--static DIR): plain GETs on the websocket port serve DIR (the
built sandbox, core/js/sandbox/dist), and /sim-server.json tells the page
its world is on the same origin -- one port for the page and the world,
behind a single-origin tunnel.

Run:
    python -m alberta_buck.sim.server --experiment \
        alberta_buck/sim/experiments/backdrop.toml --port 8787
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue, Empty

from websockets.exceptions import ConnectionClosed, InvalidMessage

from alberta_buck.sim import experiment as expmod
from alberta_buck.sim.loop import run
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil

IDLE_REAP_S = 600           # reap a session with no subscribers this long
RPC_GRACE_S = 0.5           # after a frame / an answer, a connected page's turn
RPC_YIELD_S = 5.0           # the most a day waits on queued RPC
HISTORY_MAX = 4000          # lite frames kept for a reloaded page's replay
_BUILD_LOCK = threading.Lock()   # agent classes keep build-time counters

# The depositors' book, read at each frame (the venue facet's view through
# the shell's fallback).
_SV_ABI = [
    {"type": "function", "name": n, "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint256", "name": ""}]}
    for n in ("totalOutstandingBuck", "stressBonusPrincipal", "treasuryBuckPending")
]
# The equity basket's book: shares, and the equity, lien and liquidity of its
# credit account (BuckBasketEquity).
_SV_EQ_ABI = [
    {"type": "function", "name": n, "stateMutability": "view", "inputs": [],
     "outputs": [{"type": t, "name": ""}]}
    for n, t in (("totalShares", "uint256"), ("treasuryShares", "uint256"),
                 ("equity", "uint256"), ("sharePrice", "uint256"),
                 ("lien", "uint256"), ("reliefAccrued", "uint256"),
                 ("liquidity", "uint256"), ("headroom", "int256"),
                 ("LAMBDA_BP", "uint256"))
]
_POSITIONS_ABI = [{"type": "function", "name": "positions", "stateMutability": "view",
                   "inputs": [{"type": "bytes32", "name": "key"}],
                   "outputs": [{"type": "uint128", "name": "liquidity"}] + [
                       {"type": "uint256", "name": n} for n in ("fg0", "fg1")] + [
                       {"type": "uint128", "name": n} for n in ("owed0", "owed1")]}]


def _is_equity(d) -> bool:
    """An equity basket (a credit holder paying its exits in BUCK)."""
    return str(getattr(d, "basket_impl", "")).startswith("equity")


def _depositor_buck(d) -> int | None:
    """B of the venue's poolBuckValues -- each pool's BUCK balance, the
    depositors' share of the basket's position (its liquidity less the
    treasury's slice) -- read without poolBuckValues' slippage guard, which
    reverts whenever any pool is off its average: the book is the book."""
    from web3 import Web3
    B = 0
    for i in range(len(d.tokens)):
        c = d.basket.functions.constituents(i).call()
        pool, lo, hi, treasury_l = c[5], c[6], c[7], c[10]
        key = Web3.solidity_keccak(["address", "int24", "int24"], [d.basket.address, lo, hi])
        total_l = d.w3.eth.contract(address=pool, abi=_POSITIONS_ABI).functions.positions(key).call()[0]
        if total_l <= treasury_l:
            continue
        B += (total_l - treasury_l) * int(d.buck.functions.balanceOf(pool).call()) // total_l
    return B or None


class StopSession(Exception):
    """Raised inside on_day_start to unwind a reaped session's loop."""


class ResetSession(Exception):
    """Raised by a "reset" control to unwind the run and rebuild the world
    from its first day, in place: the same session, link and watchers."""


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
        self.paused = False
        self.step_left: int | None = None
        self._enrolled: dict = {}           # sim_enroll: address -> (register args, sk)
        self.day = None
        self.d = None                       # the deployment, once built
        self.agents = None
        self.lite: set[asyncio.Queue] = set()   # subscribers wanting lite frames
        self._info = None
        self._holding = False
        self._last_day_ts = time.monotonic()
        self._idle_since = time.monotonic()
        self.history: list[str] = []            # lite frames, for replay
        self.rpc_clients = 0                    # open rpc channels
        self.rpc_waiting = 0                    # RPC envelopes queued on the chain
        self._rpc_count = threading.Lock()
        self._rpc_ts = 0.0                      # the last frame or answer
        self._sv = None
        self.scenario = None
        exp = expmod.load(exp_path, sets=sets)
        self.scenario_factory = lambda: expmod.build(exp)
        self.basket_impl = basket_impl or exp.scenario.get("basket", "prorata")
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"sim-{sid}")
        self.thread.start()

    # ---- sim-thread side --------------------------------------------------

    def _run(self):
        try:
            while True:
                try:
                    self._build_and_run()
                    break
                except ResetSession:
                    self._restarting()
        except StopSession:
            print(f"[sim.server] session '{self.sid}' reaped (idle)")
        except Exception as e:
            print(f"[sim.server] session '{self.sid}' died: {e!r}")
            self._fanout_threadsafe(json.dumps({"error": str(e)}))
        finally:
            if self._holding:               # a day died holding the chain
                self._holding = False
                self.chain_lock.release()
            self.done = True
            self._fanout_threadsafe(json.dumps({"done": True}))

    def _build_and_run(self):
        with _BUILD_LOCK:
            scenario = self.scenario = self.scenario_factory()
            anvil = PyrevmAnvil()
            anvil.start()
            self.provider = anvil.w3.provider
        try:
            run(scenario, anvil, out_path=None, verbose=False,
                basket_impl=self.basket_impl,
                on_day_start=self._on_day_start, on_frame=self._on_frame)
        finally:
            anvil.stop()

    def _restarting(self):
        """Between a reset and the rebuild: let the chain go, forget the old
        world (its day, its deployment, the keys enrolled with its issuer,
        the ops queued for it), and tell the watchers -- whose pages clear
        and wait for the new world's first frame."""
        if self._holding:
            self._holding = False
            self.chain_lock.release()
        self.day = self.d = self.agents = self._info = self._sv = None
        self._enrolled.clear()
        self.paused, self.step_left = False, None
        while True:
            try:
                self.controls.get_nowait()
            except Empty:
                break
        self._idle_since = self._last_day_ts = time.monotonic()
        print(f"[sim.server] session '{self.sid}' reset")
        if self.aloop is not None:
            self.aloop.call_soon_threadsafe(self._reset_fanout)

    def _reset_fanout(self):
        """(asyncio side) The replay starts over, and every watcher hears so."""
        self.history.clear()
        msg = json.dumps({"reset": True})
        for q in list(self.subscribers):
            q.put_nowait(msg)

    def _on_day_start(self, day, d, agents, ctr):
        self.day, self.d, self.agents = day, d, agents
        if self.subscribers:
            self._idle_since = time.monotonic()
        elif time.monotonic() - self._idle_since > IDLE_REAP_S:
            raise StopSession()
        if self.pace > 0:
            wait = (1.0 / self.pace) - (time.monotonic() - self._last_day_ts)
            if wait > 0:
                time.sleep(wait)
        self._drain(day, agents)
        self._advance_step()
        self._wait_while_paused(day, agents)
        self._last_day_ts = time.monotonic()
        self._yield_to_rpc()
        self.chain_lock.acquire()           # the day is the world's
        self._holding = True

    def _yield_to_rpc(self):
        """Between days, let queued RPC through before the day takes the
        chain.  The sim thread would otherwise re-take the lock at once (a
        released lock is not handed to its waiter), and a page's deposit
        could wait forever.  While a page holds an rpc channel open, a short
        grace after each frame and each answer lets the reads a frame
        prompts, and a sequence of calls, land in the same gap."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < RPC_YIELD_S:
            if self.rpc_waiting:
                time.sleep(0.002)
            elif self.rpc_clients and time.monotonic() - self._rpc_ts < RPC_GRACE_S:
                time.sleep(0.005)
            else:
                break

    def _wait_while_paused(self, day, agents):
        """Hold the day's start while paused (RPC is served meanwhile).  A
        step begun here starts with THIS day: its first day is counted now,
        or "+1 day" would run two."""
        while self.paused:
            if self.subscribers:
                self._idle_since = time.monotonic()
            elif time.monotonic() - self._idle_since > IDLE_REAP_S:
                raise StopSession()
            time.sleep(0.05)
            self._drain(day, agents)
            if not self.paused and self.step_left is not None:
                self._advance_step()

    def _advance_step(self):
        """A "step" of n days runs n days, then pauses at the next start."""
        if self.step_left is None:
            return
        if self.step_left <= 0:
            self.paused, self.step_left = True, None
        else:
            self.step_left -= 1

    def _drain(self, day, agents):
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
        elif kind == "reset":
            raise ResetSession()
        elif kind == "pause":
            self.paused = True
        elif kind == "resume":
            self.paused, self.step_left = False, None
        elif kind == "step":
            self.step_left = max(1, int(op.get("days", 1)))
            self.paused = False
        elif kind == "shock":
            for a in agents:
                if type(a).__name__ == "ShockAgent":
                    a.arm(op.get("side", "sell"), float(op.get("usd", 0)),
                          int(op.get("days", 1)))
                    break
        elif kind == "chain":
            for a in agents:
                if hasattr(a, "set_chain"):
                    a.set_chain(str(op.get("name", "l2")))

    # The per-agent telemetry a browser client does not need.
    HEAVY = ("ag", "octl", "arb2", "lp", "mx")

    def _on_frame(self, frame):
        self._sv = self._savings()          # still the day's: the chain is ours
        if self._holding:
            self._holding = False
            self.chain_lock.release()       # between days: RPC may run
        self._rpc_ts = time.monotonic()
        self._fanout_threadsafe(*self._payloads(frame))

    def _savings(self) -> dict | None:
        """The savings facility's book at this frame: outstanding principal
        O (receipts P plus the credited bonus S), the depositors' BUCK side
        B, the treasury's pending BUCK, and D -- what a depositor is paid, in
        BUCK value, per BUCK deposited.  A redemption burns its share Rb of O
        and pays the TOKEN side of its claim Rb*2B/O: the half, when B >= O
        (the BUCK surplus goes to the treasury), or the claim less the burn
        when B < O (TOKEN converted to cover it) -- per BUCK of principal
        R = Rb*P/O, D = min(B, 2B - O) / P.  1 at the start; the wheel's
        credits and the harvest of reversion raise it."""
        d = self.d
        if d is None or getattr(d, "basket", None) is None:
            return None
        if _is_equity(d):
            return self._savings_equity()
        try:
            c = d.w3.eth.contract(address=d.basket.address, abi=_SV_ABI)
            O = int(c.functions.totalOutstandingBuck().call())
            S = int(c.functions.stressBonusPrincipal().call())
            T = int(c.functions.treasuryBuckPending().call())
        except Exception:
            return None
        try:
            B = _depositor_buck(d)
        except Exception:                   # no depositors yet
            B = None
        P = O - S
        D = (min(B, 2 * B - O) / P) if (B is not None and P > 0) else None
        return {"kind": "prorata", "O": O, "S": S, "P": P, "B": B, "T": T,
                "D": round(D, 6) if D is not None else None}

    def _savings_equity(self) -> dict | None:
        """The equity basket's book at this frame: its shares N (the
        treasury's cut among them, NT), the equity E they own (1e18-scaled
        share price sp, at the TWAP marks), and its credit account -- the
        lien L, the relief accrued on it, the BUCK it can spend (liquidity Q)
        and the room left under K x equity (headroom H, negative under
        water).  A redemption of `shares` with cost basis b pays
        worth = shares x sp less the treasury's cut, lam of the gain over b,
        less the exit charge chg = (1 + K)/2 x the widest pool fee (1e18),
        at the pools' low marks.  D = sp: BUCK value per BUCK deposited, 1
        at the start (less the entry charge)."""
        d = self.d
        try:
            c = d.w3.eth.contract(address=d.basket.address, abi=_SV_EQ_ABI)
            v = {n: c.functions[n]().call() for n in (
                "totalShares", "treasuryShares", "equity", "sharePrice", "lien",
                "reliefAccrued", "liquidity", "headroom", "LAMBDA_BP")}
            k = int(d.kctrl.functions.buckK().call())
            fee = max((int(d.basket.functions.constituents(i).call()[4])
                       for i in range(len(d.tokens))), default=0)
        except Exception:
            return None
        sp = int(v["sharePrice"])
        return {"kind": "equity", "N": int(v["totalShares"]), "NT": int(v["treasuryShares"]),
                "E": int(v["equity"]), "sp": sp, "L": int(v["lien"]),
                "relief": int(v["reliefAccrued"]), "Q": int(v["liquidity"]),
                "H": int(v["headroom"]), "lam": int(v["LAMBDA_BP"]),
                "chg": fee * 10 ** 12 * (10 ** 18 + k) // (2 * 10 ** 18),
                "D": round(sp / 1e18, 6)}

    def enroll(self, address: str) -> dict:
        """Make `address` a registered identity -- a credential from the
        world's issuer, as the sim's own depositors hold -- and lay down its
        identity-bound approve of the basket, which a public contract needs
        before it may pay the address BUCK.  Idempotent.  The world executes
        unsigned transactions for any sender, so the server acts as the
        address; the identity's secret stays on the server."""
        from web3 import Web3
        from alberta_buck.sim import identity as idmod
        d = self.d
        addr = Web3.to_checksum_address(address)
        rng = idmod.seeded_rng(int(addr, 16) ^ int(getattr(self.scenario, "seed", 0) or 0))
        chainid, registry = int(d.w3.eth.chain_id), int(d.reg.address, 16)
        ident = self._enrolled.get(addr)
        if ident is None:
            if d.reg.functions.isVerified(addr).call():
                return {"address": addr, "registered": True, "approved": False}
            ident = idmod.register_args(
                d.issuer_kp, int(addr, 16),
                idmod.fields_for("Saver", len(self._enrolled)), rng, chainid, registry,
                with_sk=True)
            d.chain.send(d.reg.functions.register(d.issuer_addr, *ident[0]),
                         sender=addr, gas=3_000_000)
            self._enrolled[addr] = ident
        approved = False
        if _is_equity(d):
            idmod.identity_approve(d.chain, d.reg, d.buck, addr, ident[0], ident[1],
                                   d.basket.address, rng)
            approved = True
        return {"address": addr, "registered": True, "approved": approved}

    def _payloads(self, frame) -> tuple[str, str]:
        """(full, lite) JSON for one frame."""
        now = time.monotonic()
        pace = round(1.0 / max(now - self._last_day_ts, 1e-9), 2)
        full = {**frame, "pace": pace, "sid": self.sid, "paused": self.paused,
                "sv": self._sv}
        lite = {k: v for k, v in full.items() if k not in self.HEAVY}
        return json.dumps(_jsonable(full)), json.dumps(_jsonable(lite))

    def _fanout_threadsafe(self, payload, lite=None):
        if self.aloop is not None:
            self.aloop.call_soon_threadsafe(self._fanout, payload, lite)

    def _fanout(self, payload, lite=None):
        if lite is not None:                # a frame (not done / error)
            self.history.append(lite)
            if len(self.history) > HISTORY_MAX:
                del self.history[:len(self.history) - HISTORY_MAX]
        for q in list(self.subscribers):
            if q.qsize() < 100:
                q.put_nowait(lite if (lite is not None and q in self.lite)
                             else payload)

    def info(self) -> dict:
        """The world's addresses and constituents, for a page that acts in
        it (the savings demonstration's wallet)."""
        if self._info is None and self.d is not None:
            d = self.d
            toks = []
            for i, t in enumerate(d.tokens):
                try:
                    sym = t.functions.symbol().call()
                except Exception:
                    sym = f"T{i}"
                toks.append({"symbol": sym, "address": t.address,
                             "decimals": int(d.dec[i]),
                             "pool_usdc": d.pool_usdc[i],
                             "pool_buck": d.pool_buck[i]})
            sc = self.scenario
            files = list(getattr(sc, "csv_files", []) or [])
            # A window's files are stamped <end>-<n>d (gen_historical): it
            # began n-1 days before its end.
            m = re.search(r"(\d{4}-\d{2}-\d{2})-(\d+)d", files[0]) if files else None
            start = None
            if m:
                import datetime
                start = (datetime.date.fromisoformat(m.group(1))
                         - datetime.timedelta(days=int(m.group(2)) - 1)).isoformat()
            wheel = next((getattr(a, "sol", None) for a in (self.agents or [])
                          if getattr(a, "sol", None) is not None), None)
            try:
                receipt = d.basket.functions.receipt().call()
            except Exception:
                receipt = None
            self._info = {
                "chain_id": int(d.w3.eth.chain_id),
                "buck": d.buck.address, "usdc": d.usdc.address,
                "basket": d.basket.address, "receipt": receipt,
                "kctrl": d.kctrl.address, "pool_ub": d.pool_ub,
                "router": getattr(getattr(d, "router", None), "address", None),
                "wheel": wheel.address if wheel is not None else None,
                "director": getattr(getattr(d, "director", None), "address", None),
                "basket_kind": "equity" if _is_equity(d) else "prorata",
                "name": getattr(sc, "name", None),
                "days": getattr(sc, "days", None),
                "ticks_per_day": getattr(sc, "ticks_per_day", None),
                "prices": files,
                "start_date": start,
                "tokens": toks}
        return self._info or {}

    def status(self) -> dict:
        chain = next((a.profile.name for a in (self.agents or [])
                      if hasattr(a, "set_chain") and getattr(a, "profile", None)), None)
        return {"day": self.day, "paused": self.paused, "pace": self.pace,
                "step_left": self.step_left, "done": self.done, "chain": chain}

    def rpc(self, req):
        """Answer a JSON-RPC envelope (or batch) from the world's provider,
        serialized against the sim loop by the chain lock -- ONE hold for
        the whole batch, taken only if some request needs the chain."""
        batch = req if isinstance(req, list) else [req]
        out = []
        held = False
        with self._rpc_count:
            self.rpc_waiting += 1
        try:
            for r in batch:
                method = r.get("method")
                if getattr(self, "public", False) and method not in PUBLIC_RPC:
                    out.append({"jsonrpc": "2.0", "id": r.get("id"), "error": {
                        "code": -32601, "message": f"not served publicly: {method}"}})
                    continue
                if method == "sim_status":      # no chain: answered at once
                    out.append({"jsonrpc": "2.0", "id": r.get("id"),
                                "result": _jsonable(self.status())})
                    continue
                if not held:
                    self.chain_lock.acquire()
                    held = True
                if method == "sim_info":
                    out.append({"jsonrpc": "2.0", "id": r.get("id"),
                                "result": _jsonable(self.info())})
                    continue
                if method == "sim_enroll":
                    try:
                        res = self.enroll((r.get("params") or [""])[0])
                        out.append({"jsonrpc": "2.0", "id": r.get("id"),
                                    "result": _jsonable(res)})
                    except Exception as e:
                        out.append({"jsonrpc": "2.0", "id": r.get("id"),
                                    "error": {"code": -32000, "message": str(e)}})
                    continue
                try:
                    resp = self.provider.make_request(method, r.get("params", []))
                    if "error" in resp:
                        out.append({"jsonrpc": "2.0", "id": r.get("id"),
                                    "error": resp["error"]})
                    else:
                        out.append({"jsonrpc": "2.0", "id": r.get("id"),
                                    "result": _jsonable(resp.get("result"))})
                except Exception as e:
                    out.append({"jsonrpc": "2.0", "id": r.get("id"),
                                "error": {"code": -32000, "message": str(e)}})
        finally:
            if held:
                self.chain_lock.release()
            with self._rpc_count:
                self.rpc_waiting -= 1
            self._rpc_ts = time.monotonic()
        return out if isinstance(req, list) else out[0]


# What a --public server forwards to a world's chain: reads, receipts and
# signed transactions -- never an unsigned eth_sendTransaction.
PUBLIC_RPC = frozenset({
    "eth_chainId", "eth_blockNumber", "eth_gasPrice", "eth_estimateGas",
    "eth_getBalance", "eth_getCode", "eth_getTransactionCount",
    "eth_getBlockByNumber", "eth_getBlockByHash", "eth_call",
    "eth_sendRawTransaction", "eth_getTransactionReceipt",
    "eth_getTransactionByHash", "net_version", "web3_clientVersion",
    "sim_info", "sim_status", "sim_enroll"})


# The session overrides a --public server accepts (?set=key=value): the
# page's toggles, not the cast -- a visitor must not size the world.
PUBLIC_SETS = ("scenario.seed=", "scenario.prices=", "agents.BasketWheelAgent.chain=")


class SessionRefused(Exception):
    pass


class SimServer:
    def __init__(self, exp_path, basket_impl=None, pace=0.0, public=False,
                 max_sessions=0, static_dir=None):
        self.exp_path = exp_path
        self.public = public
        self.static_dir = Path(static_dir).resolve() if static_dir else None
        self.max_sessions = max_sessions
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
                if self.public:
                    sets = [x for x in sets if x.startswith(PUBLIC_SETS)]
                live = sum(1 for x in self.sessions.values() if not x.done)
                if self.max_sessions and live >= self.max_sessions:
                    raise SessionRefused(f"{live} sessions running (max {self.max_sessions})")
                print(f"[sim.server] building session '{sid}' "
                      f"(sets={sets or '-'})...")
                s = Session(sid, self.exp_path, sets, self.basket_impl,
                            self.loop, pace=self.pace)
                s.public = self.public
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
        try:
            s = self.session(sid, query)
        except SessionRefused as e:
            await ws.send(json.dumps({"error": str(e), "refused": True}))
            return
        if chan == "control":
            async for msg in ws:
                try:
                    s.controls.put(json.loads(msg))
                    await ws.send(json.dumps({"ok": True}))
                except Exception as e:
                    await ws.send(json.dumps({"ok": False, "err": str(e)}))
        elif chan == "rpc":
            s.rpc_clients += 1
            try:
                async for msg in ws:
                    try:
                        req = json.loads(msg)
                    except ValueError as e:
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": None, "error": {
                            "code": -32700, "message": f"parse error: {e}"}}))
                        continue
                    resp = await asyncio.to_thread(s.rpc, req)
                    await ws.send(json.dumps(resp))
            except ConnectionClosed:
                pass
            finally:
                s.rpc_clients -= 1
        else:                               # frames
            q: asyncio.Queue = asyncio.Queue()
            qs = urllib.parse.parse_qs(query)
            lite = qs.get("lite", ["0"])[0] not in ("0", "")
            # No await from here to the add: _fanout runs on this loop, so
            # the replay and the stream neither overlap nor miss a frame.
            past = list(s.history) if lite and qs.get("replay", ["0"])[0] not in ("0", "") else []
            s.subscribers.add(q)
            if lite:
                s.lite.add(q)
            try:
                for f in past:
                    await ws.send(f)
                while True:
                    await ws.send(await q.get())
            except ConnectionClosed:
                pass                        # browser navigated away: normal
            finally:
                s.subscribers.discard(q)
                s.lite.discard(q)

    def static(self, connection, request):
        """websockets' process_request: a plain GET (no Upgrade) is a file of
        the static page, or /sim-server.json; an upgrade proceeds."""
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        path = urllib.parse.unquote(urllib.parse.urlparse(request.path).path)
        if path == "/sim-server.json":
            body = json.dumps({"server": "same-origin", "public": self.public}).encode()
            ctype = "application/json"
        else:
            rel = path.lstrip("/")
            if rel == "" or rel.endswith("/"):
                rel += "index.html"
            f = (self.static_dir / rel).resolve()
            if not f.is_relative_to(self.static_dir) or not f.is_file():
                return connection.respond(404, "not found\n")
            body = f.read_bytes()
            ctype = {".js": "text/javascript", ".mjs": "text/javascript",
                     ".wasm": "application/wasm", ".css": "text/css",
                     ".html": "text/html; charset=utf-8",
                     ".json": "application/json", ".txt": "text/plain; charset=utf-8",
                     }.get(f.suffix) or mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        r = connection.respond(200, "")
        r.body = body
        for k in ("Content-Length", "Content-Type"):
            del r.headers[k]
        r.headers["Content-Length"] = str(len(body))
        r.headers["Content-Type"] = ctype
        r.headers["Cache-Control"] = "no-cache"
        return r

    async def serve(self, host, port):
        import websockets
        logging.getLogger("websockets.server").addFilter(_HandshakeProbes())
        self.loop = asyncio.get_running_loop()
        httpd = ThreadingHTTPServer((host, port + 1), _make_rpc_handler(self))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        extra = {"process_request": self.static} if self.static_dir else {}
        async with websockets.serve(self.route, host, port, max_size=2 ** 22, **extra):
            print(f"[sim.server] ws://{host}:{port}/s/<sid>/"
                  f"{{frames|control|rpc}}  http://{host}:{port + 1}"
                  f"/s/<sid>/rpc (POST)"
                  + (f"  page: http://{host}:{port}/ ({self.static_dir})"
                     if self.static_dir else ""))
            while True:
                await asyncio.sleep(3600)


class _HandshakeProbes(logging.Filter):
    """Drop the traceback websockets logs when a connection closes before
    sending a valid HTTP request: port probes (nc -z), scanners and health
    checks all connect and hang up without a byte.  Anything that got far
    enough to be a websocket still logs normally."""

    def filter(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, InvalidMessage)


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
            # A public server builds worlds only from the frames channel; its
            # RPC answers existing sessions.
            s = (server.sessions.get(sid) if server.public
                 else None)
            try:
                if s is None and not server.public:
                    s = server.session(sid, query)
            except SessionRefused as e:
                s, err = None, str(e)
            else:
                err = "no such session"
            if s is None or s.done:
                body = json.dumps({"jsonrpc": "2.0", "id": (req.get("id") if isinstance(req, dict) else None),
                                   "error": {"code": -32000, "message": err}}).encode()
                self.send_response(503)
            else:
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
    ap.add_argument("--public", action="store_true",
                    help="serving behind the tunnel: RPC allowlist (no unsigned "
                         "eth_sendTransaction), session overrides limited to the page's toggles")
    ap.add_argument("--max-sessions", type=int, default=0,
                    help="refuse new sessions beyond this many live ones (0 = no cap)")
    ap.add_argument("--static", default=None, metavar="DIR",
                    help="serve this directory (the built sandbox) on the websocket port")
    a = ap.parse_args(argv)
    srv = SimServer(a.experiment, basket_impl=a.basket, pace=a.pace,
                    public=a.public, max_sessions=a.max_sessions,
                    static_dir=a.static)
    asyncio.run(srv.serve(a.host, a.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
