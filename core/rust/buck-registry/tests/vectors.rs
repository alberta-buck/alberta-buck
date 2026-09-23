//! Golden vectors: buck-registry must agree bit-for-bit with the Python
//! reference (`alberta_buck/registry`), via the nonce-inclusive fixture
//! `core/vectors/registry-kernel-vectors.json` emitted by
//! `alberta_buck.registry.kernel_vectors`.

use buck_identity::W256;
use buck_registry::aggregator::CentralMerkleService;
use buck_registry::certificate::*;
use buck_registry::attributes::{prove_attributes, verify_attributes};
use buck_registry::feature::FeatureAuthority;
use buck_registry::regulator::{
    band_for_face, check_issuance, scope_id, subtree_key, InsuranceRegulator, InsurerEnvelope,
};
use std::collections::BTreeSet;
use buck_registry::tree::{identity_leaf, IdentityMerkleTree, MembershipProof};

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

fn hex_bytes(s: &str) -> Vec<u8> {
    let h = s.strip_prefix("0x").unwrap_or(s);
    (0..h.len() / 2)
        .map(|i| u8::from_str_radix(&h[i * 2..i * 2 + 2], 16).unwrap())
        .collect()
}

fn jw(v: &serde_json::Value) -> W256 {
    hex_w(v.as_str().expect("expected hex string"))
}

fn jg1(v: &serde_json::Value) -> (W256, W256) {
    (jw(&v["x"]), jw(&v["y"]))
}

fn jct(v: &serde_json::Value) -> ((W256, W256), (W256, W256)) {
    (jg1(&v["R"]), jg1(&v["C"]))
}

fn jproof(v: &serde_json::Value) -> MembershipProof {
    MembershipProof {
        leaf: jw(&v["leaf"]),
        siblings: v["siblings"].as_array().unwrap().iter().map(jw).collect(),
        index_bits: v["index_bits"]
            .as_array()
            .unwrap()
            .iter()
            .map(|b| b.as_u64().unwrap() as u8)
            .collect(),
        root: jw(&v["root"]),
        leaf_index: v["leaf_index"].as_u64().unwrap() as usize,
    }
}

fn fixture() -> serde_json::Value {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/vectors/registry-kernel-vectors.json"
    );
    let txt = std::fs::read_to_string(path).unwrap_or_else(|e| {
        panic!("cannot read {path}: {e} (run: make nix-venv-core-registry-vectors)")
    });
    serde_json::from_str(&txt).unwrap()
}

#[test]
fn tree_replay() {
    let v = fixture();
    let t = &v["tree"];
    let depth = t["depth"].as_u64().unwrap() as usize;
    let leaves: Vec<W256> = t["leaves"].as_array().unwrap().iter().map(jw).collect();
    let points: Vec<_> = t["points"].as_array().unwrap().iter().map(jg1).collect();

    // identity_leaf of each recorded point matches the recorded leaf.
    for (p, l) in points.iter().zip(leaves.iter()) {
        assert_eq!(identity_leaf(p).unwrap(), *l);
    }

    // Stepwise incremental roots.
    let mut tree = IdentityMerkleTree::new(depth).unwrap();
    assert_eq!(tree.root().unwrap(), jw(&t["empty_root"]));
    for (leaf, root) in leaves.iter().zip(t["roots_after_insert"].as_array().unwrap()) {
        tree.insert_leaf(*leaf);
        assert_eq!(tree.root().unwrap(), jw(root));
    }

    // Paths at the recorded indices.
    for (idx, want) in t["paths"].as_object().unwrap() {
        let idx: usize = idx.parse().unwrap();
        let got = tree.path(idx).unwrap();
        let want = jproof(want);
        assert_eq!(got, want, "path {idx}");
        assert!(got.verify().unwrap());
        // A corrupted sibling must fail.
        let mut bad = got.clone();
        bad.siblings[0][31] ^= 1;
        assert!(!bad.verify().unwrap());
    }

    // Leaf replacement (the aggregator's update path).
    let rep = &t["replace"];
    let idx = rep["index"].as_u64().unwrap() as usize;
    tree.set_leaf(idx, jw(&rep["leaf"])).unwrap();
    assert_eq!(tree.root().unwrap(), jw(&rep["root"]));
    assert_eq!(tree.path(idx).unwrap(), jproof(&rep["path"]));

    // Event-log reconstruction.
    let rebuilt = IdentityMerkleTree::from_leaves(tree.leaves(), depth).unwrap();
    assert_eq!(rebuilt.root().unwrap(), tree.root().unwrap());
}

