"""Safe, reproducible linking for Solidity creation artifacts."""

import copy
import json
from pathlib import Path

import pytest

from buck_core.artifacts import link_bytecode

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
