//! ReceiptCore builders -- mirror `alberta_buck/wallet/build_receipt.py`
//! (and the record helpers of `envelope.py`) byte-for-byte.
//!
//! Every builder returns the receipt-core JSON tree; serialize it with
//! [`crate::envelope::serialize_core`].  Nonces are explicit trailing
//! arguments in exactly the order the Python reference draws them from
//! its rng (one verifiable-decryption `t` per self-naming proof).
//!
//! Hex convention: every scalar/coordinate field is
//! `bn254.scalar_to_hex` -- reduced mod ORDER then full-width lowercase
//! hex.  (The Python `_g1_hex` routes point COORDINATES through the same
//! reduction; an Fq value in `[r, q)` would alias, with probability
//! ~2^-128 -- mirrored here for byte-compatibility.)

use serde_json::{json, Map, Value};

use buck_identity::chaum_pedersen::CpProof;
use buck_identity::issuer_reenc::IssuerReencProof;
use buck_identity::schnorr::SchnorrProof;
use buck_identity::verifiable_decrypt::{verifiable_decrypt_prove, VdProof};

use crate::{scalar_hex, w_from_u64, Ctw, G1w, IdError, NoteOpening, Result, W256};

// ---------------------------------------------------------------------------
// Record helpers (envelope.py's _g1_hex / _ct_hex / *_record family)
// ---------------------------------------------------------------------------

pub fn g1_hex(p: &G1w) -> Value {
    json!({"x": scalar_hex(&p.0), "y": scalar_hex(&p.1)})
}

pub fn ct_hex(ct: &Ctw) -> Value {
    json!({"R": g1_hex(&ct.0), "C": g1_hex(&ct.1)})
}

fn num_u128(v: u128) -> Value {
    Value::Number(
        serde_json::from_str::<serde_json::Number>(&v.to_string())
            .expect("u128 decimal is a valid JSON number"),
    )
}

/// A verifiable-decryption proof record over `(E_ct, M)` naming `account`.
pub fn vd_proof_record(
    e_ct: &Ctw,
    m_named: &G1w,
    account: &W256,
    chainid: u64,
    proof: &VdProof,
) -> Value {
    json!({
        "E_ct": ct_hex(e_ct),
        "M_named": g1_hex(m_named),
        "account": scalar_hex(account),
        "chainid": chainid,
        "proof": {
            "e": scalar_hex(&proof.e),
            "s": scalar_hex(&proof.s),
            "T1": g1_hex(&proof.t1),
            "T2": g1_hex(&proof.t2),
        },
    })
}

/// A Chaum-Pedersen approve record (re-encryption of a registered
/// credential).
#[allow(clippy::too_many_arguments)]
pub fn cp_proof_record(
    e_sender: &Ctw,
    e_spender: &Ctw,
    pk_sender: &G1w,
    pk_spender: &G1w,
    sender: &W256,
    spender: &W256,
    chainid: u64,
    nonce: &W256,
    proof: &CpProof,
) -> Value {
    json!({
        "E_a": ct_hex(e_sender),
        "E_b": ct_hex(e_spender),
        "pk_a": g1_hex(pk_sender),
        "pk_b": g1_hex(pk_spender),
        "sender": scalar_hex(sender),
        "spender": scalar_hex(spender),
        "chainid": chainid,
        "protocol": "AlbertaBuck:Approve:v3",
        "nonce": scalar_hex(nonce),
        "proof": {
            "e": scalar_hex(&proof.e),
            "s1": scalar_hex(&proof.s1),
            "s2": scalar_hex(&proof.s2),
            "T1": g1_hex(&proof.t1),
            "T2": g1_hex(&proof.t2),
            "T3": g1_hex(&proof.t3),
        },
    })
}

