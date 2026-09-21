"""The Notes receiving key: a mailbox key, which is deliberately not the Identity.

An addressed note names an Identity and is *keyed* to that Identity's
receiving key.  Two objects, two jobs, and they cannot be collapsed into one.

The read/write split makes the identity scalar ``m`` a READ CAPABILITY: the
design hands it to every counterparty, because that is how a receipt names a
person.  So it cannot also be what opens the recipient's mail -- one
disclosure would read every note that recipient ever receives, in both
directions in time.  A read capability you give away cannot be a decryption
key you keep.

The arithmetic is less forgiving than the principle.  Encrypting an identity
point under itself yields

    eRec = (r'G, M + r'M) = (r'G, m(G + R))

where message and key share one secret, so the Diffie-Hellman hardness
protecting every other ciphertext in the system is simply absent: a candidate
identity is tested with ONE scalar multiplication, needing no proof, no key
and no issuer secret.  Keying the same plaintext to ``pk_recv`` instead turns
that test into a decisional Diffie-Hellman decision, which is the lock the
registration layer already rests on.

    k       = keccak256(RECV_DOMAIN || seed || rotation) mod ORDER, redrawn if 0
    pk_recv = k*G

Three requirements shape the derivation, and each is load-bearing:

  1. ``k`` MUST NOT be derivable from ``m``, from ``M``, or from the certified
     record.  Those are all values a counterparty holds by design.  A reading
     key recoverable from a disclosed record is a reading key held by everyone
     who has the record -- which is the defect this module exists to remove.
     It therefore derives from the wallet SEED, which no counterparty sees.

  2. ``k`` MUST be recoverable from that seed material, so device loss does
     not strand a note.  Recoverability moves from the certified record (where
     it was universal, and so a vulnerability) to the seed (where every wallet
     has always kept its reading keys).

  3. Rotation MUST be indexed, and by the same counter the accumulator's salt
     uses, so one number recovers both the key and the leaf that binds it.
     See ``alberta_buck.wallet.salt.derive_salt``'s ``association_counter``:
     rotating a receiving key IS an accumulator re-association, not a second
     mechanism.

The binding between ``pk_recv`` and the Identity lives inside the accumulator
leaf (``registry.tree.receiving_leaf``), never in public.  A spend proving
against a PUBLIC binding would reveal the recipient's registered key and
deanonymise them to everyone, which is worse than the problem being solved.

Reference: doc/review/notes-receiving-key.org (architecture of record),
doc/review/accumulator-spec.org sections 3, 4, 7 and 8.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Tuple

from alberta_buck.wallet.bn254 import G1, ORDER, mul
from alberta_buck.wallet.transcript import keccak_raw

if TYPE_CHECKING:  # pragma: no cover
    from alberta_buck.registry.tree import MembershipProof

__all__ = [
    "RECV_DOMAIN",
    "derive_receiving_secret",
    "receiving_key",
    "receiving_public",
    "ReceivingBinding",
    "prove_receiving_binding",
    "verify_receiving_binding",
    "WRAP_DOMAIN",
    "wrap_mask",
    "mailbox_shared_minter",
    "mailbox_shared_recipient",
    "wrap_scalar",
    "unwrap_scalar",
]


# Domain separator, so a receiving secret can never collide with another value
# the wallet derives from the same seed (an identity scalar, an account key, a
# salt).  Distinct domains are what let one seed hold every secret safely.
RECV_DOMAIN = b"AlbertaBuck/Notes/ReceivingKey/v1"
WRAP_DOMAIN = b"AlbertaBuck/Notes/PayloadWrap/v1"


# --------------------------------------------------------------------------- #
# Wrapping the payload's SCALARS to the same key its ciphertexts are keyed to.
# --------------------------------------------------------------------------- #
#
# A delivered note carries more than ciphertexts.  A spend needs the issuer's
# mint randomness (the fold's witness is the SCALAR r'*k, and a recipient
# holding only the point k*R cannot produce it) and, for A2, the salt of the
# issuer's naming association.  Both have to travel, and in the clear both
# defeat the encryption they travel beside:
#
#   r'        with r' and pk_recv, any holder of the payload computes
#             M = C - r'*pk_recv.  The channel reads the issuer's Identity
#             without ever touching k.
#   salt_iss  with the salt and the published subtree, a holder can TEST
#             candidate Identities against identity_leaf_salted -- a scan, on
#             the leaf whose whole purpose was to be unscannable.
#
# So the scalars are wrapped to the same mailbox key.  Both sides derive the
# same point without an extra round trip -- the minter from what it chose, the
# recipient from what it holds:
#
#   minter     S = r' * pk_recv
#   recipient  S = k  * R          where R = r'*G is already in the payload
#
# which is Diffie-Hellman, and the only party that can compute it is the party
# the note is addressed to.  The wrap is addition in the scalar field:
# invertible, length-preserving, and nothing to get wrong about padding.  It is
# not a new assumption -- the same DDH that keeps the ciphertexts shut keeps
# the mask unguessable.
#
# Each field gets its OWN mask, keyed by a label.  One shared point masking two
# scalars would be one pad used twice, and an observer subtracting the two
# blobs would learn r' - salt_iss -- not either secret, but not nothing either,
# and a pad reused is a pad to explain.  A label costs one hash.


def wrap_mask(shared, label: bytes = b"") -> int:
    """The one-time mask for ONE payload field, from the shared point.

    Args:
        shared: The Diffie-Hellman point both sides derive.
        label: The field's name, so each field draws its own mask from the
            same point.  A caller that omits it is masking a single field.
    """
    from alberta_buck.wallet.bn254 import point_to_words
    from alberta_buck.wallet.transcript import keccak_raw
    x, y = point_to_words(shared)
    digest = keccak_raw(WRAP_DOMAIN + b"/" + label + b"/"
                        + x.to_bytes(32, "big") + y.to_bytes(32, "big"))
    return int.from_bytes(digest, "big") % ORDER


def mailbox_shared_minter(r_prime: int, pk_recv):
    """The shared point as the MINTER computes it: ``r' * pk_recv``."""
    from alberta_buck.wallet.bn254 import mul
    return mul(pk_recv, r_prime % ORDER)


