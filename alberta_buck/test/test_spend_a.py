"""Phase 8 A-spend witness emitter + constraint oracle tests.

Validates the planned ``spend_a.circom`` semantics in pure Python: each
constraint group's rejection path, plus the headline Phase 8 stop condition
-- a recipient who lost ``sk_rec`` and re-registered cannot spend their old
A-flavor notes (the *single*-``sk_dep`` witness binds
``pk_rec_mint == pk_dep_current``).

These tests run today (no circom required); they become the rejection-suite
template for ``test/SpendAVerifier.t.sol`` when the circuit ships.
"""

from __future__ import annotations

import pytest

from alberta_buck.wallet import (
    G1, ORDER, mul,
    FLAVOR_A2, FLAVOR_B1, NoteOpening,
    elgamal_encrypt, identity_keygen,
    note_commitment, nullifier_a, NULLIFIER_TAG_B,
    poseidon, F_R,
    MerkleTree, merkle_walk, EMPTY_ROOT, TREE_DEPTH,
    SpendAWitness, make_spend_a_witness, spend_a_satisfied,
)


# --- Merkle tree primitives -----------------------------------------------

def test_empty_tree_root_matches_pinned_constant():
    t = MerkleTree()
    assert t.root() == EMPTY_ROOT


def test_single_leaf_proof_round_trips():
    t = MerkleTree()
    t.append(7)
    sibs, bits = t.proof(0)
    assert len(sibs) == TREE_DEPTH and len(bits) == TREE_DEPTH
    assert merkle_walk(7, sibs, bits) == t.root()


def test_many_leaves_each_proves_back_to_root():
    t = MerkleTree()
    for v in [11, 22, 33, 44, 55, 66, 77, 88]:
        t.append(v)
    root = t.root()
    for i, v in enumerate([11, 22, 33, 44, 55, 66, 77, 88]):
        sibs, bits = t.proof(i)
        assert merkle_walk(v, sibs, bits) == root, f"leaf {i} failed"


def test_appending_changes_root():
    t = MerkleTree()
    r0 = t.root()
    t.append(123)
    r1 = t.root()
    t.append(456)
    r2 = t.root()
    assert len({r0, r1, r2}) == 3


def test_proof_of_oob_index_raises():
    t = MerkleTree()
    t.append(1)
    with pytest.raises(ValueError):
        t.proof(2)
    with pytest.raises(ValueError):
        t.proof(-1)


# --- A-spend happy path ---------------------------------------------------

def _setup_aspend(recipient_int=0xa11ce):
    """Build a tree with one A2 note minted to a recipient with sk_rec.

    Returns (witness, sk_rec, kp) so tests can mutate then re-check.
    """
    kp = identity_keygen()           # sk_rec, pk_rec
    m  = 0x4242                      # issuer's identity scalar (synthetic)
    M  = mul(G1, m)
    e_note = elgamal_encrypt(M, kp.pk, 11)   # ElGamal(M, pk_rec, r=11)
    e_reg  = elgamal_encrypt(M, kp.pk, 22)   # registry-side encryption of same M

    id_h = poseidon([1, 2, 3, 4])    # synthetic id_hash for the test
    opening = NoteOpening(
        flavor=FLAVOR_A2, v=1_000, rho=42, id_hash=id_h, predicate=0,
    )
    cm = note_commitment(opening)

    tree = MerkleTree()
    tree.append(cm)

    w = make_spend_a_witness(
        opening=opening,
        tree=tree,
        leaf_index=0,
        sk_dep=kp.sk,
        e_note=e_note,
        e_reg=e_reg,
        recipient=recipient_int,
        chain_id=1,
    )
    return w, kp.sk, kp


def test_spend_a_happy_path_accepts():
    w, _sk, _kp = _setup_aspend()
    assert spend_a_satisfied(w) is True


def test_make_witness_rejects_b_flavor_opening():
    kp = identity_keygen()
    bad = NoteOpening(flavor=FLAVOR_B1, v=1, rho=1, id_hash=2)
    tree = MerkleTree(); tree.append(note_commitment(bad))
    e1 = elgamal_encrypt(G1, kp.pk, 11)
    with pytest.raises(ValueError, match="A-flavor"):
        make_spend_a_witness(bad, tree, 0, kp.sk, e1, e1, 0, 1)


