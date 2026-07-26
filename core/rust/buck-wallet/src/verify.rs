//! Tier-1 offline receipt verification from a deserialized AB-RCPT/1
//! core -- mirrors `alberta_buck/wallet/verify_receipt.py` predicate for
//! predicate (and reason string for reason string).
//!
//! Consumes the receipt-core JSON tree (no chain access) and re-runs
//! every check: the point-to-human bridge `keccak(identity)*G == M` for
//! both parties, the generating side's self-naming verifiable
//! decryption, the type-specific naming, and the note anchor (idHash
//! preimage, cm-in-batch, unified 4242 nullifier, face).

use serde_json::Value;

use buck_identity::elgamal::elgamal_decrypt;
use buck_identity::issuer_reenc::{issuer_reenc_verify, IssuerReencProof};
use buck_identity::keccak::identity_scalar;
use buck_identity::notes::{
    id_hash_a1, id_hash_a2, id_hash_b1, nullifier_b, FLAVOR_A1, FLAVOR_A2, FLAVOR_B1,
};
use buck_identity::schnorr::{batch_commitment, issuer_schnorr_verify, SchnorrProof};
use buck_identity::verifiable_decrypt::{verifiable_decrypt_verify, VdProof};
use buck_identity::{g1_generator, g1_mul};

use crate::jsonv::{as_ct, get, get_ct, get_g1, get_opt, get_str, get_u128, get_w};
use crate::receipt::RcptResult;
use crate::{w_from_u128, Ctw, G1w, IdError, NoteOpening, Result, W256};

/// `M' = keccak(identity) * G == M`.
fn check_point_identity(m_pt: &G1w, identity: &str) -> Result<bool> {
    let m = identity_scalar(identity.as_bytes());
    Ok(g1_mul(&g1_generator(), &m)? == *m_pt)
}

struct VdRecord {
    e_ct: Ctw,
    m_named: G1w,
    account: W256,
    chainid: u128,
    proof: VdProof,
}

fn vd_from_record(rec: &Value) -> Result<VdRecord> {
    let p = get(rec, "proof")?;
    Ok(VdRecord {
        e_ct: get_ct(rec, "E_ct")?,
        m_named: get_g1(rec, "M_named")?,
        account: get_w(rec, "account")?,
        chainid: get_u128(rec, "chainid")?,
        proof: VdProof {
            e: get_w(p, "e")?,
            s: get_w(p, "s")?,
            t1: get_g1(p, "T1")?,
            t2: get_g1(p, "T2")?,
        },
    })
}

struct Party {
    addr: W256,
    kind: String,
    identity: String,
    m_pt: G1w,
    pk: G1w,
    e_addr: Option<Ctw>,
}

fn party(v: &Value, key: &str) -> Result<Party> {
    let p = get(v, key)?;
    Ok(Party {
        addr: get_w(p, "addr")?,
        kind: get_str(p, "kind")?.to_string(),
        identity: get_str(p, "identity")?.to_string(),
        m_pt: get_g1(p, "M")?,
        pk: get_g1(p, "pk")?,
        e_addr: match get_opt(p, "E_addr") {
            Some(e) => Some(as_ct(e)?),
            None => None,
        },
    })
}

/// Check a self-naming vd record against `party`'s registered record.
/// Returns `""` on success, else the failure reason.
fn self_naming_ok(vd_rec: &Value, party: &Party, chainid: u128) -> Result<String> {
    let vd = vd_from_record(vd_rec)?;
    if vd.chainid != chainid {
        return Ok("chainid mismatch".into());
    }
    if vd.account != party.addr {
        return Ok("account mismatch".into());
    }
    if vd.m_named != party.m_pt {
        return Ok("named M mismatch".into());
    }
    let Some(e_reg) = &party.e_addr else {
        return Ok("ciphertext is not the registered E_addr".into());
    };
    if vd.e_ct != *e_reg {
        return Ok("ciphertext is not the registered E_addr".into());
    }
    if !verifiable_decrypt_verify(
        &vd.e_ct,
        &party.pk,
        &vd.m_named,
        &vd.proof,
        &vd.account,
        &w_from_u128(vd.chainid),
    )? {
        return Ok("vd fails".into());
    }
    Ok(String::new())
}

