//! JSON-args adapters -- the single named-argument surface both language
//! bindings (PyO3 and wasm-bindgen) expose.
//!
//! Every entry point takes ONE JSON text of named inputs and returns a
//! JSON text (or the canonical receipt text), so the two bindings are
//! one-line shims and cannot drift from each other.  The value shapes
//! are the wallet's standard ones -- `"0x..."` hex words, `{"x","y"}`
//! G1 points, `{"R","C"}` ciphertexts -- i.e. exactly the shapes the
//! committed vector fixtures use.

use serde_json::{json, Map, Value};

use buck_identity::chaum_pedersen::CpProof;
use buck_identity::issuer_reenc::IssuerReencProof;
use buck_identity::schnorr::SchnorrProof;
use buck_registry::tree::IdentityMerkleTree;

use crate::builders::*;
use crate::flows::*;
use crate::issuer::issue_credential;
use crate::jsonv::{as_ct, as_g1, get, get_ct, get_g1, get_opt, get_str, get_u128, get_w};
use crate::verify::verify_receipt;
use crate::{scalar_hex, Ctw, G1w, IdError, NoteOpening, Result, W256};

fn opening_from(v: &Value) -> Result<NoteOpening> {
    let flavor_w = get_w(v, "flavor")?;
    if flavor_w[..24].iter().any(|b| *b != 0) {
        return Err(IdError("unknown flavor"));
    }
    Ok(NoteOpening {
        flavor: u64::from_be_bytes(flavor_w[24..].try_into().unwrap()),
        v: get_w(v, "v")?,
        rho: get_w(v, "rho")?,
        id_hash: get_w(v, "idHash")?,
        predicate: get_w(v, "predicate")?,
    })
}

fn schnorr_from(v: &Value) -> Result<SchnorrProof> {
    Ok(SchnorrProof {
        e: get_w(v, "e")?,
        s: get_w(v, "s")?,
        r: get_g1(v, "R")?,
    })
}

fn cp_from(v: &Value) -> Result<CpProof> {
    Ok(CpProof {
        e: get_w(v, "e")?,
        s1: get_w(v, "s1")?,
        s2: get_w(v, "s2")?,
        t1: get_g1(v, "T1")?,
        t2: get_g1(v, "T2")?,
        t3: get_g1(v, "T3")?,
    })
}

fn binding_from(v: &Value) -> Result<IssuerReencProof> {
    Ok(IssuerReencProof {
        e: get_w(v, "e")?,
        s_r: get_w(v, "s_r")?,
        s_b: get_w(v, "s_b")?,
        s_s: get_w(v, "s_s")?,
        s_g: get_w(v, "s_g")?,
        a1: get_g1(v, "A1")?,
        a2: get_g1(v, "A2")?,
        a3: get_g1(v, "A3")?,
        a4: get_g1(v, "A4")?,
        a5: get_g1(v, "A5")?,
        q: get_g1(v, "Q")?,
        u: get_g1(v, "U")?,
        t: get_g1(v, "T")?,
    })
}

fn cms_from(v: &Value) -> Result<Vec<W256>> {
    v.as_array()
        .ok_or(IdError("args: cms must be an array"))?
        .iter()
        .map(|c| crate::w_from_hex(c.as_str().unwrap_or_default()))
        .collect()
}

fn tree_from(v: &Value) -> Result<IdentityMerkleTree> {
    let depth = get_u128(v, "depth")? as usize;
    let leaves: Vec<W256> = get(v, "leaves")?
        .as_array()
        .ok_or(IdError("args: leaves must be an array"))?
        .iter()
        .map(|l| crate::w_from_hex(l.as_str().unwrap_or_default()))
        .collect::<Result<_>>()?;
    IdentityMerkleTree::from_leaves(&leaves, depth)
}

fn vd_json(v: &buck_identity::verifiable_decrypt::VdProof) -> Value {
    json!({"e": scalar_hex(&v.e), "s": scalar_hex(&v.s),
           "T1": g1_hex(&v.t1), "T2": g1_hex(&v.t2)})
}

fn opening_json(o: &NoteOpening) -> Value {
    json!({"flavor": scalar_hex(&crate::w_from_u64(o.flavor)),
           "v": scalar_hex(&o.v), "rho": scalar_hex(&o.rho),
           "idHash": scalar_hex(&o.id_hash),
           "predicate": scalar_hex(&o.predicate)})
}

struct PartyArgs {
    addr: W256,
    identity: String,
    m_pt: G1w,
    pk: G1w,
    e_addr: Option<Ctw>,
    sk: Option<W256>,
}

