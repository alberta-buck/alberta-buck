"""Serve the viewer and the repo's vectors (see the package docstring)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent


def repo_root() -> Path:
    env = os.environ.get("ALBERTA_BUCK_REPO")
    if env:
        return Path(env).resolve()
    p = HERE
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return HERE.parents[2]


DATA_DIRS = ("build/sim", "test/vectors")


def _index(root: Path) -> list[dict]:
    out = []
    for d in DATA_DIRS:
        base = root / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.json")):
            try:
                out.append({"path": str(p.relative_to(root)), "size": p.stat().st_size})
            except OSError:
                pass
    return out


class Handler(SimpleHTTPRequestHandler):
    root: Path = repo_root()

    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(HERE), **k)

    def do_GET(self):  # noqa: N802  (http.server's name)
        path = self.path.split("?", 1)[0]
        if path == "/data/index.json":
            body = json.dumps(_index(self.root)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/data/"):
            rel = path[len("/data/"):]
            if not any(rel == d or rel.startswith(d + "/") for d in DATA_DIRS) or ".." in rel:
                self.send_error(404)
                return
            target = (self.root / rel).resolve()
            if not target.is_file():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if target.suffix == ".json" else "text/plain")
            self.send_header("Content-Length", str(target.stat().st_size))
            self.end_headers()
            with target.open("rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)
            return
        return super().do_GET()

    def log_message(self, fmt, *args):
        sys.stderr.write("[viewer] " + (fmt % args) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alberta_buck.sim.viewer")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args(argv)
    if not (HERE / "vendor" / "uPlot.iife.min.js").exists():
        print("[viewer] vendor/uPlot.iife.min.js missing: run `make viewer-vendor`", file=sys.stderr)
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    print(f"[viewer] serving {HERE} and {Handler.root}/{{build/sim,test/vectors}} on http://{a.bind}:{a.port}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