def mailbox_shared_recipient(k_recv: int, R):
    """The shared point as the RECIPIENT computes it: ``k * R``."""
    from alberta_buck.wallet.bn254 import mul
    return mul(R, k_recv % ORDER)


def wrap_scalar(value: int, shared, label: bytes = b"") -> int:
    """Mask a payload scalar to the mailbox.  Inverse of :func:`unwrap_scalar`."""
    return (value + wrap_mask(shared, label)) % ORDER


def unwrap_scalar(blob: int, shared, label: bytes = b"") -> int:
    """Recover a wrapped payload scalar.  The label MUST match the wrap's."""
    return (blob - wrap_mask(shared, label)) % ORDER


def derive_receiving_secret(seed: int, rotation: int = 0) -> int:
    """Derive this wallet's receiving secret ``k`` at ``rotation``.

    Args:
        seed: Wallet seed material.  MUST NOT leave the wallet, and MUST NOT
            be the identity scalar: see requirement 1 in the module docstring.
        rotation: 0 for the first receiving key, incremented on each rotation.
            The same counter is passed to
            :func:`alberta_buck.wallet.salt.derive_salt` as its
            ``association_counter``, so one number recovers the key and the
            salt of the leaf binding it.

    Returns:
        A scalar in [1, ORDER), suitable as an ElGamal decryption key.

    Raises:
        ValueError: if `seed` is not a positive int or `rotation` is negative.
    """
    if not isinstance(seed, int) or seed <= 0:
        raise ValueError("seed must be a positive int")
    if not isinstance(rotation, int) or rotation < 0:
        raise ValueError("rotation must be a non-negative int")
    preimage = (
        RECV_DOMAIN
        + (seed % ORDER).to_bytes(32, "big")
        + rotation.to_bytes(32, "big")
    )
    k = int.from_bytes(keccak_raw(preimage), "big") % ORDER
    # keccak returning 0 mod ORDER is negligible, but a zero secret would make
    # pk_recv the point at infinity and every ciphertext trivially readable, so
    # step to the next rotation rather than emit it.
    if k == 0:
        return derive_receiving_secret(seed, rotation + 1)
    return k


def receiving_public(k: int) -> Tuple:
    """``pk_recv = k*G`` -- the address a payer encrypts to.

    Safe to hand out: given only ``pk_recv``, deciding whether a ciphertext is
    addressed to it is a DDH instance.  Publishing it links nothing, which is
    why the payer can learn it on the same out-of-band channel that carries
    ``M_rec`` and needs nothing from the recipient at payment time.
    """
    if not isinstance(k, int) or not (1 <= k < ORDER):
        raise ValueError("receiving secret must be in [1, ORDER)")
    return mul(G1, k)