#[test]
fn registry_schnorr_replay() {
    let v = fixture();
    let s = &v["registry_schnorr"];
    let msg: [u8; 32] = hex_bytes(s["msg_hash"].as_str().unwrap())
        .try_into()
        .unwrap();
    let rid = s["registry_id"].as_str().unwrap();
    let chainid = {
        let mut w = [0u8; 32];
        w[31] = s["chainid"].as_u64().unwrap() as u8;
        w
    };
    let sig = registry_schnorr_sign(&jw(&s["sk"]), &msg, rid, &chainid, &jw(&s["k"])).unwrap();
    assert_eq!(sig.e, jw(&s["proof"]["e"]));
    assert_eq!(sig.s, jw(&s["proof"]["s"]));
    assert_eq!(sig.r, jg1(&s["proof"]["R"]));
    assert!(registry_schnorr_verify(&jg1(&s["pk"]), &sig, &msg, rid, &chainid).unwrap());
    assert!(!registry_schnorr_verify(&jg1(&s["pk"]), &sig, &msg, "other-registry", &chainid).unwrap());
}

#[test]
fn certificate_replay() {
    let v = fixture();
    let c = &v["certificate"];
    let chainid = {
        let mut w = [0u8; 32];
        w[31] = c["chainid"].as_u64().unwrap() as u8;
        w
    };
    let signed = registry_sign_certificate(
        &jw(&c["registry_sk"]),
        c["registry_id"].as_str().unwrap(),
        c["canonical_identity"].as_str().unwrap(),
        c["serial"].as_u64().unwrap(),
        c["issued_at"].as_i64().unwrap(),
        c["expires_at"].as_i64().unwrap(),
        &chainid,
        &jw(&c["k"]),
    )
    .unwrap();

    assert_eq!(signed.cert.m(), jw(&c["m"]));
    assert_eq!(signed.cert.m_point, jg1(&c["M"]));
    assert_eq!(
        signed.cert.to_hash_bytes().to_vec(),
        hex_bytes(c["hash_bytes"].as_str().unwrap())
    );
    assert_eq!(signed.signature.e, jw(&c["signature"]["e"]));
    assert_eq!(signed.signature.s, jw(&c["signature"]["s"]));
    assert_eq!(signed.signature.r, jg1(&c["signature"]["R"]));

    // Wire formats, byte for byte, and their round-trips.
    assert_eq!(
        signed.cert.serialize(),
        hex_bytes(c["cert_wire"].as_str().unwrap())
    );
    assert_eq!(
        signed.serialize(),
        hex_bytes(c["signed_wire"].as_str().unwrap())
    );
    assert_eq!(
        SignedCertificate::deserialize(&signed.serialize()).unwrap(),
        signed
    );

    assert!(registry_verify_certificate(&signed, &chainid).unwrap());
    let wrong_chainid = {
        let mut w = [0u8; 32];
        w[31] = 2;
        w
    };
    assert_eq!(
        registry_verify_certificate(&signed, &wrong_chainid).unwrap(),
        c["wrong_chainid_verifies"].as_bool().unwrap()
    );

    // Sealing.
    let sealed = seal_certificate(&signed, &jg1(&c["client_pk"]), &jw(&c["r_seal"])).unwrap();
    assert_eq!(sealed.ct, jct(&c["sealed_ct"]));
    assert_eq!(
        sealed.envelope(),
        hex_bytes(c["sealed_envelope"].as_str().unwrap())
    );
    let reparsed = buck_registry::certificate::SealedCertificate::from_envelope(&sealed.envelope())
        .unwrap();
    assert_eq!(reparsed, sealed);
    assert_eq!(
        unseal_certificate(&sealed, &jw(&c["client_sk"])).unwrap(),
        signed
    );
    // The wrong client key must be rejected.
    let mut wrong_sk = jw(&c["client_sk"]);
    wrong_sk[31] ^= 1;
    assert!(unseal_certificate(&sealed, &wrong_sk).is_err());
}

