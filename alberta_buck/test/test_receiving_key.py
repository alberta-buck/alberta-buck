"""Conformance tests for the Notes receiving key and the folded deposit gate.

The architecture of record is doc/review/notes-receiving-key.org.  Three
properties carry the design, and each has a test here that fails if the
property is lost:

  1. The identity scalar is a read capability, so it MUST NOT decrypt notes.
     The collapsed design -- a point encrypted under itself -- is scannable
     with one scalar multiplication per candidate; the real one is not.
  2. The binding between an Identity and its receiving key MUST be committed,
     not published, and MUST be unscannable to a party holding every certified
     identity, the whole subtree, and the receiving key itself.
  3. The deposit gate MUST state the tie between the two secrets rather than
     infer it.  A split gate is complete, sound in each half, and lets a thief
     holding a stolen payload spend with its own Identity: the theft is a test
     here, not a warning.
"""

from __future__ import annotations

import pytest

from alberta_buck.registry.tree import (
    IdentityMerkleTree,
    identity_leaf_salted,
    receiving_leaf,
)
from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, eq, mul, neg, point_to_words,
)
from alberta_buck.wallet.deposit_fold import (
    DepositFoldRefused,
    deposit_fold_check,
    deposit_fold_witness,
)
from alberta_buck.wallet.elgamal import elgamal_decrypt, elgamal_encrypt
from alberta_buck.wallet.issuer_reenc import H_POINT
from alberta_buck.wallet.poseidon import F_R
from alberta_buck.wallet.recvkey import (
    RECV_DOMAIN,
    derive_receiving_secret,
    prove_receiving_binding,
    receiving_key,
    receiving_public,
    verify_receiving_binding,
)
from alberta_buck.wallet.salt import derive_salt

KYC = "kyc:ca-ab-2026"

# The harvester's candidate list: every identity a registry ever certified.
# It holds these scalars legitimately -- that is the whole difficulty.
SCALARS = [2000 + i for i in range(16)]
SEEDS = [0xBE11E_0000 + i for i in range(16)]


def _holder(i: int = 0):
    """One holder: Identity, receiving key, salt, all from its own secrets."""
    m = SCALARS[i]
    seed = SEEDS[i]
    k, pk_recv = receiving_key(seed)
    return dict(m=m, M=mul(G1, m), k=k, pk_recv=pk_recv,
                salt=derive_salt(seed, KYC), seed=seed)


# ---------------------------------------------------------------- derivation

def test_receiving_secret_is_recoverable_from_the_seed():
    """Losing local state must not lose the ability to read one's own mail."""
    k, pk = receiving_key(SEEDS[0])
    assert derive_receiving_secret(SEEDS[0]) == k
    assert eq(receiving_public(k), pk)


def test_receiving_secret_is_not_derivable_from_the_identity():
    """The requirement that makes the whole design work: a counterparty holding
    m -- which the receipt hands it by design -- cannot reach k.

    Two halves.  Structurally, the derivation does not take the identity as an
    input at all: one seed yields one key, whatever Identity it is paired
    with.  Numerically, k is none of the identity-derived values a counterparty
    holds."""
    h = _holder()
    k = h["k"]

    # Structural: the identity contributes nothing, so knowing it contributes
    # nothing.  The same seed under a different Identity gives the same key.
    assert derive_receiving_secret(h["seed"]) == k
    # And a different seed under the SAME Identity gives a different key, so
    # the key follows the wallet rather than the person named in the receipt.
    assert derive_receiving_secret(SEEDS[1]) != k

    # Numerical: none of what a counterparty legitimately holds IS the key.
    Mx, My = point_to_words(h["M"])
    for held in (h["m"], Mx, My, Mx % ORDER, My % ORDER):
        assert k != held


def test_rotation_yields_an_independent_key():
    k0, _ = receiving_key(SEEDS[0], 0)
    k1, _ = receiving_key(SEEDS[0], 1)
    assert k0 != k1
    assert receiving_key(SEEDS[0], 1)[0] == k1          # and is recoverable


