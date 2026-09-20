"""Holder-side salt derivation for private accumulator subtrees.

A private subtree's leaf is Poseidon(M.x, M.y, salt) rather than a hash of
the identity alone, so that no party holding identity scalars can decide
membership (accumulator specification, sections 3 and 4).  This module
derives those salts.

Two requirements shape the derivation, and both are load-bearing:

  1. The salt MUST NOT be derivable from m, M, or anything an authority
     holds.  An authority knows the identity it certified, so a salt derived
     from the identity would let it recompute every other salt the holder
     uses and scan every tree -- defeating the construction it is part of.
     The salt therefore derives from a HOLDER secret the authority never
     sees.

  2. The wallet MUST be able to recompute every salt it has used from its
     seed material.  Losing local state must not lose the ability to prove
     membership, and the holder cannot fall back on asking the authority,
     because the authority is exactly who must not be able to derive it.

  salt = Poseidon([holder_secret, H(tree_id), association_counter]) mod F_R

The counter increments on each re-association into the same subtree (an
address change, a renewal the registry makes interactive), so the old and
new leaves share no salt and are unlinkable to anyone without the
authority's own records.

Distinct subtrees yield distinct salts for one identity: otherwise two
authorities' leaves for the same person would be equal and link across
trees.
"""

from __future__ import annotations

from alberta_buck.wallet.poseidon import F_R, poseidon
from alberta_buck.wallet.transcript import keccak_raw

__all__ = ["tree_tag", "derive_salt", "SALT_DOMAIN"]


# Domain separator, so a salt can never collide with another Poseidon
# preimage the wallet computes (a leaf, a nullifier, a commitment).
SALT_DOMAIN = int.from_bytes(
    keccak_raw(b"AlbertaBuck/Accumulator/Salt/v1"), "big"
) % F_R


def tree_tag(tree_id: str) -> int:
    """Hash a subtree identifier to a field element.

    `tree_id` is the namespaced string of the accumulator specification's
    vocabulary, for example "kyc:ca-ab-2026" or "feature:age-over-18".
    """
    if not isinstance(tree_id, str) or not tree_id:
        raise ValueError("tree_id must be a non-empty string")
    return int.from_bytes(keccak_raw(tree_id.encode("utf-8")), "big") % F_R


def derive_salt(holder_secret: int, tree_id: str, association_counter: int = 0) -> int:
    """Derive this holder's salt for `tree_id` at `association_counter`.

    Args:
        holder_secret: A wallet secret recoverable from the wallet's seed
            material.  MUST NOT leave the wallet: it is what an authority
            must not hold.
        tree_id: The subtree identifier the salt is for.
        association_counter: 0 for a first association, incremented on each
            re-association into the same subtree.

    Returns:
        A salt in [1, F_R), suitable for identity_leaf_salted.
    """
    if not isinstance(holder_secret, int) or holder_secret <= 0:
        raise ValueError("holder_secret must be a positive int")
    if not isinstance(association_counter, int) or association_counter < 0:
        raise ValueError("association_counter must be a non-negative int")
    tag = tree_tag(tree_id)
    salt = poseidon([
        SALT_DOMAIN,
        holder_secret % F_R,
        tag,
        association_counter % F_R,
    ]) % F_R
    # Poseidon returning 0 is negligible, but a zero salt would make the leaf
    # deterministic, so step to the next counter rather than emit it.
    if salt == 0:
        return derive_salt(holder_secret, tree_id, association_counter + 1)
    return salt