#[test]
fn aggregator_replay() {
    let v = fixture();
    let a = &v["aggregator"];
    let identity = jg1(&a["identity"]);

    // Rebuild the sub-trees.
    let reg_a_leaves: Vec<W256> = a["reg_a"]["leaves"].as_array().unwrap().iter().map(jw).collect();
    let reg_b_leaves: Vec<W256> = a["reg_b"]["leaves"].as_array().unwrap().iter().map(jw).collect();
    let mut reg_a = IdentityMerkleTree::new(12).unwrap();
    reg_a.insert_batch(&reg_a_leaves[..3]);
    let mut reg_b = IdentityMerkleTree::new(12).unwrap();
    reg_b.insert_batch(&reg_b_leaves);
    let mut feat = FeatureAuthority::new("feature:age-over-18", 10, false).unwrap();

    let mut svc = CentralMerkleService::new(a["depth"].as_u64().unwrap() as usize).unwrap();
    let ts = |i: u64| 1770000100.0 + i as f64;
    svc.enroll("ca-ab-2026", "kyc", reg_a.root().unwrap(), ts(0)).unwrap();
    svc.enroll("ca-bc-2026", "kyc", reg_b.root().unwrap(), ts(1)).unwrap();
    svc.enroll(
        "feature:age-over-18",
        "feature",
        feat.sub_root().unwrap(),
        ts(2),
    )
    .unwrap();
    assert_eq!(svc.identity_root().unwrap(), jw(&a["root_after_enroll"]));

    // Duplicate enrollment must be rejected.
    assert!(svc.enroll("ca-ab-2026", "kyc", reg_a.root().unwrap(), ts(0)).is_err());

    feat.attest(&identity, None, ts(3), Some([0x11; 32])).unwrap();
    assert_eq!(
        svc.update_sub_root("feature:age-over-18", feat.sub_root().unwrap(), ts(3))
            .unwrap(),
        jw(&a["root_after_attest"])
    );

    reg_a.insert_leaf(reg_a_leaves[3]);
    assert_eq!(
        svc.update_sub_root("ca-ab-2026", reg_a.root().unwrap(), ts(4))
            .unwrap(),
        jw(&a["root_final"])
    );

    // Sub-tree + full + composed proofs.
    let sub_proof = reg_a.path(0).unwrap();
    assert_eq!(sub_proof, jproof(&a["sub_proof"]));
    let full = svc
        .full_proof("ca-ab-2026", sub_proof.clone(), identity.0, identity.1)
        .unwrap();
    let fp = &a["full_proof"]["aggregator"];
    assert_eq!(full.aggregator_proof.sub_root, jw(&fp["sub_root"]));
    assert_eq!(full.aggregator_proof.aggregator_root, jw(&fp["aggregator_root"]));
    assert_eq!(
        full.aggregator_proof.aggregator_leaf_index,
        fp["leaf_index"].as_u64().unwrap() as usize
    );
    assert_eq!(
        full.verify().unwrap(),
        a["full_proof"]["verify"].as_bool().unwrap()
    );

    let feat_proof = feat.membership_proof_for_identity(&identity).unwrap().unwrap();
    assert_eq!(feat_proof, jproof(&a["feature_proof"]));
    let composed = svc
        .composed_proof(
            vec![
                ("ca-ab-2026".to_string(), sub_proof),
                ("feature:age-over-18".to_string(), feat_proof),
            ],
            identity.0,
            identity.1,
        )
        .unwrap();
    assert_eq!(
        composed.verify().unwrap(),
        a["composed_verify"].as_bool().unwrap()
    );

    let agg = svc.aggregator_proof("ca-bc-2026").unwrap();
    let ab = &a["agg_proof_b"];
    assert_eq!(agg.sub_root, jw(&ab["sub_root"]));
    assert_eq!(agg.aggregator_root, jw(&ab["aggregator_root"]));
    assert_eq!(
        agg.aggregator_leaf_index,
        ab["leaf_index"].as_u64().unwrap() as usize
    );
    assert!(agg.verify().unwrap());
}