@pytest.mark.parametrize("bad", [0, -1, "seed"])
def test_bad_seeds_are_refused(bad):
    with pytest.raises(ValueError):
        derive_receiving_secret(bad)


@pytest.mark.parametrize("bad", [0, ORDER, -5])
def test_a_receiving_secret_out_of_range_is_refused(bad):
    """k = 0 would make pk_recv the point at infinity and every ciphertext
    addressed to it trivially readable."""
    with pytest.raises(ValueError):
        receiving_public(bad)


def test_domain_separation_is_stable():
    """Pinned: changing it silently invalidates every wallet's derived key."""
    assert RECV_DOMAIN == b"AlbertaBuck/Notes/ReceivingKey/v1"


# -------------------------------------------------------- the scan, and its absence

def test_the_collapsed_design_is_scannable_and_the_real_one_is_not():
    """Property 1.  Encrypting an Identity under ITSELF leaves C = m(G+R),
    where message and key share a secret and the Diffie-Hellman hardness
    protecting every other ciphertext is absent.  One scalar multiplication
    per candidate identifies the recipient.  Keying to pk_recv instead makes
    the same test a DDH decision, and it finds nothing."""
    h = _holder(3)
    candidates = [mul(G1, m) for m in SCALARS]
    assert any(eq(c, h["M"]) for c in candidates)        # the target IS listed

    collapsed = elgamal_encrypt(h["M"], h["M"], 0xC0FFEE)
    hits = [m for m in SCALARS
            if eq(collapsed.C, mul(add(G1, collapsed.R), m))]
    assert hits == [h["m"]], "the collapsed design must be scannable"

    keyed = elgamal_encrypt(h["M"], h["pk_recv"], 0xC0FFEE)
    hits = [m for m in SCALARS if eq(keyed.C, mul(add(G1, keyed.R), m))]
    assert hits == [], "keying to pk_recv must defeat the scan"

    # And the recipient still reads its mail -- with k, not with m.
    assert eq(elgamal_decrypt(keyed, h["k"]), h["M"])
    assert not eq(elgamal_decrypt(keyed, h["m"]), h["M"])


def test_the_leaf_is_unscannable_given_every_identity_the_tree_and_the_key():
    """Property 2.  The adversary is handed more than it could ever have: all
    16 certified scalars, the whole published subtree, and pk_recv itself.  It
    still decides no membership, because it lacks the holder's salt."""
    tree = IdentityMerkleTree(depth=10, private=True)
    holders = [_holder(i) for i in range(16)]
    for h in holders:
        tree.insert_receiving(h["M"], h["pk_recv"], h["salt"])
    published = set(tree.leaves)

    target = holders[5]
    guesses = set()
    for h in holders:                      # every identity it certified
        for other in holders:              # paired with every key it has seen
            guesses.add(receiving_leaf(h["M"], other["pk_recv"], 1))
            guesses.add(identity_leaf_salted(h["M"], 1))
    assert not (guesses & published), "the salt is what makes the leaf hiding"

    # With the holder's own salt -- which only the holder derives -- it is a member.
    assert receiving_leaf(target["M"], target["pk_recv"], target["salt"]) in published


def test_rotation_yields_an_unlinkable_leaf():
    """Rotation is the accumulator's re-association, not a second mechanism."""
    h = _holder()
    k2, pk2 = receiving_key(h["seed"], 1)
    salt2 = derive_salt(h["seed"], KYC, 1)
    assert receiving_leaf(h["M"], pk2, salt2) != receiving_leaf(
        h["M"], h["pk_recv"], h["salt"])


@pytest.mark.parametrize("bad", [0, F_R, F_R + 1, -1])
def test_a_receiving_leaf_refuses_a_bad_salt(bad):
    h = _holder()
    with pytest.raises(ValueError):
        receiving_leaf(h["M"], h["pk_recv"], bad)