fn party_from(v: &Value, key: &str) -> Result<PartyArgs> {
    let p = get(v, key)?;
    Ok(PartyArgs {
        addr: get_w(p, "addr")?,
        identity: get_str(p, "identity")?.to_string(),
        m_pt: get_g1(p, "M")?,
        pk: get_g1(p, "pk")?,
        e_addr: match get_opt(p, "E") {
            Some(e) => Some(as_ct(e)?),
            None => None,
        },
        sk: match get_opt(p, "sk") {
            Some(s) => Some(crate::w_from_hex(s.as_str().ok_or(IdError(
                "args: sk must be a hex string",
            ))?)?),
            None => None,
        },
    })
}

fn notes_from(v: &Value) -> Option<Vec<String>> {
    v.get("notes").and_then(|n| n.as_array()).map(|a| {
        a.iter()
            .filter_map(|s| s.as_str().map(str::to_string))
            .collect()
    })
}

/// Build any receipt kind from named JSON args; returns the CANONICAL
/// receipt text.  `kind` is the receipt type (`eoa-pub` ... `note-a2`);
/// `role` defaults to `recipient`.
pub fn build_receipt_args(args: &Value) -> Result<String> {
    let kind = get_str(args, "kind")?;
    let role = args
        .get("role")
        .and_then(|r| r.as_str())
        .unwrap_or("recipient");
    let chainid = get_u128(args, "chainid")? as u64;
    let contracts = get(args, "contracts")?;
    let payer = party_from(args, "payer")?;
    let payee = party_from(args, "payee")?;
    let txn = get(args, "txn")?;
    let nonces = get(args, "nonces")?;
    let notes = notes_from(args);
    let notes_ref = notes.as_deref();

    let value = get_u128(txn, "value")?;
    let block_time = get_u128(txn, "block_time")? as u64;
    let txhash = get_str(txn, "txhash")?;
    let block = get_u128(txn, "block")? as u64;
    let logindex = get_u128(txn, "logindex")? as u64;

    let core = match kind {
        "eoa-pub" => build_eoa_pub(
            chainid,
            contracts,
            &payer.addr,
            &payer.identity,
            &payer.m_pt,
            &payer.pk,
            &payee.addr,
            &payee.identity,
            &payee.m_pt,
            &payee.pk,
            payee.sk.as_ref().ok_or(IdError("args: payee.sk required"))?,
            payee.e_addr.as_ref().ok_or(IdError("args: payee.E required"))?,
            value,
            block_time,
            txhash,
            block,
            logindex,
            notes_ref,
            &get_w(nonces, "t_self")?,
        )?,
        "eoa-priv" => build_eoa_priv(
            chainid,
            contracts,
            &payer.addr,
            &payer.identity,
            &payer.m_pt,
            &payer.pk,
            payer.e_addr.as_ref().ok_or(IdError("args: payer.E required"))?,
            &get_ct(args, "E_for_payee")?,
            &cp_from(get(args, "cp_proof")?)?,
            &payee.addr,
            &payee.identity,
            &payee.m_pt,
            &payee.pk,
            payee.sk.as_ref().ok_or(IdError("args: payee.sk required"))?,
            payee.e_addr.as_ref().ok_or(IdError("args: payee.E required"))?,
            value,
            block_time,
            txhash,
            block,
            logindex,
            notes_ref,
            &get_w(nonces, "t_vd_payer")?,
            &get_w(nonces, "t_self")?,
        )?,
        "note-b1" | "note-a1" | "note-a2" => {
            let mint_txhash = get_str(txn, "mint_txhash")?;
            let mint_block = get_u128(txn, "mint_block")? as u64;
            let opening = opening_from(get(args, "opening")?)?;
            let cms = cms_from(get(args, "cms")?)?;
            let nullifier = get_w(args, "nullifier")?;
            let face = get_w(args, "face")?;
            match kind {
                "note-b1" => {
                    let e_dep = match get_opt(args, "eDepForIss") {
                        Some(e) => Some(as_ct(e)?),
                        None => None,
                    };
                    build_note_b1(
                        chainid,
                        contracts,
                        &payer.addr,
                        &payer.identity,
                        &payer.m_pt,
                        &payer.pk,
                        &payee.addr,
                        &payee.identity,
                        &payee.m_pt,
                        &payee.pk,
                        &opening,
                        &cms,
                        &schnorr_from(get(args, "issuer_sig")?)?,
                        &get_g1(args, "sigma_R")?,
                        &get_w(args, "sigma_s")?,
                        &nullifier,
                        &face,
                        value,
                        block_time,
                        txhash,
                        block,
                        logindex,
                        mint_txhash,
                        mint_block,
                        role,
                        payee.sk.as_ref(),
                        payee.e_addr.as_ref(),
                        e_dep.as_ref(),
                        payer.sk.as_ref(),
                        notes_ref,
                        &get_w(nonces, "t_vd")?,
                    )?
                }
                "note-a1" => {
                    let t_vd = match get_opt(nonces, "t_vd") {
                        Some(t) => Some(crate::w_from_hex(t.as_str().ok_or(IdError(
                            "args: t_vd must be a hex string",
                        ))?)?),
                        None => None,
                    };
                    build_note_a1(
                        chainid,
                        contracts,
                        &payer.addr,
                        &payer.identity,
                        &payer.m_pt,
                        &payer.pk,
                        &payee.addr,
                        &payee.identity,
                        &payee.m_pt,
                        &payee.pk,
                        &opening,
                        &cms,
                        &schnorr_from(get(args, "issuer_sig")?)?,
                        &get_ct(args, "eNote")?,
                        &get_ct(args, "eRec")?,
                        &get_g1(args, "sigma_R")?,
                        &get_w(args, "sigma_s")?,
                        &nullifier,
                        &face,
                        value,
                        block_time,
                        txhash,
                        block,
                        logindex,
                        mint_txhash,
                        mint_block,
                        role,
                        payee.sk.as_ref(),
                        payee.e_addr.as_ref(),
                        notes_ref,
                        t_vd.as_ref(),
                    )?
                }
                _ => {
                    let binding = match get_opt(args, "binding") {
                        Some(b) => Some(binding_from(b)?),
                        None => None,
                    };
                    build_note_a2(
                        chainid,
                        contracts,
                        &payer.addr,
                        &payer.identity,
                        &payer.m_pt,
                        &payer.pk,
                        payer
                            .e_addr
                            .as_ref()
                            .ok_or(IdError("args: payer.E required"))?,
                        &payee.addr,
                        &payee.identity,
                        &payee.m_pt,
                        &payee.pk,
                        &opening,
                        &cms,
                        &get_ct(args, "eNote")?,
                        &get_ct(args, "eIss")?,
                        &nullifier,
                        &face,
                        value,
                        block_time,
                        txhash,
                        block,
                        logindex,
                        mint_txhash,
                        mint_block,
                        binding.as_ref(),
                        role,
                        payee.sk.as_ref(),
                        payee.e_addr.as_ref(),
                        payer.sk.as_ref(),
                        notes_ref,
                        &get_w(nonces, "t_vd")?,
                    )?
                }
                // unreachable: the outer match covers exactly these kinds
            }
        }
        _ => return Err(IdError("args: unknown receipt kind")),
    };
    Ok(crate::canonical::canonical_json_value(&core)?)
}