#[test]
fn feature_authority_replay() {
    let v = fixture();
    let f = &v["feature_authority"];
    let pa = jg1(&f["points"][0]);
    let pb = jg1(&f["points"][1]);
    let mut fa = FeatureAuthority::new(
        f["id"].as_str().unwrap(),
        f["depth"].as_u64().unwrap() as usize,
        false,
    )
    .unwrap();
    fa.attest(&pa, None, 1770000100.0, None).unwrap();
    fa.attest(&pb, None, 1770000101.0, Some([0x22; 32])).unwrap();
    assert_eq!(fa.sub_root().unwrap(), jw(&f["root_after_two"]));
    assert_eq!(fa.attest(&pa, None, 0.0, None).is_err(), f["dup_rejected"].as_bool().unwrap());
    assert_eq!(
        fa.revoke(&pa).unwrap(),
        Some(f["revoked_index"].as_u64().unwrap() as usize)
    );
    assert_eq!(fa.sub_root().unwrap(), jw(&f["root_after_revoke"]));
    assert_eq!(fa.has_identity(&pa).unwrap(), f["has_pa"].as_bool().unwrap());
    assert_eq!(fa.has_identity(&pb).unwrap(), f["has_pb"].as_bool().unwrap());

    // The prefix convention is enforced.
    assert!(FeatureAuthority::new("age-over-18", 10, false).is_err());
}


// ---- phase 5 of the accumulator plan ----------------------------------------

fn leaves(v: &serde_json::Value) -> Vec<W256> {
    v.as_array().unwrap().iter().map(jw).collect()
}

fn tree_of(v: &serde_json::Value, depth: usize) -> IdentityMerkleTree {
    IdentityMerkleTree::from_leaves(&leaves(v), depth).unwrap()
}

#[test]
fn salt_replay() {
    let v = fixture();
    let s = &v["salt"];
    let secret = jw(&s["secret"]);
    for (id, tag) in s["tree_tags"].as_object().unwrap() {
        assert_eq!(buck_identity::salt::tree_tag(id).unwrap(), jw(tag), "{id}");
    }
    for c in s["cases"].as_array().unwrap() {
        let got = buck_identity::salt::derive_salt(
            &secret,
            c["tree_id"].as_str().unwrap(),
            c["counter"].as_u64().unwrap(),
        )
        .unwrap();
        assert_eq!(got, jw(&c["salt"]));
    }
    assert!(buck_identity::salt::derive_salt(&[0u8; 32], "kyc:x", 0).is_err());
    assert!(buck_identity::salt::tree_tag("").is_err());
}

#[test]
fn feature_private_replay() {
    let v = fixture();
    let f = &v["feature_private"];
    let (qa, qb) = (jg1(&f["points"][0]), jg1(&f["points"][1]));
    let (sa, sb) = (jw(&f["salts"][0]), jw(&f["salts"][1]));
    let mut fa =
        FeatureAuthority::new(f["id"].as_str().unwrap(), f["depth"].as_u64().unwrap() as usize, true)
            .unwrap();
    assert_eq!(fa.attest(&qa, Some(&sa), 0.0, None).unwrap().leaf, jw(&f["leaf_a"]));
    fa.attest(&qb, Some(&sb), 0.0, None).unwrap();
    assert_eq!(fa.sub_root().unwrap(), jw(&f["root_after_two"]));
    assert_eq!(fa.membership_proof_for_identity(&qa).unwrap().unwrap(), jproof(&f["proof_a"]));
    let seven = buck_identity::g1_mul(&buck_identity::g1_generator(), &{
        let mut w = [0u8; 32];
        w[31] = 7;
        w
    })
    .unwrap();
    assert_eq!(fa.attest(&seven, None, 0.0, None).is_err(), f["no_salt_refused"].as_bool().unwrap());
    assert_eq!(fa.attest(&qa, Some(&sa), 0.0, None).is_err(), f["dup_refused"].as_bool().unwrap());
    assert_eq!(fa.revoke(&qa).unwrap(), Some(f["revoked_index"].as_u64().unwrap() as usize));
    assert_eq!(fa.sub_root().unwrap(), jw(&f["root_after_revoke"]));
    assert_eq!(fa.has_identity(&qa).unwrap(), f["has_a"].as_bool().unwrap());
    assert_eq!(fa.has_identity(&qb).unwrap(), f["has_b"].as_bool().unwrap());
    // A public subtree refuses a salt.
    let mut public = FeatureAuthority::new("feature:x", 4, false).unwrap();
    assert!(public.attest(&qa, Some(&sa), 0.0, None).is_err());
}

