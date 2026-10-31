"""Foundry artifact + repo-checkout helpers.

Lifted verbatim from alberta_buck/sim/chain.py (which now re-exports these
names) so that core never imports alberta_buck; see alberta-buck-platform.org.
"""

from __future__ import annotations

import json
import os
import re
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


def link_bytecode(bytecode: str, references: dict, libraries: dict[str, str]) -> str:
    """Link Foundry's byte offsets using fully qualified ``source:Library`` names.

    The artifact's creation bytecode, not its deployed-bytecode offsets, must
    be passed here. Reject missing or malformed references rather than deploy
    a token with an unresolved or accidentally mislinked library.
    """
    if not isinstance(bytecode, str) or not bytecode.startswith("0x") or len(bytecode) % 2:
        raise ValueError("invalid creation bytecode")
    if libraries and not references:
        raise ValueError("library link-reference metadata unavailable")
    data = bytecode[2:]
    occupied: list[tuple[int, int]] = []
    for source, named in references.items():
        for name, locations in named.items():
            qualified = f"{source}:{name}"
            address = libraries.get(qualified)
            if (not isinstance(address, str) or re.fullmatch(r"0x[0-9a-fA-F]{40}", address) is None
                    or int(address[2:], 16) == 0):
                raise ValueError(f"missing or invalid library address: {qualified}")
            for location in locations:
                start, length = location.get("start"), location.get("length")
                if (type(start) is not int or type(length) is not int or start < 0 or length != 20
                        or 2 * (start + length) > len(data)):
                    raise ValueError(f"invalid library location: {qualified}")
                end = start + length
                if any(start < other_end and other_start < end for other_start, other_end in occupied):
                    raise ValueError(f"overlapping library location: {qualified}")
                occupied.append((start, end))
                data = data[:2 * start] + address[2:].lower() + data[2 * end:]
    if re.fullmatch(r"[0-9a-fA-F]*", data) is None:
        raise ValueError("unresolved or invalid creation bytecode")
    return "0x" + data


def load_artifact(name: str, sol_file: str | None = None, *,
                  libraries: dict[str, str] | None = None) -> tuple[list, str]:
    """Return (abi, bytecode) for a contract.

    Resolution order:

    1. ``out/<sol_file or name>.sol/<name>.json`` in a repo checkout -- the
       developer path, unchanged: a freshly built artifact always wins, so
       editing a contract and rebuilding takes effect immediately.
    2. the installed ``alberta-buck-contracts`` package.

    When ``libraries`` is provided, its keys must be fully qualified
    ``source:Library`` names from the artifact's creation link references.
    Bundles containing linked contracts must retain that metadata.

    The fallback is what lets a pip-installed ``alberta_buck`` deploy a
    world at all: ``repo_root()`` raises ``FileNotFoundError`` when no
    ``foundry.toml`` is reachable, so without it an installed package could
    import fine and then fail on its first deploy (see
    alberta-buck-deployment.org, P2.5).
    """
    try:
        f = repo_root() / "out" / f"{sol_file or name}.sol" / f"{name}.json"
        art = json.loads(f.read_text(encoding="utf-8"))
        bytecode = art["bytecode"]["object"]
        if libraries is not None:
            bytecode = link_bytecode(bytecode, art["bytecode"].get("linkReferences", {}), libraries)
        return art["abi"], bytecode
    except (FileNotFoundError, OSError):
        pass

    try:
        import buck_contracts
    except ImportError:
        raise FileNotFoundError(
            f"no artifact for {name}: not in a repo checkout with out/ built "
            f"(run `make build`), and alberta-buck-contracts is not installed "
            f"(pip install alberta-buck-contracts)") from None

    abi, bytecode = buck_contracts.artifact(name)
    if libraries is not None:
        entry = buck_contracts.contracts()[name]
        bytecode = link_bytecode(bytecode, entry.get("linkReferences", {}), libraries)
    return abi, bytecode
