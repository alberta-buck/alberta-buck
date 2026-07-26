//! THE canonical JSON dialect -- mirrors `alberta_buck/wallet/identity.py`.
//!
//! One dialect for every spec surface (the identity preimage, the
//! AB-RCPT/1 receipt core): sorted keys, compact separators, raw UTF-8,
//! values restricted to strings, integers, booleans and null (floats are
//! not canonical).  This is the output `json.dumps(obj, sort_keys=True,
//! separators=(",", ":"), ensure_ascii=False)` produces, which is also
//! what `JSON.stringify` and `serde_json` produce natively for in-dialect
//! values -- pinned across Rust/Python/JS by the canonicalization rows of
//! `core/vectors/wallet-kernel-vectors.json`.
//!
//! The writer here is explicit rather than delegated to `serde_json`'s
//! serializer so the escaping rules are pinned in one place: `"` and `\`
//! escaped, the C0 shorthands `\b \t \n \f \r`, every other control
//! character as lowercase `\u00xx`, and everything else -- including all
//! non-ASCII -- as raw UTF-8.  Integer tokens pass through exactly
//! (`arbitrary_precision` keeps their text), so integers of any size
//! survive; any non-integer number is rejected.

use serde_json::Value;

use buck_identity::keccak;
use buck_identity::{IdError, Result, W256};

/// Canonicalize a JSON text: parse, then re-emit in THE dialect.
pub fn canonical_json(text: &str) -> Result<String> {
    let v: Value =
        serde_json::from_str(text).map_err(|_| IdError("canonical_json: invalid JSON"))?;
    canonical_json_value(&v)
}

/// Canonical form of an already-parsed JSON value.
pub fn canonical_json_value(v: &Value) -> Result<String> {
    let mut out = String::new();
    write_canonical(v, &mut out)?;
    Ok(out)
}

/// `canonical_identity_data`: the identity dict's canonical JSON.  The
/// input must be a JSON object.
pub fn canonical_identity_data(fields_text: &str) -> Result<String> {
    let v: Value = serde_json::from_str(fields_text)
        .map_err(|_| IdError("canonical_identity_data: invalid JSON"))?;
    if !v.is_object() {
        return Err(IdError("canonical_identity_data: expected a JSON object"));
    }
    canonical_json_value(&v)
}

/// `m = keccak256(canonical) mod ORDER` over an already-canonical string
/// (`identity.identity_scalar` with a str argument).
pub fn identity_scalar_canonical(canonical: &str) -> W256 {
    keccak::identity_scalar(canonical.as_bytes())
}

/// `identity_scalar` over raw identity fields (canonicalizes first).
pub fn identity_scalar_fields(fields_text: &str) -> Result<W256> {
    Ok(identity_scalar_canonical(&canonical_identity_data(
        fields_text,
    )?))
}

fn write_canonical(v: &Value, out: &mut String) -> Result<()> {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => {
            // arbitrary_precision preserves the token text; the dialect
            // admits exactly the canonical integer grammar.
            let t = n.to_string();
            if !is_canonical_int(&t) {
                return Err(IdError("canonical_json: floats are not canonical"));
            }
            out.push_str(&t);
        }
        Value::String(s) => write_string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canonical(item, out)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            // serde_json's default Map is a BTreeMap: iteration is already
            // key-sorted, and UTF-8 byte order == code-point order, so this
            // matches Python's sort_keys exactly.
            out.push('{');
            for (i, (k, val)) in map.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(k, out);
                out.push(':');
                write_canonical(val, out)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

fn is_canonical_int(t: &str) -> bool {
    let d = t.strip_prefix('-').unwrap_or(t);
    if d.is_empty() || !d.bytes().all(|b| b.is_ascii_digit()) {
        return false;
    }
    // No leading zeros (except "0" itself), no "-0".
    if d.len() > 1 && d.starts_with('0') {
        return false;
    }
    !(t.starts_with('-') && d == "0")
}

/// Python `json.dumps(..., ensure_ascii=False)` string escaping.
fn write_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{0008}' => out.push_str("\\b"),
            '\u{0009}' => out.push_str("\\t"),
            '\u{000a}' => out.push_str("\\n"),
            '\u{000c}' => out.push_str("\\f"),
            '\u{000d}' => out.push_str("\\r"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
}