#[test]
fn root_ring_replay() {
    let v = fixture();
    let r = &v["root_ring"];
    let mut svc = CentralMerkleService::new(r["depth"].as_u64().unwrap() as usize).unwrap();
    let id = r["sub_tree_id"].as_str().unwrap();
    let mut one = [0u8; 32];
    one[31] = 1;
    svc.enroll(id, "kyc", one, r["enroll_ts"].as_f64().unwrap()).unwrap();
    for p in r["posts"].as_array().unwrap() {
        let mut sub = [0u8; 32];
        sub[24..].copy_from_slice(&p["sub_root"].as_u64().unwrap().to_be_bytes());
        let at = p["at"].as_f64().unwrap();
        svc.update_sub_root(id, sub, at).unwrap();
        let rec = svc.post(at).unwrap();
        assert_eq!(rec.root, jw(&p["root"]));
        assert_eq!(rec.sequence, p["sequence"].as_u64().unwrap());
    }
    let posts = r["posts"].as_array().unwrap();
    let r0 = jw(&posts[0]["root"]);
    let r1 = jw(&posts[1]["root"]);
    assert_eq!(svc.root_record(&r0).is_some(), r["r0_retained"].as_bool().unwrap());
    assert_eq!(svc.root_record(&r1).unwrap().posted_at, r["r1_posted_at"].as_f64().unwrap());
    let now = r["now"].as_f64().unwrap();
    assert_eq!(svc.max_retained_age(now), r["max_retained_age"].as_f64().unwrap());
    for a in r["accepts"].as_array().unwrap() {
        assert_eq!(
            svc.accepts(&jw(&a["root"]), a["max_age"].as_f64().unwrap(), now),
            a["want"].as_bool().unwrap()
        );
    }
}

#[test]
fn composed_path_replay() {
    let v = fixture();
    let c = &v["composed"];
    let depth = c["depth"].as_u64().unwrap() as usize;
    let sub_depth = c["sub_depth"].as_u64().unwrap() as usize;
    let mut svc = CentralMerkleService::new(depth).unwrap();
    svc.enroll("kyc:neighbour", "kyc", tree_of(&c["neighbour_leaves"], sub_depth).root().unwrap(), 0.0)
        .unwrap();
    let kyc = tree_of(&c["kyc_leaves"], sub_depth);
    svc.enroll("kyc:ca-ab-2026", "kyc", kyc.root().unwrap(), 0.0).unwrap();
    let sub = kyc.path(1).unwrap();
    assert_eq!(sub, jproof(&c["sub_proof"]));
    let full = svc.full_proof("kyc:ca-ab-2026", sub, [0u8; 32], [0u8; 32]).unwrap();
    let comp = full.composed();
    assert_eq!(comp, jproof(&c["composed"]));
    assert!(comp.verify().unwrap());
    assert_eq!(comp.root, svc.identity_root().unwrap());
}

fn jenvelope(e: &serde_json::Value, scopes: BTreeSet<W256>) -> InsurerEnvelope {
    InsurerEnvelope::new(
        true,
        e["face_band"].as_u64().unwrap() as u8,
        e["dep_types"].as_array().unwrap().iter().map(|d| d.as_u64().unwrap() as u8).collect(),
        e["max_dep_rate"].as_u64().unwrap() as u32,
        e["max_premium_rate"].as_u64().unwrap() as u32,
        e["expires_at"].as_f64().unwrap(),
        scopes,
    )
    .unwrap()
}