def receiving_key(seed: int, rotation: int = 0) -> Tuple[int, Tuple]:
    """Derive ``(k, pk_recv)`` at ``rotation`` in one call."""
    k = derive_receiving_secret(seed, rotation)
    return k, receiving_public(k)


# ===================== The binding, and its evidence ========================
#
# A note addressed to a key of the payer's choosing would break mutual
# decryptability and the receipt, so `pk_recv` MUST be bound to the Identity.
# The binding lives in the accumulator leaf, and because it is hidden there,
# the payer's assurance is a proof the RECIPIENT produces and the receipt
# captures at payment time.  That is the pattern the accumulator already chose
# for every attribute (alberta_buck.wallet.attributes), so it needs no new
# mechanism.
#
# Scope of the code below, stated as `attributes.py` states its own: it builds
# and checks the binding IN THE CLEAR, which is the witness the circuit
# consumes.  A verifier running it learns the holder's salt and so can locate
# the leaf in the published subtree.  For a receipt that already names both
# parties in plaintext, disclosing the position of a leaf to its own subject's
# counterparty is a small price; hiding it is the SNARK's job, exactly as for
# every other private-subtree claim.


@dataclass(frozen=True)
class ReceivingBinding:
    """Holder-produced evidence that ``pk_recv`` is the registered receiving
    key of the Identity ``M``.

    It discloses NO secret.  An earlier shape carried ``k_recv`` itself, on the
    reasoning that the holder was naming itself to this counterparty anyway --
    which was wrong twice over: the mailbox key reads every note to that
    address, in both directions in time, and a payer who checks the evidence is
    not the only party who ever sees a receipt.  What the payer needs is that
    the accumulator certifies the pair, and
    :func:`alberta_buck.registry.tree.mailbox_leaf` states exactly that over
    the two POINTS the payer already holds.  So the evidence is a hash and a
    path, checkable by anyone, secret to no one.

    Attributes:
        pk_recv: The receiving key ``k*G``.  Safe to disclose: deciding which
            ciphertexts it addresses, from the key alone, is DDH.
        salt: The salt of the mailbox association -- NOT the salt the holder's
            spend proves under.  Distinct associations carry distinct salts, so
            this one locates the payer's leaf and says nothing about the gate's.
        path: The membership path of ``mailbox_leaf(M, pk_recv, salt)``.
    """
    pk_recv: Tuple
    salt: int
    path: "MembershipProof"


def prove_receiving_binding(M_rec, pk_recv, salt: int, tree) -> ReceivingBinding:
    """Build the binding evidence for ``(M_rec, pk_recv)`` from its mailbox salt.

    Takes the two POINTS, because that is all it needs and all a payer can
    check.  The holder's receiving secret is not an argument to this function
    and must never become one.

    Args:
        M_rec: The holder's Identity point.
        pk_recv: The holder's receiving key.
        salt: The salt of the mailbox association.
        tree: The subtree the association was admitted to.

    Raises:
        ValueError: if no leaf for this pair is in the tree, which is the
            honest answer when a holder claims a key it never registered.
    """
    from alberta_buck.registry.tree import mailbox_leaf

    leaf = mailbox_leaf(M_rec, pk_recv, salt)
    if leaf not in tree.leaves:
        raise ValueError(
            "no registered leaf commits this (Identity, receiving key) pair")
    return ReceivingBinding(
        pk_recv=pk_recv, salt=salt, path=tree.path(tree.leaves.index(leaf)),
    )


def verify_receiving_binding(M_rec, binding: "ReceivingBinding",
                             root: int) -> bool:
    """Check that ``binding`` ties ``M_rec`` to its receiving key under ``root``.

    Needs no secret, which is the whole point: a payer runs this before paying,
    and a receipt verifier runs the same check offline.

    Recomputes the leaf from the claimed pair rather than trusting the one in
    the path: a path proves that SOME leaf is a member, and which one is the
    entire question.
    """
    from alberta_buck.registry.tree import mailbox_leaf

    try:
        leaf = mailbox_leaf(M_rec, binding.pk_recv, binding.salt)
    except ValueError:
        return False
    return (binding.path.leaf == leaf
            and binding.path.verify()
            and binding.path.root == root)
