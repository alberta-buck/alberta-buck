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
    """Return (abi, bytecode) for a contract.

    Resolution order:

    1. ``out/<sol_file or name>.sol/<name>.json`` in a repo checkout -- the
       developer path, unchanged: a freshly built artifact always wins, so
       editing a contract and rebuilding takes effect immediately.
    2. the installed ``alberta-buck-contracts`` package.

    The fallback is what lets a pip-installed ``alberta_buck`` deploy a
    world at all: ``repo_root()`` raises ``FileNotFoundError`` when no
    ``foundry.toml`` is reachable, so without it an installed package could
    import fine and then fail on its first deploy (see
    alberta-buck-deployment.org, P2.5).
    """
    try:
        f = repo_root() / "out" / f"{sol_file or name}.sol" / f"{name}.json"
        art = json.loads(f.read_text(encoding="utf-8"))
        return art["abi"], art["bytecode"]["object"]
    except (FileNotFoundError, OSError):
        pass

    try:
        import buck_contracts
    except ImportError:
        raise FileNotFoundError(
            f"no artifact for {name}: not in a repo checkout with out/ built "
            f"(run `make build`), and alberta-buck-contracts is not installed "
            f"(pip install alberta-buck-contracts)") from None

    return buck_contracts.artifact(name)
