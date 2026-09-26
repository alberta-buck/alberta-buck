//! Typed accessors over the receipt-core JSON tree.
//!
//! The AB-RCPT/2 core is a canonical JSON map; the verifiers walk it the
//! way the Python reference walks its dicts.  These helpers convert the
//! wallet's standard shapes -- `"0x..."` hex words, `{"x","y"}` G1
//! points, `{"R","C"}` ciphertexts -- with kernel errors on anything
//! missing or malformed.

use serde_json::Value;

use crate::{w_from_hex, Ctw, G1w, IdError, Result, W256};

pub fn get<'a>(v: &'a Value, key: &str) -> Result<&'a Value> {
    v.get(key).ok_or(IdError("receipt: missing field"))
}

/// Python's `_opt` / `.get(k)` idiom: absent, `null`, or an EMPTY dict
/// all read as `None`.
pub fn get_opt<'a>(v: &'a Value, key: &str) -> Option<&'a Value> {
    match v.get(key) {
        None | Some(Value::Null) => None,
        Some(Value::Object(m)) if m.is_empty() => None,
        Some(x) => Some(x),
    }
}

pub fn as_str(v: &Value) -> Result<&str> {
    v.as_str().ok_or(IdError("receipt: expected a string"))
}

pub fn get_str<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    as_str(get(v, key)?)
}

/// A `"0x..."` hex field as a word (`int(s, 16)`).
pub fn get_w(v: &Value, key: &str) -> Result<W256> {
    w_from_hex(as_str(get(v, key)?)?)
}

/// A `{"x": "0x..", "y": "0x.."}` G1 point.
pub fn as_g1(v: &Value) -> Result<G1w> {
    Ok((get_w(v, "x")?, get_w(v, "y")?))
}

pub fn get_g1(v: &Value, key: &str) -> Result<G1w> {
    as_g1(get(v, key)?)
}

/// A `{"R": {..}, "C": {..}}` ElGamal ciphertext.
pub fn as_ct(v: &Value) -> Result<Ctw> {
    Ok((get_g1(v, "R")?, get_g1(v, "C")?))
}

pub fn get_ct(v: &Value, key: &str) -> Result<Ctw> {
    as_ct(get(v, key)?)
}

/// A canonical-integer field as `u128` (the dialect's plain-int values:
/// `chainid`, `txn.value`, timestamps, blocks).
pub fn as_u128(v: &Value) -> Result<u128> {
    let n = v.as_number().ok_or(IdError("receipt: expected an integer"))?;
    n.to_string()
        .parse::<u128>()
        .map_err(|_| IdError("receipt: integer out of range"))
}

pub fn get_u128(v: &Value, key: &str) -> Result<u128> {
    as_u128(get(v, key)?)
}

/// A plain-int field as a word.
pub fn get_int_w(v: &Value, key: &str) -> Result<W256> {
    Ok(crate::w_from_u128(get_u128(v, key)?))
}

/// A decimal-string word -- the delivery and circuit-witness convention.
pub fn get_dec_w(v: &Value, key: &str) -> Result<W256> {
    crate::w_from_dec(as_str(get(v, key)?)?)
}

/// A `{"x": "<dec>", "y": "<dec>"}` G1 point.
pub fn as_g1_dec(v: &Value) -> Result<G1w> {
    Ok((get_dec_w(v, "x")?, get_dec_w(v, "y")?))
}

pub fn get_g1_dec(v: &Value, key: &str) -> Result<G1w> {
    as_g1_dec(get(v, key)?)
}

/// A decimal `{"R": {..}, "C": {..}}` ElGamal ciphertext.
pub fn get_ct_dec(v: &Value, key: &str) -> Result<Ctw> {
    let c = get(v, key)?;
    Ok((get_g1_dec(c, "R")?, get_g1_dec(c, "C")?))
}