def test_make_witness_rejects_leaf_mismatch():
    kp = identity_keygen()
    opening = NoteOpening(flavor=FLAVOR_A2, v=1, rho=1, id_hash=2)
    tree = MerkleTree(); tree.append(0xC0FFEE)   # wrong leaf
    e1 = elgamal_encrypt(G1, kp.pk, 11)
    with pytest.raises(ValueError, match="does not match"):
        make_spend_a_witness(opening, tree, 0, kp.sk, e1, e1, 0, 1)


# --- (R) range / face binding ---------------------------------------------

def test_face_v_mismatch_rejected():
    w, _, _ = _setup_aspend()
    bad = SpendAWitness(**{**w.__dict__, "face": w.v + 1})
    assert spend_a_satisfied(bad) is False


def test_v_above_2pow128_rejected():
    w, _, _ = _setup_aspend()
    bad = SpendAWitness(**{**w.__dict__, "v": 1 << 128, "face": 1 << 128})
    assert spend_a_satisfied(bad) is False


# --- (C) commitment tamper ------------------------------------------------

def test_tampered_predicate_breaks_merkle_walk():
    w, _, _ = _setup_aspend()
    bad = SpendAWitness(**{**w.__dict__, "predicate": w.predicate ^ 1})
    # Flipping predicate changes cm, so the walk yields a different root.
    assert spend_a_satisfied(bad) is False


def test_tampered_id_hash_breaks_both_commitment_and_nullifier():
    w, _, _ = _setup_aspend()
    bad = SpendAWitness(**{**w.__dict__, "id_hash": w.id_hash ^ 1})
    assert spend_a_satisfied(bad) is False


# --- (M) Merkle path tamper -----------------------------------------------

def test_swapped_path_bit_breaks_root():
    w, _, _ = _setup_aspend()
    flipped = list(w.bits)
    flipped[0] ^= 1
    bad = SpendAWitness(**{**w.__dict__, "bits": tuple(flipped)})
    assert spend_a_satisfied(bad) is False


def test_swapped_sibling_breaks_root():
    w, _, _ = _setup_aspend()
    perturbed = list(w.sibs)
    perturbed[3] = (perturbed[3] + 1) % F_R
    bad = SpendAWitness(**{**w.__dict__, "sibs": tuple(perturbed)})
    assert spend_a_satisfied(bad) is False


# --- (N) nullifier domain separation --------------------------------------

def test_b_tag_nullifier_rejected_in_a_spend():
    """A spender who tries to publish a B-tag nullifier is rejected."""
    w, _, _ = _setup_aspend()
    nf_b = poseidon([w.rho, w.id_hash, NULLIFIER_TAG_B])
    bad  = SpendAWitness(**{**w.__dict__, "nullifier": nf_b})
    assert spend_a_satisfied(bad) is False


def test_unrelated_nullifier_rejected():
    w, _, _ = _setup_aspend()
    bad = SpendAWitness(**{**w.__dict__, "nullifier": 0xDEADBEEF})
    assert spend_a_satisfied(bad) is False


# --- (CP) Phase 8 invariant: single-sk_dep ElGamal equality ---------------

def test_wrong_sk_dep_rejected():
    """An attacker without the recipient's sk_rec cannot satisfy CP equality."""
    w, _, _ = _setup_aspend()
    bad_sk = (w.sk_dep + 1) % ORDER
    bad = SpendAWitness(**{**w.__dict__, "sk_dep": bad_sk})
    assert spend_a_satisfied(bad) is False


