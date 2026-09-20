//! Golden vectors: buck-wallet must agree bit-for-bit with the Python
//! reference (`alberta_buck/wallet`), via the nonce-inclusive fixture
//! `core/vectors/wallet-kernel-vectors.json` emitted by
//! `alberta_buck.wallet.wallet_kernel_vectors` -- canonical bytes,
//! receipt ids, envelope text, every builder and the tier-1 verifier,
//! plus the unilateral A1/A2 flows and the issuer ceremony.

use serde_json::Value;

use buck_identity::chaum_pedersen::CpProof;
use buck_identity::issuer_reenc::IssuerReencProof;
use buck_identity::schnorr::SchnorrProof;
use buck_identity::W256;
use buck_registry::tree::IdentityMerkleTree;
use buck_wallet::builders::*;
use buck_wallet::canonical::{canonical_json, identity_scalar_canonical};
use buck_wallet::envelope::*;
use buck_wallet::flows::*;
use buck_wallet::issuer::{issue_credential, verify_credential};
use buck_wallet::verify::verify_receipt;
use buck_wallet::{u128_from_w, NoteOpening};

fn hex_w(s: &str) -> W256 {
    let h = s.strip_prefix("0x").unwrap_or(s);
    assert!(h.len() <= 64, "word too long: {s}");
    let padded = format!("{:0>64}", h);
    let mut w = [0u8; 32];
    for (i, b) in w.iter_mut().enumerate() {
        *b = u8::from_str_radix(&padded[i * 2..i * 2 + 2], 16).unwrap();
    }
    w
}

fn jw(v: &Value) -> W256 {
    hex_w(v.as_str().expect("expected hex string"))
}

fn jg1(v: &Value) -> (W256, W256) {
    (jw(&v["x"]), jw(&v["y"]))
}

fn jct(v: &Value) -> ((W256, W256), (W256, W256)) {
    (jg1(&v["R"]), jg1(&v["C"]))
}

fn jg2(v: &Value) -> ((W256, W256), (W256, W256)) {
    (
        (jw(&v["x"][0]), jw(&v["x"][1])),
        (jw(&v["y"][0]), jw(&v["y"][1])),
    )
}

fn jopening(v: &Value) -> NoteOpening {
    let flavor_w = jw(&v["flavor"]);
    NoteOpening {
        flavor: u64::from_be_bytes(flavor_w[24..].try_into().unwrap()),
        v: jw(&v["v"]),
        rho: jw(&v["rho"]),
        id_hash: jw(&v["idHash"]),
        predicate: jw(&v["predicate"]),
    }
}

fn jschnorr(v: &Value) -> SchnorrProof {
    SchnorrProof {
        e: jw(&v["e"]),
        s: jw(&v["s"]),
        r: jg1(&v["R"]),
    }
}

fn jbinding(v: &Value) -> IssuerReencProof {
    IssuerReencProof {
        e: jw(&v["e"]),
        s_r: jw(&v["s_r"]),
        s_b: jw(&v["s_b"]),
        s_s: jw(&v["s_s"]),
        s_g: jw(&v["s_g"]),
        a1: jg1(&v["A1"]),
        a2: jg1(&v["A2"]),
        a3: jg1(&v["A3"]),
        a4: jg1(&v["A4"]),
        a5: jg1(&v["A5"]),
        q: jg1(&v["Q"]),
        u: jg1(&v["U"]),
        t: jg1(&v["T"]),
    }
}

fn fixture() -> Value {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/vectors/wallet-kernel-vectors.json"
    );
    let txt = std::fs::read_to_string(path).unwrap_or_else(|e| {
        panic!("cannot read {path}: {e} (run: make nix-venv-core-wallet-vectors)")
    });
    serde_json::from_str(&txt).unwrap()
}

#[test]
fn canonical_json_replay() {
    let v = fixture();
    for row in v["canonical_json"].as_array().unwrap() {
        let input = row["input"].as_str().unwrap();
        let want = row["canonical"].as_str().unwrap();
        let got = canonical_json(input).unwrap();
        assert_eq!(got, want, "canonical_json({input})");
        assert_eq!(identity_scalar_canonical(&got), jw(&row["m"]));
    }
    // Floats are out of dialect.
    assert!(canonical_json("{\"x\": 1.5}").is_err());
    assert!(canonical_json("{\"x\": 1e3}").is_err());
}