#[test]
fn regulator_replay() {
    let v = fixture();
    let r = &v["regulator"];
    let mut reg =
        InsuranceRegulator::new(r["jurisdiction"].as_str().unwrap(), r["depth"].as_u64().unwrap() as usize)
            .unwrap();
    let (ins, other) = (jg1(&r["insurer"]), jg1(&r["other"]));
    let env = reg.attest(&ins, &jenvelope(&r["envelope"], BTreeSet::new()), &["asset:bicycle"], true).unwrap();
    reg.attest(&other, &jenvelope(&r["other_envelope"], BTreeSet::new()), &["asset:car"], false).unwrap();
    let want_scopes: Vec<W256> = r["scopes"].as_array().unwrap().iter().map(jw).collect();
    assert_eq!(env.scopes.iter().copied().collect::<Vec<_>>(), want_scopes);
    let names = reg.predicate_names(&env, &["asset:bicycle"]);
    let want: Vec<&str> = r["predicate_names"].as_array().unwrap().iter().map(|n| n.as_str().unwrap()).collect();
    assert_eq!(names, want);
    for (n, k) in names.iter().zip(r["subtree_keys"].as_array().unwrap()) {
        assert_eq!(subtree_key(n).unwrap(), jw(k));
    }
    assert_eq!(reg.membership_proof(&ins, "insurer:face:5").unwrap().unwrap(), jproof(&r["face_proof"]));
    assert_eq!(reg.revoke(&other).unwrap(), r["cleared_other"].as_u64().unwrap() as usize);
    let got: Vec<(String, W256)> = reg.sub_roots().unwrap();
    for (pair, (n, root)) in r["sub_roots_after_revoke"].as_array().unwrap().iter().zip(got.iter()) {
        assert_eq!(pair[0].as_str().unwrap(), n);
        assert_eq!(jw(&pair[1]), *root);
    }
    assert_eq!(got.len(), r["sub_roots_after_revoke"].as_array().unwrap().len());
    for b in r["bands"].as_array().unwrap() {
        assert_eq!(band_for_face(b[0].as_str().unwrap().parse().unwrap()), b[1].as_u64().unwrap() as u8);
    }
    for c in r["cases"].as_array().unwrap() {
        let got = check_issuance(
            &env,
            &jw(&c["scope"]),
            c["face"].as_str().unwrap().parse().unwrap(),
            c["dep_type"].as_u64().unwrap() as u8,
            c["dep_rate"].as_u64().unwrap() as u32,
            c["premium_rate"].as_u64().unwrap() as u32,
            c["now"].as_f64().unwrap(),
        );
        let want = c["want"].as_str().unwrap();
        match got {
            Ok(()) => assert_eq!(want, ""),
            Err(e) => assert_eq!(e.0, want),
        }
    }
    assert!(env.scopes.contains(&scope_id(&reg.scope_name("asset:bicycle")).unwrap()));
}

#[test]
fn attributes_replay() {
    let v = fixture();
    let a = &v["attributes"];
    let depth = a["depth"].as_u64().unwrap() as usize;
    let kyc = tree_of(&a["kyc_leaves"], 12);
    let age = tree_of(&a["age_leaves"], 10);
    let mut svc = CentralMerkleService::new(depth).unwrap();
    svc.enroll("kyc:ca-ab-2026", "kyc", kyc.root().unwrap(), 0.0).unwrap();
    svc.enroll("feature:age-over-18", "feature", age.root().unwrap(), 0.0).unwrap();
    svc.post(a["posted_at"].as_f64().unwrap()).unwrap();
    let person = jg1(&a["person"]);
    let ap = prove_attributes(
        &svc,
        vec![
            ("kyc:ca-ab-2026".to_string(), kyc.path(0).unwrap()),
            ("feature:age-over-18".to_string(), age.path(0).unwrap()),
        ],
        person.0,
        person.1,
    )
    .unwrap();
    assert_eq!(ap.root, jw(&a["root"]));
    for c in a["verify"].as_array().unwrap() {
        let req: Vec<&str> = c["required"].as_array().unwrap().iter().map(|r| r.as_str().unwrap()).collect();
        let got = verify_attributes(&svc, &ap, &req, c["max_age"].as_f64().unwrap(), c["now"].as_f64().unwrap())
            .unwrap();
        assert_eq!(got, c["want"].as_bool().unwrap());
    }
    assert!(verify_attributes(&svc, &ap, &[], 1.0, 0.0).is_err());
}
