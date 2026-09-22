//! Golden vectors: the kernel must agree bit-for-bit with the Python
//! reference (`alberta_buck/wallet`), whose emitted fixture
//! `test/vectors/identity.json` also drives the Solidity verifier tests.
//!
//! Coverage here: every deterministic recomputation the fixture allows
//! (identity scalars, ElGamal encryptions with recorded randomness,
//! commitments, nullifiers, batch hashes) plus every verifier over the
//! recorded proofs, with negative variants.  Prove-path replay (recorded
//! nonces) lives in `core/vectors/identity-kernel-vectors.json` once the
//! Python emitter lands.

use buck_identity::*;

// ---------------------------------------------------------------------------
// Small JSON/hex helpers (dev-dependency serde_json; no hex crate)
// ---------------------------------------------------------------------------

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

fn jw(v: &serde_json::Value) -> W256 {
    hex_w(v.as_str().expect("expected hex string"))
}

fn jg1(v: &serde_json::Value) -> G1w {
    (jw(&v["x"]), jw(&v["y"]))
}

fn jct(v: &serde_json::Value) -> (G1w, G1w) {
    (jg1(&v["R"]), jg1(&v["C"]))
}

fn jg2(v: &serde_json::Value) -> G2w {
    (
        (jw(&v["x"][0]), jw(&v["x"][1])),
        (jw(&v["y"][0]), jw(&v["y"][1])),
    )
}

fn fixture() -> serde_json::Value {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/vectors/identity.json"
    );
    let txt = std::fs::read_to_string(path)
        .unwrap_or_else(|e| panic!("cannot read {path}: {e} (run: make nix-vectors-identity)"));
    serde_json::from_str(&txt).unwrap()
}

// ---------------------------------------------------------------------------
// Curve / hash sanity against py_ecc-derived constants
// ---------------------------------------------------------------------------

#[test]
fn sanity_curve_and_hashes() {
    assert_eq!(
        order(),
        hex_w("0x30644e72e131a029b85045b68181585d2833e84879b9709143e1f593f0000001")
    );

    // 2*G1 (py_ecc: multiply(G1, 2))
    let two = {
        let mut w = [0u8; 32];
        w[31] = 2;
        w
    };
    let g2x = g1_mul(&g1_generator(), &two).unwrap();
    assert_eq!(
        g2x.0,
        hex_w("0x030644e72e131a029b85045b68181585d97816a916871ca8d3c208c16d87cfd3")
    );
    assert_eq!(
        g2x.1,
        hex_w("0x15ed738c0e0a7c92e7845f96b2ae9c0a68a6a449e3538fc7ff3ebf7a5a18a2c4")
    );
    // add(G1, G1) == 2*G1; neg roundtrip
    assert_eq!(g1_add(&g1_generator(), &g1_generator()).unwrap(), g2x);
    let inf = g1_add(&g1_generator(), &g1_neg(&g1_generator()).unwrap()).unwrap();
    assert_eq!(inf, (ZERO_W, ZERO_W));

    // G2 generator coords (py_ecc FQ2 coeffs order [c0, c1])
    let g2 = g2_generator();
    assert_eq!(
        g2.0 .0,
        hex_w("0x1800deef121f1e76426a00665e5c4479674322d4f75edadd46debd5cd992f6ed")
    );
    assert_eq!(
        g2.0 .1,
        hex_w("0x198e9393920d483a7260bfb731fb5d25f1aa493335a9e71297e485b7aef312c2")
    );
    assert_eq!(
        g2.1 .0,
        hex_w("0x12c85ea5db8c6deb4aab71808dcb408fe3d1e7690c43d37b4ce6cc0166fa7daa")
    );
    assert_eq!(
        g2.1 .1,
        hex_w("0x090689d0585ff075ec9e99ad690c3395bc4b313370b38ef355acdadcd122975b")
    );

    // H_PEDERSEN = hash_to_curve("AlbertaBuck/Pedersen/H/v2"), the Python reference's point
    let h = nums::h_pedersen();
    assert_eq!(
        h.0,
        hex_w("0x0a348afa1b7f9d733f1859da101a77b2eb9b8cb66c00f5ffc3f8cd384de9f862")
    );
    assert_eq!(
        h.1,
        hex_w("0x1e8103c6fd7743dde39c36379e3a19b80e6bc98a989c6df8786da5c9bedecd42")
    );

    // e(G1, G2) * e(-G1, G2) == 1
    assert!(pairing::pairing_check(&[
        (g1_generator(), g2_generator()),
        (g1_neg(&g1_generator()).unwrap(), g2_generator()),
    ])
    .unwrap());

    // Poseidon vs the Python reference (circomlibjs-identical)
    let w = |v: u64| {
        let mut w = [0u8; 32];
        w[24..].copy_from_slice(&v.to_be_bytes());
        w
    };
    let dec = |s: &str| {
        // decimal string -> W256 via u128 halves is overkill; go through hex
        let n = s.parse::<num_dec::BigDec>().unwrap();
        n.to_w()
    };
    // poseidon([1,2])
    assert_eq!(
        poseidon::poseidon(&[w(1), w(2)]).unwrap(),
        dec("7853200120776062878684798364095072458815029376092732009249414926327459813530")
    );
    assert_eq!(
        poseidon::poseidon(&[w(1), w(2), w(3), w(4), w(5)]).unwrap(),
        dec("6183221330272524995739186171720101788151706631170188140075976616310159254464")
    );
    assert_eq!(
        poseidon::poseidon(&[w(1), w(2), w(3), w(4), w(5), w(6), w(7), w(8), w(9)]).unwrap(),
        dec("13589767895268936107593642967621470491511464502761040466226072462545218539640")
    );

    // keccak_scalar(1, 2, 3)
    assert_eq!(
        keccak::keccak_scalar(&[w(1), w(2), w(3)]),
        hex_w("0x0d43c5933e4f0b80c25defb26e3c6a4cf3a149d3772d0383d254f4c541f2949a")
    );
}

