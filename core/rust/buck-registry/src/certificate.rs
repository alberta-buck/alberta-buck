//! Identity certificates -- registry-signed, ElGamal-sealed for the
//! client; mirrors `alberta_buck/registry/certificate.py` byte-for-byte,
//! wire formats included.
//!
//! A registry derives `m = H(canonical_identity) % ORDER` and
//! `M = m*G`, issues a signed certificate binding the KYC data to `M`,
//! and ElGamal-encrypts `M` itself under the client's key so the wallet
//! can store the ciphertext as-is and later CP-re-encrypt it -- the same
//! primitives the on-chain IdentityRegistry uses.

use buck_identity::keccak::{identity_scalar, keccak_raw, keccak_scalar};
use buck_identity::{
    elgamal, fr_mod, g1_add, g1_generator, g1_mul, scalar_from_be_bytes_mod_order, w_from_fr,
    G1w, IdError, Result, W256,
};

// ---------------------------------------------------------------------------
// Certificate
// ---------------------------------------------------------------------------

/// A registry-issued certificate binding KYC data to an identity point M.
///
/// `canonical_identity` is THE canonical JSON (sorted keys, compact,
/// raw UTF-8) hashing to `m`; construction validates `M == m*G` exactly
/// as the Python dataclass `__post_init__` does.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IdentityCertificate {
    pub registry_id: String,
    pub serial: u64,
    pub canonical_identity: String,
    pub m_point: G1w,
    pub issued_at: i64,
    pub expires_at: i64,
}

impl IdentityCertificate {
    pub fn new(
        registry_id: String,
        serial: u64,
        canonical_identity: String,
        m_point: G1w,
        issued_at: i64,
        expires_at: i64,
    ) -> Result<Self> {
        let m = identity_scalar(canonical_identity.as_bytes());
        let m_calc = g1_mul(&g1_generator(), &m)?;
        if m_calc != m_point {
            return Err(IdError("M does not match canonical_identity"));
        }
        Ok(IdentityCertificate {
            registry_id,
            serial,
            canonical_identity,
            m_point,
            issued_at,
            expires_at,
        })
    }

    /// keccak256 of the canonical identity data.
    pub fn identity_hash(&self) -> [u8; 32] {
        keccak_raw(self.canonical_identity.as_bytes())
    }

    /// The identity scalar `m`.
    pub fn m(&self) -> W256 {
        identity_scalar(self.canonical_identity.as_bytes())
    }

    /// Deterministic 32-byte signing hash over all certificate fields.
    ///
    /// Layout: `registry_id` (u32-BE length + UTF-8), `serial` (u64 BE),
    /// `identity_hash` (32B), `M.x`, `M.y` (32B each), `issued_at`,
    /// `expires_at` (i64 BE each); keccak256 of the concatenation.
    pub fn to_hash_bytes(&self) -> [u8; 32] {
        let rid = self.registry_id.as_bytes();
        let mut payload = Vec::with_capacity(4 + rid.len() + 8 + 32 + 64 + 16);
        payload.extend_from_slice(&(rid.len() as u32).to_be_bytes());
        payload.extend_from_slice(rid);
        payload.extend_from_slice(&self.serial.to_be_bytes());
        payload.extend_from_slice(&self.identity_hash());
        payload.extend_from_slice(&self.m_point.0);
        payload.extend_from_slice(&self.m_point.1);
        payload.extend_from_slice(&self.issued_at.to_be_bytes());
        payload.extend_from_slice(&self.expires_at.to_be_bytes());
        keccak_raw(&payload)
    }