/// The A2 issuer re-encryption binding record.
pub fn issuer_reenc_record(proof: &IssuerReencProof) -> Value {
    json!({
        "e": scalar_hex(&proof.e),
        "s_r": scalar_hex(&proof.s_r),
        "s_b": scalar_hex(&proof.s_b),
        "s_s": scalar_hex(&proof.s_s),
        "s_g": scalar_hex(&proof.s_g),
        "A1": g1_hex(&proof.a1),
        "A2": g1_hex(&proof.a2),
        "A3": g1_hex(&proof.a3),
        "A4": g1_hex(&proof.a4),
        "A5": g1_hex(&proof.a5),
        "Q": g1_hex(&proof.q),
        "U": g1_hex(&proof.u),
        "T": g1_hex(&proof.t),
    })
}

/// The Identity-M-bound note payload (idHash preimage material).
pub fn note_payload_record(
    e_note: Option<&Ctw>,
    e_rec: Option<&Ctw>,
    e_iss: Option<&Ctw>,
    sigma_r: Option<&G1w>,
    sigma_s: Option<&W256>,
    e_dep_for_iss: Option<&Ctw>,
) -> Value {
    let mut d = Map::new();
    if let Some(e) = e_note {
        d.insert("eNote".into(), ct_hex(e));
    }
    if let Some(e) = e_rec {
        d.insert("eRec".into(), ct_hex(e));
    }
    if let Some(e) = e_iss {
        d.insert("eIss".into(), ct_hex(e));
    }
    if let Some(r) = sigma_r {
        d.insert("sigma_R".into(), g1_hex(r));
    }
    if let Some(s) = sigma_s {
        d.insert("sigma_s".into(), Value::String(scalar_hex(s)));
    }
    if let Some(e) = e_dep_for_iss {
        d.insert("eDepForIss".into(), ct_hex(e));
    }
    Value::Object(d)
}

/// The Note opening/anchor record (`issuer_sig` present for B1/A1).
pub fn receipts_proof_record(
    opening: &NoteOpening,
    cms: &[W256],
    issuer_sig: Option<&SchnorrProof>,
    nullifier: &W256,
    face: &W256,
) -> Value {
    let mut d = Map::new();
    d.insert(
        "opening".into(),
        json!({
            "flavor": scalar_hex(&w_from_u64(opening.flavor)),
            "v": scalar_hex(&opening.v),
            "rho": scalar_hex(&opening.rho),
            "idHash": scalar_hex(&opening.id_hash),
            "predicate": scalar_hex(&opening.predicate),
        }),
    );
    d.insert(
        "cms".into(),
        Value::Array(cms.iter().map(|c| Value::String(scalar_hex(c))).collect()),
    );
    d.insert("nullifier".into(), Value::String(scalar_hex(nullifier)));
    d.insert("face".into(), Value::String(scalar_hex(face)));
    if let Some(sig) = issuer_sig {
        d.insert(
            "issuer_sig".into(),
            json!({
                "e": scalar_hex(&sig.e),
                "s": scalar_hex(&sig.s),
                "R": g1_hex(&sig.r),
            }),
        );
    }
    Value::Object(d)
}

fn party_record(
    addr: &W256,
    kind: &str,
    identity: &str,
    m_pt: &G1w,
    pk: &G1w,
    e_addr: Option<&Ctw>,
) -> Value {
    let mut d = Map::new();
    d.insert("addr".into(), Value::String(scalar_hex(addr)));
    d.insert("kind".into(), Value::String(kind.into()));
    d.insert("identity".into(), Value::String(identity.into()));
    d.insert("M".into(), g1_hex(m_pt));
    d.insert("pk".into(), g1_hex(pk));
    if let Some(e) = e_addr {
        d.insert("E_addr".into(), ct_hex(e));
    }
    Value::Object(d)
}

