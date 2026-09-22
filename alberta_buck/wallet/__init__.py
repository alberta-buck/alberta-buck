"""Alberta Buck off-chain wallet reference.

Implements the cryptographic surface of the IdentityRegistry / BUCK identity layer:
PS issuance, ElGamal credential derivation, registration NIZK, Chaum-Pedersen
re-encryption proofs.  Doubles as the test-vector emitter for the Solidity tests.

The reference for the protocol details is alberta-buck-identity-example.org.
"""

from alberta_buck.wallet._kernel import kernel_active, backend
from alberta_buck.wallet.bn254 import (
    G1, G2, ORDER, add, mul, neg, eq, pairing, is_inf,
    point_to_words, words_to_point, scalar_to_word, word_to_scalar,
)
from alberta_buck.wallet.transcript import keccak_scalar, keccak_bytes
from alberta_buck.wallet.identity import (
    canonical_json, canonical_identity_data, identity_scalar,
)
from alberta_buck.wallet.ps import (
    PSKeyPair, PSSignature, PSPresentation,
    ps_keygen, ps_key_consistent, ps_sign, ps_verify, ps_rerandomize, ps_present,
)
from alberta_buck.wallet.elgamal import (
    ElGamalCiphertext, IdentityKeyPair, identity_keygen, elgamal_encrypt, elgamal_decrypt,
)
from alberta_buck.wallet.nizk import (
    RegistrationProof, registration_prove, registration_verify,
    bind_contract_prove, presentation_point,
)
from alberta_buck.wallet.contract_binding import (
    ContractBindingProof, contract_binding_prove, contract_binding_verify,
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
    IssuerReencProof, issuer_reenc_prove, issuer_reenc_verify,
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
    Issuer, IssuedCredential, present_for_registration,
)
from alberta_buck.wallet.salt import derive_salt, tree_tag, SALT_DOMAIN
from alberta_buck.wallet.attributes import (
    AttributeProof, prove_attributes, verify_attributes,
)
from alberta_buck.wallet.recvkey import (
    RECV_DOMAIN, ReceivingBinding,
    derive_receiving_secret, receiving_key, receiving_public,
    prove_receiving_binding, verify_receiving_binding,
)
from alberta_buck.wallet.deposit_fold import (
    DepositFoldRefused, DepositFoldWitness,
    deposit_fold_witness, deposit_fold_check,
)

__all__ = [
    "derive_salt", "tree_tag", "SALT_DOMAIN",
    "AttributeProof", "prove_attributes", "verify_attributes",
    "RECV_DOMAIN", "ReceivingBinding",
    "derive_receiving_secret", "receiving_key", "receiving_public",
    "prove_receiving_binding", "verify_receiving_binding",
    "DepositFoldRefused", "DepositFoldWitness",
    "deposit_fold_witness", "deposit_fold_check",
    "kernel_active", "backend",
    "G1", "G2", "ORDER", "add", "mul", "neg", "eq", "pairing", "is_inf",
    "point_to_words", "words_to_point", "scalar_to_word", "word_to_scalar",
    "keccak_scalar", "keccak_bytes",
    "canonical_json", "canonical_identity_data", "identity_scalar",
    "PSKeyPair", "PSSignature", "PSPresentation",
    "ps_keygen", "ps_key_consistent", "ps_sign", "ps_verify", "ps_rerandomize", "ps_present",
    "ElGamalCiphertext", "IdentityKeyPair", "identity_keygen",
    "elgamal_encrypt", "elgamal_decrypt",
    "RegistrationProof", "registration_prove", "registration_verify",
    "bind_contract_prove", "presentation_point",
    "ContractBindingProof", "contract_binding_prove", "contract_binding_verify",
    "CPProof", "chaum_pedersen_prove", "chaum_pedersen_verify",
    "poseidon", "F_R",
    "NoteOpening", "FLAVOR_A1", "FLAVOR_A2", "FLAVOR_B1",
    "NULLIFIER_TAG_A", "NULLIFIER_TAG_B",
    "note_commitment", "nullifier_a", "nullifier_b",
    "id_payload_a1", "id_payload_a2", "id_payload_b1",
    "id_hash_a1", "id_hash_a2", "id_hash_b1",
    "SchnorrProof", "batch_commitment", "issuer_schnorr_sign", "issuer_schnorr_verify",
    "VDProof", "verifiable_decrypt_prove", "verifiable_decrypt_verify",
    "IssuerReencProof", "issuer_reenc_prove", "issuer_reenc_verify",
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
    "Issuer", "IssuedCredential", "present_for_registration",
]