    /// Canonical wire encoding (transport alongside the ElGamal ct).
    ///
    /// Layout: `registry_id` (u32 len + UTF-8), `serial` (u64 BE),
    /// `canonical_identity` (u32 len + UTF-8), `M.x`, `M.y`,
    /// `issued_at`, `expires_at` (i64 BE each).
    pub fn serialize(&self) -> Vec<u8> {
        let rid = self.registry_id.as_bytes();
        let ci = self.canonical_identity.as_bytes();
        let mut out = Vec::with_capacity(4 + rid.len() + 8 + 4 + ci.len() + 64 + 16);
        out.extend_from_slice(&(rid.len() as u32).to_be_bytes());
        out.extend_from_slice(rid);
        out.extend_from_slice(&self.serial.to_be_bytes());
        out.extend_from_slice(&(ci.len() as u32).to_be_bytes());
        out.extend_from_slice(ci);
        out.extend_from_slice(&self.m_point.0);
        out.extend_from_slice(&self.m_point.1);
        out.extend_from_slice(&self.issued_at.to_be_bytes());
        out.extend_from_slice(&self.expires_at.to_be_bytes());
        out
    }

    /// Parse the wire format produced by [`serialize`](Self::serialize)
    /// (validating `M` against the canonical identity, as the Python
    /// constructor does).
    pub fn deserialize(data: &[u8]) -> Result<Self> {
        let mut r = Reader::new(data);
        let rid_len = r.u32()? as usize;
        let registry_id = r.utf8(rid_len)?;
        let serial = r.u64()?;
        let ci_len = r.u32()? as usize;
        let canonical_identity = r.utf8(ci_len)?;
        let mx = r.w256()?;
        let my = r.w256()?;
        let issued_at = r.i64()?;
        let expires_at = r.i64()?;
        Self::new(
            registry_id,
            serial,
            canonical_identity,
            (mx, my),
            issued_at,
            expires_at,
        )
    }
}

/// Bounds-checked big-endian reader for the certificate wire formats.
struct Reader<'a> {
    data: &'a [u8],
    off: usize,
}

impl<'a> Reader<'a> {
    fn new(data: &'a [u8]) -> Self {
        Reader { data, off: 0 }
    }

    fn take(&mut self, n: usize) -> Result<&'a [u8]> {
        if self.off + n > self.data.len() {
            return Err(IdError("certificate wire data truncated"));
        }
        let s = &self.data[self.off..self.off + n];
        self.off += n;
        Ok(s)
    }

    fn u32(&mut self) -> Result<u32> {
        Ok(u32::from_be_bytes(self.take(4)?.try_into().unwrap()))
    }

    fn u64(&mut self) -> Result<u64> {
        Ok(u64::from_be_bytes(self.take(8)?.try_into().unwrap()))
    }

    fn i64(&mut self) -> Result<i64> {
        Ok(i64::from_be_bytes(self.take(8)?.try_into().unwrap()))
    }

    fn w256(&mut self) -> Result<W256> {
        Ok(self.take(32)?.try_into().unwrap())
    }

    fn utf8(&mut self, n: usize) -> Result<String> {
        String::from_utf8(self.take(n)?.to_vec()).map_err(|_| IdError("invalid UTF-8 in wire data"))
    }
}

// ---------------------------------------------------------------------------
// Registry Schnorr over an arbitrary 32-byte message
// ---------------------------------------------------------------------------

/// Schnorr proof over a 32-byte message hash, transcript-bound to the
/// registry id -- the certificate-signing sibling of the issuer batch
/// Schnorr.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegistrySchnorrProof {
    pub e: W256,
    pub s: W256,
    pub r: G1w,
}

/// Fiat-Shamir challenge: points `(pk, R)` then scalars
/// `(msg_hash % ORDER, registry_id-bytes % ORDER, chainid)`.
fn registry_schnorr_transcript(
    pk_registry: &G1w,
    r_point: &G1w,
    msg_hash: &[u8; 32],
    registry_id: &str,
    chainid: &W256,
) -> W256 {
    let msg_int = scalar_from_be_bytes_mod_order(msg_hash);
    let rid_int = scalar_from_be_bytes_mod_order(registry_id.as_bytes());
    keccak_scalar(&[
        pk_registry.0,
        pk_registry.1,
        r_point.0,
        r_point.1,
        msg_int,
        rid_int,
        *chainid,
    ])
}