def test_the_receiving_leaf_is_distinct_from_the_plain_salted_leaf():
    """A third leaf function, not a changed one: the two-input and three-input
    leaves stay valid for the trees that use them, so committed vectors hold."""
    h = _holder()
    assert receiving_leaf(h["M"], h["pk_recv"], h["salt"]) != identity_leaf_salted(
        h["M"], h["salt"])


# ------------------------------------------------------------- the binding

def test_the_binding_is_provable_and_pinned_to_the_pair():
    h = _holder()
    tree = IdentityMerkleTree(depth=10, private=True)
    tree.insert_receiving(h["M"], h["pk_recv"], h["salt"])

    binding = prove_receiving_binding(h["M"], h["pk_recv"], h["salt"], tree)
    assert verify_receiving_binding(h["M"], binding, tree.root())
    # A different Identity does not get to claim this key's binding.
    assert not verify_receiving_binding(mul(G1, SCALARS[7]), binding, tree.root())
    # Nor does a stale root.
    assert not verify_receiving_binding(h["M"], binding, tree.root() ^ 1)


def test_an_unregistered_receiving_key_has_no_binding():
    """A holder claiming a key it never registered gets the honest answer."""
    h = _holder()
    tree = IdentityMerkleTree(depth=10, private=True)
    tree.insert_receiving(h["M"], h["pk_recv"], h["salt"])
    _, pk2 = receiving_key(h["seed"], 1)
    with pytest.raises(ValueError, match="no registered leaf"):
        prove_receiving_binding(h["M"], pk2, h["salt"], tree)


# ------------------------------------------------- the folded deposit gate

def _world():
    """A recipient with a registered account, a note addressed to its key, and
    a thief who has stolen the whole payload."""
    rec = _holder(0)
    sk_dep = 0xDEF0_1234
    pk_dep = mul(G1, sk_dep)
    E_dep = elgamal_encrypt(rec["M"], pk_dep, 0xAAA1)

    M_iss = mul(G1, SCALARS[9])
    note_ct = elgamal_encrypt(M_iss, rec["pk_recv"], 0xBBB2)

    tree = IdentityMerkleTree(depth=10, private=True)
    tree.insert_receiving(rec["M"], rec["pk_recv"], rec["salt"])

    thief = _holder(1)
    sk_thief = 0xBAD0_4321
    E_thief = elgamal_encrypt(thief["M"], mul(G1, sk_thief), 0xCCC3)
    tree.insert_receiving(thief["M"], thief["pk_recv"], thief["salt"])

    return dict(rec=rec, sk_dep=sk_dep, pk_dep=pk_dep, E_dep=E_dep,
                M_iss=M_iss, note_ct=note_ct, tree=tree,
                thief=thief, sk_thief=sk_thief, E_thief=E_thief)


def test_the_honest_spender_has_a_witness_and_P_hides_the_issuer():
    w_ = _world()
    rec = w_["rec"]
    w = deposit_fold_witness(
        m_rec=rec["m"], k=rec["k"], sk_dep=w_["sk_dep"], salt=rec["salt"],
        E_dep=w_["E_dep"], note_ct=w_["note_ct"], tree=w_["tree"], b=0x51A1,
    )
    assert eq(w.M, w_["M_iss"]), "(1) k must decrypt the note"
    assert eq(add(w.P, neg(mul(H_POINT, w.b))), w_["M_iss"]), \
        "P must commit the decrypted point"
    assert deposit_fold_check(w, pk_dep=w_["pk_dep"], E_dep=w_["E_dep"],
                              note_ct=w_["note_ct"], root=w_["tree"].root())


