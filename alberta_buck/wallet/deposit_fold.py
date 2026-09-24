"""The folded deposit gate: one witness, one circuit, four relations.

Spending an addressed Note requires two facts about two DIFFERENT secrets:
"I can read this note" (the receiving secret ``k``) and "I am this registered
Identity" (the identity scalar ``m_rec``, held by every account the recipient
registers).  Since the receiving key is deliberately not the Identity
(:mod:`alberta_buck.wallet.recvkey`), those two facts no longer share a value,
and a gate that proved them SIDE BY SIDE would say nothing about their owner.

That is not a hypothetical.  A thief holding a stolen note payload -- and so
the ``k`` inside it -- supplies the reading half with the stolen key and the
Identity half with its OWN registered Identity.  Both halves are true.
Neither says they belong together, and the note becomes spendable by the wrong
person.  ``scripts/review/deposit_gate_split.py`` demonstrates it as a passing
test rather than a warning.

This is review finding 5 in a second place: an equality inferred from two
proofs that merely share a public point.  The remedy is the same one -- state
the tie instead of assuming it -- and it is why the receiving-key change and
the finding-5 fold are ONE piece of circuit work rather than two adjacent
ones.  Both replace a public-point equality with a shared private witness, and
both need the accumulator leaf.

The four relations over one witness:

    (1) k decrypts the note ciphertext to a point M
    (2) the account credential decrypts under sk_dep to the Identity M_rec
    (3) a registered leaf commits the pair (M_rec, k*G) under the holder's salt
    (4) that leaf's path folds to a posted identity root

Relation (3) is the step that used to be free, because the two scalars were
the same value.  It is now the load-bearing one: the thief fails there, since
no registered leaf pairs ITS Identity with the key it stole.

Scope of this module.  It builds the witness and evaluates the relations IN
THE CLEAR.  That is the reference the folded circuit is checked against --
the same role :mod:`alberta_buck.registry.regulator` plays for its Solidity
port -- and it is what a prover feeds the circuit.  It is NOT the on-chain
verifier: hiding the witness is the SNARK's job, and the gate must ship as
one circuit carrying all four relations, never as a sigma plus a separate
membership proof.  What ``M`` must be is the flavour's business: A1's is the
holder's own Identity, and A2's is the issuer's, which the A2 circuit proves
registered (relation 5) and tied to the mint binding's ``T``.

Reference: doc/review/notes-receiving-key.org section 3.3a (architecture of
record), scripts/review/deposit_gate_folded.py (the prototype this promotes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, eq, mul, neg,
)
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.nums import H_PEDERSEN
from alberta_buck.wallet.poseidon import poseidon
from alberta_buck.registry.tree import receiving_leaf

__all__ = [
    "DepositFoldRefused",
    "DepositFoldWitness",
    "deposit_fold_witness",
    "deposit_fold_check",
    "deposit_fold_a1_witness",
    "deposit_fold_a2_witness",
]


class DepositFoldRefused(ValueError):
    """A relation of the folded gate does not hold for this witness.

    Carries which one, because the interesting failures differ in kind: a
    wrong account key fails (2) and is a mistake, while a stolen receiving
    key fails (3) and is a theft.

    Attributes:
        relation: 1, 2, 3 or 4 -- the relation that refused.
    """

    def __init__(self, relation: int, message: str) -> None:
        super().__init__(f"relation ({relation}): {message}")
        self.relation = relation


@dataclass(frozen=True)
class DepositFoldWitness:
    """One witness for the whole deposit gate.

    The private fields are the circuit's private inputs; the public ones are
    what the chain sees.  Every relation is a constraint the folded circuit
    MUST carry -- nothing here may be left to an inference across proofs.

    Private:
        m_rec: The spender's identity scalar.
        k: The receiving secret that opens the note.
        sk_dep: The deposit account's key.
        salt: The holder's salt for the leaf binding ``(M_rec, k*G)``.
        siblings, index_bits: The membership path of that leaf.
        M: The point ``k`` decrypts the note to.  Never public: the chain
            learns nothing that names it.  There is no hidden copy of it
            either -- a split gate needed one, ``M + b*H``, so that a sigma
            and a separate membership SNARK could share a value, and its blind
            was witnessed rather than proven (finding 5's first defect).
            Folding removes the second proof, and with it the shared point,
            the blind, and the defect.

    Public:
        root: The posted identity root the path folds to.
    """
    m_rec: int
    k: int
    sk_dep: int
    salt: int
    siblings: List[int] = field(repr=False)
    index_bits: List[int] = field(repr=False)
    root: int = 0
    M: Tuple = field(default=(), repr=False)
    leaf: int = 0


def deposit_fold_witness(
    *,
    m_rec:   int,
    k:       int,
    sk_dep:  int,
    salt:    int,
    E_dep:   ElGamalCiphertext,   # the deposit account's registered credential
    note_ct: ElGamalCiphertext,   # the note's addressed ciphertext (eIss or eRec)
    tree,                         # the private subtree holding the holder's leaf
) -> DepositFoldWitness:
    """Build the folded witness, refusing loudly on the relation that fails.

    Args:
        m_rec: The spender's identity scalar.
        k: The receiving secret for the note.
        sk_dep: The deposit account's key.
        salt: The holder's salt for its registered leaf.
        E_dep: The deposit account's registered credential ``(R_d, C_d)``.
        note_ct: The note's ciphertext, keyed to ``k*G``.
        tree: The subtree the holder's leaf was admitted to.

    Returns:
        The witness, with ``M``, ``root`` and the path filled in.

    Raises:
        DepositFoldRefused: naming the relation that does not hold.
    """
    m_rec %= ORDER
    k %= ORDER
    sk_dep %= ORDER
    if not (m_rec and k and sk_dep):
        raise DepositFoldRefused(1, "witness scalars must be nonzero")

    M_rec = mul(G1, m_rec)

    # (1) k decrypts the note ciphertext to M.
    M = add(note_ct.C, neg(mul(note_ct.R, k)))

    # (2) The account credential decrypts, under its own key, to the Identity.
    if not eq(E_dep.C, add(M_rec, mul(E_dep.R, sk_dep))):
        raise DepositFoldRefused(
            2, "the account credential does not decrypt to m_rec*G under sk_dep")

    # (3) A registered leaf commits the pair, under the holder's own salt.
    #     This is the relation a split gate leaves out, and the one the thief
    #     of a stolen payload cannot satisfy.
    leaf = receiving_leaf(m_rec, k, salt)
    if leaf not in tree.leaves:
        raise DepositFoldRefused(
            3, "no registered leaf commits this (Identity, receiving key) pair")

    # (4) That leaf's path folds to a posted root.
    proof = tree.path(tree.leaves.index(leaf))
    if not proof.verify():
        raise DepositFoldRefused(4, "the membership path does not fold to the root")

    return DepositFoldWitness(
        m_rec=m_rec, k=k, sk_dep=sk_dep, salt=salt,
        siblings=list(proof.siblings), index_bits=list(proof.index_bits),
        root=proof.root, M=M, leaf=leaf,
    )


def deposit_fold_check(
    witness: DepositFoldWitness,
    *,
    pk_dep,                       # the deposit account's registered key
    E_dep:   ElGamalCiphertext,
    note_ct: ElGamalCiphertext,
    root:    int,
) -> bool:
    """Evaluate all four relations against the public inputs, in the clear.

    This is what the folded circuit must decide, and the reference its
    constraint system is checked against.  A verifier calling it learns the
    witness, which is why the shipped gate is the SNARK and not this.

    Returns True only if every relation holds against the given public inputs.
    """
    M_rec = mul(G1, witness.m_rec)

    # (2a) the account key is the registered one -- this is what pins sk_dep,
    #      and without it relation (2) would hold for an unregistered account.
    if not eq(pk_dep, mul(G1, witness.sk_dep)):
        return False
    # (2b) the credential decrypts to the Identity under that key.
    if not eq(E_dep.C, add(M_rec, mul(E_dep.R, witness.sk_dep))):
        return False
    # (1) k decrypts the note to the witness's M.
    M = add(note_ct.C, neg(mul(note_ct.R, witness.k)))
    if not eq(witness.M, M):
        return False
    # (3) the leaf commits the pair under the holder's salt.
    try:
        leaf = receiving_leaf(witness.m_rec, witness.k, witness.salt)
    except ValueError:
        return False
    if leaf != witness.leaf:
        return False
    # (4) the path folds to the posted root.
    cur = leaf
    for sib, bit in zip(witness.siblings, witness.index_bits):
        cur = poseidon([cur, sib]) if bit == 0 else poseidon([sib, cur])
    return cur == root


# ===================== The circuit witness ==================================
#
# `deposit_fold_witness` above is the clear-text reference: it decides the four
# relations and refuses on the one that fails.  What follows turns an accepted
# witness into the JSON `circuits/deposit_fold_a1.circom` consumes, which is a
# different shape for two reasons.
#
# First, the circuit works in 4x64-bit limbs, because BN254's base field does
# not fit the native field the constraints live in.
#
# Second, and more interesting, the circuit folds sums of POINTS into sums of
# SCALARS before multiplying: it checks `eEnc.C = (m_rec + t*k)*G` rather than
# `m_rec*G + (t*k)*G`.  That is not an optimisation.  Elliptic-curve addition in
# circom is incomplete -- it misbehaves on doubling and identity cases, which is
# one of the three finding-5 defects -- and a circuit that performs no point
# addition at all cannot be driven into those cases.  The combined scalars are
# the witnessed values `u`, `w` and `cd` below.


def _limbs(val: int, n: int = 4, bits: int = 64):
    """Little-endian 64-bit limbs, the encoding every circuit input uses."""
    mask = (1 << bits) - 1
    return [(val >> (i * bits)) & mask for i in range(n)]


def deposit_fold_a1_witness(
    *,
    witness:  DepositFoldWitness,   # from deposit_fold_witness, already checked
    rho:      int,
    id_hash:  int,
    e_note:   ElGamalCiphertext,    # the note's value ciphertext
    v:        int,                  # the note face (public at spend)
    m_issuer: int,
    r_note:   int,                  # eNote's randomness (travels in the payload)
    t:        int,                  # eEnc's total randomness (r' + s)
    r_E:      int,                  # the account's registration randomness
    e_dep:    ElGamalCiphertext,    # the account's registered credential
    pk_dep,                         # the account's registered key
    e_enc:    ElGamalCiphertext,    # the spend's re-randomized ciphertext
    identity_root: int,
) -> dict:
    """Build the JSON witness for `circuits/deposit_fold_a1.circom`.

    Every relation the circuit constrains is asserted here first, so a witness
    that would fail inside the prover fails in Python with a message instead.

    Raises:
        AssertionError: naming the relation whose arithmetic does not close.
    """
    from alberta_buck.wallet.bn254 import point_to_words
    from alberta_buck.wallet.notes import id_hash_a1, nullifier
    from alberta_buck.wallet.poseidon import F_R

    m_rec, k, sk_dep = witness.m_rec, witness.k, witness.sk_dep
    t %= ORDER
    r_note %= ORDER
    r_E %= ORDER

    # The combined scalars the circuit multiplies by, in place of adding points.
    u_val = (v + r_note * k) % ORDER              # eNote.C = u*G
    w_val = (m_rec + t * k) % ORDER               # eEnc.C  = w*G
    cd_val = (m_rec + sk_dep * r_E) % ORDER       # E_dep.C = cd*G

    # -- the note tie -------------------------------------------------------
    assert id_hash == id_hash_a1(e_note, m_issuer), "idHash != id_hash_a1(eNote, m_issuer)"
    assert eq(e_note.R, mul(G1, r_note)), "eNote.R != rn*G"
    assert eq(e_note.C, mul(G1, u_val)), "eNote.C != u*G (u = v + rn*k)"
    # -- (1) k decrypts the spend's ciphertext to M_rec ---------------------
    assert eq(e_enc.R, mul(G1, t)), "eEnc.R != t*G"
    assert eq(e_enc.C, mul(G1, w_val)), "eEnc.C != w*G (w = m_rec + t*k)"
    # -- (2) the account credential decrypts to M_rec -----------------------
    assert eq(pk_dep, mul(G1, sk_dep)), "pk_dep != sk_dep*G"
    assert eq(e_dep.R, mul(G1, r_E)), "E_dep.R != r_E*G"
    assert eq(e_dep.C, mul(G1, cd_val)), "E_dep.C != cd*G (cd = m_rec + sk*r_E)"
    # -- (3) the leaf commits the pair --------------------------------------
    assert witness.leaf == receiving_leaf(m_rec, k, witness.salt), \
        "the witness leaf does not commit (m_rec, k, salt)"
    # -- (4) the path folds to the posted root ------------------------------
    assert witness.root == identity_root, \
        "the witness root is not the posted identity root"

    nf = nullifier(rho, id_hash)

    def _w(P):
        x, y = point_to_words(P)
        return _limbs(x), _limbs(y)

    eEncRx, eEncRy = _w(e_enc.R)
    eEncCx, eEncCy = _w(e_enc.C)
    pkDepX, pkDepY = _w(pk_dep)
    eDepRx, eDepRy = _w(e_dep.R)
    eDepCx, eDepCy = _w(e_dep.C)

    nRx, nRy = point_to_words(e_note.R)
    nCx, nCy = point_to_words(e_note.C)

    return {
        "nullifier": str(nf),
        "v": str(v),
        "identityRoot": str(identity_root),
        "eEncRx": [str(x) for x in eEncRx], "eEncRy": [str(x) for x in eEncRy],
        "eEncCx": [str(x) for x in eEncCx], "eEncCy": [str(x) for x in eEncCy],
        "pkDepX": [str(x) for x in pkDepX], "pkDepY": [str(x) for x in pkDepY],
        "eDepRx": [str(x) for x in eDepRx], "eDepRy": [str(x) for x in eDepRy],
        "eDepCx": [str(x) for x in eDepCx], "eDepCy": [str(x) for x in eDepCy],
        "rho": str(rho % F_R),
        "idHash": str(id_hash % F_R),
        "eNote": [str(nRx % F_R), str(nRy % F_R), str(nCx % F_R), str(nCy % F_R)],
        "mIss": str(m_issuer % F_R),
        "rn": [str(x) for x in _limbs(r_note)],
        "m_rec": [str(x) for x in _limbs(m_rec)],
        "k_recv": [str(x) for x in _limbs(k)],
        "u": [str(x) for x in _limbs(u_val)],
        "t": [str(x) for x in _limbs(t)],
        "w": [str(x) for x in _limbs(w_val)],
        "sk_dep": [str(x) for x in _limbs(sk_dep)],
        "r_E": [str(x) for x in _limbs(r_E)],
        "cd": [str(x) for x in _limbs(cd_val)],
        "salt": str(witness.salt),
        "pathElements": [str(x) for x in witness.siblings],
        "pathIndices": [str(x) for x in witness.index_bits],
    }


def deposit_fold_a2_witness(
    *,
    witness:  DepositFoldWitness,   # from deposit_fold_witness, already checked
    rho:      int,
    id_hash:  int,
    e_note:   ElGamalCiphertext,    # the note's value ciphertext (hashed only)
    e_iss:    ElGamalCiphertext,    # the note's committed issuer ciphertext
    r_prime:  int,                  # the issuer's mint randomness
    t:        int,                  # eEnc's total randomness (r' + s)
    r_E:      int,
    e_dep:    ElGamalCiphertext,
    pk_dep,
    e_enc:    ElGamalCiphertext,    # the spend's re-randomized ciphertext
    salt_iss: int,                  # the issuer's salt, shipped in the payload
    iss_path,                       # the issuer's MembershipProof
    T,                              # the mint binding's r'*pk_recv + gamma*H, in idHash
    gamma:    int,                  # its blind, shipped in the payload
    identity_root: int,
) -> dict:
    """Build the JSON witness for `circuits/deposit_fold_a2.circom`.

    A2 differs from A1 in one fact with several consequences: the ciphertext
    decrypts to the ISSUER's Identity, a point the spender holds no scalar for.

    So the decrypted point cannot be folded into a scalar sum, and enters as
    witnessed coordinates -- which is why the circuit range-checks its limbs
    explicitly and asserts, at each addition, that the two x-coordinates
    differ.  circom-lib offers only incomplete addition, so A2 enforces the
    precondition where A1 avoided the operation entirely.

    And a fifth relation appears: the decrypted Identity must itself be
    registered, or a colluding issuer keys the note to a throwaway point and
    the recipient holds garbage.  The recipient proves that about the issuer,
    using the salt the issuer shipped (see ``MintedA2.salt_iss``) and a path it
    rebuilds from the published subtree.

    And the key tie: ``idHash`` commits the mint binding's ``T``, which must open
    as ``rm*G + gamma*H`` for the ``rm = r'*k`` the note tie already fixes.  The
    binding proved ``T = r'*pk_Q + gamma*H`` for the key hidden in its ``Q``, so
    this says ``pk_Q`` is the spender's own key, up to a multiple of ``H`` whose
    logarithm no one knows (doc/review/notes-receiving-key.org, section 4.6).

    Raises:
        AssertionError: naming the relation whose arithmetic does not close.
    """
    from alberta_buck.registry.tree import identity_leaf_salted
    from alberta_buck.wallet.bn254 import point_to_words
    from alberta_buck.wallet.notes import id_hash_a2, nullifier
    from alberta_buck.wallet.poseidon import F_R, poseidon

    m_rec, k, sk_dep = witness.m_rec, witness.k, witness.sk_dep
    r_prime %= ORDER
    t %= ORDER
    r_E %= ORDER
    gamma %= ORDER

    rm_val = (r_prime * k) % ORDER                # eIss.C = M_I + rm*G, T = rm*G + gamma*H
    tk_val = (t * k) % ORDER                      # eEnc.C = M_I + tk*G
    cd_val = (m_rec + sk_dep * r_E) % ORDER       # E_dep.C = cd*G
    M_I = witness.M                               # what k decrypted to

    # -- the note tie ------------------------------------------------------
    assert eq(e_iss.R, mul(G1, r_prime)), "eIss.R != r'*G"
    assert eq(e_iss.C, add(M_I, mul(G1, rm_val))), "eIss.C != M_I + rm*G"
    # -- (1) k decrypts the spend's ciphertext to the same M_I -------------
    assert eq(e_enc.R, mul(G1, t)), "eEnc.R != t*G"
    assert eq(e_enc.C, add(M_I, mul(G1, tk_val))), "eEnc.C != M_I + tk*G"
    # -- the key tie: T opens to this spender's k ---------------------------
    gH = mul(H_PEDERSEN, gamma)
    assert eq(T, add(mul(G1, rm_val), gH)), "T != rm*G + gamma*H (keyed to another mailbox)"
    # -- the incomplete-addition precondition the circuit enforces ---------
    for label, a, b in (("eIss.C", M_I, mul(G1, rm_val)), ("eEnc.C", M_I, mul(G1, tk_val)),
                        ("T", mul(G1, rm_val), gH)):
        ax, _ = point_to_words(a)
        bx, _ = point_to_words(b)
        assert ax % F_R != bx % F_R, (
            f"{label}: the addends share an x-coordinate mod F_R, so the "
            "incomplete addition would land on a doubling or the identity")
    # -- (2) the account credential decrypts to m_rec ----------------------
    assert eq(pk_dep, mul(G1, sk_dep)), "pk_dep != sk_dep*G"
    assert eq(e_dep.R, mul(G1, r_E)), "E_dep.R != r_E*G"
    assert eq(e_dep.C, mul(G1, cd_val)), "E_dep.C != cd*G"
    # -- (3)+(4) the recipient's leaf and its path -------------------------
    assert witness.leaf == receiving_leaf(m_rec, k, witness.salt), \
        "the witness leaf does not commit (m_rec, k, salt)"
    assert witness.root == identity_root, "the witness root is not the posted root"
    # -- (5) the issuer's leaf, under the shipped salt ---------------------
    assert iss_path.leaf == identity_leaf_salted(M_I, salt_iss), \
        "the shipped issuer salt does not open the issuer's leaf"
    assert iss_path.verify() and iss_path.root == identity_root, \
        "the issuer's path does not fold to the posted root"
    # -- the note tie: idHash opens to the ciphertexts and T ----------------
    assert id_hash == id_hash_a2(e_note, e_iss, T), "idHash != id_hash_a2(eNote, eIss, T)"

    nf = nullifier(rho, id_hash)

    def _w(P):
        x, y = point_to_words(P)
        return _limbs(x), _limbs(y)

    eEncRx, eEncRy = _w(e_enc.R)
    eEncCx, eEncCy = _w(e_enc.C)
    pkDepX, pkDepY = _w(pk_dep)
    eDepRx, eDepRy = _w(e_dep.R)
    eDepCx, eDepCy = _w(e_dep.C)
    MIx, MIy = _w(M_I)

    nRx, nRy = point_to_words(e_note.R)
    nCx, nCy = point_to_words(e_note.C)
    iRx, iRy = point_to_words(e_iss.R)
    iCx, iCy = point_to_words(e_iss.C)
    Tx, Ty = point_to_words(T)

    return {
        "nullifier": str(nf),
        "identityRoot": str(identity_root),
        "eEncRx": [str(x) for x in eEncRx], "eEncRy": [str(x) for x in eEncRy],
        "eEncCx": [str(x) for x in eEncCx], "eEncCy": [str(x) for x in eEncCy],
        "pkDepX": [str(x) for x in pkDepX], "pkDepY": [str(x) for x in pkDepY],
        "eDepRx": [str(x) for x in eDepRx], "eDepRy": [str(x) for x in eDepRy],
        "eDepCx": [str(x) for x in eDepCx], "eDepCy": [str(x) for x in eDepCy],
        "rho": str(rho % F_R),
        "idHash": str(id_hash % F_R),
        "eNote": [str(nRx % F_R), str(nRy % F_R), str(nCx % F_R), str(nCy % F_R)],
        "eIss0": [str(iRx % F_R), str(iRy % F_R), str(iCx % F_R), str(iCy % F_R)],
        "T": [str(Tx % F_R), str(Ty % F_R)],
        "r": [str(x) for x in _limbs(r_prime)],
        "k_recv": [str(x) for x in _limbs(k)],
        "rm": [str(x) for x in _limbs(rm_val)],
        "t": [str(x) for x in _limbs(t)],
        "tk": [str(x) for x in _limbs(tk_val)],
        "m_rec": [str(x) for x in _limbs(m_rec)],
        "sk_dep": [str(x) for x in _limbs(sk_dep)],
        "r_E": [str(x) for x in _limbs(r_E)],
        "cd": [str(x) for x in _limbs(cd_val)],
        "gamma": [str(x) for x in _limbs(gamma)],
        "MI": [[str(x) for x in MIx], [str(x) for x in MIy]],
        "salt": str(witness.salt),
        "saltIss": str(salt_iss),
        "pathElements": [str(x) for x in witness.siblings],
        "pathIndices": [str(x) for x in witness.index_bits],
        "issPathElements": [str(x) for x in iss_path.siblings],
        "issPathIndices": [str(x) for x in iss_path.index_bits],
    }
