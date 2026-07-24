//! Golden vectors: buck-registry must agree bit-for-bit with the Python
//! reference (`alberta_buck/registry`), via the nonce-inclusive fixture
//! `core/vectors/registry-kernel-vectors.json` emitted by
//! `alberta_buck.registry.kernel_vectors`.

use buck_identity::W256;
use buck_registry::aggregator::CentralMerkleService;
use buck_registry::certificate::*;
use buck_registry::feature::FeatureAuthority;
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
        "/../../vectors/registry-kernel-vectors.json"
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
    let mut feat = FeatureAuthority::new("feature:age-over-18", 10).unwrap();

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

    feat.attest(&identity, ts(3), Some([0x11; 32])).unwrap();
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
    )
    .unwrap();
    fa.attest(&pa, 1770000100.0, None).unwrap();
    fa.attest(&pb, 1770000101.0, Some([0x22; 32])).unwrap();
    assert_eq!(fa.sub_root().unwrap(), jw(&f["root_after_two"]));
    assert_eq!(fa.attest(&pa, 0.0, None).is_err(), f["dup_rejected"].as_bool().unwrap());
    assert_eq!(
        fa.revoke(&pa).unwrap(),
        Some(f["revoked_index"].as_u64().unwrap() as usize)
    );
    assert_eq!(fa.sub_root().unwrap(), jw(&f["root_after_revoke"]));
    assert_eq!(fa.has_identity(&pa).unwrap(), f["has_pa"].as_bool().unwrap());
    assert_eq!(fa.has_identity(&pb).unwrap(), f["has_pb"].as_bool().unwrap());

    // The prefix convention is enforced.
    assert!(FeatureAuthority::new("age-over-18", 10).is_err());
}