#[test]
fn envelope_replay() {
    let v = fixture();
    for row in v["envelope"].as_array().unwrap() {
        let blob = row["canonical"].as_str().unwrap().as_bytes().to_vec();
        assert_eq!(receipt_id(&blob, 12), row["id12"].as_str().unwrap());
        assert_eq!(receipt_id(&blob, 20), row["id20"].as_str().unwrap());
        assert_eq!(envelope_text(&blob, 64), row["envelope64"].as_str().unwrap());
        assert_eq!(envelope_text(&blob, 8), row["envelope8"].as_str().unwrap());
        assert_eq!(parse_envelope(row["envelope64"].as_str().unwrap()).unwrap(), blob);
        assert_eq!(parse_envelope(row["noisy"].as_str().unwrap()).unwrap(), blob);
    }
    assert!(parse_envelope("no header here .END").is_err());
    assert!(parse_envelope("AB-RCPT/1.\nQUJD\n").is_err());
}

struct P {
    addr: W256,
    identity: String,
    m_pt: (W256, W256),
    pk: (W256, W256),
    sk: W256,
    e: ((W256, W256), (W256, W256)),
}

fn party(v: &Value, name: &str) -> P {
    let p = &v["parties"][name];
    P {
        addr: jw(&p["addr"]),
        identity: p["identity"].as_str().unwrap().to_string(),
        m_pt: jg1(&p["M"]),
        pk: jg1(&p["pk"]),
        sk: jw(&p["sk"]),
        e: jct(&p["E"]),
    }
}

fn check_outputs(core: &Value, row: &Value) {
    let blob = serialize_core(core).unwrap();
    assert_eq!(
        std::str::from_utf8(&blob).unwrap(),
        row["canonical"].as_str().unwrap(),
        "canonical bytes for {} {}",
        row["kind"],
        row["role"]
    );
    assert_eq!(receipt_id(&blob, 12), row["receipt_id"].as_str().unwrap());
    assert_eq!(envelope_text(&blob, 64), row["envelope"].as_str().unwrap());
    let reparsed = deserialize_core(&parse_envelope(row["envelope"].as_str().unwrap()).unwrap())
        .unwrap();
    let res = verify_receipt(&reparsed).unwrap();
    let want = &row["verify"];
    assert_eq!(res.ok, want["ok"].as_bool().unwrap());
    assert_eq!(res.reason, want["reason"].as_str().unwrap());
    if want["identity_M"].is_object() {
        assert_eq!(res.identity_m, Some(jg1(&want["identity_M"])));
    }
    if want["value"].is_number() {
        let got = res.value.expect("verify value");
        assert_eq!(
            u128_from_w(&got).unwrap().to_string(),
            want["value"].as_number().unwrap().to_string()
        );
    }
}

