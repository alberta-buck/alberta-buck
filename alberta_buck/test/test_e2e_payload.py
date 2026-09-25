"""The delivered payload is sufficient to spend.

A Note travels off chain.  Whatever the recipient needs in order to build the
folded gate's witness has to be IN that delivery, and an earlier shape shipped
only the ciphertexts while the generator kept the rest of the mint's randomness
in memory -- so the fixtures proved a spend that no real recipient could have
performed.  These tests read the fixture's ``notePayload`` and nothing else
except the recipient's own three secrets (the Identity scalar, the mailbox key
and its salt, all of which a wallet derives from its seed), and check that the
shipped values open the shipped ciphertexts and locate the leaves.

What is NOT asserted here is the Merkle path, because a path is rebuilt from
the published subtree at spend time rather than shipped (see
doc/review/notes-receiving-key.org section 4.4: salts travel, paths go stale).
"""

import json
import os

import pytest

from alberta_buck.wallet.bn254 import G1, eq, mul, words_to_point
from alberta_buck.wallet.elgamal import ElGamalCiphertext, elgamal_decrypt
from alberta_buck.registry.tree import (
    identity_leaf_salted, mailbox_leaf, receiving_leaf,
)
from alberta_buck.wallet.recvkey import (
    mailbox_shared_recipient, unwrap_scalar,
)
from alberta_buck.wallet.delivery import (
    open_a1, open_a2,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "vectors", "e2e")


def _world(flavor):
    path = os.path.join(FIXTURES, f"{flavor}.json")
    if not os.path.exists(path):
        pytest.skip(f"e2e fixture {flavor} not generated")
    return json.load(open(path))


def _pt(d):
    return words_to_point(int(d["x"]), int(d["y"]))


def _ct(d):
    return ElGamalCiphertext(_pt(d["R"]), _pt(d["C"]))


def _recipient(w):
    d = w["parties"]["depositor"]
    return int(d["m"]), int(d["kRecv"]), int(d["salt"]), _pt(d["M"])


# --------------------------------------------------------------------------- #
# A2: the ciphertext decrypts to the ISSUER, so the payload must carry the
# issuer's naming salt as well as the mint randomness.
# --------------------------------------------------------------------------- #

def test_a2_payload_opens_the_issuer_ciphertext():
    w = _world("a2")
    np_ = w["notePayload"]
    assert set(np_) == {"flavor", "predicate", "eNote", "eIss", "T", "rhoWrapped", "vWrapped",
                        "rPrimeWrapped", "saltIssWrapped", "gammaWrapped"}, \
        "an A2 delivery is the two ciphertexts, the binding's T, and every secret scalar WRAPPED"

    m_rec, k, salt, M_rec = _recipient(w)
    eIss = _ct(np_["eIss"])

    # The recipient opens the delivery with k, and it recomputes to the very
    # commitment the minter folded into the tree.
    opened = open_a2(np_, k)
    assert opened.cm == int(w["opening"]["cm"])
    r_prime, salt_iss = opened.r_prime, opened.salt_iss

    # The recovered r' is the randomness of the shipped ciphertext: the fold's
    # note tie is eIss.R == r'*G, and a wrong r' fails it rather than proving
    # something else.
    assert eq(eIss.R, mul(G1, r_prime))

    # The mailbox key -- not the Identity scalar -- opens it, to the issuer.
    M_iss = _pt(w["parties"]["issuer"]["M"])
    assert eq(elgamal_decrypt(eIss, k), M_iss)
    assert not eq(elgamal_decrypt(eIss, m_rec), M_iss), \
        "the identity scalar must not be a decryption key"

    # And the recovered salt locates the issuer's NAMING leaf -- relation (5).
    # That it is a second association, not the issuer's own receiving leaf, is
    # what keeps the issuer's mailbox private (section 4.4).
    leaf = identity_leaf_salted(M_iss, salt_iss)
    assert leaf != receiving_leaf(m_rec, k, salt)
    assert isinstance(leaf, int) and leaf > 0


def test_the_channel_cannot_read_the_payload_it_carries():
    """The delivery channel holds everything the recipient does except k.

    That has to be enough to stop it, and the wrap is what makes it enough.  In
    the clear, r' turns eIss into plaintext (M = C - r'*pk_recv), salt_iss turns
    the issuer's naming leaf into a scannable one, and rho with idHash IS the
    nullifier -- so the channel would learn the issuer, and when the note is
    spent.
    """
    from alberta_buck.wallet.bn254 import add, neg
    from alberta_buck.wallet.notes import nullifier
    w = _world("a2")
    np_ = w["notePayload"]
    eIss = _ct(np_["eIss"])
    pk_recv = _pt(w["parties"]["depositor"]["pkRecv"])
    M_iss = _pt(w["parties"]["issuer"]["M"])
    nf = int(w["opening"]["nullifier"])
    idh = int(w["opening"]["idHash"])

    # What the channel has: the delivery, both public points, no k.
    assert not eq(add(eIss.C, neg(mul(pk_recv, int(np_["rPrimeWrapped"])))), M_iss), \
        "the wrapped r' must not work as the randomness"
    assert nullifier(int(np_["rhoWrapped"]), idh) != nf, \
        "the wrapped rho must not yield the nullifier"
    # And with k it does, which is the other half of the statement.
    _, k, _, _ = _recipient(w)
    opened = open_a2(np_, k)
    assert eq(add(eIss.C, neg(mul(pk_recv, opened.r_prime))), M_iss)
    assert nullifier(opened.opening.rho, idh) == nf


