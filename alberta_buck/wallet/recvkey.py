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
]


# Domain separator, so a receiving secret can never collide with another value
# the wallet derives from the same seed (an identity scalar, an account key, a
# salt).  Distinct domains are what let one seed hold every secret safely.
RECV_DOMAIN = b"AlbertaBuck/Notes/ReceivingKey/v1"


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

    Attributes:
        pk_recv: The receiving key claimed, as a point, so a verifier can check
            a verifiable decryption under it.  Safe to disclose: deciding which
            ciphertexts it addresses, from the key alone, is DDH.
        k_recv: The receiving secret, the leaf's actual preimage.  Disclosing
            it hands over the mailbox, so this evidence is for a counterparty
            the holder is already naming itself to -- and the circuit proves
            the same statement without it.
        salt: The holder's witness for the hiding leaf.  In the clear here;
            private in the circuit.
        path: The membership path of ``receiving_leaf(m_rec, k_recv, salt)``.
    """
    pk_recv: Tuple
    k_recv: int
    salt: int
    path: "MembershipProof"


def prove_receiving_binding(m_rec: int, k_recv: int, salt: int,
                            tree) -> ReceivingBinding:
    """Build the binding evidence for ``(m_rec, k_recv)`` from the holder's salt.

    Args:
        m_rec: The holder's identity scalar.
        k_recv: The holder's receiving secret.
        salt: The holder's salt for this subtree.
        tree: The subtree the pair was admitted to.

    Raises:
        ValueError: if no leaf for this pair is in the tree, which is the
            honest answer when a holder claims a key it never registered.
    """
    from alberta_buck.registry.tree import receiving_leaf

    leaf = receiving_leaf(m_rec, k_recv, salt)
    if leaf not in tree.leaves:
        raise ValueError(
            "no registered leaf commits this (Identity, receiving key) pair")
    return ReceivingBinding(
        pk_recv=receiving_public(k_recv), k_recv=k_recv, salt=salt,
        path=tree.path(tree.leaves.index(leaf)),
    )


def verify_receiving_binding(m_rec: int, binding: "ReceivingBinding",
                             root: int) -> bool:
    """Check that ``binding`` ties ``m_rec`` to its receiving key under ``root``.

    Recomputes the leaf from the claimed pair rather than trusting the one in
    the path: a path proves that SOME leaf is a member, and the whole point is
    which one.  Also checks that the disclosed secret really is the secret of
    the disclosed key, so the point a verifier decrypts under is the one the
    leaf commits.
    """
    from alberta_buck.registry.tree import receiving_leaf
    from alberta_buck.wallet.bn254 import eq

    try:
        leaf = receiving_leaf(m_rec, binding.k_recv, binding.salt)
    except ValueError:
        return False
    if not eq(binding.pk_recv, receiving_public(binding.k_recv)):
        return False
    return (binding.path.leaf == leaf
            and binding.path.verify()
            and binding.path.root == root)
