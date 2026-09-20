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
   authority remains the Identity even though reading does not.  Anti-framing
   rides on the shipped :mod:`issuer_reenc` binding (with ``pk_rec :=
   pk_recv``): it forces ``eIss`` to re-encrypt the issuer's *own* registered
   ``M_I``, so the recovered issuer is the true minter, never a victim.

2. *Deposit gate* (the on-chain gate, all identities hidden).  Reading the note
   and being the Identity are now facts about two different secrets, so the gate
   must state the tie rather than infer it: one witness carrying
   ``(m_rec, k, sk_dep, b, salt)`` and four relations -- ``k`` decrypts ``eIss``
   to the point committed in ``P_I``, the deposit account's credential holds
   ``M_rec``, a registered accumulator leaf commits the pair ``(M_rec, k*G)``,
   and that leaf's path folds to a posted root.  See
   :mod:`alberta_buck.wallet.deposit_fold`; the third relation is what a
   single-secret design got for free, and without it a payload thief spends with
   its own Identity.

3. *Receipt* (off-chain, unilateral).  The recipient holds the secrets that name
   *both* parties: its own ``M_rec`` (which it knows, and which the note names),
   and the issuer's ``M_I = C_e - k*R_e`` by decryption under the receiving
   secret.  A :mod:`verifiable_decrypt` proof + the mint binding + tree
   membership of both points make the plaintext receipt third-party-checkable
   with no secret.

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
from alberta_buck.wallet.poseidon import F_R, poseidon
from alberta_buck.wallet.transcript import keccak_scalar
from alberta_buck.wallet.notes import (
    FLAVOR_A2, NoteOpening, note_commitment, nullifier_a,
)
from alberta_buck.wallet.issuer_reenc import (
    H_POINT, IssuerReencProof, issuer_reenc_prove, issuer_reenc_verify,
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
# the IdentityRegistry contract maintains on chain (IDENTITY_TREE_DEPTH = 10) and
# that ``circuits/identity_membership_g1tie.circom`` proves membership against.
# ``IdentityTree`` below is a thin *point-centric* facade over it for the
# unilateral-A2 receipt flow; the Merkle algorithm itself is not duplicated.

# On-chain depth: IdentityRegistry.IDENTITY_TREE_DEPTH and the
# identity_membership_g1tie circuit both fix depth 10, so a wallet-built root
# matches the contract's identityRoot and a path verifies in the circuit.
IDENTITY_TREE_DEPTH = 10


class IdentityTree(IdentityMerkleTree):
    """Point-centric facade over the canonical registry IdentityMerkleTree.

    Adds the identity-*point* conveniences the unilateral-A2 receipt flow uses
    (``insert(M)``, ``contains(M[, root])``) so callers work in identity points
    rather than pre-hashed leaves.  The accumulator itself -- zeros, incremental
    insertion, path, root -- is the single canonical implementation in
    :mod:`alberta_buck.registry.tree`, matched byte-for-byte by the on-chain
    IdentityRegistry accumulator and the membership circuit.  Defaults to the
    on-chain depth (10).
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
    (Poseidon8, matching ``mint_batch_a2.circom``).  ``eIss``/``binding`` go on
    chain (the binding anchors anti-framing at mint); the full ``opening`` +
    ``eNote`` + ``eIss`` travel to the recipient off chain.
    """
    eNote:   ElGamalCiphertext   # (r_n*G, v*G + r_n*pk_recv) -- value, to the mailbox
    eIss:    ElGamalCiphertext   # (r'*G, M_I + r'*pk_recv)   -- issuer M, to the mailbox
    M_I:     Tuple               # issuer's registered identity (issuer-side only)
    idHash:  int
    cm:      int
    opening: NoteOpening
    binding: IssuerReencProof    # issuer_reenc with pk_rec := M_rec (anti-framing)
    r_prime: int                 # issuer-held randomness (off chain)
    r_note:  int                 # note-value encryption randomness (off chain)


def a2_id_hash(eNote: ElGamalCiphertext, eIss: ElGamalCiphertext) -> int:
    """``idHash = Poseidon8(eNote, eIss)`` — 8 field elements reduced mod F_R.

    Matches the on-chain layout in ``mint_batch_a2.circom`` and
    :func:`alberta_buck.wallet.notes.id_hash_a2`.  Both ciphertexts are committed
    so the note-binding circuit can open ``idHash`` and re-encrypt ``eIss`` under
    the recipient identity, proving the spend's ``eEnc`` binds to THIS note.
    """
    from alberta_buck.wallet.notes import id_hash_a2
    return id_hash_a2(eNote, eIss)


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
    # recipient key, and now genuinely is one.
    binding = issuer_reenc_prove(
        sk_iss, r_prime, pk_recv, E_reg, eIss, issuer, chainid, rng=rng,
    )

    idHash = a2_id_hash(eNote, eIss)
    opening = NoteOpening(FLAVOR_A2, v, rho, idHash, predicate)
    cm = note_commitment(opening)
    return MintedA2(eNote=eNote, eIss=eIss, M_I=M_I, idHash=idHash, cm=cm,
                    opening=opening, binding=binding, r_prime=r_prime,
                    r_note=r_note)


# ========================== Deposit coupling ================================
#
# LEGACY SIGMA -- superseded by the folded gate, retained only for the vectors
# and contracts that still replay it.  New code MUST use
# :mod:`alberta_buck.wallet.deposit_fold`.
#
# This sigma proves knowledge of (m_rec, sk_dep, b) with:
#
#   E4:  pk_dep         = sk_dep * G                 (the real account key)
#   E2:  C_d            = m_rec  * G + sk_dep * R_d   (account bound to M_rec=m_rec*G)
#   E3:  C_e - P_I      = m_rec  * R_e - b * H        (eIss decrypts under m_rec to P_I-b*H)
#
# and it works only while ONE secret does both jobs.  Under the receiving key
# the note decrypts under k and the account credential holds m_rec, so E3 and
# E2 speak about different scalars and the shared Fiat-Shamir nonce no longer
# ties them: E3 becomes "P commits whatever this scalar decrypts to", which
# every scalar satisfies.  A thief holding a stolen payload then proves the
# reading half with the stolen k and the Identity half with its OWN registered
# Identity -- both true, neither joining them.
#
# The remedy is not a patch to this sigma.  It is one witness, one circuit, and
# the tie stated as a relation over a registered accumulator leaf; see
# deposit_fold and doc/review/notes-receiving-key.org section 3.3a.

@dataclass(frozen=True)
class DepositCouplingProof:
    e:   int
    s_m: int    # response for m_rec
    s_s: int    # response for sk_dep
    s_b: int    # response for b
    A2:  Tuple  # k_m*G + k_s*R_d
    A3:  Tuple  # k_m*R_e - k_b*H
    A4:  Tuple  # k_s*G
    P_I: Tuple  # M_I + b*H   (hides the decrypted issuer identity)


def _dc_transcript(pk_dep, E_dep: ElGamalCiphertext, eIss: ElGamalCiphertext,
                   P_I, A2, A3, A4, account: int, chainid: int) -> int:
    pts = [pk_dep, E_dep.R, E_dep.C, eIss.R, eIss.C, P_I, A2, A3, A4]
    words: List[int] = []
    for P in pts:
        x, y = point_to_words(P)
        words.append(x)
        words.append(y)
    words.append(account)
    words.append(chainid)
    return keccak_scalar(*words)


def deposit_couple_prove(
    m_rec:   int,
    sk_dep:  int,
    E_dep:   ElGamalCiphertext,   # deposit account's registered credential (R_d, C_d)
    eIss:    ElGamalCiphertext,   # the note leaf's eIss (R_e, C_e)
    account: int,                 # deposit account address (msg.sender at spend)
    chainid: int,
    b:       Optional[int] = None,
    rng=None,
) -> DepositCouplingProof:
    """Prove deposit eligibility for an identity-targeted A2 note, all identities
    hidden.  Asserts the witness consistency before proving so misuse fails loudly.
    """
    pk_dep = mul(G1, sk_dep % ORDER)
    R_d, C_d = E_dep.R, E_dep.C
    R_e, C_e = eIss.R, eIss.C

    M_rec = mul(G1, m_rec % ORDER)
    M_I = add(C_e, neg(mul(R_e, m_rec % ORDER)))   # decrypt eIss under m_rec
    # Sanity: the deposit account must be bound to identity m_rec.
    assert eq(C_d, add(M_rec, mul(R_d, sk_dep % ORDER))), \
        "E_dep does not decrypt to m_rec*G under sk_dep"

    b = rand_scalar(rng) if b is None else (b % ORDER)
    P_I = add(M_I, mul(H_POINT, b))                # commit/hide the issuer identity

    k_m = rand_scalar(rng)
    k_s = rand_scalar(rng)
    k_b = rand_scalar(rng)
    A4 = mul(G1, k_s)                                      # k_s*G
    A2 = add(mul(G1, k_m), mul(R_d, k_s))                  # k_m*G + k_s*R_d
    A3 = add(mul(R_e, k_m), neg(mul(H_POINT, k_b)))        # k_m*R_e - k_b*H

    e = _dc_transcript(pk_dep, E_dep, eIss, P_I, A2, A3, A4, account, chainid)
    s_m = (k_m + e * (m_rec % ORDER)) % ORDER
    s_s = (k_s + e * (sk_dep % ORDER)) % ORDER
    s_b = (k_b + e * b) % ORDER
    return DepositCouplingProof(e=e, s_m=s_m, s_s=s_s, s_b=s_b,
                                A2=A2, A3=A3, A4=A4, P_I=P_I)


def deposit_couple_verify(
    pk_dep,
    E_dep:   ElGamalCiphertext,
    eIss:    ElGamalCiphertext,
    proof:   DepositCouplingProof,
    account: int,
    chainid: int,
) -> bool:
    """Verify deposit eligibility.  True iff some identity scalar binds the
    deposit account *and* decrypts ``eIss`` to the point committed in ``P_I``."""
    R_d, C_d = E_dep.R, E_dep.C
    R_e, C_e = eIss.R, eIss.C
    e, s_m, s_s, s_b = proof.e, proof.s_m, proof.s_s, proof.s_b

    # E4: s_s*G == A4 + e*pk_dep
    if not eq(mul(G1, s_s), add(proof.A4, mul(pk_dep, e))):
        return False
    # E2: s_m*G + s_s*R_d == A2 + e*C_d
    if not eq(add(mul(G1, s_m), mul(R_d, s_s)), add(proof.A2, mul(C_d, e))):
        return False
    # E3: s_m*R_e - s_b*H == A3 + e*(C_e - P_I)
    X = add(C_e, neg(proof.P_I))
    if not eq(add(mul(R_e, s_m), neg(mul(H_POINT, s_b))), add(proof.A3, mul(X, e))):
        return False
    # Fiat-Shamir
    return proof.e == _dc_transcript(pk_dep, E_dep, eIss, proof.P_I,
                                     proof.A2, proof.A3, proof.A4, account, chainid)


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
        binding=minted.binding, issuer=issuer, chainid=chainid,
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

    # (3) Issuer identity is registered -- this is the coupling: a bogus eIss
    #     (keyed to anything but M_rec) decrypts to a non-member here.
    if not tree.contains(receipt.M_I, identity_root):
        return RcptResult(False, None, None, receipt.value, "issuer M not a registered identity")

    # (4) Recipient identity is registered.
    if not tree.contains(receipt.M_rec, identity_root):
        return RcptResult(False, None, None, receipt.value, "recipient M not a registered identity")

    return RcptResult(True, receipt.M_I, receipt.M_rec, receipt.value, "VALID")


__all__ = [
    "identity_leaf", "IdentityTree",
    "MintedA2", "a2_id_hash", "mint_unilateral_a2",
    "DepositCouplingProof", "deposit_couple_prove", "deposit_couple_verify",
    "UnilateralReceipt", "RcptResult", "make_receipt", "verify_receipt",
]
