"""Insurance regulator: a periodic attestor whose subtrees gate credit issuance.

Accumulator specification, section 12.  Insurer oversight is already a
periodic review of underwriting quality, portfolio risk, reserves and
reinsurance, concluding in an estimate of the new contracts an insurer may
write.  What this module adds is making that conclusion available to
contracts: an insurer in current good standing may issue credits consistent
with the envelope its regulator attested, and BuckCredit checks it.

Two properties distinguish this from the person-facing authorities:

  * The subtrees are PUBLIC (specification section 4).  An insurer's standing
    is a claim it wants counterparties to check, so its leaf is the unsalted
    identity_leaf and membership is provable by a plain Merkle path, with no
    circuit and no salt.
  * There is no asset taxonomy (specification section 12.2).  Underwriting
    produces a risk rating per unit time, a scale in BUCK, and a depreciation
    model chosen for the specific asset, and the credit stores all three.  The
    gate therefore checks the credit's own parameters, plus a scope the
    insurer declares, and never a classification.

What is out of scope, by decision: the assumption of pools, contracts and
company assets on an insurer's failure by its upstream reinsurers.  The
design assumes an insurer's soundness includes pools that pay the expected
rate of underwritten payouts with a buffer, and that reinsurance triggered by
failure -- together with clearance from the regulator's subtree -- tops those
pools up where needed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple

from alberta_buck.registry.tree import (
    FEATURE_SUBTREE_DEPTH, IdentityMerkleTree, MembershipProof, identity_leaf,
)
from alberta_buck.wallet.transcript import keccak_raw

__all__ = [
    "DepreciationType", "FACE_BAND_MAX", "BUCK_DECIMALS",
    "band_ceiling", "band_for_face", "scope_id", "subtree_key", "GENERAL_SCOPE",
    "InsurerEnvelope", "IssuanceRefused", "check_issuance",
    "InsuranceRegulator",
]


class DepreciationType:
    """Mirrors BuckCredit.DepreciationType."""
    NONE = 0
    LINEAR = 1
    DECLINING_BALANCE = 2


#: BuckCredit stores monetary quantities with six decimals.
BUCK_DECIMALS = 6

#: Bands are powers of ten of face, from ten BUCK to a hundred million.  The
#: ladder starts low deliberately: an individual underwriting a neighbour's
#: bicycle is the same shape of actor as an insurer underwriting a building,
#: and the gate distinguishes them only by the envelope their regulator
#: attested.
FACE_BAND_MAX = 8


def band_ceiling(band: int) -> int:
    """The exclusive face ceiling of `band`, in BuckCredit units.

    Band n admits any face strictly below 10**n BUCK.
    """
    if not isinstance(band, int) or not (1 <= band <= FACE_BAND_MAX):
        raise ValueError(f"band must be in [1, {FACE_BAND_MAX}], got {band}")
    return 10 ** (band + BUCK_DECIMALS)


def band_for_face(face_units: int) -> int:
    """The smallest band admitting `face_units`, or FACE_BAND_MAX + 1 if none.

    Returning an out-of-range band rather than raising lets the gate refuse a
    face above the whole ladder the same way it refuses one above an insurer's
    band, with no special case.
    """
    if face_units < 0:
        raise ValueError("face must be non-negative")
    for band in range(1, FACE_BAND_MAX + 1):
        if face_units < band_ceiling(band):
            return band
    return FACE_BAND_MAX + 1


def scope_id(name: str) -> int:
    """The identifier of a scope, which is the hash of its namespaced name.

    Scopes are names, not codes (specification section 12.2), so refinement is
    free: "regulator:ca-ab:scope:asset:vehicle" today and
    "regulator:ca-ab:scope:asset:vehicle:car:ford:pre-1995" tomorrow, with no
    existing name changed and no version bumped.  Two regulators' scopes of
    the same shape are different identifiers, which is correct, because they
    are different judgements.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("scope name must be a non-empty string")
    return int.from_bytes(keccak_raw(name.encode("utf-8")), "big")


#: The scope an unscoped, general insurer declares.  A regulator grants it to
#: an insurer it does not wish to restrict and withholds it from one it does,
#: by membership in its "insurer:general" subtree.
GENERAL_SCOPE = 0