#[allow(clippy::too_many_arguments)]
fn txn_record(
    kind: &str,
    value: u128,
    timestamp: u64,
    event: &str,
    txhash: &str,
    block: u64,
    logindex: u64,
    mint: Option<(&str, u64, &W256)>,
) -> Value {
    let mut d = Map::new();
    d.insert("kind".into(), Value::String(kind.into()));
    d.insert("value".into(), num_u128(value));
    d.insert("timestamp".into(), json!(timestamp));
    d.insert("event".into(), Value::String(event.into()));
    d.insert("txhash".into(), Value::String(txhash.into()));
    d.insert("block".into(), json!(block));
    d.insert("logindex".into(), json!(logindex));
    if let Some((mint_txhash, mint_block, nullifier)) = mint {
        d.insert("mint_txhash".into(), Value::String(mint_txhash.into()));
        d.insert("mint_block".into(), json!(mint_block));
        d.insert("nullifier".into(), Value::String(scalar_hex(nullifier)));
    }
    Value::Object(d)
}

/// Self-naming: prove one's own registered `E_addr` decrypts to one's M.
fn self_vd(
    e_own: &Ctw,
    sk_own: &W256,
    m_own: &G1w,
    own_addr: &W256,
    chainid: u64,
    t: &W256,
) -> Result<Value> {
    let vd = verifiable_decrypt_prove(e_own, sk_own, m_own, own_addr, &w_from_u64(chainid), t)?;
    Ok(vd_proof_record(e_own, m_own, own_addr, chainid, &vd))
}

fn check_role(role: &str) -> Result<()> {
    if role != "recipient" && role != "issuer" {
        return Err(IdError("unknown receipt role"));
    }
    Ok(())
}

fn core_base(
    typ: &str,
    chainid: u64,
    contracts: &Value,
    payer: Value,
    payee: Value,
    txn: Value,
    role: &str,
) -> Map<String, Value> {
    let mut d = Map::new();
    d.insert("v".into(), json!(1));
    d.insert("type".into(), Value::String(typ.into()));
    d.insert("chainid".into(), json!(chainid));
    d.insert("contracts".into(), contracts.clone());
    d.insert("payer".into(), payer);
    d.insert("payee".into(), payee);
    d.insert("txn".into(), txn);
    d.insert("role".into(), Value::String(role.into()));
    d
}

fn add_notes(d: &mut Map<String, Value>, notes: Option<&[String]>) {
    if let Some(ns) = notes {
        d.insert(
            "notes".into(),
            Value::Array(ns.iter().map(|s| Value::String(s.clone())).collect()),
        );
    }
}

// ---------------------------------------------------------------------------
// eoa-pub
// ---------------------------------------------------------------------------

/// EOA transfer from a public counterparty; the payee self-names
/// (`t_self` is the one vd nonce the Python reference draws).
#[allow(clippy::too_many_arguments)]
pub fn build_eoa_pub(
    chainid: u64,
    contracts: &Value,
    payer_addr: &W256,
    payer_identity: &str,
    payer_m: &G1w,
    payer_pk: &G1w,
    payee_addr: &W256,
    payee_identity: &str,
    payee_m: &G1w,
    payee_pk: &G1w,
    payee_sk: &W256,
    payee_e_addr: &Ctw,
    value: u128,
    block_time: u64,
    txhash: &str,
    block: u64,
    logindex: u64,
    notes: Option<&[String]>,
    t_self: &W256,
) -> Result<Value> {
    let vd_self = self_vd(payee_e_addr, payee_sk, payee_m, payee_addr, chainid, t_self)?;
    let mut d = core_base(
        "eoa-pub",
        chainid,
        contracts,
        party_record(payer_addr, "public", payer_identity, payer_m, payer_pk, None),
        party_record(
            payee_addr,
            "private",
            payee_identity,
            payee_m,
            payee_pk,
            Some(payee_e_addr),
        ),
        txn_record(
            "eoa-transfer",
            value,
            block_time,
            "Transfer",
            txhash,
            block,
            logindex,
            None,
        ),
        "recipient",
    );
    d.insert("payee_vd".into(), vd_self);
    add_notes(&mut d, notes);
    Ok(Value::Object(d))
}

// ---------------------------------------------------------------------------
// eoa-priv
// ---------------------------------------------------------------------------

