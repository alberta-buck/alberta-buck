"""Holder-produced attribute proofs.

Accumulator specification, sections 9 to 11.  Every predicate the system gates
on -- registered, live, not revoked, over eighteen, resident in a region,
licensed to underwrite an asset class -- is checked by a proof the holder
produces, never by a third-party lookup.  A gate is satisfied by the party
that must pass it, so producing no proof is failing the gate, and a revoked
holder's refusal is the answer rather than an obstacle.

Where a counterparty needs durable evidence without the holder's later
cooperation, the proof is captured in the receipt at the time of the payment,
which is where the receipt architecture already puts evidence.

What a proof establishes, against one posted aggregator root:

    leaf in the authority's subtree, and that subtree's root in the aggregator

for each subtree the verifier requires.  For a private subtree the leaf is the
hiding commitment and the salt is the holder's witness; for a public one it is
the unsalted leaf and anyone can check the path.

Scope of this module: it composes and checks proofs in the clear, which is the
witness the circuit consumes.  Hiding WHICH subtrees were walked is the
circuit's job (specification section 11.1), so a verifier running this code
learns the subtree identifiers.  That is correct for a public subtree and is
the reason the private path needs the SNARK.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from alberta_buck.registry.merkle_service import (
    CentralMerkleService, ComposedMembershipProof,
)
from alberta_buck.registry.tree import MembershipProof

__all__ = ["AttributeProof", "prove_attributes", "verify_attributes"]


@dataclass(frozen=True)
class AttributeProof:
    """One or more subtree memberships, against a single posted root.

    Attributes:
        composed: The subtree and aggregator paths, one pair per claim.
        tree_ids: The subtree identifiers claimed, in the same order.
        root: The aggregator root every path verifies against.
    """
    composed: ComposedMembershipProof
    tree_ids: Tuple[str, ...]
    root: int

    def verify_paths(self) -> bool:
        """Whether every path is internally valid and shares one root."""
        return self.composed.verify()


def prove_attributes(service: CentralMerkleService,
                     claims: Sequence[Tuple[str, MembershipProof]],
                     M_x: int = 0, M_y: int = 0) -> AttributeProof:
    """Compose a holder's subtree memberships into one attribute proof.

    Args:
        service: The aggregator, which supplies each subtree's path to the
            posted root.
        claims: (subtree identifier, the holder's path in that subtree).  The
            holder obtains each path from the published subtree; for a private
            subtree it computes its own leaf from its salt first.
        M_x, M_y: The identity point's coordinates, carried for the circuit's
            curve tie.

    Returns:
        An AttributeProof against the aggregator's current root.
    """
    if not claims:
        raise ValueError("an attribute proof needs at least one claim")
    composed = service.composed_proof(list(claims), M_x, M_y)
    return AttributeProof(composed=composed,
                          tree_ids=tuple(tid for tid, _ in claims),
                          root=composed.identity_root)


def verify_attributes(service: CentralMerkleService, proof: AttributeProof,
                      required: Sequence[str], max_age: float,
                      now: Optional[float] = None) -> bool:
    """Check an attribute proof the way a consumer must.

    A verifier MUST name the subtrees it requires and MUST declare its maximum
    root age (specification section 10): a proof is meaningless without the
    identifier it is against and the staleness it was accepted under.

    Returns True only if every required subtree is claimed, every path
    verifies, all paths share one root, and that root is one the aggregator
    posted within `max_age`.
    """
    if not required:
        raise ValueError("a verifier must name the subtrees it requires")
    claimed = set(proof.tree_ids)
    if not set(required).issubset(claimed):
        return False
    if not proof.verify_paths():
        return False
    if proof.composed.identity_root != proof.root:
        return False
    return service.accepts(proof.root, max_age=max_age, now=now)