/// Sign `msg_hash` under the registry key; `k` is the caller's nonce.
pub fn registry_schnorr_sign(
    sk_registry: &W256,
    msg_hash: &[u8; 32],
    registry_id: &str,
    chainid: &W256,
    k: &W256,
) -> Result<RegistrySchnorrProof> {
    let pk = g1_mul(&g1_generator(), sk_registry)?;
    let r_point = g1_mul(&g1_generator(), k)?;
    let e = registry_schnorr_transcript(&pk, &r_point, msg_hash, registry_id, chainid);
    let s = w_from_fr(&(fr_mod(k) + fr_mod(&e) * fr_mod(sk_registry)));
    Ok(RegistrySchnorrProof { e, s, r: r_point })
}

/// `s*G == R + e*pk` and the Fiat-Shamir challenge matches.
pub fn registry_schnorr_verify(
    pk_registry: &G1w,
    proof: &RegistrySchnorrProof,
    msg_hash: &[u8; 32],
    registry_id: &str,
    chainid: &W256,
) -> Result<bool> {
    let lhs = g1_mul(&g1_generator(), &proof.s)?;
    let rhs = g1_add(&proof.r, &g1_mul(pk_registry, &proof.e)?)?;
    if lhs != rhs {
        return Ok(false);
    }
    Ok(proof.e == registry_schnorr_transcript(pk_registry, &proof.r, msg_hash, registry_id, chainid))
}

// ---------------------------------------------------------------------------
// Signed certificate
// ---------------------------------------------------------------------------

/// A certificate plus the registry's Schnorr signature over
/// `cert.to_hash_bytes()`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SignedCertificate {
    pub cert: IdentityCertificate,
    pub signature: RegistrySchnorrProof,
    pub registry_pk: G1w,
}

impl SignedCertificate {
    /// Verify the signature (the Python `SignedCertificate.verify`).
    pub fn verify(&self, chainid: &W256) -> Result<bool> {
        registry_schnorr_verify(
            &self.registry_pk,
            &self.signature,
            &self.cert.to_hash_bytes(),
            &self.cert.registry_id,
            chainid,
        )
    }

    /// Wire format: `e, s, R.x, R.y, pk.x, pk.y` (32B BE each) then the
    /// certificate payload.
    pub fn serialize(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(192 + 64);
        out.extend_from_slice(&self.signature.e);
        out.extend_from_slice(&self.signature.s);
        out.extend_from_slice(&self.signature.r.0);
        out.extend_from_slice(&self.signature.r.1);
        out.extend_from_slice(&self.registry_pk.0);
        out.extend_from_slice(&self.registry_pk.1);
        out.extend_from_slice(&self.cert.serialize());
        out
    }

    pub fn deserialize(data: &[u8]) -> Result<Self> {
        if data.len() < 192 {
            return Err(IdError("certificate wire data truncated"));
        }
        let w = |i: usize| -> W256 { data[i * 32..(i + 1) * 32].try_into().unwrap() };
        Ok(SignedCertificate {
            signature: RegistrySchnorrProof {
                e: w(0),
                s: w(1),
                r: (w(2), w(3)),
            },
            registry_pk: (w(4), w(5)),
            cert: IdentityCertificate::deserialize(&data[192..])?,
        })
    }
}