/// EOA transfer from a private counterparty: the approve handshake names
/// the payer; nonce order is `t_vd_payer` then `t_self`.
#[allow(clippy::too_many_arguments)]
pub fn build_eoa_priv(
    chainid: u64,
    contracts: &Value,
    payer_addr: &W256,
    payer_identity: &str,
    payer_m: &G1w,
    payer_pk: &G1w,
    payer_e_addr: &Ctw,
    e_for_payee: &Ctw,
    cp_proof: &CpProof,
    payee_addr: &W256,
    payee_identity: &str,
    payee_m: &G1w,
    payee_pk: &G1w,
    payee_sk: &W256,
    payee_e_addr: &Ctw,
    value: u128,
    block_time: u64,
    txhash: &str,
    block: u64,
    logindex: u64,
    approve_nonce: &W256,
    notes: Option<&[String]>,
    t_vd_payer: &W256,
    t_self: &W256,
) -> Result<Value> {
    let vd_payer = verifiable_decrypt_prove(
        e_for_payee,
        payee_sk,
        payer_m,
        payee_addr,
        &w_from_u64(chainid),
        t_vd_payer,
    )?;
    let vd_payer_rec = vd_proof_record(e_for_payee, payer_m, payee_addr, chainid, &vd_payer);
    let ap_rec = cp_proof_record(
        payer_e_addr,
        e_for_payee,
        payer_pk,
        payee_pk,
        payer_addr,
        payee_addr,
        chainid,
        approve_nonce,
        cp_proof,
    );
    let vd_self = self_vd(payee_e_addr, payee_sk, payee_m, payee_addr, chainid, t_self)?;

    let mut d = core_base(
        "eoa-priv",
        chainid,
        contracts,
        party_record(
            payer_addr,
            "private",
            payer_identity,
            payer_m,
            payer_pk,
            Some(payer_e_addr),
        ),
        party_record(
            payee_addr,
            "private",
            payee_identity,
            payee_m,
            payee_pk,
            Some(payee_e_addr),
        ),
        txn_record(
            "eoa-transfer",
            value,
            block_time,
            "Transfer",
            txhash,
            block,
            logindex,
            None,
        ),
        "recipient",
    );
    d.insert(
        "proof".into(),
        json!({"approve": ap_rec, "vd_payer": vd_payer_rec}),
    );
    d.insert("payee_vd".into(), vd_self);
    add_notes(&mut d, notes);
    Ok(Value::Object(d))
}

// ---------------------------------------------------------------------------
// note-b1
// ---------------------------------------------------------------------------