fn opening_from(o: &Value) -> Result<NoteOpening> {
    let flavor_w = get_w(o, "flavor")?;
    if flavor_w[..24].iter().any(|b| *b != 0) {
        return Err(IdError("unknown flavor"));
    }
    Ok(NoteOpening {
        flavor: u64::from_be_bytes(flavor_w[24..].try_into().unwrap()),
        v: get_w(o, "v")?,
        rho: get_w(o, "rho")?,
        id_hash: get_w(o, "idHash")?,
        predicate: get_w(o, "predicate")?,
    })
}

/// Tier-1 offline verification of an AB-RCPT/1 receipt core.
pub fn verify_receipt(core: &Value) -> Result<RcptResult> {
    let t = get_str(core, "type")?.to_string();
    let role = match core.get("role") {
        Some(r) => r.as_str().ok_or(IdError("receipt: expected a string"))?.to_string(),
        None => "recipient".to_string(),
    };
    let chainid = get_u128(core, "chainid")?;
    let chainid_w = w_from_u128(chainid);

    if role != "recipient" && role != "issuer" {
        return Ok(RcptResult::fail(format!("unknown receipt role: {role}")));
    }
    if role == "issuer" && !matches!(t.as_str(), "note-b1" | "note-a1" | "note-a2") {
        return Ok(RcptResult::fail(format!(
            "{t}: issuer-side receipts exist for Notes only"
        )));
    }

    let payer = party(core, "payer")?;
    let payee = party(core, "payee")?;

    // 1. Point-to-human bridge.
    if !check_point_identity(&payer.m_pt, &payer.identity)? {
        return Ok(RcptResult::fail("payer M != keccak(identity)·G"));
    }
    if !check_point_identity(&payee.m_pt, &payee.identity)? {
        return Ok(RcptResult::fail("payee M != keccak(identity)·G"));
    }

    // 2. Generator self-naming.
    if role == "recipient" {
        let Some(vd) = get_opt(core, "payee_vd") else {
            return Ok(RcptResult::fail("missing payee self-naming proof"));
        };
        let why = self_naming_ok(vd, &payee, chainid)?;
        if !why.is_empty() {
            return Ok(RcptResult::fail(format!("payee self-naming: {why}")));
        }
    } else if payer.kind == "private" {
        let Some(vd) = get_opt(core, "payer_vd") else {
            return Ok(RcptResult::fail("missing payer self-naming proof"));
        };
        let why = self_naming_ok(vd, &payer, chainid)?;
        if !why.is_empty() {
            return Ok(RcptResult::fail(format!("payer self-naming: {why}")));
        }
    }

    // 3. Type-specific naming.
    let mut a2_unbound = false;

    if t == "eoa-pub" {
        // The payer's identity preimage -> M is the naming (tier 2
        // anchors the registry record).
    } else if t == "eoa-priv" {
        let Some(proof) = get_opt(core, "proof") else {
            return Ok(RcptResult::fail("eoa-priv: missing proof"));
        };
        let (Some(ap), Some(vp)) = (get_opt(proof, "approve"), get_opt(proof, "vd_payer")) else {
            return Ok(RcptResult::fail("eoa-priv: missing approve/vd_payer"));
        };
        let e_payer = get_ct(ap, "E_a")?;
        let e_spender = get_ct(ap, "E_b")?;
        let pk_payer = get_g1(ap, "pk_a")?;
        let pk_spender = get_g1(ap, "pk_b")?;
        let sender = get_w(ap, "sender")?;
        let spender = get_w(ap, "spender")?;
        let cid_ap = w_from_u128(get_u128(ap, "chainid")?);
        let p = get(ap, "proof")?;
        let cp = buck_identity::chaum_pedersen::CpProof {
            e: get_w(p, "e")?,
            s1: get_w(p, "s1")?,
            s2: get_w(p, "s2")?,
            t1: get_g1(p, "T1")?,
            t2: get_g1(p, "T2")?,
            t3: get_g1(p, "T3")?,
        };
        if !buck_identity::chaum_pedersen::chaum_pedersen_verify(
            &e_payer, &e_spender, &pk_payer, &pk_spender, &cp, &sender, &spender, &cid_ap,
        )? {
            return Ok(RcptResult::fail("eoa-priv: approve handshake fails"));
        }
        let vd = vd_from_record(vp)?;
        if !verifiable_decrypt_verify(
            &vd.e_ct,
            &payee.pk,
            &vd.m_named,
            &vd.proof,
            &vd.account,
            &w_from_u128(vd.chainid),
        )? {
            return Ok(RcptResult::fail("eoa-priv: vd_payer fails"));
        }
        if vd.m_named != payer.m_pt {
            return Ok(RcptResult::fail("eoa-priv: named M != payer M"));
        }
    } else if matches!(t.as_str(), "note-b1" | "note-a1" | "note-a2") {
        let Some(rp) = get_opt(core, "proof") else {
            return Ok(RcptResult::fail(format!("{t}: missing proof")));
        };
        let Some(np) = get_opt(core, "note") else {
            return Ok(RcptResult::fail(format!(
                "{t}: missing Identity-M note payload"
            )));
        };
        let opening = opening_from(get(rp, "opening")?)?;
        let expect_flavor = match t.as_str() {
            "note-b1" => FLAVOR_B1,
            "note-a1" => FLAVOR_A1,
            _ => FLAVOR_A2,
        };
        if opening.flavor != expect_flavor {
            return Ok(RcptResult::fail(format!("{t}: opening flavor mismatch")));
        }
        let cms: Vec<W256> = get(rp, "cms")?
            .as_array()
            .ok_or(IdError("receipt: expected an array"))?
            .iter()
            .map(|c| crate::w_from_hex(c.as_str().ok_or(IdError("receipt: expected a string"))?))
            .collect::<Result<_>>()?;

        // (a) cm is in the minted batch.
        let cm = opening.commitment()?;
        if !cms.contains(&cm) {
            return Ok(RcptResult::fail(format!(
                "{t}: opening cm not in minted batch"
            )));
        }

        // Identity scalars, derivable by ANY verifier.
        let m_iss = identity_scalar(payer.identity.as_bytes());
        let m_rec = identity_scalar(payee.identity.as_bytes());

        // (b) Identity-M idHash preimage; (A-flavors) addressed legs.
        let mut e_iss_a2: Option<Ctw> = None;
        if t == "note-b1" {
            let sigma_r = get_g1(np, "sigma_R")?;
            let sigma_s = get_w(np, "sigma_s")?;
            if id_hash_b1(&m_iss, &sigma_r, &sigma_s)? != opening.id_hash {
                return Ok(RcptResult::fail(
                    "note-b1: idHash != id_hash_b1(m_iss, sigma)",
                ));
            }
        } else if t == "note-a1" {
            let e_note = get_ct(np, "eNote")?;
            let e_rec = get_ct(np, "eRec")?;
            let sigma_r = get_g1(np, "sigma_R")?;
            let sigma_s = get_w(np, "sigma_s")?;
            if id_hash_a1(&e_note, &m_iss, &sigma_r, &sigma_s)? != opening.id_hash {
                return Ok(RcptResult::fail(
                    "note-a1: idHash != id_hash_a1(eNote, m_iss, sigma)",
                ));
            }
            let v_pt = g1_mul(&g1_generator(), &opening.v)?;
            if elgamal_decrypt(&e_note.0, &e_note.1, &m_rec)? != v_pt {
                return Ok(RcptResult::fail(
                    "note-a1: eNote does not decrypt to v·G under m_rec",
                ));
            }
            if elgamal_decrypt(&e_rec.0, &e_rec.1, &m_rec)? != payee.m_pt {
                return Ok(RcptResult::fail(
                    "note-a1: eRec does not decrypt to M_rec under m_rec",
                ));
            }
        } else {
            let e_note = get_ct(np, "eNote")?;
            let e_iss = get_ct(np, "eIss")?;
            if id_hash_a2(&e_note, &e_iss)? != opening.id_hash {
                return Ok(RcptResult::fail(
                    "note-a2: idHash != id_hash_a2(eNote, eIss)",
                ));
            }
            let v_pt = g1_mul(&g1_generator(), &opening.v)?;
            if elgamal_decrypt(&e_note.0, &e_note.1, &m_rec)? != v_pt {
                return Ok(RcptResult::fail(
                    "note-a2: eNote does not decrypt to v·G under m_rec",
                ));
            }
            if elgamal_decrypt(&e_iss.0, &e_iss.1, &m_rec)? != payer.m_pt {
                return Ok(RcptResult::fail(
                    "note-a2: eIss does not decrypt to issuer M under m_rec",
                ));
            }
            e_iss_a2 = Some(e_iss);
        }

        // (c) Issuer binding over the batch / leaf.
        if t == "note-b1" || t == "note-a1" {
            let Some(sig) = get_opt(rp, "issuer_sig") else {
                return Ok(RcptResult::fail(format!(
                    "{t}: missing issuer batch Schnorr"
                )));
            };
            let h_batch = batch_commitment(&cms);
            let iss_sig = SchnorrProof {
                e: get_w(sig, "e")?,
                s: get_w(sig, "s")?,
                r: get_g1(sig, "R")?,
            };
            if !issuer_schnorr_verify(&payer.pk, &iss_sig, &h_batch, &payer.addr, &chainid_w)? {
                return Ok(RcptResult::fail(format!("{t}: issuer batch binding fails")));
            }
        } else {
            match get_opt(core, "issuer_binding") {
                Some(b) => {
                    let binding = IssuerReencProof {
                        e: get_w(b, "e")?,
                        s_r: get_w(b, "s_r")?,
                        s_b: get_w(b, "s_b")?,
                        s_s: get_w(b, "s_s")?,
                        s_g: get_w(b, "s_g")?,
                        a1: get_g1(b, "A1")?,
                        a2: get_g1(b, "A2")?,
                        a3: get_g1(b, "A3")?,
                        a4: get_g1(b, "A4")?,
                        a5: get_g1(b, "A5")?,
                        q: get_g1(b, "Q")?,
                        u: get_g1(b, "U")?,
                        t: get_g1(b, "T")?,
                    };
                    let Some(e_reg) = &payer.e_addr else {
                        return Ok(RcptResult::fail("note-a2: issuer E_addr missing"));
                    };
                    let e_iss = e_iss_a2.as_ref().unwrap();
                    if !issuer_reenc_verify(
                        &payer.pk, e_reg, e_iss, &binding, &payer.addr, &chainid_w,
                    )? {
                        return Ok(RcptResult::fail("note-a2: issuer binding fails"));
                    }
                }
                None => a2_unbound = true,
            }
        }

        // (d) Spend anchor: unified 4242 nullifier + the paid face.
        let nf = get_w(rp, "nullifier")?;
        let face = get_w(rp, "face")?;
        if nf != nullifier_b(&opening.rho, &opening.id_hash)? {
            return Ok(RcptResult::fail(format!("{t}: nullifier mismatch")));
        }
        if face != opening.v {
            return Ok(RcptResult::fail(format!("{t}: face != note value")));
        }
        let txn_value = get_u128(get(core, "txn")?, "value")?;
        if w_from_u128(txn_value) != face {
            return Ok(RcptResult::fail(format!("{t}: txn value != face")));
        }

        // (e) Issuer-side B1: name the depositor via eDepForIss.
        if t == "note-b1" && role == "issuer" {
            let e_dep_hex = get_opt(np, "eDepForIss");
            let vd_payee = get_opt(core, "vd_payee");
            let (Some(e_dep_v), Some(vd_v)) = (e_dep_hex, vd_payee) else {
                return Ok(RcptResult::fail(
                    "note-b1: issuer receipt needs eDepForIss + vd_payee",
                ));
            };
            let e_dep = as_ct(e_dep_v)?;
            let vd = vd_from_record(vd_v)?;
            if vd.chainid != chainid || vd.account != payer.addr {
                return Ok(RcptResult::fail("note-b1: vd_payee context mismatch"));
            }
            if vd.e_ct != e_dep {
                return Ok(RcptResult::fail(
                    "note-b1: vd_payee ciphertext != eDepForIss",
                ));
            }
            if vd.m_named != payee.m_pt {
                return Ok(RcptResult::fail("note-b1: vd_payee names a different M"));
            }
            if !verifiable_decrypt_verify(
                &e_dep,
                &payer.pk,
                &vd.m_named,
                &vd.proof,
                &vd.account,
                &w_from_u128(vd.chainid),
            )? {
                return Ok(RcptResult::fail("note-b1: vd_payee fails"));
            }
        }
    } else {
        return Ok(RcptResult::fail(format!("unknown receipt type: {t}")));
    }

    let status = if t == "note-a2" && a2_unbound {
        "UNVERIFIED ISSUER"
    } else {
        "VALID"
    };
    let txn_value = get_u128(get(core, "txn")?, "value")?;
    Ok(RcptResult {
        ok: true,
        identity_m: Some(payer.m_pt),
        value: Some(w_from_u128(txn_value)),
        reason: status.to_string(),
    })
}