def subtree_key(name: str) -> int:
    """The on-chain key of a subtree: keccak of its namespaced name, as
    ``IdentityRegistry.enrollSubtree`` and the insurer gate compute it."""
    return scope_id(name)


@dataclass(frozen=True)
class InsurerEnvelope:
    """What a regulator attested, and what BuckCredit caches and checks.

    The underwriter risk estimate is deliberately absent: the gate asks whether
    this insurer may write this asset class at this face, and premium adequacy
    is the insurer's own business.  Putting it on chain would disclose the most
    commercially sensitive field of the review for no gate that reads it.
    """
    standing: bool
    face_band: int
    dep_types: FrozenSet[int]
    max_dep_rate: int          # basis points per year
    max_premium_rate: int      # basis points per year
    expires_at: float
    scopes: FrozenSet[int] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not (1 <= self.face_band <= FACE_BAND_MAX):
            raise ValueError(f"face_band must be in [1, {FACE_BAND_MAX}]")
        for d in self.dep_types:
            if d not in (DepreciationType.NONE, DepreciationType.LINEAR,
                         DepreciationType.DECLINING_BALANCE):
                raise ValueError(f"unknown depreciation type {d}")


class IssuanceRefused(Exception):
    """A credit the envelope does not admit.  The message is the reason, and
    mirrors the revert string BuckCredit will use."""


def check_issuance(env: InsurerEnvelope, *, scope: int, face_units: int,
                   dep_type: int, dep_rate: int, premium_rate: int,
                   now: Optional[float] = None) -> None:
    """The gate, as BuckCredit.createCredit will perform it.

    This is the executable specification the Solidity port is checked against.
    Raises IssuanceRefused with the reason; returns None if the credit is
    admitted.
    """
    ts = time.time() if now is None else now
    if not env.standing:
        raise IssuanceRefused("insurer not in good standing")
    if ts > env.expires_at:
        raise IssuanceRefused("attestation expired")
    if scope not in env.scopes:
        raise IssuanceRefused("scope not attested")
    band = band_for_face(face_units)
    if band > env.face_band:
        raise IssuanceRefused("face above attested band")
    if dep_type not in env.dep_types:
        raise IssuanceRefused("depreciation model not attested")
    if dep_rate > env.max_dep_rate:
        raise IssuanceRefused("depreciation rate above attested maximum")
    if premium_rate > env.max_premium_rate:
        raise IssuanceRefused("premium rate above attested maximum")