/// Bearer note from a public issuer, either party's side.  `t_vd` is the
/// single vd nonce (payee self-naming for `role="recipient"`, the
/// issuer's depositor naming for `role="issuer"`).
#[allow(clippy::too_many_arguments)]
pub fn build_note_b1(
    chainid: u64,
    contracts: &Value,
    issuer_addr: &W256,
    issuer_identity: &str,
    issuer_m: &G1w,
    issuer_pk: &G1w,
    payee_addr: &W256,
    payee_identity: &str,
    payee_m: &G1w,
    payee_pk: &G1w,
    opening: &NoteOpening,
    cms: &[W256],
    issuer_sig: &SchnorrProof,
    sigma_r: &G1w,
    sigma_s: &W256,
    nullifier: &W256,
    face: &W256,
    value: u128,
    block_time: u64,
    txhash: &str,
    block: u64,
    logindex: u64,
    mint_txhash: &str,
    mint_block: u64,
    role: &str,
    payee_sk: Option<&W256>,
    payee_e_addr: Option<&Ctw>,
    e_dep_for_iss: Option<&Ctw>,
    issuer_sk: Option<&W256>,
    notes: Option<&[String]>,
    t_vd: &W256,
) -> Result<Value> {
    check_role(role)?;

    let rec_proof = receipts_proof_record(opening, cms, Some(issuer_sig), nullifier, face);
    let payload = note_payload_record(None, None, None, Some(sigma_r), Some(sigma_s), e_dep_for_iss);

    let mut payee_vd_rec = None;
    let mut vd_payee_rec = None;
    if role == "recipient" {
        let (Some(sk), Some(e_addr)) = (payee_sk, payee_e_addr) else {
            return Err(IdError(
                "note-b1 recipient receipt needs payee_sk + payee_E_addr",
            ));
        };
        payee_vd_rec = Some(self_vd(e_addr, sk, payee_m, payee_addr, chainid, t_vd)?);
    } else {
        let (Some(e_dep), Some(sk)) = (e_dep_for_iss, issuer_sk) else {
            return Err(IdError("note-b1 issuer receipt needs eDepForIss + issuer_sk"));
        };
        let vd = verifiable_decrypt_prove(
            e_dep,
            sk,
            payee_m,
            issuer_addr,
            &w_from_u64(chainid),
            t_vd,
        )?;
        vd_payee_rec = Some(vd_proof_record(e_dep, payee_m, issuer_addr, chainid, &vd));
    }

    let mut d = core_base(
        "note-b1",
        chainid,
        contracts,
        party_record(issuer_addr, "public", issuer_identity, issuer_m, issuer_pk, None),
        party_record(
            payee_addr,
            "private",
            payee_identity,
            payee_m,
            payee_pk,
            payee_e_addr,
        ),
        txn_record(
            "note-spend",
            value,
            block_time,
            "SpentCoupledB1",
            txhash,
            block,
            logindex,
            Some((mint_txhash, mint_block, nullifier)),
        ),
        role,
    );
    d.insert("note".into(), payload);
    d.insert("proof".into(), rec_proof);
    if let Some(v) = payee_vd_rec {
        d.insert("payee_vd".into(), v);
    }
    if let Some(v) = vd_payee_rec {
        d.insert("vd_payee".into(), v);
    }
    add_notes(&mut d, notes);
    Ok(Value::Object(d))
}

// ---------------------------------------------------------------------------
// note-a1
// ---------------------------------------------------------------------------

/// Addressed note from a public issuer, either party's side.  `t_vd` is
/// required only for `role="recipient"` (the issuer side draws nothing).
#[allow(clippy::too_many_arguments)]
pub fn build_note_a1(
    chainid: u64,
    contracts: &Value,
    issuer_addr: &W256,
    issuer_identity: &str,
    issuer_m: &G1w,
    issuer_pk: &G1w,
    payee_addr: &W256,
    payee_identity: &str,
    payee_m: &G1w,
    payee_pk: &G1w,
    opening: &NoteOpening,
    cms: &[W256],
    issuer_sig: &SchnorrProof,
    e_note: &Ctw,
    e_rec: &Ctw,
    sigma_r: &G1w,
    sigma_s: &W256,
    nullifier: &W256,
    face: &W256,
    value: u128,
    block_time: u64,
    txhash: &str,
    block: u64,
    logindex: u64,
    mint_txhash: &str,
    mint_block: u64,
    role: &str,
    payee_sk: Option<&W256>,
    payee_e_addr: Option<&Ctw>,
    notes: Option<&[String]>,
    t_vd: Option<&W256>,
) -> Result<Value> {
    check_role(role)?;

    let rec_proof = receipts_proof_record(opening, cms, Some(issuer_sig), nullifier, face);
    let payload = note_payload_record(
        Some(e_note),
        Some(e_rec),
        None,
        Some(sigma_r),
        Some(sigma_s),
        None,
    );

    let mut payee_vd_rec = None;
    if role == "recipient" {
        let (Some(sk), Some(e_addr), Some(t)) = (payee_sk, payee_e_addr, t_vd) else {
            return Err(IdError(
                "note-a1 recipient receipt needs payee_sk + payee_E_addr",
            ));
        };
        payee_vd_rec = Some(self_vd(e_addr, sk, payee_m, payee_addr, chainid, t)?);
    }

    let mut d = core_base(
        "note-a1",
        chainid,
        contracts,
        party_record(issuer_addr, "public", issuer_identity, issuer_m, issuer_pk, None),
        party_record(
            payee_addr,
            "private",
            payee_identity,
            payee_m,
            payee_pk,
            payee_e_addr,
        ),
        txn_record(
            "note-spend",
            value,
            block_time,
            "SpentCoupledA1",
            txhash,
            block,
            logindex,
            Some((mint_txhash, mint_block, nullifier)),
        ),
        role,
    );
    d.insert("note".into(), payload);
    d.insert("proof".into(), rec_proof);
    if let Some(v) = payee_vd_rec {
        d.insert("payee_vd".into(), v);
    }
    add_notes(&mut d, notes);
    Ok(Value::Object(d))
}