/// Minimal decimal-string -> W256 support for the poseidon reference values.
mod num_dec {
    use std::str::FromStr;

    pub struct BigDec(pub [u8; 32]);

    impl BigDec {
        pub fn to_w(&self) -> [u8; 32] {
            self.0
        }
    }

    impl FromStr for BigDec {
        type Err = ();
        fn from_str(s: &str) -> Result<Self, ()> {
            // Repeated divide-by-2^8 on a decimal string is clumsy; use
            // 128-bit chunk math instead: value = hi * 10^19... simpler:
            // accumulate into [u8; 32] via multiply-by-10-and-add.
            let mut acc = [0u8; 32];
            for ch in s.bytes() {
                let d = (ch - b'0') as u16;
                let mut carry = d;
                for b in acc.iter_mut().rev() {
                    let v = (*b as u16) * 10 + carry;
                    *b = (v & 0xff) as u8;
                    carry = v >> 8;
                }
                assert_eq!(carry, 0, "decimal overflows 256 bits");
            }
            Ok(BigDec(acc))
        }
    }
}

// ---------------------------------------------------------------------------
// Golden fixture: test/vectors/identity.json
// ---------------------------------------------------------------------------

#[test]
fn golden_identity_fixture() {
    let v = fixture();
    assert_eq!(v["$schema_version"], 2);
    let chainid = jw(&v["chainid"]);
    let registry = jw(&v["registry"]);

    let iss_x = jg2(&v["issuer"]["pk_X"]);
    let iss_y = jg2(&v["issuer"]["pk_Y"]);
    assert!(ps::ps_key_consistent(&iss_y, &jg1(&v["issuer"]["pk_Y1"])).unwrap(), "issuer Y1");

    for who in ["alice", "bob"] {
        let p = &v[who];
        let canonical = p["canonical_identity_data"].as_str().unwrap();
        let m = jw(&p["m"]);
        let m_pt = jg1(&p["M"]);
        let pk = jg1(&p["elgamal_kp"]["pk"]);
        let e_ct = jct(&p["ciphertext"]);
        let registrant = jw(&p["registrant"]);

        // m = keccak(canonical) % ORDER ; M = m*G
        assert_eq!(keccak::identity_scalar(canonical.as_bytes()), m, "{who} m");
        assert_eq!(g1_mul(&g1_generator(), &m).unwrap(), m_pt, "{who} M");

        // E = ElGamal(M, pk; r) with the recorded r
        let r = jw(&p["r"]);
        assert_eq!(
            elgamal::elgamal_encrypt(&m_pt, &pk, &r).unwrap(),
            e_ct,
            "{who} ciphertext"
        );

        // The RAW credential verifies under the issuer key ...
        {
            let s1 = jg1(&p["ps_sig_raw"]["sigma_1"]);
            let s2 = jg1(&p["ps_sig_raw"]["sigma_2"]);
            assert!(ps::ps_verify(&iss_x, &iss_y, &s1, &s2, &m).unwrap(), "{who} raw");
        }
        // ... and the PUBLISHED presentation (A, B) does not: it is not a
        // signature on m (review finding R1 closed).
        let pres = (jg1(&p["ps_presentation"]["A"]), jg1(&p["ps_presentation"]["B"]));
        assert!(!ps::ps_verify(&iss_x, &iss_y, &pres.0, &pres.1, &m).unwrap(), "{who} presentation");
        assert_eq!(
            ps::ps_present(
                &jg1(&p["ps_sig_raw"]["sigma_1"]), &jg1(&p["ps_sig_raw"]["sigma_2"]),
                &jg1(&v["issuer"]["pk_Y1"]), &jw(&p["a"]), &jw(&p["b"])
            ).unwrap(),
            pres, "{who} presentation replay"
        );
        // ... and not for a different message
        let m_bad = jw(&v[if who == "alice" { "bob" } else { "alice" }]["m"]);
        let s1 = jg1(&p["ps_sig_raw"]["sigma_1"]);
        let s2 = jg1(&p["ps_sig_raw"]["sigma_2"]);
        assert!(!ps::ps_verify(&iss_x, &iss_y, &s1, &s2, &m_bad).unwrap());

        // Registration NIZK verifies; wrong registrant is rejected
        let pf = &p["registration_proof"];
        let proof = nizk::RegistrationProof {
            e: jw(&pf["e"]),
            s_m: jw(&pf["s_m"]),
            s_b: jw(&pf["s_b"]),
            s_r: jw(&pf["s_r"]),
            s_sk: jw(&pf["s_sk"]),
            c1: jg1(&pf["C1"]),
            t_c: jg1(&pf["T_C"]),
            t_r: jg1(&pf["T_R"]),
            t_key: jg1(&pf["T_key"]),
        };
        assert!(nizk::registration_verify(
            &pres.0, &pres.1, &e_ct, &pk, &iss_x, &iss_y, &proof,
            &registrant, &chainid, &registry
        )
        .unwrap());
        let mut wrong = registrant;
        wrong[31] ^= 1;
        assert!(!nizk::registration_verify(
            &pres.0, &pres.1, &e_ct, &pk, &iss_x, &iss_y, &proof,
            &wrong, &chainid, &registry
        )
        .unwrap());
    }

    // ---- unicode canonical-dialect pin ----------------------------------
    //
    // Raw UTF-8 (Latin accents + CJK, no \uXXXX escapes) must hash to the
    // recorded identity scalar -- an implementation that escapes or
    // re-encodes the canonical bytes fails here.
    let up = &v["unicode_party"];
    let canonical = up["canonical_identity_data"].as_str().unwrap();
    assert!(canonical.contains("Chloé") && canonical.contains("李"));
    assert!(!canonical.contains("\\u"));
    assert_eq!(keccak::identity_scalar(canonical.as_bytes()), jw(&up["m"]));
    assert_eq!(
        g1_mul(&g1_generator(), &jw(&up["m"])).unwrap(),
        jg1(&up["M"])
    );

    // ---- approve: Chaum-Pedersen re-encryption -------------------------
    let ap = &v["approve"];
    let e_alice = jct(&ap["E_alice"]);
    let e_for_bob = jct(&ap["E_for_bob"]);
    let pk_a = jg1(&v["alice"]["elgamal_kp"]["pk"]);
    let pk_b = jg1(&v["bob"]["elgamal_kp"]["pk"]);
    let sender = jw(&ap["sender"]);
    let spender = jw(&ap["spender"]);

    // E_for_bob = ElGamal(alice.M, bob.pk; r_prime)
    assert_eq!(
        elgamal::elgamal_encrypt(&jg1(&v["alice"]["M"]), &pk_b, &jw(&ap["r_prime"])).unwrap(),
        e_for_bob
    );

    let cp = &ap["cp_proof"];
    let cp_proof = chaum_pedersen::CpProof {
        e: jw(&cp["e"]),
        s1: jw(&cp["s1"]),
        s2: jw(&cp["s2"]),
        t1: jg1(&cp["T1"]),
        t2: jg1(&cp["T2"]),
        t3: jg1(&cp["T3"]),
    };
    assert!(chaum_pedersen::chaum_pedersen_verify(
        &e_alice, &e_for_bob, &pk_a, &pk_b, &cp_proof, &sender, &spender,
        &chainid, &jw(&ap["registry"])
    )
    .unwrap());
    // swapped sender/spender must fail
    assert!(!chaum_pedersen::chaum_pedersen_verify(
        &e_alice, &e_for_bob, &pk_a, &pk_b, &cp_proof, &spender, &sender,
        &chainid, &jw(&ap["registry"])
    )
    .unwrap());

    // ---- issuer Schnorr batch binding ----------------------------------
    //
    // batch_commitment returns the UNREDUCED keccak word -- what the
    // Schnorr transcript signs and exactly what the chain computes; the
    // fixture stores it raw.
    let is = &v["issuer_schnorr"];
    let cms: Vec<W256> = is["cms"].as_array().unwrap().iter().map(jw).collect();
    let h_batch = schnorr::batch_commitment(&cms);
    assert_eq!(h_batch, jw(&is["hBatch"]));
    let sp = schnorr::SchnorrProof {
        e: jw(&is["proof"]["e"]),
        s: jw(&is["proof"]["s"]),
        r: jg1(&is["proof"]["R"]),
    };
    let iss_addr = jw(&is["issuer"]);
    assert!(schnorr::issuer_schnorr_verify(&jg1(&is["pk"]), &sp, &h_batch, &iss_addr, &chainid)
        .unwrap());
    let mut bad_batch = h_batch;
    bad_batch[0] ^= 1;
    assert!(!schnorr::issuer_schnorr_verify(
        &jg1(&is["pk"]),
        &sp,
        &bad_batch,
        &iss_addr,
        &chainid
    )
    .unwrap());

    // ---- B1 receipt: opening / commitment / nullifier / batch ----------
    let rc = &v["receipt"];
    let op = &rc["opening"];
    let flavor = u64::from_str_radix(
        op["flavor"].as_str().unwrap().trim_start_matches("0x"),
        16,
    )
    .unwrap();
    let cm = notes::note_commitment(
        flavor,
        &jw(&op["v"]),
        &jw(&op["rho"]),
        &jw(&op["idHash"]),
        &jw(&op["predicate"]),
    )
    .unwrap();
    assert_eq!(cm, jw(&rc["cm"]));
    let rcpt_cms: Vec<W256> = rc["cms"].as_array().unwrap().iter().map(jw).collect();
    assert!(rcpt_cms.contains(&cm));
    let rcpt_h_batch = schnorr::batch_commitment(&rcpt_cms);
    assert_eq!(rcpt_h_batch, jw(&rc["hBatch"]));
    assert_eq!(
        notes::nullifier_b(&jw(&op["rho"]), &jw(&op["idHash"])).unwrap(),
        jw(&rc["nullifier"])
    );
    let rsig = schnorr::SchnorrProof {
        e: jw(&rc["issuer_sig"]["e"]),
        s: jw(&rc["issuer_sig"]["s"]),
        r: jg1(&rc["issuer_sig"]["R"]),
    };
    assert!(schnorr::issuer_schnorr_verify(
        &jg1(&rc["issuer_pk"]),
        &rsig,
        &rcpt_h_batch,
        &jw(&rc["issuer"]),
        &chainid
    )
    .unwrap());

    // ---- approve receipt: verifiable decryption ------------------------
    let ar = &v["approve_receipt"];
    let vd = verifiable_decrypt::VdProof {
        e: jw(&ar["vd_proof"]["e"]),
        s: jw(&ar["vd_proof"]["s"]),
        t1: jg1(&ar["vd_proof"]["T1"]),
        t2: jg1(&ar["vd_proof"]["T2"]),
    };
    assert!(verifiable_decrypt::verifiable_decrypt_verify(
        &jct(&ar["E_for_spender"]),
        &jg1(&ar["spender_pk"]),
        &jg1(&ar["M_named"]),
        &vd,
        &jw(&ar["spender"]),
        &chainid
    )
    .unwrap());
    // naming a different M must fail
    assert!(!verifiable_decrypt::verifiable_decrypt_verify(
        &jct(&ar["E_for_spender"]),
        &jg1(&ar["spender_pk"]),
        &jg1(&v["bob"]["M"]),
        &vd,
        &jw(&ar["spender"]),
        &chainid
    )
    .unwrap());

    // ---- A2 issuer re-encryption binding (SNARK-fixture-pinned) --------
    let ir = &v["issuer_reenc"];
    let pf = &ir["proof"];
    let ir_proof = issuer_reenc::IssuerReencProof {
        e: jw(&pf["e"]),
        s_r: jw(&pf["s_r"]),
        s_b: jw(&pf["s_b"]),
        s_s: jw(&pf["s_s"]),
        s_g: jw(&pf["s_g"]),
        a1: jg1(&pf["A1"]),
        a2: jg1(&pf["A2"]),
        a3: jg1(&pf["A3"]),
        a4: jg1(&pf["A4"]),
        a5: jg1(&pf["A5"]),
        q: jg1(&pf["Q"]),
        u: jg1(&pf["U"]),
        t: jg1(&pf["T"]),
    };
    assert!(issuer_reenc::issuer_reenc_verify(
        &jg1(&ir["pk_iss"]),
        &jct(&ir["E_reg"]),
        &jct(&ir["E_iss"]),
        &ir_proof,
        &jw(&ir["issuer"]),
        &chainid
    )
    .unwrap());
    // tampered E_iss must fail
    let (r_i, mut c_i) = jct(&ir["E_iss"]);
    c_i = jg1(&v["alice"]["M"]);
    assert!(!issuer_reenc::issuer_reenc_verify(
        &jg1(&ir["pk_iss"]),
        &jct(&ir["E_reg"]),
        &(r_i, c_i),
        &ir_proof,
        &jw(&ir["issuer"]),
        &chainid
    )
    .unwrap());
}

