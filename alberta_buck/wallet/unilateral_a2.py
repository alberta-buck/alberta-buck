"""Identity-targeted *unilateral* A2 Note -- the recipient-reproducible receipt.

Reference: alberta-buck-notes.org ("The Non-Deniable-Receipt Invariant", unilateral receipt) and alberta-buck-notes-flow.org ("A2 Identity-Targeted Spend"). See also the identity-axis unification in notes.org.

A direct EOA transfer *deduces* identities from the two accounts in play.  A Note
is the opposite object: it *names* the identities and treats accounts as
interchangeable envelopes that need only *match* the expected ``M``.  This module
implements that inversion for the A2 (addressed, private-issuer) flavour, and the
payoff is a receipt the *recipient alone* can produce -- naming both parties in
plaintext -- with the issuer un-nameability collusion gap closed by a registry
membership check rather than by an expensive account-pinning SNARK.

The three moving parts:

1. *Mint.*  The issuer encrypts its **own registered identity** ``M_I`` to the
   recipient's registered **receiving key** ``pk_recv`` (not an account key, and
   deliberately not the Identity point)::

       eIss = (R_e, C_e) = (r'*G,  M_I + r'*pk_recv),   pk_recv = k*G

   The decryption secret is therefore ``k``, a value the recipient hands to
   nobody -- unlike the identity scalar, which the design discloses to every
   counterparty because that is how a receipt names a person
   (:mod:`alberta_buck.wallet.recvkey`).  The issuer needs only ``pk_recv``,
   never an account, and any account the depositor later uses can spend, because
   authority remains the Identity even though reading does not.  The mint's
   :mod:`issuer_reenc` binding (with ``pk_rec := pk_recv``) proves ``eIss``
   encrypts the minter's *own* registered Identity under the key it hides in
   ``Q``.  That alone would not make the key ``pk_recv``: a minter could key one
   ciphertext so that its hidden key opens it to its own Identity and the
   recipient's opens it to another registered one.  So ``idHash`` commits the
   binding's ``T = r'*pk_recv + gamma*H``, the recipient receives ``gamma``,
   and the spend proves ``T`` opens under its own ``k``
   (doc/review/notes-receiving-key.org section 4.6).

2. *Deposit gate* (the on-chain gate, all identities hidden).  Reading the note
   and being the Identity are now facts about two different secrets, so the gate
   must state the tie rather than infer it: one Groth16 proof over one witness
   ``(m_rec, k, sk_dep, salt, ...)`` -- ``k`` decrypts the note to the issuer
   Identity ``M_I``, the deposit account's credential holds ``M_rec``, a
   registered leaf commits the pair ``(m_rec, k)``, its path folds to a posted
   root, ``M_I`` is itself registered, and ``T = r'*k*G + gamma*H``.  See
   :mod:`alberta_buck.wallet.deposit_fold`; the leaf relation is what a
   single-secret design got for free, and without it a payload thief spends with
   its own Identity.

3. *Receipt* (off-chain, unilateral).  The recipient holds the secrets that name
   *both* parties: its own ``M_rec`` (which it knows, and which the note names),
   and the issuer's ``M_I = C_e - k*R_e`` by decryption under the receiving
   secret.  A :mod:`verifiable_decrypt` proof + the mint binding + the tie
   ``M_I = C_e - T + gamma*H`` + tree membership of both points make the
   plaintext receipt third-party-checkable with no secret.

Privacy invariant throughout: a passive observer (Mallory) learns neither
``m_rec``, ``M_rec`` (the recipient identity), nor ``M_I`` (the issuer identity)
-- so it cannot link the issuer's mint to the depositor's account.  Nor can a
party holding every certified identity scalar test the calldata for a
recipient, which is precisely what keying to ``pk_recv`` buys.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, mul, neg, eq, rand_scalar, point_to_words,
)
from alberta_buck.wallet.elgamal import (
    ElGamalCiphertext, elgamal_encrypt, elgamal_decrypt,
)
from alberta_buck.wallet.nums import H_PEDERSEN
from alberta_buck.wallet.poseidon import F_R, poseidon
from alberta_buck.wallet.transcript import keccak_scalar
from alberta_buck.wallet.notes import (
    FLAVOR_A2, NoteOpening, note_commitment, nullifier_a,
)
from alberta_buck.wallet.issuer_reenc import (
    IssuerReencProof, issuer_reenc_prove, issuer_reenc_verify,
)
from alberta_buck.wallet.verifiable_decrypt import (
    VDProof, verifiable_decrypt_prove, verifiable_decrypt_verify,
)
from alberta_buck.registry.tree import IdentityMerkleTree, identity_leaf


# ========================= Identity registry tree ===========================
#
# The registry-Identity accumulator -- the Poseidon Merkle tree of registered
# identity *points* -- has one canonical implementation,
# :class:`alberta_buck.registry.tree.IdentityMerkleTree`.  It is the same tree
# the IdentityRegistry contract maintains on chain (IDENTITY_TREE_DEPTH = 20) and
# that the spend circuits prove membership against.
# ``IdentityTree`` below is a thin *point-centric* facade over it for the
# unilateral-A2 receipt flow; the Merkle algorithm itself is not duplicated.

# On-chain depth: IdentityRegistry.IDENTITY_TREE_DEPTH and the spend circuits
# all fix depth 20, so a wallet-built root matches the contract's identityRoot
# and a path verifies in the circuit.  The three move together and must: a
# depth-10 witness cannot be generated for a depth-20 circuit at all, and the
# fixtures embed proofs whose root is a public input.
IDENTITY_TREE_DEPTH = 20


class IdentityTree(IdentityMerkleTree):
    """Point-centric facade over the canonical registry IdentityMerkleTree.

    Adds the identity-*point* conveniences the unilateral-A2 receipt flow uses
    (``insert(M)``, ``contains(M[, root])``) so callers work in identity points
    rather than pre-hashed leaves.  The accumulator itself -- zeros, incremental
    insertion, path, root -- is the single canonical implementation in
    :mod:`alberta_buck.registry.tree`, matched byte-for-byte by the on-chain
    IdentityRegistry accumulator and the membership circuit.  Defaults to the
    on-chain depth (20).
    """

    def __init__(self, depth: int = IDENTITY_TREE_DEPTH,
                 private: bool = False) -> None:
        super().__init__(depth=depth, private=private)

    def insert(self, M) -> int:
        """Append a registered identity *point*; returns its leaf index."""
        return self.insert_identity(M)

    def contains(self, M, root: Optional[int] = None) -> bool:
        """True iff identity point ``M`` is in the tree.

        With ``root`` given, additionally require a membership path to fold to
        that root (the in-circuit relation, in the clear) -- so a stale or wrong
        root is rejected, mirroring the on-chain membership gate.
        """
        leaf = identity_leaf(M)
        if leaf not in self.leaves:
            return False
        if root is None:
            return True
        proof = self.path(self.leaves.index(leaf))
        return proof.verify() and proof.root == root


# ================================ Mint ======================================

@dataclass(frozen=True)
class MintedA2:
    """Everything the issuer produces for one identity-targeted A2 note.

    ``eNote`` encrypts the note value ``v`` and ``eIss`` the issuer identity
    ``M_I``, both to the recipient's receiving key ``pk_recv``.  Both are committed in ``idHash``
    with the binding's ``T`` (Poseidon10, matching ``mint_batch_a2.circom``).  ``eIss``/``binding``
    go on chain (the binding anchors anti-framing at mint); the full ``opening`` + ``eNote`` +
    ``eIss`` + ``gamma`` travel to the recipient off chain.
    """
    eNote:   ElGamalCiphertext   # (r_n*G, v*G + r_n*pk_recv) -- value, to the mailbox
    eIss:    ElGamalCiphertext   # (r'*G, M_I + r'*pk_recv)   -- issuer M, to the mailbox
    M_I:     Tuple               # issuer's registered identity (issuer-side only)
    idHash:  int
    cm:      int
    opening: NoteOpening
    binding: IssuerReencProof    # issuer_reenc with pk_rec := pk_recv (anti-framing)
    r_prime: int                 # issuer-held randomness (off chain)
    r_note:  int                 # note-value encryption randomness (off chain)
    gamma:   int                 # the binding's blind on T (off chain, to the recipient)
    salt_iss: Optional[int] = None
    """The issuer's registry salt for the association that NAMES it.

    A2's spend must prove that the point the recipient decrypts is a
    registered Identity -- otherwise a colluding issuer keys the note to a
    throwaway and the recipient holds garbage that no receipt can name.  That
    relation is proven by the RECIPIENT about the ISSUER, so the recipient
    needs the issuer's leaf preimage, and the only way to have it is for the
    issuer to send it.  Hence this field: A2 is the sole flavour that ships
    issuer-side witness material, and the reason is that A2 is the sole
    flavour whose issuer is private.

    It is NOT the issuer's receiving-leaf salt.  That leaf commits the
    issuer's mailbox key, and disclosing its preimage would hand every
    recipient the issuer's reading key.  This is the salt of a SECOND
    association of the same Identity -- an ordinary salted identity leaf --
    and because distinct associations carry distinct salts (accumulator
    specification, section 8.3) the two are unlinkable: disclosing the one
    that names the issuer says nothing about the one that reads its mail.

    Only the salt travels.  The Merkle path is not shipped, because paths go
    stale as the subtree grows while salts do not; the recipient rebuilds the
    path from the published subtree at spend time.

    Optional so that a deployment whose A2 issuers are institutions enrolled
    in a PUBLIC subtree can leave it unset: there the leaf is unsalted and the
    recipient computes it from the decrypted Identity alone."""


def a2_id_hash(eNote: ElGamalCiphertext, eIss: ElGamalCiphertext, T) -> int:
    """``idHash = Poseidon10(eNote, eIss, T)`` -- 10 field elements reduced mod F_R.

    Matches the on-chain layout in ``mint_batch_a2.circom`` and
    :func:`alberta_buck.wallet.notes.id_hash_a2`.  The fold opens ``idHash`` to tie
    the spend's ``eEnc`` to THIS note's ``eIss``, and ``T`` to the spender's key.
    """
    from alberta_buck.wallet.notes import id_hash_a2
    return id_hash_a2(eNote, eIss, T)


def mint_unilateral_a2(
    sk_iss:  int,
    E_reg:   ElGamalCiphertext,   # issuer's registered credential (R_reg, C_reg)
    pk_recv,                      # recipient's RECEIVING key (learned out of band)
    v:       int,
    rho:     int,
    issuer:  int,                 # issuer account address (msg.sender at mint)
    chainid: int,
    r_prime: Optional[int] = None,
    predicate: int = 0,
    salt_iss: Optional[int] = None,
    rng=None,
) -> MintedA2:
    """Issuer mints an A2 note keyed to the recipient's receiving key.

    Encrypts the issuer's *own registered* identity ``M_I`` to ``pk_recv``, and
    proves (issuer_reenc, ``pk_rec := pk_recv``) that the ciphertext
    re-encrypts ``M_I`` -- the anti-framing binding.

    A2 needs no Identity point at mint: the note names its recipient by being
    keyed to that recipient's registered mailbox, and the accumulator leaf
    binding ``pk_recv`` to ``M_rec`` is what the spend proves.  The payer SHOULD
    check that binding
    (:func:`alberta_buck.wallet.recvkey.verify_receiving_binding`) before
    minting, which is what assures it whom it is paying.

    ``salt_iss`` is the issuer's own registry salt for the association that
    names it, shipped so the recipient can prove at spend that the Identity it
    decrypts is registered.  See :class:`MintedA2`.
    """
    r_prime = rand_scalar(rng) if r_prime is None else (r_prime % ORDER)

    # Issuer's registered identity, recovered from its own credential.
    M_I = elgamal_decrypt(E_reg, sk_iss)

    # eNote = (r_n*G, v*G + r_n*pk_recv): the value, keyed to the mailbox.
    r_note = rand_scalar(rng)
    eNote = elgamal_encrypt(mul(G1, v), pk_recv, r_note)

    # eIss = (r'*G, M_I + r'*pk_recv): the issuer Identity, keyed to the mailbox.
    eIss = elgamal_encrypt(M_I, pk_recv, r_prime)

    # Anti-framing binding: eIss re-encrypts the issuer's registered M_I under the
    # (blinded) point committed in Q.  issuer_reenc's second slot IS the
    # recipient key.  gamma is kept, because the recipient opens T with it.
    beta = rand_scalar(rng)
    gamma = rand_scalar(rng)
    binding = issuer_reenc_prove(
        sk_iss, r_prime, pk_recv, E_reg, eIss, issuer, chainid,
        beta=beta, gamma=gamma, rng=rng,
    )

    idHash = a2_id_hash(eNote, eIss, binding.T)
    opening = NoteOpening(FLAVOR_A2, v, rho, idHash, predicate)
    cm = note_commitment(opening)
    return MintedA2(eNote=eNote, eIss=eIss, M_I=M_I, idHash=idHash, cm=cm,
                    opening=opening, binding=binding, r_prime=r_prime,
                    r_note=r_note, gamma=gamma, salt_iss=salt_iss)


# =============================== Receipt ====================================

@dataclass(frozen=True)
class UnilateralReceipt:
    """A plaintext, third-party-checkable receipt the *recipient alone* produces.

    Names both identities (``M_I`` issuer, ``M_rec`` recipient) and the value,
    and carries the proofs that make them sound with no secret: the mint
    anti-framing binding, the recipient's verifiable decryption of ``eIss``, and
    the membership of both points in the identity tree.
    """
    M_I:        Tuple            # issuer identity (decrypted under k)
    M_rec:      Tuple            # recipient identity (= m_rec*G), NAMED
    pk_recv:    Tuple            # recipient receiving key (= k*G), the VD key
    value:      int
    eIss:       ElGamalCiphertext
    vd:         VDProof          # eIss decrypts under pk_recv to M_I
    binding:    IssuerReencProof # mint anti-framing (eIss over issuer's registered M_I)
    gamma:      int              # opens binding.T: the tie M_I = C - T + gamma*H
    issuer:     int              # issuer account (msg.sender at mint)
    chainid:    int
    M_I_member:   bool           # convenience: was M_I in the tree at build time
    M_rec_member: bool


@dataclass(frozen=True)
class RcptResult:
    valid:  bool
    issuer_M:    Optional[Tuple]
    recipient_M: Optional[Tuple]
    value:  int
    reason: str


def make_receipt(
    k_recv:   int,                # the RECEIVING secret: what decrypts eIss
    M_rec,                        # the recipient's Identity POINT: what is named
    minted:   MintedA2,
    issuer:   int,
    chainid:  int,
    tree:     IdentityTree,
    rng=None,
) -> UnilateralReceipt:
    """Recipient produces the receipt unilaterally from ``k_recv`` and the note.

    Takes the Identity separately from the secret it decrypts with, because
    those are now two values.  The recipient still names both parties alone:
    its own Identity it knows, and the issuer's it decrypts.
    """
    eIss = minted.eIss
    M_I = elgamal_decrypt(eIss, k_recv)           # the issuer identity, named
    vd = verifiable_decrypt_prove(eIss, k_recv, M_I, issuer, chainid, rng=rng)
    return UnilateralReceipt(
        M_I=M_I, M_rec=M_rec, pk_recv=mul(G1, k_recv % ORDER),
        value=minted.opening.v, eIss=eIss, vd=vd,
        binding=minted.binding, gamma=minted.gamma, issuer=issuer, chainid=chainid,
        M_I_member=tree.contains(M_I), M_rec_member=tree.contains(M_rec),
    )


def verify_receipt(
    receipt:   UnilateralReceipt,
    pk_iss,                       # issuer account registered key (from registry)
    E_reg_iss: ElGamalCiphertext, # issuer account registered credential
    identity_root: int,
    tree:      IdentityTree,
) -> RcptResult:
    """Third-party verify, with no secret.  VALID names (issuer M_I, recipient
    M_rec, value) iff every link holds; the membership of the decrypted ``M_I``
    is exactly what forces it to be the issuer's *registered* identity."""
    eIss = receipt.eIss

    # (1) Mint anti-framing: eIss re-encrypts the issuer account's registered M,
    #     keyed to the recipient's receiving key.
    if not issuer_reenc_verify(pk_iss, E_reg_iss, eIss, receipt.binding,
                               receipt.issuer, receipt.chainid):
        return RcptResult(False, None, None, receipt.value, "issuer binding invalid")

    # (2) Recipient's verifiable decryption: eIss decrypts under pk_recv to M_I.
    if not verifiable_decrypt_verify(eIss, receipt.pk_recv, receipt.M_I, receipt.vd,
                                     receipt.issuer, receipt.chainid):
        return RcptResult(False, None, None, receipt.value, "verifiable decryption invalid")

    # (2b) The tie: the Identity the binding proved registered is the one the
    #      recipient's key names.  C - T + gamma*H is the binding's plaintext;
    #      without this a minter keys eIss so pk_recv opens it to a sock puppet.
    named = add(add(eIss.C, neg(receipt.binding.T)), mul(H_PEDERSEN, receipt.gamma % ORDER))
    if not eq(named, receipt.M_I):
        return RcptResult(False, None, None, receipt.value,
                          "the binding's Identity is not the one decrypted")

    # (3) Issuer identity is registered: a bogus eIss (keyed to anything but
    #     pk_recv) decrypts to a non-member here.
    if not tree.contains(receipt.M_I, identity_root):
        return RcptResult(False, None, None, receipt.value, "issuer M not a registered identity")

    # (4) Recipient identity is registered.
    if not tree.contains(receipt.M_rec, identity_root):
        return RcptResult(False, None, None, receipt.value, "recipient M not a registered identity")

    return RcptResult(True, receipt.M_I, receipt.M_rec, receipt.value, "VALID")


__all__ = [
    "identity_leaf", "IdentityTree",
    "MintedA2", "a2_id_hash", "mint_unilateral_a2",
    "UnilateralReceipt", "RcptResult", "make_receipt", "verify_receipt",
]