/// Create a certificate and sign it; `k` is the Schnorr nonce.
#[allow(clippy::too_many_arguments)]
pub fn registry_sign_certificate(
    registry_sk: &W256,
    registry_id: &str,
    canonical_identity: &str,
    serial: u64,
    issued_at: i64,
    expires_at: i64,
    chainid: &W256,
    k: &W256,
) -> Result<SignedCertificate> {
    let m = identity_scalar(canonical_identity.as_bytes());
    let m_point = g1_mul(&g1_generator(), &m)?;
    let cert = IdentityCertificate::new(
        registry_id.to_string(),
        serial,
        canonical_identity.to_string(),
        m_point,
        issued_at,
        expires_at,
    )?;
    let signature = registry_schnorr_sign(registry_sk, &cert.to_hash_bytes(), registry_id, chainid, k)?;
    let registry_pk = g1_mul(&g1_generator(), registry_sk)?;
    Ok(SignedCertificate {
        cert,
        signature,
        registry_pk,
    })
}

/// Verify signature and M-consistency (the Python
/// `registry_verify_certificate`).
pub fn registry_verify_certificate(signed: &SignedCertificate, chainid: &W256) -> Result<bool> {
    let m = identity_scalar(signed.cert.canonical_identity.as_bytes());
    if g1_mul(&g1_generator(), &m)? != signed.cert.m_point {
        return Ok(false);
    }
    signed.verify(chainid)
}

// ---------------------------------------------------------------------------
// Sealed (ElGamal-encrypted) delivery
// ---------------------------------------------------------------------------

/// A signed certificate ElGamal-sealed for a specific client: the
/// plaintext is the certificate's attested identity point `M` itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SealedCertificate {
    /// `(R, C) = (r*G, M + r*pk_client)`.
    pub ct: (G1w, G1w),
    pub signed_cert: SignedCertificate,
}

impl SealedCertificate {
    /// Wire format: `R.x, R.y, C.x, C.y` (32B each), u32-BE signed-cert
    /// length, signed-cert bytes.
    pub fn envelope(&self) -> Vec<u8> {
        let sc = self.signed_cert.serialize();
        let mut out = Vec::with_capacity(128 + 4 + sc.len());
        out.extend_from_slice(&self.ct.0 .0);
        out.extend_from_slice(&self.ct.0 .1);
        out.extend_from_slice(&self.ct.1 .0);
        out.extend_from_slice(&self.ct.1 .1);
        out.extend_from_slice(&(sc.len() as u32).to_be_bytes());
        out.extend_from_slice(&sc);
        out
    }

    pub fn from_envelope(data: &[u8]) -> Result<Self> {
        if data.len() < 132 {
            return Err(IdError("certificate wire data truncated"));
        }
        let w = |i: usize| -> W256 { data[i * 32..(i + 1) * 32].try_into().unwrap() };
        let sc_len = u32::from_be_bytes(data[128..132].try_into().unwrap()) as usize;
        if data.len() < 132 + sc_len {
            return Err(IdError("certificate wire data truncated"));
        }
        Ok(SealedCertificate {
            ct: ((w(0), w(1)), (w(2), w(3))),
            signed_cert: SignedCertificate::deserialize(&data[132..132 + sc_len])?,
        })
    }
}

/// ElGamal-encrypt the certificate's `M` for the client; `r` is the
/// caller's ephemeral randomness.
pub fn seal_certificate(
    signed: &SignedCertificate,
    client_pk: &G1w,
    r: &W256,
) -> Result<SealedCertificate> {
    let ct = elgamal::elgamal_encrypt(&signed.cert.m_point, client_pk, r)?;
    Ok(SealedCertificate {
        ct,
        signed_cert: signed.clone(),
    })
}

/// Decrypt and confirm the recovered point matches the certificate's `M`.
pub fn unseal_certificate(sealed: &SealedCertificate, client_sk: &W256) -> Result<SignedCertificate> {
    let m_recovered = elgamal::elgamal_decrypt(&sealed.ct.0, &sealed.ct.1, client_sk)?;
    if m_recovered != sealed.signed_cert.cert.m_point {
        return Err(IdError(
            "decrypted M does not match certificate M -- envelope was not sealed for this client",
        ));
    }
    Ok(sealed.signed_cert.clone())
}