def test_the_payload_thief_is_refused_on_relation_three():
    """Property 3, the theft.  The thief holds the stolen note payload -- and
    so the receiving secret k inside it -- and its OWN registered Identity and
    account.  Both halves of a SPLIT gate would be true.  The folded gate
    refuses it, and refuses it specifically at the tie: no registered leaf
    pairs the thief's Identity with the key it stole."""
    w_ = _world()
    with pytest.raises(DepositFoldRefused) as exc:
        deposit_fold_witness(
            m_rec=w_["thief"]["m"],          # its own Identity: genuinely its own
            k=w_["rec"]["k"],                # the stolen reading key
            sk_dep=w_["sk_thief"],           # its own account: genuinely its own
            salt=w_["thief"]["salt"],
            E_dep=w_["E_thief"], note_ct=w_["note_ct"], tree=w_["tree"],
        )
    assert exc.value.relation == 3

    # The halves the thief WOULD have passed, shown to be individually true --
    # which is exactly why the tie cannot be left to inference.
    thief, rec = w_["thief"], w_["rec"]
    assert eq(w_["E_thief"].C, add(thief["M"], mul(
        w_["E_thief"].R, w_["sk_thief"]))), "its account really holds its Identity"
    assert eq(add(w_["note_ct"].C, neg(mul(w_["note_ct"].R, rec["k"]))),
              w_["M_iss"]), "the stolen k really decrypts the note"


def test_a_wrong_account_key_is_refused_on_relation_two():
    w_ = _world()
    rec = w_["rec"]
    with pytest.raises(DepositFoldRefused) as exc:
        deposit_fold_witness(
            m_rec=rec["m"], k=rec["k"], sk_dep=w_["sk_dep"] + 1,
            salt=rec["salt"], E_dep=w_["E_dep"], note_ct=w_["note_ct"],
            tree=w_["tree"],
        )
    assert exc.value.relation == 2


def test_an_unregistered_account_is_refused_by_the_check():
    """Relation (2) alone would hold for an account nobody registered; pinning
    sk_dep to the REGISTERED pk_dep is what rules that out."""
    w_ = _world()
    rec = w_["rec"]
    w = deposit_fold_witness(
        m_rec=rec["m"], k=rec["k"], sk_dep=w_["sk_dep"], salt=rec["salt"],
        E_dep=w_["E_dep"], note_ct=w_["note_ct"], tree=w_["tree"],
    )
    assert not deposit_fold_check(w, pk_dep=mul(G1, w_["sk_dep"] + 1),
                                  E_dep=w_["E_dep"], note_ct=w_["note_ct"],
                                  root=w_["tree"].root())


def test_a_stale_root_is_refused_by_the_check():
    w_ = _world()
    rec = w_["rec"]
    w = deposit_fold_witness(
        m_rec=rec["m"], k=rec["k"], sk_dep=w_["sk_dep"], salt=rec["salt"],
        E_dep=w_["E_dep"], note_ct=w_["note_ct"], tree=w_["tree"],
    )
    assert not deposit_fold_check(w, pk_dep=w_["pk_dep"], E_dep=w_["E_dep"],
                                  note_ct=w_["note_ct"], root=w_["tree"].root() ^ 1)


def test_a_rotated_receiving_key_still_spends():
    """Rotation must not strand a note addressed to the new key."""
    w_ = _world()
    rec = w_["rec"]
    k2, pk2 = receiving_key(rec["seed"], 1)
    salt2 = derive_salt(rec["seed"], KYC, 1)
    w_["tree"].insert_receiving(rec["M"], pk2, salt2)
    note2 = elgamal_encrypt(w_["M_iss"], pk2, 0xDDD4)

    w = deposit_fold_witness(
        m_rec=rec["m"], k=k2, sk_dep=w_["sk_dep"], salt=salt2,
        E_dep=w_["E_dep"], note_ct=note2, tree=w_["tree"],
    )
    assert deposit_fold_check(w, pk_dep=w_["pk_dep"], E_dep=w_["E_dep"],
                              note_ct=note2, root=w_["tree"].root())
    assert w.leaf != receiving_leaf(rec["M"], rec["pk_recv"], rec["salt"])
