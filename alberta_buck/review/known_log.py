# SPDX-License-Identifier: GPL-3.0-or-later
"""What the protocol retired, kept for the evidence that demonstrates why.

Two things, neither of which any protocol path may import:

* ``H_KNOWN = keccak("AlbertaBuck:IssuerReenc:H") * G`` -- a second generator
  whose discrete log anyone can compute.  Finding 5's double opening of a
  commitment, and the A2 key split, both turn on that log.  The protocol now
  blinds only on :data:`alberta_buck.wallet.nums.H_PEDERSEN`.
* :func:`untagged_identity_leaf` -- ``Poseidon(M.x, M.y)``, the identity leaf
  before the v2 leaf tags.  The preserved g1tie circuit and its committed
  fixture still hash it.

The derivations are the originals, so the evidence reproduces byte for byte.
"""

from alberta_buck.wallet.bn254 import G1, ORDER, mul, point_to_words
from alberta_buck.wallet.poseidon import F_R, poseidon
from alberta_buck.wallet.transcript import keccak_raw

H_KNOWN_SCALAR = int.from_bytes(keccak_raw(b"AlbertaBuck:IssuerReenc:H"), "big") % ORDER
H_KNOWN = mul(G1, H_KNOWN_SCALAR)


def untagged_identity_leaf(M) -> int:
    """``Poseidon(M.x, M.y)``, as the g1tie circuit hashes it."""
    x, y = point_to_words(M)
    return poseidon([x % F_R, y % F_R])


__all__ = ["H_KNOWN_SCALAR", "H_KNOWN", "untagged_identity_leaf"]
