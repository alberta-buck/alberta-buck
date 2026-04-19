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
]
