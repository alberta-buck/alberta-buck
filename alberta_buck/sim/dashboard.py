"""The dashboard's local server: the page, and the run records it reads.

    python -m alberta_buck.sim.dashboard [--runs build/runs] [--host 127.0.0.1] [--port 8890]
    make nix-venv-dashboard [DASHBOARD_PORT=8890]

Serves core/js/dashboard/ and a read-only JSON API over a runs directory (alberta_buck.sim.runrecord):

  GET /api/runs                          every run's manifest, newest first
  GET /api/runs/<id>/manifest            one run's manifest
  GET /api/runs/<id>/frames?since=<day>  its frames after that day (all without since)
  GET /api/runs/<id>/interventions       its interventions

A growing run reads the same way as a finished one: the page asks for frames after the last day it
holds. Nothing here starts, steers or stops a simulation; a live session's controls are its own
server's (the manifest's capabilities.control). Local first: it binds to 127.0.0.1 unless told
otherwise, and never to the tunnel's origin ports.
"""
from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
import threading
from urllib.parse import parse_qs, urlparse

from alberta_buck.sim import runrecord

STATIC                          = Path(__file__).resolve().parents[2] / "core" / "js" / "dashboard"
RUN_ID                          = re.compile(r"^[A-Za-z0-9._-]+$")
RESERVED_PORTS                  = {8000, 8797, 8798}        # the tunnel's origins: never bind them


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


def make_handler(root: Path, static: Path = STATIC):
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
            except (ValueError, OSError) as e:
                return self._json({"error": str(e)[:200]}, 400)

    return Handler


def serve(root, host="127.0.0.1", port=8890, static: Path = STATIC):
    if port in RESERVED_PORTS:
        raise ValueError(f"port {port} is reserved for the tunnel")
    server = ThreadingHTTPServer((host, port), make_handler(Path(root), static))
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m alberta_buck.sim.dashboard", description=__doc__.splitlines()[0])
    ap.add_argument("--runs", default=None, help="runs directory (default $SIM_RUNS or build/runs)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8890)
    a = ap.parse_args(argv)
    root = Path(a.runs) if a.runs else runrecord.default_root()
    root.mkdir(parents=True, exist_ok=True)
    server = serve(root, a.host, a.port)
    print(f"dashboard: http://{a.host}:{server.server_address[1]}/  (runs in {root})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
