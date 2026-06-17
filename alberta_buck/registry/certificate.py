"""Identity certificates -- registry-signed, ElGamal-encrypted for the client.

A Registry (government KYC authority) verifies identity data, derives the BN254
identity scalar m = H(canonical_identity) % ORDER and point M = m*G, then issues
a signed certificate binding the KYC data to M.  The certificate is ElGamal-
encrypted under the client's BN254 public key: the registry encrypts the identity
point M itself (the same M the certificate certifies) so the wallet can store the
ciphertext as-received and later decrypt it or Chaum-Pedersen re-encrypt it to
reveal to a counterparty -- the identical primitives used by the IdentityRegistry
(E_addr, verifyApprove, verifyDepositorForIssuer).

Encryption: pure ElGamal over BN254 G1 using the existing alberta_buck.wallet.elgamal
functions.  The plaintext is the certificate's attested identity point M.  The
ciphertext is (R=r*G, C=M + r*pk_client).  Only the holder of sk_client recovers
M = C - sk_client * R.  The signed certificate bytes travel alongside in the clear
(it is signed and tamper-evident; the ElGamal encryption proves delivery to the
named client).

Sealed envelope wire format:
    R.x (32B BE) || R.y (32B BE) || C.x (32B BE) || C.y (32B BE) ||
    signed_cert_len (u32 BE) || signed_cert_bytes

Reference: alberta-buck-notes.org ("Mutual Decryptability"); see also alberta-buck-notes-flow.org. The Registry-Identity
Accumulator" -- "commit-before-use" discipline.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional, Tuple

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, mul, neg, eq, rand_scalar, point_to_words, words_to_point,
)
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.transcript import keccak_bytes, keccak_scalar
from alberta_buck.wallet.elgamal import (
    ElGamalCiphertext,
    IdentityKeyPair,
    identity_keygen,
    elgamal_encrypt,
    elgamal_decrypt,
)


# ---------------------------------------------------------------------------
# Certificate data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IdentityCertificate:
    """A registry-issued certificate binding KYC data to an Identity point M.

    canonical_identity is the sorted-key, no-whitespace JSON that hashes to
    identity_hash (keccak256) and whose scalar m = identity_hash % ORDER yields
    M = m*G.  The recipient recomputes all three and verifies the registry's
    signature.

    Attributes:
        registry_id: Stable identifier for the issuing registry (e.g. "ca-bc-2026").
        serial: Unique per-registry serial number.
        canonical_identity: Sorted-key JSON, no whitespace.
        M: The identity point M = identity_scalar(canonical_identity) * G.
        issued_at: POSIX timestamp of issuance.
        expires_at: POSIX timestamp of expiry (0 = no expiry).
    """
    registry_id: str
    serial: int
    canonical_identity: str
    M: Tuple
    issued_at: int
    expires_at: int

    def __post_init__(self) -> None:
        M_calc = mul(G1, identity_scalar(self.canonical_identity))
        if not eq(self.M, M_calc):
            raise ValueError("M does not match canonical_identity")

    @property
    def identity_hash(self) -> bytes:
        """keccak256 of the canonical identity data."""
        from alberta_buck.wallet.transcript import keccak_raw
        return keccak_raw(self.canonical_identity.encode("utf-8"))

    @property
    def m(self) -> int:
        """The identity scalar."""
        return identity_scalar(self.canonical_identity)

    def to_hash_bytes(self) -> bytes:
        """Deterministic 32-byte hash of all certificate fields for signing.

        Layout: registry_id (utf-8, length-prefixed u32), serial (u64 BE),
        identity_hash (32 bytes), M.x, M.y (u256 BE each), issued_at (i64 BE),
        expires_at (i64 BE).  Hashed with keccak256.
        """
        from alberta_buck.wallet.transcript import keccak_raw
        rid = self.registry_id.encode("utf-8")
        Mx, My = point_to_words(self.M)
        payload = b"".join([
            struct.pack(">I", len(rid)), rid,
            struct.pack(">Q", self.serial),
            self.identity_hash,
            Mx.to_bytes(32, "big"),
            My.to_bytes(32, "big"),
            struct.pack(">q", self.issued_at),
            struct.pack(">q", self.expires_at),
        ])
        return keccak_raw(payload)

    def serialize(self) -> bytes:
        """Canonical byte encoding for transport alongside the ElGamal ciphertext.

        Layout: registry_id (u32 len + utf-8), serial (u64 BE),
        canonical_identity (u32 len + utf-8), M.x, M.y (u256 BE each),
        issued_at (i64 BE), expires_at (i64 BE).
        """
        rid = self.registry_id.encode("utf-8")
        Mx, My = point_to_words(self.M)
        ci = self.canonical_identity.encode("utf-8")
        return b"".join([
            struct.pack(">I", len(rid)), rid,
            struct.pack(">Q", self.serial),
            struct.pack(">I", len(ci)), ci,
            Mx.to_bytes(32, "big"),
            My.to_bytes(32, "big"),
            struct.pack(">q", self.issued_at),
            struct.pack(">q", self.expires_at),
        ])

    @classmethod
    def deserialize(cls, data: bytes) -> 'IdentityCertificate':
        """Parse a certificate from the wire format produced by serialize()."""
        offset = 0
        rid_len = struct.unpack_from(">I", data, offset)[0]; offset += 4
        registry_id = data[offset:offset + rid_len].decode("utf-8"); offset += rid_len
        serial = struct.unpack_from(">Q", data, offset)[0]; offset += 8
        ci_len = struct.unpack_from(">I", data, offset)[0]; offset += 4
        canonical_identity = data[offset:offset + ci_len].decode("utf-8"); offset += ci_len
        Mx = int.from_bytes(data[offset:offset + 32], "big"); offset += 32
        My = int.from_bytes(data[offset:offset + 32], "big"); offset += 32
        M = words_to_point(Mx, My)
        issued_at = struct.unpack_from(">q", data, offset)[0]; offset += 8
        expires_at = struct.unpack_from(">q", data, offset)[0]; offset += 8
        return cls(
            registry_id=registry_id, serial=serial,
            canonical_identity=canonical_identity, M=M,
            issued_at=issued_at, expires_at=expires_at,
        )


# ---------------------------------------------------------------------------
# Registry key material
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegistryKeyPair:
    """A registry's long-term signing key -- a standard BN254 keypair.

    The registry publishes pk; clients verify signatures against it.

    Attributes:
        sk: Secret key scalar.
        pk: Public key as a G1 point (sk * G).
    """
    sk: int
    pk: Tuple


def registry_keygen(rng=None) -> RegistryKeyPair:
    """Generate a new registry signing keypair.

    Args:
        rng: Optional callable returning an int in [0, 2**256) for deterministic
            key generation.  When None, uses secrets.randbelow (cryptographic).

    Returns:
        RegistryKeyPair with fresh random sk.
    """
    sk = rand_scalar(rng)
    return RegistryKeyPair(sk=sk, pk=mul(G1, sk))


# ---------------------------------------------------------------------------
# Schnorr signing (adapted from schnorr.py for arbitrary 32-byte messages)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegistrySchnorrProof:
    """Schnorr proof over an arbitrary 32-byte message.

    Compatible shape with alberta_buck.wallet.schnorr.SchnorrProof but the
    transcript binds the registry's identity rather than a note batch.

    Attributes:
        e: Fiat-Shamir challenge scalar.
        s: Response scalar (k + e * sk).
        R: Nonce commitment point (k * G).
    """
    e: int
    s: int
    R: Tuple


def _registry_schnorr_transcript(
    pk_registry, R, msg_hash: bytes, registry_id: str, chainid: int,
) -> int:
    """Fiat-Shamir challenge: points (pk_registry, R) then scalars (msg_hash, registry_id, chainid).

    Order matches the on-chain Fiat-Shamir convention in BN254.fsChallenge:
    each point contributes (X, Y) as u256 words, then scalars.
    """
    pkx, pky = point_to_words(pk_registry)
    Rx, Ry = point_to_words(R)
    rid_int = int.from_bytes(registry_id.encode("utf-8"), "big") % ORDER
    msg_int = int.from_bytes(msg_hash, "big") % ORDER
    return keccak_scalar(pkx, pky, Rx, Ry, msg_int, rid_int, chainid)


def registry_schnorr_sign(
    sk_registry: int,
    msg_hash: bytes,
    registry_id: str,
    chainid: int = 0,
    rng=None,
) -> RegistrySchnorrProof:
    """Sign a 32-byte message hash under the registry's BN254 key.

    Args:
        sk_registry: Registry secret key scalar.
        msg_hash: 32-byte message digest to sign.
        registry_id: Stable registry identifier (bound into transcript).
        chainid: EVM chain ID for domain separation (0 for off-chain-only).
        rng: Optional callable for deterministic nonce generation.

    Returns:
        RegistrySchnorrProof with (e, s, R).
    """
    pk_registry = mul(G1, sk_registry % ORDER)
    k = rand_scalar(rng)
    R = mul(G1, k)
    e = _registry_schnorr_transcript(pk_registry, R, msg_hash, registry_id, chainid)
    s = (k + e * (sk_registry % ORDER)) % ORDER
    return RegistrySchnorrProof(e=e, s=s, R=R)


def registry_schnorr_verify(
    pk_registry,
    proof: RegistrySchnorrProof,
    msg_hash: bytes,
    registry_id: str,
    chainid: int = 0,
) -> bool:
    """Verify a registry Schnorr proof.

    Args:
        pk_registry: Registry public key (G1 point).
        proof: The Schnorr proof to verify.
        msg_hash: 32-byte message digest that was signed.
        registry_id: Stable registry identifier.
        chainid: EVM chain ID used during signing.

    Returns:
        True iff s*G == R + e*pk_registry and the Fiat-Shamir challenge matches.
    """
    if not eq(mul(G1, proof.s), add(proof.R, mul(pk_registry, proof.e))):
        return False
    return proof.e == _registry_schnorr_transcript(
        pk_registry, proof.R, msg_hash, registry_id, chainid,
    )


# ---------------------------------------------------------------------------
# Signed certificate (cert + signature)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SignedCertificate:
    """A certificate with the registry's Schnorr signature.

    The signature covers cert.to_hash_bytes() -- a deterministic hash of all
    certificate fields.  Anyone with the registry's published pk can verify.

    Attributes:
        cert: The identity certificate.
        signature: Registry Schnorr proof over cert.to_hash_bytes().
        registry_pk: The registry's public key at signing time (G1 point).
    """
    cert: IdentityCertificate
    signature: RegistrySchnorrProof
    registry_pk: Tuple

    def verify(self, chainid: int = 0) -> bool:
        """Verify the signature and M consistency.

        Args:
            chainid: EVM chain ID used during signing.

        Returns:
            True iff signature is valid and M matches canonical_identity.
        """
        return registry_schnorr_verify(
            self.registry_pk, self.signature,
            self.cert.to_hash_bytes(), self.cert.registry_id, chainid,
        )

    def serialize(self) -> bytes:
        """Canonical wire format for transport.

        Layout: e (32B BE), s (32B BE), R.x (32B BE), R.y (32B BE),
        pk.x (32B BE), pk.y (32B BE), cert payload.
        """
        Rx, Ry = point_to_words(self.signature.R)
        Px, Py = point_to_words(self.registry_pk)
        header = b"".join([
            self.signature.e.to_bytes(32, "big"),
            self.signature.s.to_bytes(32, "big"),
            Rx.to_bytes(32, "big"),
            Ry.to_bytes(32, "big"),
            Px.to_bytes(32, "big"),
            Py.to_bytes(32, "big"),
        ])
        return header + self.cert.serialize()

    @classmethod
    def deserialize(cls, data: bytes) -> 'SignedCertificate':
        """Parse a signed certificate from wire format.

        Args:
            data: Bytes produced by serialize().

        Returns:
            SignedCertificate with signature and certificate fields populated.
        """
        e = int.from_bytes(data[0:32], "big")
        s = int.from_bytes(data[32:64], "big")
        Rx = int.from_bytes(data[64:96], "big")
        Ry = int.from_bytes(data[96:128], "big")
        R = words_to_point(Rx, Ry)
        Px = int.from_bytes(data[128:160], "big")
        Py = int.from_bytes(data[160:192], "big")
        registry_pk = words_to_point(Px, Py)
        sig = RegistrySchnorrProof(e=e, s=s, R=R)
        cert = IdentityCertificate.deserialize(data[192:])
        return cls(cert=cert, signature=sig, registry_pk=registry_pk)


# ---------------------------------------------------------------------------
# Convenience: create + sign in one call
# ---------------------------------------------------------------------------

def registry_sign_certificate(
    registry_sk: int,
    registry_id: str,
    canonical_identity: str,
    serial: int,
    issued_at: int,
    expires_at: int = 0,
    chainid: int = 0,
    rng=None,
) -> SignedCertificate:
    """Create a certificate and sign it with the registry's key.

    Args:
        registry_sk: Registry secret key scalar.
        registry_id: Stable registry identifier.
        canonical_identity: Sorted-key JSON identity data.
        serial: Unique certificate serial number.
        issued_at: POSIX timestamp.
        expires_at: POSIX timestamp (0 = no expiry).
        chainid: EVM chain ID for domain separation.
        rng: Optional callable for deterministic nonce.

    Returns:
        SignedCertificate ready for ElGamal-encrypted delivery.
    """
    M = mul(G1, identity_scalar(canonical_identity))
    cert = IdentityCertificate(
        registry_id=registry_id, serial=serial,
        canonical_identity=canonical_identity, M=M,
        issued_at=issued_at, expires_at=expires_at,
    )
    sig = registry_schnorr_sign(
        registry_sk, cert.to_hash_bytes(), registry_id, chainid, rng=rng,
    )
    pk_registry = mul(G1, registry_sk % ORDER)
    return SignedCertificate(cert=cert, signature=sig, registry_pk=pk_registry)


def registry_verify_certificate(signed: SignedCertificate, chainid: int = 0) -> bool:
    """Verify a signed certificate's signature and M-consistency.

    Args:
        signed: The signed certificate to verify.
        chainid: EVM chain ID used during signing.

    Returns:
        True iff the signature is valid and M matches the canonical identity.
    """
    M_calc = mul(G1, identity_scalar(signed.cert.canonical_identity))
    if not eq(signed.cert.M, M_calc):
        return False
    return signed.verify(chainid)


# ---------------------------------------------------------------------------
# ElGamal-encrypted delivery -- the identity point M as the plaintext
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SealedCertificate:
    """A signed certificate ElGamal-encrypted for a specific client.

    The registry encrypts the certificate's attested identity point M under
    the client's public key using the same elgamal_encrypt primitive used
    throughout the Identity layer (E_addr, issuer_reenc, deposit coupling).
    The ciphertext (R, C) = (r*G, M + r*pk_client) is stored directly by the
    wallet and can be decrypted with elgamal_decrypt or Chaum-Pedersen
    re-encrypted to a counterparty.

    The signed certificate bytes travel alongside in the clear: the certificate
    is signed and tamper-evident; the ElGamal ciphertext proves delivery to the
    named client (only sk_client recovers M from it).

    Attributes:
        ct: ElGamal ciphertext encrypting M under the client's public key.
        signed_cert: The signed certificate (unencrypted, signature-protected).
    """
    ct: ElGamalCiphertext
    signed_cert: SignedCertificate

    @property
    def envelope(self) -> bytes:
        """Wire format: R.x (32B) || R.y (32B) || C.x (32B) || C.y (32B) ||
        signed_cert_len (u32 BE) || signed_cert_bytes."""
        Rx, Ry = point_to_words(self.ct.R)
        Cx, Cy = point_to_words(self.ct.C)
        sc_bytes = self.signed_cert.serialize()
        return b"".join([
            Rx.to_bytes(32, "big"), Ry.to_bytes(32, "big"),
            Cx.to_bytes(32, "big"), Cy.to_bytes(32, "big"),
            struct.pack(">I", len(sc_bytes)), sc_bytes,
        ])

    @classmethod
    def from_envelope(cls, data: bytes) -> 'SealedCertificate':
        """Parse a sealed certificate from wire format.

        Args:
            data: Bytes produced by envelope.

        Returns:
            SealedCertificate with the ElGamal ciphertext and signed certificate.
        """
        Rx = int.from_bytes(data[0:32], "big")
        Ry = int.from_bytes(data[32:64], "big")
        Cx = int.from_bytes(data[64:96], "big")
        Cy = int.from_bytes(data[96:128], "big")
        R = words_to_point(Rx, Ry)
        C = words_to_point(Cx, Cy)
        ct = ElGamalCiphertext(R=R, C=C)
        sc_len = struct.unpack_from(">I", data, 128)[0]
        sc_bytes = data[132:132 + sc_len]
        signed_cert = SignedCertificate.deserialize(sc_bytes)
        return cls(ct=ct, signed_cert=signed_cert)


def seal_certificate(
    signed: SignedCertificate,
    client_pk,   # BN254 G1 point -- the client's identity public key
    rng=None,
) -> SealedCertificate:
    """ElGamal-encrypt the certificate's identity point M for the client.

    Uses the same elgamal_encrypt primitive as the rest of the Identity layer.
    The wallet stores the resulting ElGamalCiphertext as-is and can later
    decrypt it with elgamal_decrypt or Chaum-Pedersen re-encrypt it to a
    counterparty (matching the verifyApprove / verifyDepositorForIssuer pattern).

    Args:
        signed: The signed certificate to deliver.
        client_pk: Client's BN254 G1 public key.
        rng: Optional callable for deterministic ephemeral randomness.

    Returns:
        SealedCertificate with the ElGamal ciphertext and the signed certificate.
    """
    M = signed.cert.M
    r = rand_scalar(rng) if rng is not None else None
    # Use a deterministic r if rng was provided, otherwise random.
    if rng is None:
        r = rand_scalar()
    ct = elgamal_encrypt(M, client_pk, r)
    return SealedCertificate(ct=ct, signed_cert=signed)


def unseal_certificate(
    sealed: SealedCertificate,
    client_sk: int,
) -> SignedCertificate:
    """Decrypt the ElGamal ciphertext and verify it recovers the certificate's M.

    Uses elgamal_decrypt -- the same primitive the wallet uses for all identity
    ciphertexts.  Confirms that the decrypted point matches the certificate's
    attested M (proving the registry delivered this certificate to this client).

    Args:
        sealed: The sealed certificate from the registry.
        client_sk: Client's identity secret key scalar.

    Returns:
        The decrypted SignedCertificate.

    Raises:
        ValueError: If the decrypted M does not match the certificate's M
            (the envelope was not encrypted for this client, or is corrupted).
    """
    M_recovered = elgamal_decrypt(sealed.ct, client_sk)
    if not eq(M_recovered, sealed.signed_cert.cert.M):
        raise ValueError(
            "decrypted M does not match certificate M -- "
            "envelope was not sealed for this client"
        )
    return sealed.signed_cert


__all__ = [
    "IdentityCertificate",
    "SignedCertificate",
    "SealedCertificate",
    "RegistryKeyPair",
    "RegistrySchnorrProof",
    "registry_keygen",
    "registry_schnorr_sign",
    "registry_schnorr_verify",
    "registry_sign_certificate",
    "registry_verify_certificate",
    "seal_certificate",
    "unseal_certificate",
]
