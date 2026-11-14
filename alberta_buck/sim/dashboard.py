"""The dashboard's local server: the page, and the run records it reads.

    python -m alberta_buck.sim.dashboard [--runs build/runs] [--host 127.0.0.1] [--port 8890]
    make nix-venv-dashboard [DASHBOARD_PORT=8890]

Serves core/js/dashboard/ and a JSON API over a runs directory (alberta_buck.sim.runrecord):

  GET  /api/runs                          every run's manifest, newest first
  GET  /api/runs/<id>/manifest            one run's manifest
  GET  /api/runs/<id>/frames?since=<day>  its frames after that day (all without since)
  GET  /api/runs/<id>/interventions       its interventions

and live sessions: worlds it runs itself (server.Session, one thread and pyrevm EVM each), each
writing its run record as it goes (live-<sid>), kept until its operator stops it:

  GET  /api/experiments                   the experiment files a session can start from
  GET  /api/sessions                      the sessions and their state
  POST /api/sessions                      {experiment, days?, seed?, prices?, sets?, paused?} -> {sid, run}
  GET  /api/sessions/<sid>                its state (day, paused, pace, run, last result, error)
  POST /api/sessions/<sid>/control        {op: pause|resume|step|pace|stop|reset|shock|population|
                                           knob|chain|intervene, ...}, applied between days
  GET  /api/sessions/<sid>/members        its households and their live settings
  GET  /api/sessions/<sid>/experiment     its experiment plus the operator's actions, as TOML

A growing run reads the same way as a finished one: the page asks for frames after the last day it
holds. Local only: it binds to 127.0.0.1, never to the tunnel's origin ports; live control is off
unless the host is a loopback address, and a POST must be JSON from the page's own origin.
"""
from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

from alberta_buck.sim import runrecord

STATIC                          = Path(__file__).resolve().parents[2] / "core" / "js" / "dashboard"
EXPERIMENTS                     = Path(__file__).resolve().parent / "experiments"
RUN_ID                          = re.compile(r"^[A-Za-z0-9._-]+$")
RESERVED_PORTS                  = {8000, 8797, 8798}        # the tunnel's origins: never bind them
MAX_BODY                        = 64 * 1024


class LiveSessions:
    """The worlds this dashboard runs. Each is a server.Session kept until its operator stops it
    (no reaping for want of watchers), recording under the dashboard's runs directory."""

    def __init__(self, root: Path, experiments: Path = EXPERIMENTS, max_sessions: int = 2):
        self.root, self.experiments, self.max_sessions = Path(root), Path(experiments), max_sessions
        self.sessions: dict = {}
        self.lock = threading.Lock()

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.experiments.glob("*.toml"))

    def start(self, body: dict) -> dict:
        from alberta_buck.sim.server import Session
        name = str(body.get("experiment") or "economy")
        if name not in self.names():
            raise ValueError(f"no experiment {name!r}")
        sets = [str(x) for x in body.get("sets") or []]
        for key, path in (("days", "scenario.days"), ("seed", "scenario.seed"), ("prices", "scenario.prices")):
            if body.get(key) not in (None, ""):
                sets.append(f"{path}={body[key]}")
        with self.lock:
            live = [s for s in self.sessions.values() if not s.done]
            if self.max_sessions and len(live) >= self.max_sessions:
                raise ValueError(f"{len(live)} live sessions already (at most {self.max_sessions}); stop one first")
            sid = secrets.token_hex(4)
            s = Session(sid, self.experiments / f"{name}.toml", sets, None, None, pace=0.0,
                        record_root=self.root, keep=True, control=f"/api/sessions/{sid}")
            s.experiment_name, s.sets = name, sets
            if body.get("paused"):
                s.controls.put({"op": "pause"})
            self.sessions[sid] = s
        return {"sid": sid, "run": f"live-{sid}"}

    def get(self, sid):
        return self.sessions.get(sid)

    def summary(self, s) -> dict:
        return {"sid": s.sid, "experiment": getattr(s, "experiment_name", None), "sets": getattr(s, "sets", []),
                **s.status()}


class FrameCache:
    """Each run's frames as read so far, extended from the file's last whole line on every request."""

    def __init__(self):
        self.lock = threading.Lock()
        self.runs: dict[str, tuple[int, list]] = {}

    def frames(self, directory: Path) -> list[dict]:
        key = str(directory)
        with self.lock:
            offset, rows = self.runs.get(key, (0, []))
            path = directory / runrecord.FILES["frames"]
            if path.exists() and path.stat().st_size < offset:      # rewritten (a re-import): start again
                offset, rows = 0, []
            new, offset = runrecord.read_lines(path, offset)
            rows = rows + new
            self.runs[key] = (offset, rows)
            return rows


