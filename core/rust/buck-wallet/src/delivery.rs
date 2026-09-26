//! The delivery -- what an addressed note carries from minter to recipient.
//! Mirrors `alberta_buck/wallet/delivery.py` field for field.
//!
//! The recipient must be able to spend from the delivery alone, and the channel
//! that carries it must learn nothing from it.  So every secret scalar -- the
//! face, `rho` (which with `idHash` IS the nullifier), the mint randomness, and
//! for A2 the issuer's naming salt and the binding's blind `gamma` -- travels
//! WRAPPED to the mailbox key, one mask per field, from the shared point
//! `S = r_note * pk_recv = k * eNote.R`.  The ciphertexts, A2's `T`, an A1
//! issuer's Schnorr and the predicate travel in the clear: none opens without
//! `k`.
//!
//! The delivery is a document, not kernel ABI, so its words are DECIMAL
//! strings, as the reference emits them; the functions' own arguments and
//! results keep the kernel's hex convention.

use serde_json::{json, Map, Value};

use buck_identity::elgamal::elgamal_decrypt;
use buck_identity::notes::{id_hash_a1, id_hash_a2, FLAVOR_A1, FLAVOR_A2};
use buck_identity::recvkey::{
    mailbox_shared_minter, mailbox_shared_recipient, unwrap_scalar, wrap_scalar,
};
use buck_identity::nums::h_pedersen;
use buck_identity::{g1_add, g1_generator, g1_mul};

use crate::jsonv::{get_ct_dec, get_dec_w, get_g1_dec};
use crate::{dec_w, hex_w, Ctw, G1w, IdError, NoteOpening, Result, W256};

fn pt_dec(p: &G1w) -> Value {
    json!({"x": dec_w(&p.0), "y": dec_w(&p.1)})
}

fn ct_dec(c: &Ctw) -> Value {
    json!({"R": pt_dec(&c.0), "C": pt_dec(&c.1)})
}

fn pt_hex(p: &G1w) -> Value {
    json!({"x": hex_w(&p.0), "y": hex_w(&p.1)})
}

fn ct_hex(c: &Ctw) -> Value {
    json!({"R": pt_hex(&c.0), "C": pt_hex(&c.1)})
}

fn wrapped(d: &mut Map<String, Value>, s: &G1w, fields: &[(&str, &W256)]) {
    for (name, val) in fields {
        let blob = wrap_scalar(val, s, name.as_bytes());
        d.insert(format!("{name}Wrapped"), Value::String(dec_w(&blob)));
    }
}

fn unwrap(d: &Value, s: &G1w, name: &str) -> Result<W256> {
    let blob = get_dec_w(d, &format!("{name}Wrapped"))
        .map_err(|_| IdError("the delivery is missing a wrapped field"))?;
    Ok(unwrap_scalar(&blob, s, name.as_bytes()))
}

fn flavor_of(d: &Value) -> u64 {
    d.get("flavor").and_then(Value::as_u64).unwrap_or(u64::MAX)
}

/// `k` must open `eNote` to `v*G`, and -- when the delivery carries it --
/// `r_note` must be `eNote`'s randomness.
fn check_value(e_note: &Ctw, k: &W256, v: &W256, r_note: Option<&W256>) -> Result<()> {
    if let Some(rn) = r_note {
        if g1_mul(&g1_generator(), rn)? != e_note.0 {
            return Err(IdError("the unwrapped r_note is not eNote's randomness"));
        }
    }
    if elgamal_decrypt(&e_note.0, &e_note.1, k)? != g1_mul(&g1_generator(), v)? {
        return Err(IdError(
            "k does not open eNote to the stated face: addressed to another mailbox, or corrupted",
        ));
    }
    Ok(())
}

fn opening_json(o: &NoteOpening) -> Result<Value> {
    Ok(json!({
        "flavor": o.flavor,
        "v": hex_w(&o.v),
        "rho": hex_w(&o.rho),
        "idHash": hex_w(&o.id_hash),
        "predicate": hex_w(&o.predicate),
        "cm": hex_w(&o.commitment()?),
    }))
}

// ---- the minter's side ------------------------------------------------------

/// The delivery for an A1 note.  `eRec`'s randomness does not travel: the
/// recipient opens `eRec` with `k`, and only an issuer-side receipt needs it.
pub fn deliver_a1(
    e_note: &Ctw,
    e_rec: &Ctw,
    v: &W256,
    rho: &W256,
    predicate: &W256,
    r_note: &W256,
    pk_recv: &G1w,
) -> Result<Value> {
    let s = mailbox_shared_minter(r_note, pk_recv)?;
    let mut d = Map::new();
    d.insert("flavor".into(), json!(FLAVOR_A1));
    d.insert("predicate".into(), Value::String(dec_w(predicate)));
    d.insert("eNote".into(), ct_dec(e_note));
    d.insert("eRec".into(), ct_dec(e_rec));
    wrapped(&mut d, &s, &[("rho", rho), ("v", v), ("rNote", r_note)]);
    Ok(Value::Object(d))
}

