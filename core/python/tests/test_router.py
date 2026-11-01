"""Compatibility and bounds for the shared Universal Router encoder."""

from eth_abi import decode
import pytest

from buck_core.router import encode_path, ur_exec_args
from alberta_buck.sim.router import encode_path as legacy_encode_path, ur_exec_args as legacy_ur_exec_args


def test_exact_input_preserves_legacy_prefunding_and_encodes_user_bounds():
    token_in = "0x" + "11" * 20
    token_out = "0x" + "22" * 20
    recipient = "0x" + "33" * 20
    path = encode_path([token_in, 500, token_out])
    assert path == bytes.fromhex("11" * 20 + "0001f4" + "22" * 20)
    assert legacy_encode_path([token_in, 500, token_out]) == path
    commands, old = legacy_ur_exec_args(recipient, 100, path)
    assert commands == b"\x00"
    types = ["address", "uint256", "uint256", "bytes", "bool", "uint256[]"]
    assert decode(types, old[0]) == (recipient, 100, 0, path, False, ())
    commands, bounded = ur_exec_args(recipient, 100, path, minimum_out=87, payer_is_user=True)
    assert commands == b"\x00"
    assert decode(types, bounded[0]) == (recipient, 100, 87, path, True, ())
    with pytest.raises(ValueError, match="swap arguments"):
        ur_exec_args(recipient, 100, path, minimum_out=-1)