/// Tier-1 verify from canonical receipt text; returns the RcptResult as
/// JSON (`{"ok", "reason", "identity_M", "value"}`).
pub fn verify_receipt_args(core_text: &str) -> Result<String> {
    let core = crate::envelope::deserialize_core(core_text.as_bytes())?;
    let res = verify_receipt(&core)?;
    let out = json!({
        "ok": res.ok,
        "reason": res.reason,
        "identity_M": res.identity_m.map(|m| g1_hex(&m)),
        "value": res.value.map(|v| scalar_hex(&v)),
    });
    Ok(out.to_string())
}

/// Mint a unilateral A2 note from named args; returns the minted record
/// as JSON (the wallet-vectors `minted` schema + the held randomness).
pub fn mint_unilateral_a2_args(args: &Value) -> Result<String> {
    let n = get(args, "nonces")?;
    let minted = mint_unilateral_a2(
        &get_w(args, "sk_iss")?,
        &get_ct(args, "E_reg")?,
        &get_g1(args, "M_rec")?,
        &get_w(args, "v")?,
        &get_w(args, "rho")?,
        &get_w(args, "issuer")?,
        &get_w(args, "chainid")?,
        &get_w(args, "predicate")?,
        &get_w(n, "r_prime")?,
        &get_w(n, "r_note")?,
        &get_w(n, "beta")?,
        &get_w(n, "gamma")?,
        &get_w(n, "k_r")?,
        &get_w(n, "k_b")?,
        &get_w(n, "k_s")?,
        &get_w(n, "k_g")?,
    )?;
    Ok(json!({
        "eNote": ct_hex(&minted.e_note),
        "eIss": ct_hex(&minted.e_iss),
        "M_I": g1_hex(&minted.m_i),
        "idHash": scalar_hex(&minted.id_hash),
        "cm": scalar_hex(&minted.cm),
        "opening": opening_json(&minted.opening),
        "binding": issuer_reenc_record(&minted.binding),
        "r_prime": scalar_hex(&minted.r_prime),
        "r_note": scalar_hex(&minted.r_note),
    })
    .to_string())
}

