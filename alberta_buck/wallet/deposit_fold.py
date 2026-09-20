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

    (1) k decrypts the note ciphertext to the point committed in P
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
membership proof.  A2 additionally needs the membership of the point
committed in ``P`` (the issuer Identity it decrypts to), which is the
pre-existing P-bound membership statement and rides alongside these four
rather than replacing any of them.

Reference: doc/review/notes-receiving-key.org section 3.3a (architecture of
record), scripts/review/deposit_gate_folded.py (the prototype this promotes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, eq, mul, neg, rand_scalar,
)
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.issuer_reenc import H_POINT
from alberta_buck.wallet.poseidon import poseidon
from alberta_buck.registry.tree import receiving_leaf

__all__ = [
    "DepositFoldRefused",
    "DepositFoldWitness",
    "deposit_fold_witness",
    "deposit_fold_check",
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
        b: The blinding that hides the decrypted point in ``P``.
        salt: The holder's salt for the leaf binding ``(M_rec, k*G)``.
        siblings, index_bits: The membership path of that leaf.

    Public:
        P: ``M + b*H``, the decrypted point, hidden.
        root: The posted identity root the path folds to.
        M: The decrypted point itself -- present so a prover can check its
            own work and a test can name it.  It is NOT a public input; the
            whole purpose of ``P`` is that the chain does not see it.
    """
    m_rec: int
    k: int
    sk_dep: int
    b: int
    salt: int
    siblings: List[int] = field(repr=False)
    index_bits: List[int] = field(repr=False)
    P: Tuple = field(repr=False)
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
    b:       Optional[int] = None,
    rng=None,
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
        b: The blinding for ``P``; drawn fresh if omitted.
        rng: Randomness source for ``b``.

    Returns:
        The witness, with ``P``, ``root`` and the path filled in.

    Raises:
        DepositFoldRefused: naming the relation that does not hold.
    """
    m_rec %= ORDER
    k %= ORDER
    sk_dep %= ORDER
    if not (m_rec and k and sk_dep):
        raise DepositFoldRefused(1, "witness scalars must be nonzero")

    M_rec = mul(G1, m_rec)
    pk_recv = mul(G1, k)
    b = rand_scalar(rng) if b is None else (b % ORDER)

    # (1) k decrypts the note ciphertext to the point committed in P.
    M = add(note_ct.C, neg(mul(note_ct.R, k)))
    P = add(M, mul(H_POINT, b))

    # (2) The account credential decrypts, under its own key, to the Identity.
    if not eq(E_dep.C, add(M_rec, mul(E_dep.R, sk_dep))):
        raise DepositFoldRefused(
            2, "the account credential does not decrypt to m_rec*G under sk_dep")

    # (3) A registered leaf commits the pair, under the holder's own salt.
    #     This is the relation a split gate leaves out, and the one the thief
    #     of a stolen payload cannot satisfy.
    leaf = receiving_leaf(M_rec, pk_recv, salt)
    if leaf not in tree.leaves:
        raise DepositFoldRefused(
            3, "no registered leaf commits this (Identity, receiving key) pair")

    # (4) That leaf's path folds to a posted root.
    proof = tree.path(tree.leaves.index(leaf))
    if not proof.verify():
        raise DepositFoldRefused(4, "the membership path does not fold to the root")

    return DepositFoldWitness(
        m_rec=m_rec, k=k, sk_dep=sk_dep, b=b, salt=salt,
        siblings=list(proof.siblings), index_bits=list(proof.index_bits),
        P=P, root=proof.root, M=M, leaf=leaf,
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
    pk_recv = mul(G1, witness.k)

    # (2a) the account key is the registered one -- this is what pins sk_dep,
    #      and without it relation (2) would hold for an unregistered account.
    if not eq(pk_dep, mul(G1, witness.sk_dep)):
        return False
    # (2b) the credential decrypts to the Identity under that key.
    if not eq(E_dep.C, add(M_rec, mul(E_dep.R, witness.sk_dep))):
        return False
    # (1) k decrypts the note to the point committed in P.
    M = add(note_ct.C, neg(mul(note_ct.R, witness.k)))
    if not eq(witness.P, add(M, mul(H_POINT, witness.b))):
        return False
    # (3) the leaf commits the pair under the holder's salt.
    try:
        leaf = receiving_leaf(M_rec, pk_recv, witness.salt)
    except ValueError:
        return False
    if leaf != witness.leaf:
        return False
    # (4) the path folds to the posted root.
    cur = leaf
    for sib, bit in zip(witness.siblings, witness.index_bits):
        cur = poseidon([cur, sib]) if bit == 0 else poseidon([sib, cur])
    return cur == root
