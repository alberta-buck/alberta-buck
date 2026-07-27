"""alberta-buck-contracts -- compiled ABI + bytecode for the Alberta Buck contracts.

Nothing here is deployed at a fixed address.  Every BUCK world deploys fresh
and learns its addresses from the receipts, so what this package ships is
``(abi, bytecode)`` pairs -- not a deployment registry.  ``deployments``
exists so that publishing real addresses later is not a breaking change.

This is what lets an installed ``alberta-buck`` deploy a world without a
repository checkout: ``buck_core.artifacts.load_artifact`` falls back here
when no ``foundry.toml`` is reachable.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).resolve().parent

__all__ = ["artifact", "contracts", "compiler", "deployments", "names"]


@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    return json.loads((_HERE / f"{name}.json").read_text())


def contracts() -> dict:
    """Every published contract, keyed by name."""
    return _load("contracts")["contracts"]


def compiler() -> dict:
    """Build provenance: solc version, optimizer settings, git commit, sha256."""
    return _load("compiler")


def deployments() -> dict:
    """Known deployments by chain id.  Empty: BUCK worlds deploy their own."""
    return _load("deployments")


def names() -> list[str]:
    """The contracts this package ships."""
    return sorted(contracts())


def artifact(name: str) -> tuple[list, str]:
    """``(abi, bytecode)`` for one contract -- the shape ``load_artifact`` returns."""
    all_ = contracts()
    if name not in all_:
        external = compiler().get("external", {}).get(name)
        if external:
            raise KeyError(
                f"{name} is not published here -- it is a third-party contract; "
                f"install {external} and take it from there")
        raise KeyError(f"unknown contract {name}; this package ships: "
                       f"{', '.join(names())}")
    c = all_[name]
    return c["abi"], c["bytecode"]