#[test]
fn receipts_replay() {
    let v = fixture();
    let chainid = v["chainid"].as_u64().unwrap();
    let contracts = &v["contracts"];

    for row in v["receipts"].as_array().unwrap() {
        let kind = row["kind"].as_str().unwrap();
        let role = row["role"].as_str().unwrap();
        let payer = party(&v, row["payer"].as_str().unwrap());
        let payee = party(&v, row["payee"].as_str().unwrap());
        let txn = &row["txn"];
        let nonces = &row["nonces"];

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
                &payee.sk,
                &payee.e,
                txn["value"].as_u64().unwrap() as u128,
                txn["block_time"].as_u64().unwrap(),
                txn["txhash"].as_str().unwrap(),
                txn["block"].as_u64().unwrap(),
                txn["logindex"].as_u64().unwrap(),
                None,
                &jw(&nonces["t_self"]),
            )
            .unwrap(),
            "eoa-priv" => build_eoa_priv(
                chainid,
                contracts,
                &payer.addr,
                &payer.identity,
                &payer.m_pt,
                &payer.pk,
                &payer.e,
                &jct(&row["E_for_payee"]),
                &CpProof {
                    e: jw(&row["cp_proof"]["e"]),
                    s1: jw(&row["cp_proof"]["s1"]),
                    s2: jw(&row["cp_proof"]["s2"]),
                    t1: jg1(&row["cp_proof"]["T1"]),
                    t2: jg1(&row["cp_proof"]["T2"]),
                    t3: jg1(&row["cp_proof"]["T3"]),
                },
                &payee.addr,
                &payee.identity,
                &payee.m_pt,
                &payee.pk,
                &payee.sk,
                &payee.e,
                txn["value"].as_u64().unwrap() as u128,
                txn["block_time"].as_u64().unwrap(),
                txn["txhash"].as_str().unwrap(),
                txn["block"].as_u64().unwrap(),
                txn["logindex"].as_u64().unwrap(),
                None,
                &jw(&nonces["t_vd_payer"]),
                &jw(&nonces["t_self"]),
            )
            .unwrap(),
            "note-b1" => {
                let mint = &row["mint"];
                let cms: Vec<W256> = mint["cms"].as_array().unwrap().iter().map(jw).collect();
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
                    &jopening(&mint["opening"]),
                    &cms,
                    &jschnorr(&mint["issuer_sig"]),
                    &jg1(&mint["sigma_R"]),
                    &jw(&mint["sigma_s"]),
                    &jw(&mint["nullifier"]),
                    &jopening(&mint["opening"]).v,
                    txn["value"].as_u64().unwrap() as u128,
                    txn["block_time"].as_u64().unwrap(),
                    txn["txhash"].as_str().unwrap(),
                    txn["block"].as_u64().unwrap(),
                    txn["logindex"].as_u64().unwrap(),
                    txn["mint_txhash"].as_str().unwrap(),
                    txn["mint_block"].as_u64().unwrap(),
                    role,
                    Some(&payee.sk),
                    Some(&payee.e),
                    Some(&jct(&mint["eDepForIss"])),
                    Some(&payer.sk),
                    None,
                    &jw(&nonces["t_vd"]),
                )
                .unwrap()
            }
            "note-a1" => {
                let mint = &row["mint"];
                let cms: Vec<W256> = mint["cms"].as_array().unwrap().iter().map(jw).collect();
                let t_vd = nonces.get("t_vd").map(jw);
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
                    &jopening(&mint["opening"]),
                    &cms,
                    &jschnorr(&mint["issuer_sig"]),
                    &jct(&mint["eNote"]),
                    &jct(&mint["eRec"]),
                    &jg1(&mint["sigma_R"]),
                    &jw(&mint["sigma_s"]),
                    &jw(&mint["nullifier"]),
                    &jopening(&mint["opening"]).v,
                    txn["value"].as_u64().unwrap() as u128,
                    txn["block_time"].as_u64().unwrap(),
                    txn["txhash"].as_str().unwrap(),
                    txn["block"].as_u64().unwrap(),
                    txn["logindex"].as_u64().unwrap(),
                    txn["mint_txhash"].as_str().unwrap(),
                    txn["mint_block"].as_u64().unwrap(),
                    role,
                    Some(&payee.sk),
                    Some(&payee.e),
                    None,
                    t_vd.as_ref(),
                )
                .unwrap()
            }
            "note-a2" | "note-a2-unbound" => {
                let mint = &row["mint"];
                let cms: Vec<W256> = mint["cms"].as_array().unwrap().iter().map(jw).collect();
                let binding = if kind == "note-a2" {
                    Some(jbinding(&mint["binding"]))
                } else {
                    None
                };
                build_note_a2(
                    chainid,
                    contracts,
                    &payer.addr,
                    &payer.identity,
                    &payer.m_pt,
                    &payer.pk,
                    &payer.e,
                    &payee.addr,
                    &payee.identity,
                    &payee.m_pt,
                    &payee.pk,
                    &jopening(&mint["opening"]),
                    &cms,
                    &jct(&mint["eNote"]),
                    &jct(&mint["eIss"]),
                    &jw(&mint["nullifier"]),
                    &jopening(&mint["opening"]).v,
                    txn["value"].as_u64().unwrap() as u128,
                    txn["block_time"].as_u64().unwrap(),
                    txn["txhash"].as_str().unwrap(),
                    txn["block"].as_u64().unwrap(),
                    txn["logindex"].as_u64().unwrap(),
                    txn["mint_txhash"].as_str().unwrap(),
                    txn["mint_block"].as_u64().unwrap(),
                    binding.as_ref(),
                    role,
                    Some(&payee.sk),
                    Some(&payee.e),
                    Some(&payer.sk),
                    None,
                    &jw(&nonces["t_vd"]),
                )
                .unwrap()
            }
            other => panic!("unknown receipt kind {other}"),
        };
        check_outputs(&core, row);
    }
}