/// Recipient-side A2 receipt from named args (`minted` in the schema
/// `mint_unilateral_a2_args` returns; `tree` as `{depth, leaves}`).
pub fn make_receipt_a2_args(args: &Value) -> Result<String> {
    let minted_v = get(args, "minted")?;
    let opening = opening_from(get(minted_v, "opening")?)?;
    let minted = MintedA2 {
        e_note: get_ct(minted_v, "eNote")?,
        e_iss: get_ct(minted_v, "eIss")?,
        m_i: get_g1(minted_v, "M_I")?,
        id_hash: get_w(minted_v, "idHash")?,
        cm: get_w(minted_v, "cm")?,
        opening,
        binding: binding_from(get(minted_v, "binding")?)?,
        r_prime: [0u8; 32],
        r_note: [0u8; 32],
    };
    let tree = tree_from(get(args, "tree")?)?;
    let rcpt = make_receipt_a2(
        &get_w(args, "m_rec")?,
        &minted,
        &get_w(args, "issuer")?,
        &get_w(args, "chainid")?,
        &tree,
        &get_w(args, "t_vd")?,
    )?;
    Ok(json!({
        "M_I": g1_hex(&rcpt.m_i),
        "M_rec": g1_hex(&rcpt.m_rec),
        "value": scalar_hex(&rcpt.value),
        "eIss": ct_hex(&rcpt.e_iss),
        "vd": vd_json(&rcpt.vd),
        "binding": issuer_reenc_record(&rcpt.binding),
        "issuer": scalar_hex(&rcpt.issuer),
        "chainid": scalar_hex(&rcpt.chainid),
        "M_I_member": rcpt.m_i_member,
        "M_rec_member": rcpt.m_rec_member,
    })
    .to_string())
}

/// Third-party A2 receipt verification from named args.
pub fn verify_receipt_a2_args(args: &Value) -> Result<String> {
    let r = get(args, "receipt")?;
    let rcpt = UnilateralReceipt {
        m_i: get_g1(r, "M_I")?,
        m_rec: get_g1(r, "M_rec")?,
        value: get_w(r, "value")?,
        e_iss: get_ct(r, "eIss")?,
        vd: buck_identity::verifiable_decrypt::VdProof {
            e: get_w(get(r, "vd")?, "e")?,
            s: get_w(get(r, "vd")?, "s")?,
            t1: get_g1(get(r, "vd")?, "T1")?,
            t2: get_g1(get(r, "vd")?, "T2")?,
        },
        binding: binding_from(get(r, "binding")?)?,
        issuer: get_w(r, "issuer")?,
        chainid: get_w(r, "chainid")?,
        m_i_member: false,
        m_rec_member: false,
    };
    let tree = tree_from(get(args, "tree")?)?;
    let res = verify_receipt_a2(
        &rcpt,
        &get_g1(args, "pk_iss")?,
        &get_ct(args, "E_reg")?,
        &get_w(args, "identity_root")?,
        &tree,
    )?;
    Ok(json!({
        "valid": res.valid,
        "issuer_M": res.issuer_m.map(|m| g1_hex(&m)),
        "recipient_M": res.recipient_m.map(|m| g1_hex(&m)),
        "value": scalar_hex(&res.value),
        "reason": res.reason,
    })
    .to_string())
}

/// Mint a unilateral A1 note from named args.
pub fn mint_unilateral_a1_args(args: &Value) -> Result<String> {
    let n = get(args, "nonces")?;
    let minted = mint_unilateral_a1(
        &get_g1(args, "M_rec")?,
        &get_w(args, "v")?,
        &get_w(args, "rho")?,
        &get_w(args, "m_issuer")?,
        &get_g1(args, "sigma_R")?,
        &get_w(args, "sigma_s")?,
        &get_w(args, "predicate")?,
        &get_w(n, "r_prime")?,
        &get_w(n, "r_note")?,
    )?;
    Ok(json!({
        "eNote": ct_hex(&minted.e_note),
        "eRec": ct_hex(&minted.e_rec),
        "idHash": scalar_hex(&minted.id_hash),
        "cm": scalar_hex(&minted.cm),
        "opening": opening_json(&minted.opening),
        "r_prime": scalar_hex(&minted.r_prime),
        "r_note": scalar_hex(&minted.r_note),
    })
    .to_string())
}

