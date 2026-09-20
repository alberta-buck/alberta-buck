"""Accumulator conformance tests: the salted leaf and its salt.

Executable form of the tests named in the accumulator specification's
conformance section (doc/review/accumulator-spec.org, section 15), for the
parts phase 2 implements: the scan test, leaf parity, the salt rules, and
re-association unlinkability.
"""

from __future__ import annotations

import pytest

from alberta_buck.registry.tree import (
    EMPTY_LEAF,
    IdentityMerkleTree,
    identity_leaf,
    identity_leaf_salted,
)
from alberta_buck.wallet.bn254 import G1, mul
from alberta_buck.wallet.poseidon import F_R
from alberta_buck.wallet.salt import derive_salt, tree_tag

KYC = "kyc:ca-ab-2026"
AGE = "feature:age-over-18"
REGION_A = "feature:region:ca-ab-redwater"
REGION_B = "feature:region:ca-ab-calgary"

# A registry's certified population: the adversary of the scan test holds
# every one of these scalars, and every identity point derived from them.
SCALARS = [1000 + i for i in range(24)]
SECRETS = [0xA11CE_0000 + i for i in range(24)]


def _members():
    return [(m, mul(G1, m), s) for m, s in zip(SCALARS, SECRETS)]


def test_scan_finds_nothing_in_a_published_private_subtree():
    """Conformance 1.  A party holding every certified scalar, and the whole
    published subtree, decides no membership."""
    tree = IdentityMerkleTree(depth=10, private=True)
    for _m, M, secret in _members():
        tree.insert_identity_salted(M, derive_salt(secret, KYC))

    published = set(tree.leaves)            # publication leaks nothing
    assert len(published) == len(SCALARS)

    # The adversary's only handle without a salt is the unsalted leaf.
    for m in SCALARS:
        assert identity_leaf(mul(G1, m)) not in published

    # Non-members are equally invisible, so the adversary cannot even
    # distinguish a member from a stranger.
    for m in range(9000, 9024):
        assert identity_leaf(mul(G1, m)) not in published

    # Yet every member is present under its own salted leaf.
    for _m, M, secret in _members():
        assert identity_leaf_salted(M, derive_salt(secret, KYC)) in published


def test_public_subtree_is_deliberately_scannable():
    """Section 4: membership a member advertises stays testable, which is the
    point of a public subtree and what makes the regulator path cheap."""
    tree = IdentityMerkleTree(depth=10)                 # public by default
    for _m, M, _s in _members()[:4]:
        tree.insert_identity(M)
    for _m, M, _s in _members()[:4]:
        assert identity_leaf(M) in set(tree.leaves)


def test_private_tree_refuses_an_unsalted_leaf():
    tree = IdentityMerkleTree(depth=10, private=True)
    with pytest.raises(ValueError, match="private subtree"):
        tree.insert_identity(mul(G1, SCALARS[0]))


def test_leaf_parity_unsalted_is_unchanged():
    """Conformance 2: the unsalted leaf keeps its definition, so every
    committed vector that records it stays valid."""
    M = mul(G1, SCALARS[0])
    from alberta_buck.wallet.poseidon import poseidon
    from alberta_buck.wallet.bn254 import point_to_words
    x, y = point_to_words(M)
    assert identity_leaf(M) == poseidon([x % F_R, y % F_R])


@pytest.mark.parametrize("bad", [0, -1, F_R, F_R + 1])
def test_zero_and_out_of_range_salts_are_refused(bad):
    """Conformance 3: a zero salt would make the leaf deterministic."""
    with pytest.raises(ValueError, match="salt must be"):
        identity_leaf_salted(mul(G1, SCALARS[0]), bad)


def test_salt_is_distinct_per_tree_and_recomputable():
    """Conformance 3: one identity in two subtrees yields different leaves,
    and a wallet restored from its secret recomputes every salt."""
    secret, M = SECRETS[0], mul(G1, SCALARS[0])
    s_kyc, s_age = derive_salt(secret, KYC), derive_salt(secret, AGE)
    assert s_kyc != s_age
    assert identity_leaf_salted(M, s_kyc) != identity_leaf_salted(M, s_age)
    assert derive_salt(secret, KYC) == s_kyc          # recovery from seed
    assert 1 <= s_kyc < F_R and 1 <= s_age < F_R