#[test]
fn tampered_replay() {
    let v = fixture();
    for row in v["tampered"].as_array().unwrap() {
        let core = deserialize_core(row["canonical"].as_str().unwrap().as_bytes()).unwrap();
        let res = verify_receipt(&core).unwrap();
        assert!(!res.ok, "{}", row["note"]);
        assert_eq!(
            res.reason,
            row["verify"]["reason"].as_str().unwrap(),
            "{}",
            row["note"]
        );
    }
}

#[test]
fn unilateral_a2_replay() {
    let v = fixture();
    let u = &v["unilateral_a2"];
    let minted = mint_unilateral_a2(
        &jw(&u["sk_iss"]),
        &jct(&u["E_reg"]),
        &jg1(&u["pk_recv"]),
        &jw(&u["v"]),
        &jw(&u["rho"]),
        &jw(&u["issuer"]),
        &jw(&u["chainid"]),
        &jw(&u["predicate"]),
        &jw(&u["r_prime"]),
        &jw(&u["r_note"]),
        &jw(&u["beta"]),
        &jw(&u["gamma"]),
        &jw(&u["k_r"]),
        &jw(&u["k_b"]),
        &jw(&u["k_s"]),
        &jw(&u["k_g"]),
    )
    .unwrap();
    let m = &u["minted"];
    assert_eq!(minted.e_note, jct(&m["eNote"]));
    assert_eq!(minted.e_iss, jct(&m["eIss"]));
    assert_eq!(minted.m_i, jg1(&m["M_I"]));
    assert_eq!(minted.id_hash, jw(&m["idHash"]));
    assert_eq!(minted.cm, jw(&m["cm"]));
    assert_eq!(minted.opening, jopening(&m["opening"]));
    assert_eq!(minted.binding, jbinding(&m["binding"]));

    // The registry-tree state and the receipt.
    let leaves: Vec<W256> = u["tree"]["leaves"].as_array().unwrap().iter().map(jw).collect();
    let tree = IdentityMerkleTree::from_leaves(
        &leaves,
        u["tree"]["depth"].as_u64().unwrap() as usize,
    )
    .unwrap();
    assert_eq!(tree.root().unwrap(), jw(&u["tree"]["root"]));

    let rcpt = make_receipt_a2(
        &jw(&u["k_recv"]),
        &jg1(&u["M_rec"]),
        &minted,
        &jw(&u["issuer"]),
        &jw(&u["chainid"]),
        &tree,
        &jw(&u["t_vd"]),
    )
    .unwrap();
    let r = &u["receipt"];
    assert_eq!(rcpt.m_i, jg1(&r["M_I"]));
    assert_eq!(rcpt.m_rec, jg1(&r["M_rec"]));
    assert_eq!(rcpt.pk_recv, jg1(&r["pk_recv"]));
    assert_eq!(rcpt.value, jw(&r["value"]));
    assert_eq!(rcpt.vd.e, jw(&r["vd"]["e"]));
    assert_eq!(rcpt.vd.s, jw(&r["vd"]["s"]));
    assert_eq!(rcpt.vd.t1, jg1(&r["vd"]["T1"]));
    assert_eq!(rcpt.vd.t2, jg1(&r["vd"]["T2"]));
    assert_eq!(rcpt.m_i_member, r["M_I_member"].as_bool().unwrap());
    assert_eq!(rcpt.m_rec_member, r["M_rec_member"].as_bool().unwrap());

    let res = verify_receipt_a2(
        &rcpt,
        &jg1(&v["parties"]["bob"]["pk"]),
        &jct(&u["E_reg"]),
        &tree.root().unwrap(),
        &tree,
    )
    .unwrap();
    assert_eq!(res.valid, u["verify"]["valid"].as_bool().unwrap());
    assert_eq!(res.reason, u["verify"]["reason"].as_str().unwrap());

    // Against a tree missing the issuer identity.
    let wrong_leaves: Vec<W256> = u["wrong_root_tree"]["leaves"]
        .as_array()
        .unwrap()
        .iter()
        .map(jw)
        .collect();
    let wrong_tree = IdentityMerkleTree::from_leaves(&wrong_leaves, 10).unwrap();
    assert_eq!(wrong_tree.root().unwrap(), jw(&u["wrong_root_tree"]["root"]));
    let res_bad = verify_receipt_a2(
        &rcpt,
        &jg1(&v["parties"]["bob"]["pk"]),
        &jct(&u["E_reg"]),
        &wrong_tree.root().unwrap(),
        &wrong_tree,
    )
    .unwrap();
    assert_eq!(res_bad.valid, u["wrong_root_verify"]["valid"].as_bool().unwrap());
    assert_eq!(
        res_bad.reason,
        u["wrong_root_verify"]["reason"].as_str().unwrap()
    );
}

