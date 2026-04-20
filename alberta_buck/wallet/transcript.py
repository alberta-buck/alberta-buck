"""Fiat-Shamir transcript hashing.

Two flavors, serving different call sites:

* ``keccak_scalar(*uints)`` -- packs every argument as a 32-byte big-endian word,
  then hashes.  Solidity equivalent:
  ``uint256(keccak256(abi.encodePacked(uint256, uint256, ...))) % ORDER``.
  Used for every Fiat-Shamir challenge in the identity protocols, where all
  inputs (point coordinates, scalars, addresses) are uint256-sized.

* ``keccak_raw(data: bytes)`` -- a plain keccak256 over arbitrary-length bytes,
  no packing.  Used by ``identity_scalar`` to hash the canonical identity_data
  JSON string (arbitrary length).
"""

from __future__ import annotations

from eth_utils import keccak

from alberta_buck.wallet.bn254 import ORDER


def _word(v) -> bytes:
    """Encode an int as a 32-byte big-endian word.  Only ints allowed."""
    if not isinstance(v, int):
        raise TypeError(f"transcript: expected int, got {type(v).__name__}")
    if v < 0 or v.bit_length() > 256:
        raise ValueError(f"transcript word out of range: {v}")
    return v.to_bytes(32, "big")


def keccak_bytes(*uints: int) -> bytes:
    """keccak256(concat(32-byte-be(uints...))) -> 32-byte digest."""
    return keccak(b"".join(_word(v) for v in uints))


def keccak_scalar(*uints: int) -> int:
    """keccak_bytes(*uints) reduced modulo ORDER, suitable as a Fiat-Shamir challenge."""
    return int.from_bytes(keccak_bytes(*uints), "big") % ORDER


def keccak_raw(data: bytes) -> bytes:
    """keccak256(data) with no padding -- arbitrary-length input.

    Used for hashing identity_data (which is a UTF-8 JSON string of unbounded
    length) into an identity scalar.  Solidity equivalent: keccak256(bytes).
    """
    return keccak(data)
