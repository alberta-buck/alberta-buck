"""Safe, reproducible linking for Solidity creation artifacts."""

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from buck_core.artifacts import link_bytecode, load_artifact

_VECTOR = json.loads((Path(__file__).resolve().parents[3] / "test/fixtures/library-linking.json").read_text())


def test_shared_linking_vector():
    assert link_bytecode(_VECTOR["bytecode"], _VECTOR["references"], _VECTOR["libraries"]) == _VECTOR["linked"]


def test_missing_and_invalid_library_addresses_are_rejected():
    libraries = dict(_VECTOR["libraries"])
    del libraries["src/First.sol:First"]
    with pytest.raises(ValueError, match="missing or invalid library"):
        link_bytecode(_VECTOR["bytecode"], _VECTOR["references"], libraries)
    libraries["src/First.sol:First"] = "0xabc"
    with pytest.raises(ValueError, match="missing or invalid library"):
        link_bytecode(_VECTOR["bytecode"], _VECTOR["references"], libraries)
    libraries["src/First.sol:First"] = "0x" + "0" * 40
    with pytest.raises(ValueError, match="missing or invalid library"):
        link_bytecode(_VECTOR["bytecode"], _VECTOR["references"], libraries)


def test_bad_locations_and_unresolved_bytecode_are_rejected():
    references = copy.deepcopy(_VECTOR["references"])
    references["src/Second.sol"]["Second"][0]["start"] = 10
    with pytest.raises(ValueError, match="overlapping"):
        link_bytecode(_VECTOR["bytecode"], references, _VECTOR["libraries"])
    references["src/Second.sol"]["Second"][0]["start"] = 1000
    with pytest.raises(ValueError, match="invalid library location"):
        link_bytecode(_VECTOR["bytecode"], references, _VECTOR["libraries"])
    with pytest.raises(ValueError, match="metadata unavailable"):
        link_bytecode(_VECTOR["bytecode"], {}, _VECTOR["libraries"])
    with pytest.raises(ValueError, match="unresolved"):
        link_bytecode(_VECTOR["bytecode"], {}, {})


def test_installed_bundle_uses_its_library_metadata(monkeypatch):
    """A packaged linked contract must work without a checkout/out directory."""
    artifact = {"abi": [], "bytecode": _VECTOR["bytecode"], "linkReferences": _VECTOR["references"]}
    bundle = SimpleNamespace(artifact=lambda name: (artifact["abi"], artifact["bytecode"]),
                             contracts=lambda: {"Example": artifact})
    monkeypatch.setitem(sys.modules, "buck_contracts", bundle)
    monkeypatch.setattr("buck_core.artifacts.repo_root", lambda: Path("/definitely-no-buck-checkout"))
    abi, code = load_artifact("Example", libraries=_VECTOR["libraries"])
    assert abi == []
    assert code == _VECTOR["linked"]
    artifact.pop("linkReferences")
    with pytest.raises(ValueError, match="metadata unavailable"):
        load_artifact("Example", libraries=_VECTOR["libraries"])


def test_distribution_check_rejects_missing_or_malformed_link_metadata():
    path = Path(__file__).resolve().parents[3] / "scripts/contracts-dist.py"
    spec = importlib.util.spec_from_file_location("contracts_dist", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    check = module.check_link_references
    assert check("Example", _VECTOR["bytecode"], _VECTOR["references"], "creation") == []
    assert "unresolved" in check("Example", _VECTOR["bytecode"], {}, "creation")[0]
    refs = copy.deepcopy(_VECTOR["references"])
    refs["src/Second.sol"]["Second"][0]["start"] = 10
    assert "overlapping" in check("Example", _VECTOR["bytecode"], refs, "creation")[0]
