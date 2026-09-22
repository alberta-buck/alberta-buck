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

Three tags live inside circuits and so move only with a trusted setup: the
hash-to-curve generator's, the Notes tree's zero, and the accumulator leaf
functions'.  They are listed at the end, with where they stand.
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

# ---- wire formats -----------------------------------------------------------

RECEIPT_ENVELOPE                = "AB-RCPT/2"

# ---- inside circuits: these move with the v2 trusted-setup round -----------
#
# PEDERSEN_H     "AlbertaBuck/Pedersen/H/v1" today.  Hash-to-curve domain of
#                H_PEDERSEN, whose powers table is compiled into the B1
#                membership circuit and will be into the A2 fold.
# NOTES_ZERO     "AlbertaBuck:Notes:zero" today.  The Notes tree's empty leaf,
#                compiled into every batch-mint circuit and Notes.sol.
# leaf tags      none today.  identity_leaf_salted and receiving_leaf are both
#                three-input Poseidons, so one value can be both kinds of leaf.
#                A leading field-element tag separates them; every gate hashes
#                a leaf.
# H_POINT        "AlbertaBuck:IssuerReenc:H" today, a known-log generator that
#                only the A2 mint binding uses.  Retired when that binding's
#                blinds move to H_PEDERSEN, rather than renamed.


__all__ = [
    "word",
    "IDENTITY_SCALAR",
    "FS_REGISTER", "FS_CONTRACT_BINDING", "FS_APPROVE", "FS_ISSUER_SCHNORR",
    "FS_ISSUER_REENC", "FS_DEPOSITOR_BINDING", "FS_VERIFIABLE_DECRYPT",
    "CONTRACT_BINDING_CONTROL",
    "ACCUMULATOR_SALT", "NOTES_RECEIVING_KEY", "NOTES_PAYLOAD_WRAP",
    "RECEIPT_ENVELOPE",
]