def test_salt_is_not_derivable_from_the_identity():
    """Section 7: two holders of the SAME identity scalar but different wallet
    secrets get different salts, which is what stops an authority that knows
    the identity from recomputing the holder's other leaves."""
    M = mul(G1, SCALARS[0])
    a = derive_salt(SECRETS[0], KYC)
    b = derive_salt(SECRETS[1], KYC)
    assert a != b
    assert identity_leaf_salted(M, a) != identity_leaf_salted(M, b)


def test_re_association_leaves_are_unlinkable():
    """Conformance 5: an address change yields two leaves that no party
    without the authority's records can link."""
    secret, M = SECRETS[0], mul(G1, SCALARS[0])
    old = IdentityMerkleTree(depth=10, private=True)
    new = IdentityMerkleTree(depth=10, private=True)
    old_leaf = identity_leaf_salted(M, derive_salt(secret, REGION_A, 0))
    new_leaf = identity_leaf_salted(M, derive_salt(secret, REGION_B, 1))
    old.insert_leaf(old_leaf)
    new.insert_leaf(new_leaf)
    assert old_leaf != new_leaf
    # The cleared slot shows a removal happened, not who was removed.
    old.clear_leaf(0)
    assert old.leaves[0] == EMPTY_LEAF
    assert new.path(0).verify()


def test_membership_path_verifies_under_the_salted_leaf():
    tree = IdentityMerkleTree(depth=10, private=True)
    idx = []
    for _m, M, secret in _members()[:5]:
        idx.append(tree.insert_identity_salted(M, derive_salt(secret, KYC)))
    for i, (_m, M, secret) in zip(idx, _members()[:5]):
        proof = tree.path(i)
        assert proof.verify()
        assert proof.root == tree.root()
        assert proof.leaf == identity_leaf_salted(M, derive_salt(secret, KYC))


def test_tree_tag_is_stable_and_distinct():
    assert tree_tag(KYC) == tree_tag(KYC)
    assert tree_tag(KYC) != tree_tag(AGE)
    with pytest.raises(ValueError):
        tree_tag("")


# --- the authority side -----------------------------------------------------

def test_private_authority_attests_revokes_and_keeps_no_history():
    """Sections 6 and 8: the authority inserts the hiding leaf from the salt
    the holder sends, clears it on revocation, and retains no superseded
    association."""
    from alberta_buck.registry.feature_authority import FeatureAuthority

    auth = FeatureAuthority(AGE, tree_depth=10, private=True)
    secret, M = SECRETS[0], mul(G1, SCALARS[0])
    salt = derive_salt(secret, AGE)

    rec = auth.attest(M, salt=salt)
    assert rec.leaf == identity_leaf_salted(M, salt)
    assert rec.salt == salt
    assert auth.has_identity(M)
    root_attested = auth.sub_root

    # A party holding the identity but not the salt sees nothing.
    assert identity_leaf(M) not in set(auth._tree.leaves)

    proof = auth.membership_proof_for_identity(M)
    assert proof is not None and proof.verify()
    assert proof.leaf == rec.leaf

    idx = auth.revoke(M)
    assert idx == rec.leaf_index
    assert not auth.has_identity(M)
    assert auth.sub_root != root_attested          # a fresh sub-root to push
    assert auth._tree.leaves[idx] == EMPTY_LEAF


def test_private_authority_requires_a_salt_and_public_refuses_one():
    from alberta_buck.registry.feature_authority import FeatureAuthority

    M = mul(G1, SCALARS[0])
    priv = FeatureAuthority(AGE, tree_depth=10, private=True)
    with pytest.raises(ValueError, match="private subtree"):
        priv.attest(M)

    pub = FeatureAuthority(AGE, tree_depth=10)
    with pytest.raises(ValueError, match="public subtree"):
        pub.attest(M, salt=derive_salt(SECRETS[0], AGE))
    assert pub.attest(M).leaf == identity_leaf(M)
