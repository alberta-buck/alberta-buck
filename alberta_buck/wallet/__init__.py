"""Alberta Buck off-chain wallet reference.

Implements the cryptographic surface of the IdentityRegistry / BUCK identity layer:
PS issuance, ElGamal credential derivation, registration NIZK, Chaum-Pedersen
re-encryption proofs.  Doubles as the test-vector emitter for the Solidity tests.

The reference for the protocol details is alberta-buck-identity-example.org.
"""

from alberta_buck.wallet.bn254 import (
    G1, G2, ORDER, add, mul, neg, eq, pairing, is_inf,
    point_to_words, words_to_point, scalar_to_word, word_to_scalar,
)
from alberta_buck.wallet.transcript import keccak_scalar, keccak_bytes
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.ps import (
    PSKeyPair, PSSignature, ps_keygen, ps_sign, ps_verify, ps_rerandomize,
)
from alberta_buck.wallet.elgamal import (
    ElGamalCiphertext, IdentityKeyPair, identity_keygen, elgamal_encrypt, elgamal_decrypt,
)
from alberta_buck.wallet.nizk import (
    RegistrationProof, registration_prove, registration_verify,
)
from alberta_buck.wallet.chaum_pedersen import (
    CPProof, chaum_pedersen_prove, chaum_pedersen_verify,
)
from alberta_buck.wallet.poseidon import poseidon, F_R
from alberta_buck.wallet.notes import (
    NoteOpening, FLAVOR_A1, FLAVOR_A2, FLAVOR_B1,
    NULLIFIER_TAG_A, NULLIFIER_TAG_B,
    note_commitment, nullifier_a, nullifier_b,
    id_payload_a1, id_payload_a2, id_payload_b1,
    id_hash_a1, id_hash_a2, id_hash_b1,
)
from alberta_buck.wallet.schnorr import (
    SchnorrProof, batch_commitment, issuer_schnorr_sign, issuer_schnorr_verify,
)
from alberta_buck.wallet.verifiable_decrypt import (
    VDProof, verifiable_decrypt_prove, verifiable_decrypt_verify,
)
from alberta_buck.wallet.issuer_reenc import (
    IssuerReencProof, issuer_reenc_prove, issuer_reenc_verify, H_POINT,
)
from alberta_buck.wallet.receipt import (
    RegisteredIdentity, Receipt, RcptResult, receipt_verify,
    ApproveReceipt, approve_receipt_verify,
)
from alberta_buck.wallet.envelope import (
    PartyRecord, TxnRecord, ReceiptCore,
    serialize_core, deserialize_core,
    envelope_text, parse_envelope, receipt_id,
)
from alberta_buck.wallet.build_receipt import (
    build_eoa_pub, build_eoa_priv,
    build_note_b1, build_note_a1, build_note_a2,
)
from alberta_buck.wallet.verify_receipt import verify_receipt as verify_receipt_core
from alberta_buck.wallet.render import (
    Detail, StyledLine, ReceiptSection, ReceiptDoc,
    Driver, TextDriver, render_receipt,
)
from alberta_buck.wallet.issuer import (
    Issuer, IssuedCredential, rerandomize_for_registration,
)
from alberta_buck.wallet.spend_a import (
    TREE_DEPTH, ZERO_VALUE, EMPTY_ROOT,
    MerkleTree, merkle_walk,
    SpendAWitness, make_spend_a_witness, spend_a_satisfied,
)

__all__ = [
    "G1", "G2", "ORDER", "add", "mul", "neg", "eq", "pairing", "is_inf",
    "point_to_words", "words_to_point", "scalar_to_word", "word_to_scalar",
    "keccak_scalar", "keccak_bytes",
    "canonical_identity_data", "identity_scalar",
    "PSKeyPair", "PSSignature", "ps_keygen", "ps_sign", "ps_verify", "ps_rerandomize",
    "ElGamalCiphertext", "IdentityKeyPair", "identity_keygen",
    "elgamal_encrypt", "elgamal_decrypt",
    "RegistrationProof", "registration_prove", "registration_verify",
    "CPProof", "chaum_pedersen_prove", "chaum_pedersen_verify",
    "poseidon", "F_R",
    "NoteOpening", "FLAVOR_A1", "FLAVOR_A2", "FLAVOR_B1",
    "NULLIFIER_TAG_A", "NULLIFIER_TAG_B",
    "note_commitment", "nullifier_a", "nullifier_b",
    "id_payload_a1", "id_payload_a2", "id_payload_b1",
    "id_hash_a1", "id_hash_a2", "id_hash_b1",
    "SchnorrProof", "batch_commitment", "issuer_schnorr_sign", "issuer_schnorr_verify",
    "VDProof", "verifiable_decrypt_prove", "verifiable_decrypt_verify",
    "IssuerReencProof", "issuer_reenc_prove", "issuer_reenc_verify", "H_POINT",
    "RegisteredIdentity", "Receipt", "RcptResult", "receipt_verify",
    "ApproveReceipt", "approve_receipt_verify",
    "PartyRecord", "TxnRecord", "ReceiptCore",
    "serialize_core", "deserialize_core",
    "envelope_text", "parse_envelope", "receipt_id",
    "build_eoa_pub", "build_eoa_priv",
    "build_note_b1", "build_note_a1", "build_note_a2",
    "verify_receipt_core",
    "Detail", "StyledLine", "ReceiptSection", "ReceiptDoc",
    "Driver", "TextDriver", "render_receipt",
    "Issuer", "IssuedCredential", "rerandomize_for_registration",
    "TREE_DEPTH", "ZERO_VALUE", "EMPTY_ROOT",
    "MerkleTree", "merkle_walk",
    "SpendAWitness", "make_spend_a_witness", "spend_a_satisfied",
]