/// The delivery for an A2 note.  Without the issuer's naming salt the
/// recipient cannot prove the issuer registered, and without `gamma` it cannot
/// open the binding's `T` that `idHash` commits: either way, it cannot spend.
#[allow(clippy::too_many_arguments)]
pub fn deliver_a2(
    e_note: &Ctw,
    e_iss: &Ctw,
    t: &G1w,
    v: &W256,
    rho: &W256,
    predicate: &W256,
    r_note: &W256,
    r_prime: &W256,
    salt_iss: &W256,
    gamma: &W256,
    pk_recv: &G1w,
) -> Result<Value> {
    let s = mailbox_shared_minter(r_note, pk_recv)?;
    let mut d = Map::new();
    d.insert("flavor".into(), json!(FLAVOR_A2));
    d.insert("predicate".into(), Value::String(dec_w(predicate)));
    d.insert("eNote".into(), ct_dec(e_note));
    d.insert("eIss".into(), ct_dec(e_iss));
    d.insert("T".into(), pt_dec(t));
    wrapped(
        &mut d,
        &s,
        &[
            ("rho", rho),
            ("v", v),
            ("rPrime", r_prime),
            ("saltIss", salt_iss),
            ("gamma", gamma),
        ],
    );
    Ok(Value::Object(d))
}

// ---- the recipient's side ---------------------------------------------------

/// Open an A1 delivery with the mailbox secret `k`.  `m_issuer` is the public
/// issuer's identity scalar, which `idHash` commits: a delivery claiming a
/// different issuer recomputes to a different note.
pub fn open_a1(d: &Value, k: &W256, m_issuer: &W256) -> Result<Value> {
    if flavor_of(d) != FLAVOR_A1 {
        return Err(IdError("not an A1 delivery"));
    }
    let e_note = get_ct_dec(d, "eNote")?;
    let e_rec = get_ct_dec(d, "eRec")?;
    let s = mailbox_shared_recipient(k, &e_note.0)?;
    let rho = unwrap(d, &s, "rho")?;
    let v = unwrap(d, &s, "v")?;
    let r_note = unwrap(d, &s, "rNote")?;
    check_value(&e_note, k, &v, Some(&r_note))?;
    let opening = NoteOpening {
        flavor: FLAVOR_A1,
        v,
        rho,
        id_hash: id_hash_a1(&e_note, m_issuer)?,
        predicate: get_dec_w(d, "predicate")?,
    };
    Ok(json!({
        "opening": opening_json(&opening)?,
        "eNote": ct_hex(&e_note),
        "eRec": ct_hex(&e_rec),
        "r_note": hex_w(&r_note),
    }))
}

/// Open an A2 delivery with the mailbox secret `k`.  Checks that the unwrapped
/// `r'` is `eIss`'s randomness and that `T == k*eIss.R + gamma*H` -- the fold's
/// note tie and key tie, so a wrong `r'` or `gamma` would otherwise surface only
/// as a proof that will not build.  A `T` keyed to some other point is what a
/// minter framing a sock puppet would have to send, and it is refused here.
pub fn open_a2(d: &Value, k: &W256) -> Result<Value> {
    if flavor_of(d) != FLAVOR_A2 {
        return Err(IdError("not an A2 delivery"));
    }
    let e_note = get_ct_dec(d, "eNote")?;
    let e_iss = get_ct_dec(d, "eIss")?;
    let t = get_g1_dec(d, "T")?;
    let s = mailbox_shared_recipient(k, &e_note.0)?;
    let rho = unwrap(d, &s, "rho")?;
    let v = unwrap(d, &s, "v")?;
    let r_prime = unwrap(d, &s, "rPrime")?;
    let salt_iss = unwrap(d, &s, "saltIss")?;
    let gamma = unwrap(d, &s, "gamma")?;
    check_value(&e_note, k, &v, None)?;
    if g1_mul(&g1_generator(), &r_prime)? != e_iss.0 {
        return Err(IdError("the unwrapped r' is not eIss's randomness"));
    }
    if t != g1_add(&g1_mul(&e_iss.0, k)?, &g1_mul(&h_pedersen(), &gamma)?)? {
        return Err(IdError("T does not open to this mailbox under the unwrapped gamma"));
    }
    let opening = NoteOpening {
        flavor: FLAVOR_A2,
        v,
        rho,
        id_hash: id_hash_a2(&e_note, &e_iss, &t)?,
        predicate: get_dec_w(d, "predicate")?,
    };
    Ok(json!({
        "opening": opening_json(&opening)?,
        "eNote": ct_hex(&e_note),
        "eIss": ct_hex(&e_iss),
        "M_I": pt_hex(&elgamal_decrypt(&e_iss.0, &e_iss.1, k)?),
        "r_prime": hex_w(&r_prime),
        "salt_iss": hex_w(&salt_iss),
        "T": pt_hex(&t),
        "gamma": hex_w(&gamma),
    }))
}
