//! The credential-issuer ceremony -- mirrors
//! `alberta_buck/wallet/issuer.py`'s `Issuer.issue()`.
//!
//! The issuer stamps its `issuer_id` into the identity record (so signed
//! credentials cannot lie about their signer), canonicalizes, computes
//! `m = H(canonical)`, signs `sigma = PS_sign(m)`, and -- when a
//! confidential delivery key is supplied -- ElGamal-encrypts `M = m*G`.
//! Revocation bookkeeping (the issuance log, the revoked set) is host
//! bookkeeping and stays in the language shims.
//!
//! Nonce order: `t_sig` (the PS `h = t*G1` nonce) then, when delivering,
//! the ElGamal `r` -- exactly the Python draw order.

use serde_json::Value;

use buck_identity::elgamal::elgamal_encrypt;
use buck_identity::keccak::identity_scalar;
use buck_identity::ps::{ps_sign, ps_verify};
use buck_identity::{g1_generator, g1_mul, G2w};

use crate::canonical::canonical_json_value;
use crate::{Ctw, G1w, IdError, Result, W256};

/// What the issuer hands back to a successful applicant.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IssuedCredential {
    /// The canonical JSON the m-hash was taken over.
    pub canonical: String,
    pub m: W256,
    /// `sigma = (h, (x + m*y)*h)` -- raw, not yet rerandomized.
    pub sigma: (G1w, G1w),
    /// Confidential delivery `(R, C) = (r*G, M + r*pk_applicant)`.
    pub delivery: Option<Ctw>,
}

/// Run the issuance ceremony over the applicant's identity fields
/// (a JSON object text).  `issuer_id` overwrites any submitted value.
pub fn issue_credential(
    sk_x: &W256,
    sk_y: &W256,
    issuer_id: &str,
    identity_fields_json: &str,
    t_sig: &W256,
    delivery: Option<(&G1w, &W256)>,
) -> Result<IssuedCredential> {
    let v: Value = serde_json::from_str(identity_fields_json)
        .map_err(|_| IdError("issuer: identity fields must be valid JSON"))?;
    let Value::Object(mut record) = v else {
        return Err(IdError("issuer: identity fields must be a JSON object"));
    };
    record.insert(
        "issuer_id".to_string(),
        Value::String(issuer_id.to_string()),
    );
    let canonical = canonical_json_value(&Value::Object(record))?;
    let m = identity_scalar(canonical.as_bytes());
    let sigma = ps_sign(sk_x, sk_y, &m, t_sig)?;

    let delivery = match delivery {
        Some((applicant_pk, r)) => {
            let m_pt = g1_mul(&g1_generator(), &m)?;
            Some(elgamal_encrypt(&m_pt, applicant_pk, r)?)
        }
        None => None,
    };

    Ok(IssuedCredential {
        canonical,
        m,
        sigma,
        delivery,
    })
}

/// Check `sigma` is valid for `m` under the issuer's PS public key (the
/// wallet's pre-rerandomization check; the `issuer_id` equality guard is
/// host bookkeeping).
pub fn verify_credential(
    pk_x: &G2w,
    pk_y: &G2w,
    sigma: &(G1w, G1w),
    m: &W256,
) -> Result<bool> {
    ps_verify(pk_x, pk_y, &sigma.0, &sigma.1, m)
}
