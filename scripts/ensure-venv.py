#!/usr/bin/env python3
"""Maintain a checkout's editable environment, including after Nix replaces its Python store path."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys


def ensure(root, path):
    root                        = root.resolve()
    path                        = path.absolute()
    python                      = Path(sys._base_executable).resolve()
    executable                  = path / "bin/python"
    stamp                       = path / ".installed.json"
    projects                    = [root / "core/contracts/python", root / "core/python", root]
    wanted                      = dict(python=str(python), version=sys.version, root=str(root),
                                       extras="tests,dev,server", projects={str(p): sha256(
                                           (p / "pyproject.toml").read_bytes()).hexdigest() for p in projects})
    try:
        current                 = json.loads(stamp.read_text())
    except (FileNotFoundError, ValueError):
        current                 = {}
    try:
        healthy                 = executable.exists() and subprocess.run(
            [str(executable), "-c", "import pip, sys; assert sys.prefix != sys.base_prefix"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        healthy                 = False
    if current == wanted and healthy:
        return
    if path.exists() and not (path / "pyvenv.cfg").is_file():
        raise ValueError(f"{path} exists but is not a virtual environment; choose VENV=another/path")
    if not healthy or current.get("python") != str(python):
        command                 = [str(python), "-m", "venv"]
        if path.exists():
            command.append("--upgrade")
        subprocess.run(command + [str(path)], check=True)
    command                     = [str(executable), "-m", "pip", "install", "--no-user", "--upgrade"]
    for p in projects:
        command                += ["-e", str(p) + ("[tests,dev,server]" if p == root else "")]
    subprocess.run(command, check=True)
    stamp.write_text(json.dumps(wanted, indent=2) + "\n")


if __name__ == "__main__":
    parser                      = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args                        = parser.parse_args()
    ensure(args.root, args.path)