#[test]
fn unilateral_a1_replay() {
    let v = fixture();
    let u = &v["unilateral_a1"];
    let minted = mint_unilateral_a1(
        &jg1(&u["M_rec"]),
        &jg1(&u["pk_recv"]),
        &jw(&u["v"]),
        &jw(&u["rho"]),
        &jw(&u["m_issuer"]),
        &jg1(&u["sigma_R"]),
        &jw(&u["sigma_s"]),
        &jw(&u["predicate"]),
        &jw(&u["r_prime"]),
        &jw(&u["r_note"]),
    )
    .unwrap();
    let m = &u["minted"];
    assert_eq!(minted.e_note, jct(&m["eNote"]));
    assert_eq!(minted.e_rec, jct(&m["eRec"]));
    assert_eq!(minted.id_hash, jw(&m["idHash"]));
    assert_eq!(minted.cm, jw(&m["cm"]));
    assert_eq!(minted.opening, jopening(&m["opening"]));

    // Same tree as the A2 flow (the fixture reuses it).
    let leaves: Vec<W256> = v["unilateral_a2"]["tree"]["leaves"]
        .as_array()
        .unwrap()
        .iter()
        .map(jw)
        .collect();
    let tree = IdentityMerkleTree::from_leaves(&leaves, 10).unwrap();

    let rcpt = make_receipt_a1(
        &jw(&u["k_recv"]),
        &jg1(&u["M_rec"]),
        &minted,
        &jg1(&u["M_iss"]),
        &jw(&u["issuer"]),
        &jw(&u["chainid"]),
        &tree,
        &jw(&u["t_vd"]),
    )
    .unwrap();
    let r = &u["receipt"];
    assert_eq!(rcpt.m_iss, jg1(&r["M_iss"]));
    assert_eq!(rcpt.m_rec, jg1(&r["M_rec"]));
    assert_eq!(rcpt.pk_recv, jg1(&r["pk_recv"]));
    assert_eq!(rcpt.value, jw(&r["value"]));
    assert_eq!(rcpt.vd.e, jw(&r["vd"]["e"]));
    assert_eq!(rcpt.vd.s, jw(&r["vd"]["s"]));
    assert_eq!(rcpt.m_iss_member, r["M_iss_member"].as_bool().unwrap());
    assert_eq!(rcpt.m_rec_member, r["M_rec_member"].as_bool().unwrap());

    let res = verify_receipt_a1(&rcpt, &tree.root().unwrap(), &tree).unwrap();
    assert_eq!(res.valid, u["verify"]["valid"].as_bool().unwrap());
    assert_eq!(res.reason, u["verify"]["reason"].as_str().unwrap());
}

#[test]
fn issuer_replay() {
    let v = fixture();
    let i = &v["issuer"];
    let fields_text = serde_json::to_string(&i["fields"]).unwrap();
    let cred = issue_credential(
        &jw(&i["sk_x"]),
        &jw(&i["sk_y"]),
        i["issuer_id"].as_str().unwrap(),
        &fields_text,
        &jw(&i["t_sig"]),
        Some((&jg1(&i["applicant_pk"]), &jw(&i["r_delivery"]))),
    )
    .unwrap();
    assert_eq!(cred.canonical, i["canonical"].as_str().unwrap());
    assert_eq!(cred.m, jw(&i["m"]));
    assert_eq!(cred.sigma.0, jg1(&i["sigma_1"]));
    assert_eq!(cred.sigma.1, jg1(&i["sigma_2"]));
    assert_eq!(cred.delivery, Some(jct(&i["delivery"])));
    assert!(verify_credential(&jg2(&i["pk_X"]), &jg2(&i["pk_Y"]), &cred.sigma, &cred.m).unwrap());
    // ... and not for a different m.
    let mut wrong_m = cred.m;
    wrong_m[31] ^= 1;
    assert!(!verify_credential(&jg2(&i["pk_X"]), &jg2(&i["pk_Y"]), &cred.sigma, &wrong_m).unwrap());
}
