"""Every domain tag the v2 protocol hashes, in one place.

A domain tag names the protocol, the version and the purpose of a hash, so
that no two uses of the same hash function over the same bytes can ever be
confused.  Without one, a transcript of one sigma protocol is structurally a
candidate transcript of another, and the identity scalar of a person is
whatever keccak of those bytes means anywhere else.

The convention, one rule per kind of use:

  * Tags read ``AlbertaBuck/<Area>/<Name>/v2`` -- slash-separated, version
    last, and the version is the PROTOCOL's, not a revision count per
    primitive.  Nothing earlier is deployed, so nothing earlier is kept.
  * A Fiat-Shamir transcript contains exactly one domain word,
    ``uint256(keccak256(tag))``, at a fixed position.  New tags go LAST, the
    position the registration transcript already used, so neither
    ``keccak_scalar`` nor ``BN254.fsChallenge`` changes shape.  The
    contract-binding transcript keeps its leading position.
  * Derived keys, masks, salts and the identity scalar hash the tag as BYTES
    ahead of their payload.
  * Wire formats carry the version in their header (``AB-RCPT/2``).

Mirrored by ``core/rust/buck-identity/src/domains.rs`` and by the constants in
``src/IdentityRegistry.sol``.  A tag changed here and not there is a transcript
that verifies nowhere, which the kernel vectors and the Forge suite both catch.

Three kinds of tag live inside circuits and so moved only with a trusted setup:
the hash-to-curve generator's, the Notes tree's zero, and the accumulator leaf
functions'.  Poseidon commitments take their tag as a leading field element,
``keccak(tag) mod F_R`` (:func:`field_tag`).
"""

from __future__ import annotations

from alberta_buck.wallet.transcript import keccak_raw


def word(tag: bytes) -> int:
    """The full keccak word of a tag, as a transcript entry.  NOT reduced mod
    ORDER: it is metadata folded into a hash, not a scalar."""
    return int.from_bytes(keccak_raw(tag), "big")


# ---- the identity -----------------------------------------------------------

#: m = keccak(IDENTITY_SCALAR || canonical record) mod ORDER.  The one hash in
#: the system that maps a person into the group, and so the one most worth
#: keeping out of any other protocol's range.
IDENTITY_SCALAR                 = b"AlbertaBuck/Identity/Scalar/v2"

# ---- Fiat-Shamir transcripts, named by the contract that verifies them ------

FS_REGISTER                     = b"AlbertaBuck/FiatShamir/IdentityRegistry/Register/v2"
FS_CONTRACT_BINDING             = b"AlbertaBuck/FiatShamir/IdentityRegistry/ContractBinding/v2"
FS_APPROVE                      = b"AlbertaBuck/FiatShamir/IdentityRegistry/Approve/v2"
FS_ISSUER_SCHNORR               = b"AlbertaBuck/FiatShamir/IdentityRegistry/IssuerSchnorr/v2"
FS_ISSUER_REENC                 = b"AlbertaBuck/FiatShamir/IdentityRegistry/IssuerReenc/v2"
FS_DEPOSITOR_BINDING            = b"AlbertaBuck/FiatShamir/IdentityRegistry/DepositorBinding/v2"
#: Verified off chain, by a receipt checker; no contract carries it.
FS_VERIFIABLE_DECRYPT           = b"AlbertaBuck/FiatShamir/Receipt/VerifiableDecrypt/v2"

# ---- authorizations that are hashes, not proofs ----------------------------

CONTRACT_BINDING_CONTROL        = b"AlbertaBuck/IdentityRegistry/ContractBindingControl/v2"

# ---- derivations ------------------------------------------------------------

ACCUMULATOR_SALT                = b"AlbertaBuck/Accumulator/Salt/v2"
NOTES_RECEIVING_KEY             = b"AlbertaBuck/Notes/ReceivingKey/v2"
NOTES_PAYLOAD_WRAP              = b"AlbertaBuck/Notes/PayloadWrap/v2"

# ---- constants compiled into circuits -----------------------------------------
#
# These move only with a trusted setup, and moved in the v2 setup round.

#: Hash-to-curve domain of H_PEDERSEN, the one hiding generator with no known
#: logarithm.  Its powers table is compiled into the B1 membership circuit and
#: the A2 fold, and the A2 mint binding blinds on it.
PEDERSEN_H                      = b"AlbertaBuck/Pedersen/H/v2"
#: The Notes tree's empty leaf, keccak(tag) mod F_R: compiled into every
#: batch-mint circuit and into Notes.sol.
NOTES_ZERO                      = b"AlbertaBuck/Notes/Zero/v2"
#: The accumulator leaf functions' leading field-element tags.  Two of the four
#: are three-input Poseidons, so without a tag one value can be both kinds of
#: leaf; the tag makes each leaf kind its own function.
LEAF_IDENTITY                   = b"AlbertaBuck/Accumulator/Leaf/Identity/v2"
LEAF_IDENTITY_SALTED            = b"AlbertaBuck/Accumulator/Leaf/IdentitySalted/v2"
LEAF_RECEIVING                  = b"AlbertaBuck/Accumulator/Leaf/Receiving/v2"
LEAF_MAILBOX                    = b"AlbertaBuck/Accumulator/Leaf/Mailbox/v2"


def field_tag(tag: bytes) -> int:
    """A tag as a Poseidon input: keccak(tag) mod F_R, the native field."""
    from alberta_buck.wallet.poseidon import F_R
    return int.from_bytes(keccak_raw(tag), "big") % F_R


# ---- wire formats -----------------------------------------------------------

RECEIPT_ENVELOPE                = "AB-RCPT/2"

# ---- retired -----------------------------------------------------------------
#
# H_POINT ("AlbertaBuck:IssuerReenc:H") was a generator with a KNOWN logarithm
# that the A2 mint binding blinded on.  A known-log blind binds nothing, which
# is how a minter could key one ciphertext to two issuers; the binding now
# blinds on H_PEDERSEN and the tag is gone.  alberta_buck/review keeps its own
# copy, because a known logarithm is exactly what that evidence demonstrates.


__all__ = [
    "word",
    "IDENTITY_SCALAR",
    "FS_REGISTER", "FS_CONTRACT_BINDING", "FS_APPROVE", "FS_ISSUER_SCHNORR",
    "FS_ISSUER_REENC", "FS_DEPOSITOR_BINDING", "FS_VERIFIABLE_DECRYPT",
    "CONTRACT_BINDING_CONTROL",
    "ACCUMULATOR_SALT", "NOTES_RECEIVING_KEY", "NOTES_PAYLOAD_WRAP",
    "PEDERSEN_H", "NOTES_ZERO",
    "LEAF_IDENTITY", "LEAF_IDENTITY_SALTED", "LEAF_RECEIVING", "LEAF_MAILBOX", "field_tag",
    "RECEIPT_ENVELOPE",
]