class InsuranceRegulator:
    """A jurisdiction's insurance regulator, as a public attribute authority.

    It owns one public subtree per predicate it attests: standing, each asset
    scope, each face band, each depreciation model, and -- where it delegates
    -- its delegates and their granted scope.  An insurer's envelope is its
    membership in those subtrees; the on-chain attestation entry point verifies
    the paths once per review period and caches the result, so issuance costs
    a storage read rather than a proof.
    """

    def __init__(self, jurisdiction: str,
                 tree_depth: int = FEATURE_SUBTREE_DEPTH) -> None:
        if not jurisdiction:
            raise ValueError("jurisdiction must be a non-empty string")
        self.jurisdiction = jurisdiction
        self.tree_depth = tree_depth
        self._trees: Dict[str, IdentityMerkleTree] = {}
        self._envelopes: Dict[Tuple[int, int], InsurerEnvelope] = {}

    # -- naming --------------------------------------------------------------

    def subtree_id(self, suffix: str) -> str:
        """The namespaced identifier of one of this regulator's subtrees."""
        return f"regulator:{self.jurisdiction}:{suffix}"

    def scope_name(self, asset_path: str) -> str:
        """The namespaced name of an asset scope, e.g. "asset:vehicle:car"."""
        return self.subtree_id(f"scope:{asset_path}")

    # -- attestation ---------------------------------------------------------

    def _tree(self, suffix: str) -> IdentityMerkleTree:
        t = self._trees.get(suffix)
        if t is None:
            # Public: membership here is a fact the insurer advertises.
            t = IdentityMerkleTree(depth=self.tree_depth, private=False)
            self._trees[suffix] = t
        return t

    def attest(self, M, env: InsurerEnvelope,
               scope_names: Optional[List[str]] = None,
               general: bool = False) -> InsurerEnvelope:
        """Attest an insurer's envelope for this review period.

        Inserts the insurer into the subtree for each predicate the envelope
        asserts.  Membership in a fine scope does not imply the coarse one, so
        a regulator intending an insurer to write both attests both.
        ``general`` grants the general scope, which the eight-argument
        ``createCredit`` declares; a scoped insurer is attested without it.
        """
        names = list(scope_names or [])
        scopes = frozenset(scope_id(self.scope_name(n)) for n in names)
        if general:
            scopes = scopes | {GENERAL_SCOPE}
        if env.scopes and env.scopes != scopes:
            raise ValueError("env.scopes must match scope_names and general, or be empty")
        env = InsurerEnvelope(
            standing=env.standing, face_band=env.face_band,
            dep_types=frozenset(env.dep_types), max_dep_rate=env.max_dep_rate,
            max_premium_rate=env.max_premium_rate, expires_at=env.expires_at,
            scopes=scopes,
        )
        leaf = identity_leaf(M)
        for suffix in self._suffixes(env, names):
            t = self._tree(suffix)
            if not t.contains(leaf):
                t.insert_leaf(leaf)
        self._envelopes[self._key(M)] = env
        return env

    def revoke(self, M) -> int:
        """Clear an insurer from every subtree it is in.

        Returns the number of subtrees cleared.  Its existing credits are
        untouched: clearance stops new issuance, and the fate of written
        contracts is the reinsurance question this design leaves out.
        """
        leaf = identity_leaf(M)
        cleared = 0
        for t in self._trees.values():
            if t.contains(leaf):
                t.clear_leaf(t.index_of_leaf(leaf))
                cleared += 1
        self._envelopes.pop(self._key(M), None)
        return cleared

    def envelope_of(self, M) -> Optional[InsurerEnvelope]:
        """The envelope this regulator last attested for M, if any."""
        return self._envelopes.get(self._key(M))

    # -- proofs and roots ----------------------------------------------------

    def membership_proof(self, M, suffix: str) -> Optional[MembershipProof]:
        """A plain Merkle path proving M is in this regulator's `suffix` subtree.

        Public, so no circuit is involved and anyone may check it.
        """
        t = self._trees.get(suffix)
        if t is None:
            return None
        leaf = identity_leaf(M)
        if not t.contains(leaf):
            return None
        return t.path(t.index_of_leaf(leaf))

    def sub_roots(self) -> Dict[str, int]:
        """Every subtree's identifier and current root, for the aggregator."""
        return {self.subtree_id(sfx): t.root() for sfx, t in self._trees.items()}

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _key(M) -> Tuple[int, int]:
        from alberta_buck.wallet.bn254 import point_to_words
        x, y = point_to_words(M)
        return (x, y)

    def predicate_names(self, env: InsurerEnvelope, scope_names: List[str]) -> List[str]:
        """The namespaced subtree names an attestation proves membership in, in
        the order ``BuckCredit.attestInsurer`` takes their paths.  On chain a
        subtree is keyed by ``subtree_key`` of its name."""
        return [self.subtree_id(sfx) for sfx in self._suffixes(env, scope_names)]

    @staticmethod
    def _suffixes(env: InsurerEnvelope, scope_names: List[str]) -> List[str]:
        """Each predicate the envelope asserts, as a subtree suffix, in the
        gate's claim order: standing, face band, each depreciation type
        (ascending), the maximum depreciation and premium rates, the general
        scope if granted, then each named scope.  The rates are subtrees like
        the band -- a value the chain can check only if the regulator attested
        it by membership."""
        out: List[str] = []
        if env.standing:
            out.append("insurer")
        out.append(f"insurer:face:{env.face_band}")
        for d in sorted(env.dep_types):
            out.append(f"insurer:dep:{d}")
        out.append(f"insurer:depRate:{env.max_dep_rate}")
        out.append(f"insurer:premium:{env.max_premium_rate}")
        if GENERAL_SCOPE in env.scopes:
            out.append("insurer:general")
        for n in scope_names:
            out.append(f"scope:{n}")
        return out