def test_the_mailbox_binding_needs_no_secret():
    """The payer's view of the association: a hash and a path over the two
    POINTS it already holds.  The gate's leaf commits the same fact over the
    two scalars, which no payer can open -- that is why there are two."""
    for flavor in ("a1", "a2"):
        w = _world(flavor)
        b = w["mailboxBinding"]
        M_rec = _pt(w["parties"]["depositor"]["M"])
        pk_recv = _pt(w["parties"]["depositor"]["pkRecv"])
        leaf = mailbox_leaf(M_rec, pk_recv, int(b["salt"]))
        assert leaf == int(b["leaf"])
        # And the path folds, with no witness of any kind.
        from alberta_buck.wallet.poseidon import poseidon
        cur = leaf
        for sib, bit in zip(b["siblings"], b["indexBits"]):
            sib = int(sib)
            cur = poseidon([cur, sib]) if int(bit) == 0 else poseidon([sib, cur])
        assert cur == int(b["root"]) == int(w["identityRoot"])


def test_a2_payload_value_ciphertext_is_the_committed_one():
    """eNote and the binding's T are hashed into idHash, so a substituted one
    breaks the nullifier."""
    w = _world("a2")
    from alberta_buck.wallet.notes import id_hash_a2
    np_ = w["notePayload"]
    assert id_hash_a2(_ct(np_["eNote"]), _ct(np_["eIss"]), _pt(np_["T"])) == \
        int(w["opening"]["idHash"])


# --------------------------------------------------------------------------- #
# A1: the ciphertext decrypts to the recipient's OWN Identity, so no third
# party's witness material travels -- but eNote's randomness must, because the
# fold pins eNote against the public face.
# --------------------------------------------------------------------------- #

def test_a1_payload_carries_its_own_randomness_and_no_issuer_salt():
    w = _world("a1")
    np_ = w["notePayload"]
    assert set(np_) == {"flavor", "predicate", "eNote", "eRec",
                        "rhoWrapped", "vWrapped", "rNoteWrapped"}
    assert not any("salt" in k for k in np_), \
        "A1 asserts nothing about a third party, so nothing about one travels"

    m_rec, k, salt, M_rec = _recipient(w)
    eNote = _ct(np_["eNote"])
    opened = open_a1(np_, k, int(w["parties"]["issuer"]["m"]))
    assert opened.cm == int(w["opening"]["cm"])
    r_note = opened.r_note
    face = int(w["face"])
    assert opened.opening.v == face

    # eNote = (rn*G, v*G + rn*pk_recv): the fold checks it as (v + rn*k)*G with
    # v public, which is what makes the addressed Identity unique.
    assert eq(eNote.R, mul(G1, r_note))
    assert eq(elgamal_decrypt(eNote, k), mul(G1, face))

    # eRec names the recipient to itself; k opens it, the Identity scalar does not.
    eRec = _ct(np_["eRec"])
    assert eq(elgamal_decrypt(eRec, k), M_rec)
    assert not eq(elgamal_decrypt(eRec, m_rec), M_rec)


def test_a1_and_a2_recipient_leaf_commits_the_pair():
    """The gate's relation (3): one leaf commits the Identity AND the mailbox
    key, under the holder's own salt.  This is the relation a payload thief
    cannot satisfy, and the reason the two facts cannot be proven side by side."""
    for flavor in ("a1", "a2"):
        w = _world(flavor)
        m_rec, k, salt, _ = _recipient(w)
        leaf = receiving_leaf(m_rec, k, salt)
        # A leaf over the SCALARS: ORDER == F_R on BN254, so a scalar is a field
        # element, and the circuit hashes the very signals its other relations
        # consume.
        assert leaf != receiving_leaf(m_rec, k + 1, salt)
        assert leaf != receiving_leaf(m_rec + 1, k, salt)
        assert leaf != receiving_leaf(m_rec, k, salt + 1)


def test_b1_ships_no_mailbox_material():
    """A bearer note is addressed to nobody, so nothing about a mailbox travels
    with it: it carries its opening, and nothing else."""
    np_ = _world("b1")["notePayload"]
    assert np_ == {}