// ---------------------------------------------------------------------------
// note-a2
// ---------------------------------------------------------------------------

/// Addressed note from a *private* issuer, either party's side.  `t_vd`
/// is the single self-naming nonce (payee for recipient role, the
/// private issuer for issuer role).
#[allow(clippy::too_many_arguments)]
pub fn build_note_a2(
    chainid: u64,
    contracts: &Value,
    issuer_addr: &W256,
    issuer_identity: &str,
    issuer_m: &G1w,
    issuer_pk: &G1w,
    issuer_e_addr: &Ctw,
    payee_addr: &W256,
    payee_identity: &str,
    payee_m: &G1w,
    payee_pk: &G1w,
    opening: &NoteOpening,
    cms: &[W256],
    e_note: &Ctw,
    e_iss: &Ctw,
    nullifier: &W256,
    face: &W256,
    value: u128,
    block_time: u64,
    txhash: &str,
    block: u64,
    logindex: u64,
    mint_txhash: &str,
    mint_block: u64,
    binding: Option<&IssuerReencProof>,
    role: &str,
    payee_sk: Option<&W256>,
    payee_e_addr: Option<&Ctw>,
    issuer_sk: Option<&W256>,
    notes: Option<&[String]>,
    t_vd: &W256,
) -> Result<Value> {
    check_role(role)?;

    let rec_proof = receipts_proof_record(opening, cms, None, nullifier, face);
    let payload = note_payload_record(Some(e_note), None, Some(e_iss), None, None, None);

    let mut payee_vd_rec = None;
    let mut payer_vd_rec = None;
    if role == "recipient" {
        let (Some(sk), Some(e_addr)) = (payee_sk, payee_e_addr) else {
            return Err(IdError(
                "note-a2 recipient receipt needs payee_sk + payee_E_addr",
            ));
        };
        payee_vd_rec = Some(self_vd(e_addr, sk, payee_m, payee_addr, chainid, t_vd)?);
    } else {
        let Some(sk) = issuer_sk else {
            return Err(IdError("note-a2 issuer receipt needs issuer_sk"));
        };
        payer_vd_rec = Some(self_vd(
            issuer_e_addr,
            sk,
            issuer_m,
            issuer_addr,
            chainid,
            t_vd,
        )?);
    }

    let mut d = core_base(
        "note-a2",
        chainid,
        contracts,
        party_record(
            issuer_addr,
            "private",
            issuer_identity,
            issuer_m,
            issuer_pk,
            Some(issuer_e_addr),
        ),
        party_record(
            payee_addr,
            "private",
            payee_identity,
            payee_m,
            payee_pk,
            payee_e_addr,
        ),
        txn_record(
            "note-spend",
            value,
            block_time,
            "SpentCoupledA2",
            txhash,
            block,
            logindex,
            Some((mint_txhash, mint_block, nullifier)),
        ),
        role,
    );
    d.insert("note".into(), payload);
    d.insert("proof".into(), rec_proof);
    if let Some(v) = payee_vd_rec {
        d.insert("payee_vd".into(), v);
    }
    if let Some(v) = payer_vd_rec {
        d.insert("payer_vd".into(), v);
    }
    d.insert(
        "issuer_binding_status".into(),
        Value::String(if binding.is_some() { "bound" } else { "unverified" }.into()),
    );
    if let Some(b) = binding {
        d.insert("issuer_binding".into(), issuer_reenc_record(b));
    }
    add_notes(&mut d, notes);
    Ok(Value::Object(d))
}
