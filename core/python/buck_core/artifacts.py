"""Foundry artifact + repo-checkout helpers.

Lifted verbatim from alberta_buck/sim/chain.py (which now re-exports these
names) so that core never imports alberta_buck; see alberta-buck-platform.org.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def repo_root() -> Path:
    """The repository checkout root -- needed for the Foundry artifacts
    (out/) and contract sources the live-EVM helpers consume.

    Resolution order: the ``ALBERTA_BUCK_REPO`` env var; walking up from this
    file (a repo checkout / editable install); walking up from the cwd (a
    venv-installed package run from inside the repo).  ``foundry.toml`` is
    the marker.  Raises FileNotFoundError when no repo is reachable -- the
    fixture-only paths (package data) do not need one.
    """
    env = os.environ.get("ALBERTA_BUCK_REPO")
    candidates = ([Path(env)] if env else []) + [
        Path(__file__).resolve(), Path.cwd().resolve()]
    for start in candidates:
        for p in (start, *start.parents):
            if (p / "foundry.toml").exists():
                return p
    raise FileNotFoundError(
        "alberta-buck repo root not found (looked for foundry.toml from "
        f"{[str(c) for c in candidates]}); set ALBERTA_BUCK_REPO or run "
        "from inside the repo checkout")


def load_artifact(name: str, sol_file: str | None = None) -> tuple[list, str]:
    """Return (abi, bytecode) for out/<sol_file or name>.sol/<name>.json."""
    f = repo_root() / "out" / f"{sol_file or name}.sol" / f"{name}.json"
    art = json.loads(f.read_text())
    return art["abi"], art["bytecode"]["object"]