def make_handler(root: Path, static: Path = STATIC, live: LiveSessions | None = None):
    cache = FrameCache()

    class Handler(SimpleHTTPRequestHandler):
        extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript",
                          ".mjs": "text/javascript", ".json": "application/json"}

        def __init__(self, *args, **kw):
            super().__init__(*args, directory=str(static), **kw)

        def log_message(self, fmt, *args):          # quiet: the page polls
            pass

        def _json(self, body, status=200):
            data = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            """A POST's JSON, only from this page's own origin: a JSON content type cannot be sent
            cross-origin without a preflight this server never answers."""
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                raise PermissionError("a POST must be application/json")
            origin = self.headers.get("Origin")
            if origin is not None and urlparse(origin).netloc != self.headers.get("Host"):
                raise PermissionError("cross-origin POST refused")
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("a JSON object is expected")
            return body

        def _session(self, sid):
            s = live.get(sid) if live is not None else None
            if s is None:
                raise LookupError("no such session")
            s._idle_since = time.monotonic()                    # watched
            return s

        def _text(self, text, name):
            data = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/toml; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            try:
                if live is None:
                    return self._json({"error": "live sessions are off"}, 403)
                body = self._body()
                if parts == ["api", "sessions"]:
                    return self._json(live.start(body))
                if len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "control":
                    s = self._session(parts[2])
                    if s.done:
                        return self._json({"error": "the session has ended"}, 409)
                    if not isinstance(body.get("op"), str):
                        raise ValueError("a control needs an op")
                    s.controls.put(body)
                    return self._json({"ok": True, "queued": s.controls.qsize()})
                return self._json({"error": "unknown request"}, 404)
            except PermissionError as e:
                return self._json({"error": str(e)}, 403)
            except LookupError as e:
                return self._json({"error": str(e)}, 404)
            except (ValueError, OSError) as e:
                return self._json({"error": str(e)[:300]}, 400)

        def _live_get(self, parts):
            if live is None:
                return self._json({"error": "live sessions are off"}, 403)
            if parts == ["api", "experiments"]:
                return self._json(live.names())
            if parts == ["api", "sessions"]:
                return self._json([live.summary(s) for s in live.sessions.values()])
            s = self._session(parts[2])
            if len(parts) == 3:
                return self._json(live.summary(s))
            if parts[3] == "members":
                return self._json(s.members())
            if parts[3] == "experiment":
                if s.d is None:
                    raise LookupError("the world is still being built")
                return self._text(s.experiment_toml(), f"live-{s.sid}-day{s.day}.toml")
            return self._json({"error": "unknown request"}, 404)

        def _run(self, run_id) -> Path | None:
            if not RUN_ID.match(run_id):
                return None
            directory = root / run_id
            return directory if (directory / "manifest.json").exists() else None

        def do_GET(self):
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            if not parts or parts[0] != "api":
                return super().do_GET()
            try:
                if parts[:2] in (["api", "sessions"], ["api", "experiments"]):
                    return self._live_get(parts)
                if parts == ["api", "runs"]:
                    return self._json(runrecord.runs(root))
                if len(parts) == 4 and parts[:2] == ["api", "runs"]:
                    directory = self._run(parts[2])
                    if directory is None:
                        return self._json({"error": "no such run"}, 404)
                    if parts[3] == "manifest":
                        return self._json(json.loads((directory / "manifest.json").read_text()))
                    if parts[3] == "frames":
                        since = int((parse_qs(url.query).get("since") or ["-1"])[0])
                        return self._json([f for f in cache.frames(directory) if f["day"] > since])
                    if parts[3] == "interventions":
                        rows, _ = runrecord.read_lines(directory / runrecord.FILES["interventions"])
                        return self._json(rows)
                return self._json({"error": "unknown request"}, 404)
            except LookupError as e:
                return self._json({"error": str(e)}, 404)
            except (ValueError, OSError) as e:
                return self._json({"error": str(e)[:200]}, 400)

    return Handler


def loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def serve(root, host="127.0.0.1", port=8890, static: Path = STATIC, live: bool = True, max_sessions: int = 2,
          experiments: Path = EXPERIMENTS):
    """The dashboard's server (not yet serving: call serve_forever). Live sessions only on a loopback host."""
    if port in RESERVED_PORTS:
        raise ValueError(f"port {port} is reserved for the tunnel")
    sessions = LiveSessions(root, experiments, max_sessions) if live and loopback(host) else None
    server = ThreadingHTTPServer((host, port), make_handler(Path(root), static, sessions))
    server.daemon_threads = True
    server.live = sessions
    return server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m alberta_buck.sim.dashboard", description=__doc__.splitlines()[0])
    ap.add_argument("--runs", default=None, help="runs directory (default $SIM_RUNS or build/runs)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8890)
    ap.add_argument("--no-live", action="store_true", help="read records only; start no sessions")
    ap.add_argument("--max-sessions", type=int, default=2, help="live sessions at once (each its own EVM)")
    a = ap.parse_args(argv)
    root = Path(a.runs) if a.runs else runrecord.default_root()
    root.mkdir(parents=True, exist_ok=True)
    server = serve(root, a.host, a.port, live=not a.no_live, max_sessions=a.max_sessions)
    mode = "live sessions on" if server.live is not None else "records only"
    print(f"dashboard: http://{a.host}:{server.server_address[1]}/  (runs in {root}; {mode})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