def test_recipient_who_lost_sk_rec_and_re_registered_cannot_spend():
    """The Phase 8 stop condition (alberta-buck-ethereum.org L1543-1544).

    Sequence:
      1. Issue an A-note bound to Alice's pk_rec_old = sk_rec_old * G.
      2. Alice loses sk_rec_old.
      3. Alice re-registers with pk_dep_new = sk_rec_new * G; the on-chain
         registry now has E_reg under pk_dep_new (not pk_rec_old).
      4. Alice tries to spend her old A-note using sk_rec_new.

    With the corrected single-sk_dep CP-equality binding, no single sk_dep
    can simultaneously decrypt E_note (under pk_rec_old) and E_reg (under
    pk_dep_new) -- so the spend MUST be rejected.  This test asserts the
    rejection in the Python oracle, predating spend_a.circom.
    """
    # Setup: original recipient identity.
    kp_old = identity_keygen()       # sk_rec_old
    m  = 0xBEEF
    M  = mul(G1, m)
    e_note = elgamal_encrypt(M, kp_old.pk, 11)   # bound to pk_rec_old

    # The note opening + a Merkle tree containing its commitment.
    id_h = poseidon([5, 6, 7, 8])
    opening = NoteOpening(
        flavor=FLAVOR_A2, v=2_500, rho=99, id_hash=id_h, predicate=0,
    )
    cm = note_commitment(opening)
    tree = MerkleTree(); tree.append(cm)

    # Alice loses sk_rec_old, generates sk_rec_new, re-registers.
    kp_new = identity_keygen()       # fresh ElGamal keypair
    e_reg_new = elgamal_encrypt(M, kp_new.pk, 22)   # bound to pk_dep_new

    # Spender's only available secret is sk_rec_new.
    w = SpendAWitness(
        note_root=tree.root(),
        nullifier=nullifier_a(opening.rho, opening.id_hash),
        face=opening.v,
        recipient=0xa11ce,
        chain_id=1,
        flavor=opening.flavor,
        v=opening.v,
        rho=opening.rho,
        id_hash=opening.id_hash,
        predicate=opening.predicate,
        sibs=tuple(tree.proof(0)[0]),
        bits=tuple(tree.proof(0)[1]),
        sk_dep=kp_new.sk,           # only secret she has
        e_note=e_note,              # bound to pk_rec_old
        e_reg=e_reg_new,            # bound to pk_dep_new
    )
    assert spend_a_satisfied(w) is False, \
        "STOP CONDITION BREACH: A-note spendable after sk_rec loss + re-issuance"

    # Sanity: the SAME note IS spendable when the spender still has sk_rec_old.
    e_reg_old = elgamal_encrypt(M, kp_old.pk, 33)
    w_ok = SpendAWitness(**{**w.__dict__, "sk_dep": kp_old.sk, "e_reg": e_reg_old})
    assert spend_a_satisfied(w_ok) is True


def test_wrong_recipient_in_e_reg_rejected_under_unique_sk_dep():
    """Two distinct recipients cannot spend each other's notes.

    Concretely: a note bound to Alice (E_note under pk_alice) plus a
    registered ciphertext for Bob (E_reg under pk_bob) cannot both be opened
    by any single sk_dep, so the CP equality fails for any choice of sk_dep
    that the spender supplies.
    """
    alice = identity_keygen()
    bob   = identity_keygen()
    m  = 0xCAFE
    M  = mul(G1, m)
    e_note_alice = elgamal_encrypt(M, alice.pk, 11)
    e_reg_bob    = elgamal_encrypt(M, bob.pk,   22)

    opening = NoteOpening(flavor=FLAVOR_A2, v=1, rho=7, id_hash=poseidon([0]), predicate=0)
    tree = MerkleTree(); tree.append(note_commitment(opening))

    for sk_try in (alice.sk, bob.sk, (alice.sk + bob.sk) % ORDER, 1, 12345):
        sibs, bits = tree.proof(0)
        w = SpendAWitness(
            note_root=tree.root(),
            nullifier=nullifier_a(opening.rho, opening.id_hash),
            face=opening.v, recipient=0, chain_id=1,
            flavor=opening.flavor, v=opening.v, rho=opening.rho,
            id_hash=opening.id_hash, predicate=opening.predicate,
            sibs=tuple(sibs), bits=tuple(bits),
            sk_dep=sk_try, e_note=e_note_alice, e_reg=e_reg_bob,
        )
        assert spend_a_satisfied(w) is False, \
            f"sk_try={sk_try} unexpectedly satisfied a cross-recipient witness"


# --- Replay protection (oracle-level) -------------------------------------

def test_same_opening_yields_same_nullifier():
    """Double-spend at the contract level: nullifier is deterministic in
    (rho, id_hash), so two spends of the same opening collide -- exactly
    what the on-chain ``nullifiers`` mapping rejects.
    """
    w, _, _ = _setup_aspend()
    assert nullifier_a(w.rho, w.id_hash) == w.nullifier
