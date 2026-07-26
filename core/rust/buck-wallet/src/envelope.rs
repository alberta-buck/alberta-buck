//! The AB-RCPT/1 receipt envelope -- mirrors
//! `alberta_buck/wallet/envelope.py` byte-for-byte.
//!
//! A receipt core is a single canonical JSON map (THE dialect); its
//! serialization IS the receipt.  The receipt id is
//! `base32(sha256(canonical_bytes))` truncated; the printable envelope is
//! `AB-RCPT/1.` + wrapped base64url + `.END`.

use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::canonical::canonical_json_value;
use crate::{IdError, Result};

pub const ENVELOPE_HEADER: &str = "AB-RCPT/1.";
pub const ENVELOPE_FOOTER: &str = ".END";

/// Canonical JSON bytes of a receipt core.
pub fn serialize_core(core: &Value) -> Result<Vec<u8>> {
    if !core.is_object() {
        return Err(IdError("receipt core must be a JSON object"));
    }
    Ok(canonical_json_value(core)?.into_bytes())
}

/// Parse canonical bytes back to the JSON tree.
pub fn deserialize_core(canonical_bytes: &[u8]) -> Result<Value> {
    let text = std::str::from_utf8(canonical_bytes)
        .map_err(|_| IdError("receipt core is not UTF-8"))?;
    let v: Value =
        serde_json::from_str(text).map_err(|_| IdError("receipt core is not valid JSON"))?;
    if !v.is_object() {
        return Err(IdError("receipt core must be a JSON object"));
    }
    Ok(v)
}

/// `base32(sha256(canonical_bytes))` lowercased, unpadded, truncated to
/// `prefix_len` -- the receipt handle.
pub fn receipt_id(canonical_bytes: &[u8], prefix_len: usize) -> String {
    let digest = Sha256::digest(canonical_bytes);
    let b32 = base32_encode(&digest);
    b32.to_lowercase().chars().take(prefix_len).collect()
}

/// Wrap to `width` columns.
pub fn wrap_text(payload: &str, width: usize) -> String {
    payload
        .as_bytes()
        .chunks(width.max(1))
        .map(|c| std::str::from_utf8(c).unwrap())
        .collect::<Vec<_>>()
        .join("\n")
}

/// The printable receipt envelope.
pub fn envelope_text(canonical_bytes: &[u8], width: usize) -> String {
    let b64 = base64url_encode(canonical_bytes);
    format!(
        "{}\n{}\n{}",
        ENVELOPE_HEADER,
        wrap_text(&b64, width),
        ENVELOPE_FOOTER
    )
}

/// Extract the canonical bytes from an envelope: everything between the
/// header and footer, whitespace stripped, base64url-decoded.
pub fn parse_envelope(text: &str) -> Result<Vec<u8>> {
    let text = text.replace("\r\n", "\n");
    let start = text
        .find(ENVELOPE_HEADER)
        .ok_or(IdError("envelope: missing AB-RCPT/1. header or .END footer"))?;
    let after = start + ENVELOPE_HEADER.len();
    let end = text[after..]
        .find(ENVELOPE_FOOTER)
        .map(|i| after + i)
        .ok_or(IdError("envelope: missing AB-RCPT/1. header or .END footer"))?;
    let b64: String = text[after..end].chars().filter(|c| !c.is_whitespace()).collect();
    base64url_decode(&b64)
}

// ---------------------------------------------------------------------------
// base32 (RFC 4648) / base64url -- hand-rolled, vector-pinned
// ---------------------------------------------------------------------------

const B32_ALPHABET: &[u8; 32] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

fn base32_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(5) * 8);
    for chunk in data.chunks(5) {
        let mut buf = [0u8; 5];
        buf[..chunk.len()].copy_from_slice(chunk);
        let v = ((buf[0] as u64) << 32)
            | ((buf[1] as u64) << 24)
            | ((buf[2] as u64) << 16)
            | ((buf[3] as u64) << 8)
            | (buf[4] as u64);
        let n_chars = [0, 2, 4, 5, 7, 8][chunk.len()];
        for i in 0..n_chars {
            let idx = ((v >> (35 - 5 * i)) & 0x1f) as usize;
            out.push(B32_ALPHABET[idx] as char);
        }
        // Padding is stripped by the caller (receipt_id rstrips '=');
        // emit none.
    }
    out
}

const B64URL_ALPHABET: &[u8; 64] =
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

fn base64url_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let mut buf = [0u8; 3];
        buf[..chunk.len()].copy_from_slice(chunk);
        let v = ((buf[0] as u32) << 16) | ((buf[1] as u32) << 8) | (buf[2] as u32);
        let n_chars = [0, 2, 3, 4][chunk.len()];
        for i in 0..n_chars {
            let idx = ((v >> (18 - 6 * i)) & 0x3f) as usize;
            out.push(B64URL_ALPHABET[idx] as char);
        }
    }
    out // unpadded, matching `.rstrip("=")`
}

fn b64url_val(c: u8) -> Result<u32> {
    Ok(match c {
        b'A'..=b'Z' => (c - b'A') as u32,
        b'a'..=b'z' => (c - b'a' + 26) as u32,
        b'0'..=b'9' => (c - b'0' + 52) as u32,
        b'-' => 62,
        b'_' => 63,
        _ => return Err(IdError("envelope: invalid base64url")),
    })
}

fn base64url_decode(s: &str) -> Result<Vec<u8>> {
    // Accept unpadded input (the Python side re-pads then decodes).
    let s = s.trim_end_matches('=');
    let bytes = s.as_bytes();
    if bytes.len() % 4 == 1 {
        return Err(IdError("envelope: invalid base64url"));
    }
    let mut out = Vec::with_capacity(bytes.len() * 3 / 4);
    for chunk in bytes.chunks(4) {
        let mut v = 0u32;
        for (i, c) in chunk.iter().enumerate() {
            v |= b64url_val(*c)? << (18 - 6 * i);
        }
        let n_bytes = [0, 0, 1, 2, 3][chunk.len()];
        for i in 0..n_bytes {
            out.push(((v >> (16 - 8 * i)) & 0xff) as u8);
        }
    }
    Ok(out)
}
