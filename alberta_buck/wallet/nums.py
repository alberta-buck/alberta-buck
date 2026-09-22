"""The one hiding generator: H_PEDERSEN, whose discrete log no one knows.

Every blind in the protocol that is opened by more than one proof sits on this
generator, and in the protocol every blind is.  Two defects found the rule:

*B1's commitment.*  B1 publishes ``P_dep = M_dep + b*H`` and proves two things
about it: a sigma that opens it as ``m_dep*G + b*H``, and a membership proof
that opens it as ``M + b'*H`` for a registered ``M``.  The composition is
sound only if the two openings must agree, and with a known ``h = log_G(H)``
they need not.  An adversary holding any registered identity scalar ``m'`` --
which counterparties hold by design, since the identity is a disclosed read
capability -- sets

    b' = b + (m_dep - m') / h

and the membership half then speaks about ``m'`` while the sigma half speaks
about its own ``m_dep``.  That is review finding 5.

*A2's mint binding.*  The binding blinds ``T = r'*pk + gamma*H``, and the A2
fold opens it again against the recipient's own key.  A blind on a known-log
generator was once defended as harmless "inside one sigma", because the
extractor recovers both openings; but the value crosses into the fold, and
there a known ``h`` lets a minter pay any difference of Identities in gamma.
That is the A2 key split (doc/review/notes-receiving-key.org, section 4.6).

So there is no second, cheaper generator.  :data:`H_PEDERSEN` is derived by
hashing to the curve rather than by multiplying ``G``, which is exactly what a
Pedersen commitment requires of its second generator, and why it is named for
one.  ``alberta_buck.review.known_log`` keeps the retired known-log generator
for the evidence that needs it.

Derivation: try-and-increment, which is the standard construction for a FIXED
public parameter.  Constant-time hashing to the curve matters when the input is
a secret; here the input is a domain string, the output is computed once, and
the only property required is that the result be a curve point nobody chose.
"""

from __future__ import annotations

from typing import Tuple

from py_ecc import bn128 as _bc

from alberta_buck.wallet.domains import PEDERSEN_H as H_PEDERSEN_DOMAIN
from alberta_buck.wallet.transcript import keccak_raw

__all__ = ["Q", "H_PEDERSEN_DOMAIN", "hash_to_curve_g1", "H_PEDERSEN"]


# The BN254 BASE field (coordinates), not the scalar field.
Q = _bc.field_modulus


def hash_to_curve_g1(domain: bytes, limit: int = 256) -> Tuple:
    """Hash ``domain`` to a G1 point by try-and-increment.

    Candidate x-coordinates are ``keccak(domain || counter)``; the first one
    for which ``x^3 + 3`` is a quadratic residue gives the point, taking the
    even ``y`` so the result is canonical.

    ``q = 3 (mod 4)``, so the square root is one exponentiation, and squaring
    the result back is the residue test.

    Args:
        domain: The domain-separating string.  Changing it changes the point.
        limit: How many counters to try.  Each has probability about 1/2 of
            succeeding, so exhausting 256 of them is not a thing that happens.

    Returns:
        A point on BN254 G1 whose discrete log with respect to ``G`` nobody
        knows, because it was never computed as a multiple of ``G``.

    Raises:
        RuntimeError: if no counter under ``limit`` yields a residue.
    """
    for ctr in range(limit):
        x = int.from_bytes(
            keccak_raw(domain + ctr.to_bytes(4, "big")), "big"
        ) % Q
        y2 = (pow(x, 3, Q) + 3) % Q
        y = pow(y2, (Q + 1) // 4, Q)          # q = 3 mod 4
        if (y * y) % Q != y2:
            continue                          # x^3 + 3 was not a residue
        if y % 2 == 1:
            y = Q - y                         # canonical: the even root
        return (_bc.FQ(x), _bc.FQ(y))
    raise RuntimeError(f"no curve point for domain {domain!r} under {limit} tries")


# The Pedersen generator.  Used wherever a commitment must be opened by two
# different proofs that have to agree -- today that is B1's P_dep, and nothing
# else: neither folded deposit gate needs a hiding point at all, because
# folding removed the second proof there was one to share with.
H_PEDERSEN = hash_to_curve_g1(H_PEDERSEN_DOMAIN)