/// Recipient-side A1 receipt from named args.
pub fn make_receipt_a1_args(args: &Value) -> Result<String> {
    let minted_v = get(args, "minted")?;
    let minted = MintedA1 {
        e_note: get_ct(minted_v, "eNote")?,
        e_rec: get_ct(minted_v, "eRec")?,
        id_hash: get_w(minted_v, "idHash")?,
        cm: get_w(minted_v, "cm")?,
        opening: opening_from(get(minted_v, "opening")?)?,
        r_prime: [0u8; 32],
        r_note: [0u8; 32],
    };
    let tree = tree_from(get(args, "tree")?)?;
    let rcpt = make_receipt_a1(
        &get_w(args, "m_rec")?,
        &minted,
        &get_g1(args, "M_iss")?,
        &get_w(args, "issuer")?,
        &get_w(args, "chainid")?,
        &tree,
        &get_w(args, "t_vd")?,
    )?;
    Ok(json!({
        "M_iss": g1_hex(&rcpt.m_iss),
        "M_rec": g1_hex(&rcpt.m_rec),
        "value": scalar_hex(&rcpt.value),
        "eRec": ct_hex(&rcpt.e_rec),
        "vd": vd_json(&rcpt.vd),
        "issuer": scalar_hex(&rcpt.issuer),
        "chainid": scalar_hex(&rcpt.chainid),
        "M_iss_member": rcpt.m_iss_member,
        "M_rec_member": rcpt.m_rec_member,
    })
    .to_string())
}

/// Third-party A1 receipt verification from named args.
pub fn verify_receipt_a1_args(args: &Value) -> Result<String> {
    let r = get(args, "receipt")?;
    let rcpt = A1Receipt {
        m_iss: get_g1(r, "M_iss")?,
        m_rec: get_g1(r, "M_rec")?,
        value: get_w(r, "value")?,
        e_rec: get_ct(r, "eRec")?,
        vd: buck_identity::verifiable_decrypt::VdProof {
            e: get_w(get(r, "vd")?, "e")?,
            s: get_w(get(r, "vd")?, "s")?,
            t1: get_g1(get(r, "vd")?, "T1")?,
            t2: get_g1(get(r, "vd")?, "T2")?,
        },
        issuer: get_w(r, "issuer")?,
        chainid: get_w(r, "chainid")?,
        m_iss_member: false,
        m_rec_member: false,
    };
    let tree = tree_from(get(args, "tree")?)?;
    let res = verify_receipt_a1(&rcpt, &get_w(args, "identity_root")?, &tree)?;
    Ok(json!({
        "valid": res.valid,
        "issuer_M": res.issuer_m.map(|m| g1_hex(&m)),
        "recipient_M": res.recipient_m.map(|m| g1_hex(&m)),
        "value": scalar_hex(&res.value),
        "reason": res.reason,
    })
    .to_string())
}

/// The issuance ceremony from named args (`fields` is the identity
/// OBJECT; `applicant_pk`/`r_delivery` optional together).
pub fn issue_credential_args(args: &Value) -> Result<String> {
    let fields = get(args, "fields")?;
    if !fields.is_object() {
        return Err(IdError("issuer: identity fields must be a JSON object"));
    }
    let fields_text =
        serde_json::to_string(fields).map_err(|_| IdError("args: fields serialization"))?;
    let delivery_pk = match get_opt(args, "applicant_pk") {
        Some(p) => Some(as_g1(p)?),
        None => None,
    };
    let r_del = match get_opt(args, "r_delivery") {
        Some(r) => Some(crate::w_from_hex(r.as_str().ok_or(IdError(
            "args: r_delivery must be a hex string",
        ))?)?),
        None => None,
    };
    let delivery = match (&delivery_pk, &r_del) {
        (Some(pk), Some(r)) => Some((pk, r)),
        (None, None) => None,
        _ => return Err(IdError("args: applicant_pk and r_delivery go together")),
    };
    let cred = issue_credential(
        &get_w(args, "sk_x")?,
        &get_w(args, "sk_y")?,
        get_str(args, "issuer_id")?,
        &fields_text,
        &get_w(args, "t_sig")?,
        delivery,
    )?;
    let mut out = Map::new();
    out.insert("canonical".into(), Value::String(cred.canonical));
    out.insert("m".into(), Value::String(scalar_hex(&cred.m)));
    out.insert("sigma_1".into(), g1_hex(&cred.sigma.0));
    out.insert("sigma_2".into(), g1_hex(&cred.sigma.1));
    if let Some(ct) = cred.delivery {
        out.insert("delivery".into(), ct_hex(&ct));
    }
    Ok(Value::Object(out).to_string())
}