// ---------------------------------------------------------------------------
// Nonce-inclusive replay: core/vectors/identity-kernel-vectors.json
// ---------------------------------------------------------------------------

fn kernel_fixture() -> serde_json::Value {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/vectors/identity-kernel-vectors.json"
    );
    let txt = std::fs::read_to_string(path).unwrap_or_else(|e| {
        panic!("cannot read {path}: {e} (run: make nix-venv-core-identity-vectors)")
    });
    serde_json::from_str(&txt).unwrap()
}

#[test]
fn kernel_vectors_replay() {
    let v = kernel_fixture();
    assert_eq!(v["$schema_version"], 2);
    assert_eq!(v["backend"], "py", "vectors must come from the py reference");
    // ---- g1 / g2 ops ----------------------------------------------------
    for row in v["g1_ops"].as_array().unwrap() {
        let a = jg1(&row["A"]);
        let b = jg1(&row["B"]);
        let k = jw(&row["k"]);
        assert_eq!(g1_add(&a, &b).unwrap(), jg1(&row["add"]));
        assert_eq!(g1_mul(&a, &k).unwrap(), jg1(&row["mul"]));
        assert_eq!(g1_neg(&a).unwrap(), jg1(&row["neg"]));
    }
    for row in v["g2_ops"].as_array().unwrap() {
        assert_eq!(
            g2_mul(&g2_generator(), &jw(&row["k"])).unwrap(),
            jg2(&row["mul"])
        );
    }
    for row in v["pairing"].as_array().unwrap() {
        let pairs: Vec<(G1w, G2w)> = row["pairs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| (jg1(&p["g1"]), jg2(&p["g2"])))
            .collect();
        assert_eq!(
            pairing::pairing_check(&pairs).unwrap(),
            row["ok"].as_bool().unwrap()
        );
    }

    // ---- keccak / identity scalar ----------------------------------------
    for row in v["keccak_scalar"].as_array().unwrap() {
        let words: Vec<W256> = row["words"].as_array().unwrap().iter().map(jw).collect();
        assert_eq!(keccak::keccak_scalar(&words), jw(&row["scalar"]));
    }
    for row in v["identity_scalar"].as_array().unwrap() {
        let canonical = row["canonical"].as_str().unwrap();
        assert_eq!(
            keccak::identity_scalar(canonical.as_bytes()),
            jw(&row["m"]),
            "identity_scalar({canonical})"
        );
    }

    // ---- poseidon: every arity ---------------------------------------------
    for row in v["poseidon"].as_array().unwrap() {
        let inputs: Vec<W256> = row["inputs"].as_array().unwrap().iter().map(jw).collect();
        assert_eq!(
            poseidon::poseidon(&inputs).unwrap(),
            jw(&row["hash"]),
            "poseidon arity {}",
            inputs.len()
        );
    }

    // ---- elgamal -------------------------------------------------------------
    for row in v["elgamal"].as_array().unwrap() {
        let m = jg1(&row["M"]);
        let pk = jg1(&row["pk"]);
        let e = jct(&row["E"]);
        assert_eq!(
            elgamal::elgamal_encrypt(&m, &pk, &jw(&row["r"])).unwrap(),
            e
        );
        assert_eq!(elgamal::elgamal_decrypt(&e.0, &e.1, &jw(&row["sk"])).unwrap(), m);
    }

    // ---- ps --------------------------------------------------------------------
    let ps_v = &v["ps"];
    assert_eq!(
        g2_mul(&g2_generator(), &jw(&ps_v["sk_x"])).unwrap(),
        jg2(&ps_v["pk_X"])
    );
    assert_eq!(
        g2_mul(&g2_generator(), &jw(&ps_v["sk_y"])).unwrap(),
        jg2(&ps_v["pk_Y"])
    );
    for row in ps_v["signs"].as_array().unwrap() {
        let m = jw(&row["m"]);
        let sig = ps::ps_sign(&jw(&ps_v["sk_x"]), &jw(&ps_v["sk_y"]), &m, &jw(&row["t"])).unwrap();
        assert_eq!(sig.0, jg1(&row["sigma_1"]));
        assert_eq!(sig.1, jg1(&row["sigma_2"]));
        assert!(ps::ps_verify(&jg2(&ps_v["pk_X"]), &jg2(&ps_v["pk_Y"]), &sig.0, &sig.1, &m).unwrap());
        let rr = ps::ps_rerandomize(&sig.0, &sig.1, &jw(&row["rerand_t"])).unwrap();
        assert_eq!(rr.0, jg1(&row["rerand_sigma_1"]));
        assert_eq!(rr.1, jg1(&row["rerand_sigma_2"]));
        let pres = ps::ps_present(&sig.0, &sig.1, &jg1(&ps_v["pk_Y1"]),
                                  &jw(&row["present_a"]), &jw(&row["present_b"])).unwrap();
        assert_eq!(pres.0, jg1(&row["present_A"]));
        assert_eq!(pres.1, jg1(&row["present_B"]));
        assert!(!ps::ps_verify(&jg2(&ps_v["pk_X"]), &jg2(&ps_v["pk_Y"]), &pres.0, &pres.1, &m).unwrap());
    }
    assert!(ps::ps_key_consistent(&jg2(&ps_v["pk_Y"]), &jg1(&ps_v["pk_Y1"])).unwrap());

    // ---- schnorr -----------------------------------------------------------------
    let sc = &v["schnorr"];
    let cms: Vec<W256> = sc["cms"].as_array().unwrap().iter().map(jw).collect();
    let h_batch = schnorr::batch_commitment(&cms);
    assert_eq!(h_batch, jw(&sc["h_batch_raw"]), "raw unreduced keccak word");
    let sig = schnorr::issuer_schnorr_sign(
        &jw(&sc["sk_iss"]),
        &h_batch,
        &jw(&sc["issuer"]),
        &jw(&sc["chainid"]),
        &jw(&sc["k"]),
    )
    .unwrap();
    assert_eq!(sig.e, jw(&sc["proof"]["e"]));
    assert_eq!(sig.s, jw(&sc["proof"]["s"]));
    assert_eq!(sig.r, jg1(&sc["proof"]["R"]));
    assert!(schnorr::issuer_schnorr_verify(
        &jg1(&sc["pk_iss"]),
        &sig,
        &h_batch,
        &jw(&sc["issuer"]),
        &jw(&sc["chainid"])
    )
    .unwrap());

    // ---- registration NIZK -------------------------------------------------
    let rg = &v["registration"];
    let pres = ps::ps_present(
        &jg1(&rg["sigma_1"]), &jg1(&rg["sigma_2"]), &jg1(&rg["Y1"]),
        &jw(&rg["a"]), &jw(&rg["b"]),
    )
    .unwrap();
    assert_eq!(pres.0, jg1(&rg["A"]));
    assert_eq!(pres.1, jg1(&rg["B"]));
    let proof = nizk::registration_prove(
        &pres.0,
        &pres.1,
        &jw(&rg["b"]),
        &jw(&rg["m"]),
        &jw(&rg["r"]),
        &jg1(&rg["pk"]),
        &jct(&rg["E"]),
        &jw(&rg["registrant"]),
        &jw(&rg["sk"]),
        &jw(&rg["chainid"]),
        &jw(&rg["registry"]),
        &jw(&rg["m_tilde"]),
        &jw(&rg["b_tilde"]),
        &jw(&rg["r_tilde"]),
        &jw(&rg["sk_tilde"]),
    )
    .unwrap();
    assert_eq!(proof.e, jw(&rg["proof"]["e"]));
    assert_eq!(proof.s_m, jw(&rg["proof"]["s_m"]));
    assert_eq!(proof.s_b, jw(&rg["proof"]["s_b"]));
    assert_eq!(proof.s_r, jw(&rg["proof"]["s_r"]));
    assert_eq!(proof.s_sk, jw(&rg["proof"]["s_sk"]));
    assert_eq!(proof.c1, jg1(&rg["proof"]["C1"]));
    assert_eq!(proof.t_c, jg1(&rg["proof"]["T_C"]));
    assert_eq!(proof.t_r, jg1(&rg["proof"]["T_R"]));
    assert_eq!(proof.t_key, jg1(&rg["proof"]["T_key"]));
    assert!(nizk::registration_verify(
        &pres.0,
        &pres.1,
        &jct(&rg["E"]),
        &jg1(&rg["pk"]),
        &jg2(&v["ps"]["pk_X"]),
        &jg2(&v["ps"]["pk_Y"]),
        &proof,
        &jw(&rg["registrant"]),
        &jw(&rg["chainid"]),
        &jw(&rg["registry"]),
    )
    .unwrap());

    // ---- chaum-pedersen ------------------------------------------------------
    let cp = &v["chaum_pedersen"];
    let cpp = chaum_pedersen::chaum_pedersen_prove(
        &jct(&cp["E_a"]),
        &jct(&cp["E_b"]),
        &jg1(&cp["pk_a"]),
        &jg1(&cp["pk_b"]),
        &jw(&cp["sk_a"]),
        &jw(&cp["r_prime"]),
        &jw(&cp["sender"]),
        &jw(&cp["spender"]),
        &jw(&cp["chainid"]),
        &jw(&cp["registry"]),
        &jw(&cp["k1"]),
        &jw(&cp["k2"]),
    )
    .unwrap();
    assert_eq!(cpp.e, jw(&cp["proof"]["e"]));
    assert_eq!(cpp.s1, jw(&cp["proof"]["s1"]));
    assert_eq!(cpp.s2, jw(&cp["proof"]["s2"]));
    assert_eq!(cpp.t1, jg1(&cp["proof"]["T1"]));
    assert_eq!(cpp.t2, jg1(&cp["proof"]["T2"]));
    assert_eq!(cpp.t3, jg1(&cp["proof"]["T3"]));
    assert!(chaum_pedersen::chaum_pedersen_verify(
        &jct(&cp["E_a"]),
        &jct(&cp["E_b"]),
        &jg1(&cp["pk_a"]),
        &jg1(&cp["pk_b"]),
        &cpp,
        &jw(&cp["sender"]),
        &jw(&cp["spender"]),
        &jw(&cp["chainid"]),
        &jw(&cp["registry"]),
    )
    .unwrap());

    // ---- verifiable decryption --------------------------------------------------
    let vd = &v["verifiable_decrypt"];
    let vdp = verifiable_decrypt::verifiable_decrypt_prove(
        &jct(&vd["E"]),
        &jw(&vd["sk"]),
        &jg1(&vd["M"]),
        &jw(&vd["account"]),
        &jw(&vd["chainid"]),
        &jw(&vd["t"]),
    )
    .unwrap();
    assert_eq!(vdp.e, jw(&vd["proof"]["e"]));
    assert_eq!(vdp.s, jw(&vd["proof"]["s"]));
    assert_eq!(vdp.t1, jg1(&vd["proof"]["T1"]));
    assert_eq!(vdp.t2, jg1(&vd["proof"]["T2"]));

    // ---- issuer re-encryption binding ---------------------------------------------
    let ir = &v["issuer_reenc"];
    let irp = issuer_reenc::issuer_reenc_prove(
        &jw(&ir["sk_iss"]),
        &jw(&ir["r_prime"]),
        &jg1(&ir["pk_rec"]),
        &jct(&ir["E_reg"]),
        &jct(&ir["E_iss"]),
        &jw(&ir["issuer"]),
        &jw(&ir["chainid"]),
        &jw(&ir["beta"]),
        &jw(&ir["gamma"]),
        &jw(&ir["k_r"]),
        &jw(&ir["k_b"]),
        &jw(&ir["k_s"]),
        &jw(&ir["k_g"]),
    )
    .unwrap();
    let pf = &ir["proof"];
    assert_eq!(irp.e, jw(&pf["e"]));
    assert_eq!(irp.s_r, jw(&pf["s_r"]));
    assert_eq!(irp.s_b, jw(&pf["s_b"]));
    assert_eq!(irp.s_s, jw(&pf["s_s"]));
    assert_eq!(irp.s_g, jw(&pf["s_g"]));
    assert_eq!(irp.a1, jg1(&pf["A1"]));
    assert_eq!(irp.a2, jg1(&pf["A2"]));
    assert_eq!(irp.a3, jg1(&pf["A3"]));
    assert_eq!(irp.a4, jg1(&pf["A4"]));
    assert_eq!(irp.a5, jg1(&pf["A5"]));
    assert_eq!(irp.q, jg1(&pf["Q"]));
    assert_eq!(irp.u, jg1(&pf["U"]));
    assert_eq!(irp.t, jg1(&pf["T"]));
    assert!(issuer_reenc::issuer_reenc_verify(
        &jg1(&ir["pk_iss"]),
        &jct(&ir["E_reg"]),
        &jct(&ir["E_iss"]),
        &irp,
        &jw(&ir["issuer"]),
        &jw(&ir["chainid"]),
    )
    .unwrap());

    // ---- b1 depositor binding --------------------------------------------------------
    let db = &v["b1_bind"];
    let (dbp, e_dep_for_iss) = b1_binding::b1_bind_prove(
        &jw(&db["m_dep"]),
        &jw(&db["sk_dep"]),
        &jct(&db["E_dep"]),
        &jg1(&db["pk_iss"]),
        &jw(&db["account"]),
        &jw(&db["chainid"]),
        &jw(&db["r"]),
        &jw(&db["b"]),
        &jw(&db["k_m"]),
        &jw(&db["k_s"]),
        &jw(&db["k_r"]),
        &jw(&db["k_b"]),
    )
    .unwrap();
    assert_eq!(e_dep_for_iss, jct(&db["eDepForIss"]));
    let pf = &db["proof"];
    assert_eq!(dbp.e, jw(&pf["e"]));
    assert_eq!(dbp.s_m, jw(&pf["s_m"]));
    assert_eq!(dbp.s_s, jw(&pf["s_s"]));
    assert_eq!(dbp.s_r, jw(&pf["s_r"]));
    assert_eq!(dbp.s_b, jw(&pf["s_b"]));
    assert_eq!(dbp.a2, jg1(&pf["A2"]));
    assert_eq!(dbp.a4, jg1(&pf["A4"]));
    assert_eq!(dbp.b1, jg1(&pf["B1"]));
    assert_eq!(dbp.b2, jg1(&pf["B2"]));
    assert_eq!(dbp.a_p, jg1(&pf["A_p"]));
    assert_eq!(dbp.p_dep, jg1(&pf["P_dep"]));
    assert!(b1_binding::b1_bind_verify(
        &jg1(&db["pk_dep"]),
        &jct(&db["E_dep"]),
        &jg1(&db["pk_iss"]),
        &e_dep_for_iss,
        &dbp,
        &jw(&db["account"]),
        &jw(&db["chainid"]),
    )
    .unwrap());

    // ---- notes family -------------------------------------------------------------
    let nt = &v["notes"];
    assert_eq!(
        notes::id_hash_b1(&jw(&nt["m_issuer"]), &jg1(&nt["sigma_R"]), &jw(&nt["sigma_s"])).unwrap(),
        jw(&nt["id_hash_b1"])
    );
    assert_eq!(
        notes::id_hash_a1(
            &jct(&nt["eNote"]),
            &jw(&nt["m_issuer"]),
            &jg1(&nt["sigma_R"]),
            &jw(&nt["sigma_s"])
        )
        .unwrap(),
        jw(&nt["id_hash_a1"])
    );
    assert_eq!(
        notes::id_hash_a2(&jct(&nt["eNote"]), &jct(&nt["eIss"]), &jg1(&nt["T"])).unwrap(),
        jw(&nt["id_hash_a2"])
    );
    let op = &nt["opening"];
    let flavor =
        u64::from_str_radix(op["flavor"].as_str().unwrap().trim_start_matches("0x"), 16).unwrap();
    assert_eq!(
        notes::note_commitment(
            flavor,
            &jw(&op["v"]),
            &jw(&op["rho"]),
            &jw(&op["idHash"]),
            &jw(&op["predicate"])
        )
        .unwrap(),
        jw(&nt["cm"])
    );
    assert_eq!(
        notes::nullifier_b(&jw(&op["rho"]), &jw(&op["idHash"])).unwrap(),
        jw(&nt["nullifier_b"])
    );
    assert_eq!(
        notes::nullifier_a(&jw(&op["rho"]), &jw(&op["idHash"])).unwrap(),
        jw(&nt["nullifier_a"])
    );
    assert_eq!(
        notes::identity_leaf(&jg1(&nt["identity_leaf_M"])).unwrap(),
        jw(&nt["identity_leaf"])
    );

    // ---- merkle: fold the recorded leaves to the recorded root ---------------------
    let mk = &v["merkle"];
    let depth = mk["depth"].as_u64().unwrap() as usize;
    let leaves: Vec<W256> = mk["leaves"].as_array().unwrap().iter().map(jw).collect();
    // zeros[d] = poseidon(zeros[d-1], zeros[d-1]), zeros[0] = 0
    let mut zeros = vec![ZERO_W; depth + 1];
    for d in 1..=depth {
        zeros[d] = poseidon::poseidon(&[zeros[d - 1], zeros[d - 1]]).unwrap();
    }
    let mut nodes = leaves.clone();
    for d in 0..depth {
        let mut next = Vec::new();
        for pair in nodes.chunks(2) {
            let left = pair[0];
            let right = if pair.len() == 2 { pair[1] } else { zeros[d] };
            next.push(poseidon::poseidon(&[left, right]).unwrap());
        }
        if next.is_empty() {
            next.push(zeros[d + 1]);
        }
        nodes = next;
    }
    assert_eq!(nodes[0], jw(&mk["root"]), "merkle root");
    // path verify: fold leaf up with siblings / index_bits
    let idx = mk["path_index"].as_u64().unwrap() as usize;
    let sibs: Vec<W256> = mk["siblings"].as_array().unwrap().iter().map(jw).collect();
    let bits: Vec<u64> = mk["index_bits"]
        .as_array()
        .unwrap()
        .iter()
        .map(|b| b.as_u64().unwrap())
        .collect();
    let mut cur = leaves[idx];
    for (sib, bit) in sibs.iter().zip(bits.iter()) {
        cur = if *bit == 0 {
            poseidon::poseidon(&[cur, *sib]).unwrap()
        } else {
            poseidon::poseidon(&[*sib, cur]).unwrap()
        };
    }
    assert_eq!(cur, jw(&mk["root"]), "merkle path");
}
