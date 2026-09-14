//! wasm-bindgen binding for the buck-identity kernel.
//!
//! ABI: every 256-bit word is a 0x-hex string (64 nybbles out; any length
//! in); a G1 point is two consecutive words `x, y`; an ElGamal ciphertext
//! four words `Rx, Ry, Cx, Cy`; a G2 point four words `x_c0, x_c1, y_c0,
//! y_c1`.  Multi-value results come back as flat `Vec<String>` in the
//! Python dataclass field order.  `core/js/src/identity.js` wraps this in
//! the BigInt-native structured API -- use that, not this, from JS code.
//!
//! Deterministic: every nonce is an argument; the JS wrapper draws
//! randomness (or replays recorded nonces in tests).

use wasm_bindgen::prelude::*;

use kernel::{G1w, G2w, W256};

// ---------------------------------------------------------------------------
// hex <-> word helpers
// ---------------------------------------------------------------------------

fn w(s: &str) -> Result<W256, JsError> {
    let h = s.strip_prefix("0x").unwrap_or(s);
    if h.len() > 64 || h.is_empty() {
        return Err(JsError::new("word must be 1..64 hex nybbles"));
    }
    let mut out = [0u8; 32];
    // Right-align: parse from the string's end into the word's end.
    let bytes = h.as_bytes();
    let mut i = bytes.len();
    let mut oi = 32;
    while i > 0 {
        let lo = bytes[i - 1];
        let hi = if i >= 2 { bytes[i - 2] } else { b'0' };
        let nyb = |c: u8| -> Result<u8, JsError> {
            match c {
                b'0'..=b'9' => Ok(c - b'0'),
                b'a'..=b'f' => Ok(c - b'a' + 10),
                b'A'..=b'F' => Ok(c - b'A' + 10),
                _ => Err(JsError::new("invalid hex")),
            }
        };
        oi -= 1;
        out[oi] = (nyb(hi)? << 4) | nyb(lo)?;
        i = i.saturating_sub(2);
    }
    Ok(out)
}

fn hx(x: &W256) -> String {
    let mut s = String::with_capacity(66);
    s.push_str("0x");
    for b in x {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

fn g1(x: &str, y: &str) -> Result<G1w, JsError> {
    Ok((w(x)?, w(y)?))
}

fn g2(x0: &str, x1: &str, y0: &str, y1: &str) -> Result<G2w, JsError> {
    Ok(((w(x0)?, w(x1)?), (w(y0)?, w(y1)?)))
}

fn ct(rx: &str, ry: &str, cx: &str, cy: &str) -> Result<(G1w, G1w), JsError> {
    Ok((g1(rx, ry)?, g1(cx, cy)?))
}

fn out_g1(p: &G1w) -> Vec<String> {
    vec![hx(&p.0), hx(&p.1)]
}

fn out_ct(c: &(G1w, G1w)) -> Vec<String> {
    vec![hx(&c.0 .0), hx(&c.0 .1), hx(&c.1 .0), hx(&c.1 .1)]
}

fn err(e: kernel::IdError) -> JsError {
    JsError::new(e.0)
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

#[wasm_bindgen]
pub fn order() -> String {
    hx(&kernel::order())
}

#[wasm_bindgen]
pub fn field_modulus() -> String {
    hx(&kernel::field_modulus())
}

#[wasm_bindgen]
pub fn g1_generator() -> Vec<String> {
    out_g1(&kernel::g1_generator())
}

#[wasm_bindgen]
pub fn g2_generator() -> Vec<String> {
    let g = kernel::g2_generator();
    vec![hx(&g.0 .0), hx(&g.0 .1), hx(&g.1 .0), hx(&g.1 .1)]
}

#[wasm_bindgen]
pub fn h_point() -> Vec<String> {
    out_g1(&kernel::issuer_reenc::h_point())
}

// ---------------------------------------------------------------------------
// Curve ops / hashes
// ---------------------------------------------------------------------------

#[wasm_bindgen]
pub fn g1_add(ax: &str, ay: &str, bx: &str, by: &str) -> Result<Vec<String>, JsError> {
    Ok(out_g1(&kernel::g1_add(&g1(ax, ay)?, &g1(bx, by)?).map_err(err)?))
}

#[wasm_bindgen]
pub fn g1_mul(px: &str, py: &str, k: &str) -> Result<Vec<String>, JsError> {
    Ok(out_g1(&kernel::g1_mul(&g1(px, py)?, &w(k)?).map_err(err)?))
}

#[wasm_bindgen]
pub fn g1_neg(px: &str, py: &str) -> Result<Vec<String>, JsError> {
    Ok(out_g1(&kernel::g1_neg(&g1(px, py)?).map_err(err)?))
}

#[wasm_bindgen]
pub fn g2_mul(x0: &str, x1: &str, y0: &str, y1: &str, k: &str) -> Result<Vec<String>, JsError> {
    let r = kernel::g2_mul(&g2(x0, x1, y0, y1)?, &w(k)?).map_err(err)?;
    Ok(vec![hx(&r.0 .0), hx(&r.0 .1), hx(&r.1 .0), hx(&r.1 .1)])
}

/// Pairs flattened as 6 words each: `g1x, g1y, x_c0, x_c1, y_c0, y_c1`.
#[wasm_bindgen]
pub fn pairing_check(flat: Vec<String>) -> Result<bool, JsError> {
    if flat.len() % 6 != 0 {
        return Err(JsError::new("pairing_check expects 6 words per pair"));
    }
    let mut pairs = Vec::with_capacity(flat.len() / 6);
    for ch in flat.chunks(6) {
        pairs.push((
            g1(&ch[0], &ch[1])?,
            g2(&ch[2], &ch[3], &ch[4], &ch[5])?,
        ));
    }
    kernel::pairing::pairing_check(&pairs).map_err(err)
}

#[wasm_bindgen]
pub fn keccak_scalar(words: Vec<String>) -> Result<String, JsError> {
    let ws: Vec<W256> = words.iter().map(|s| w(s)).collect::<Result<_, _>>()?;
    Ok(hx(&kernel::keccak::keccak_scalar(&ws)))
}

#[wasm_bindgen]
pub fn identity_scalar(canonical: &str) -> String {
    hx(&kernel::keccak::identity_scalar(canonical.as_bytes()))
}

#[wasm_bindgen]
pub fn reduce_mod_order(v: &str) -> Result<String, JsError> {
    Ok(hx(&kernel::reduce_mod_order(&w(v)?)))
}

#[wasm_bindgen]
pub fn poseidon(inputs: Vec<String>) -> Result<String, JsError> {
    let ws: Vec<W256> = inputs.iter().map(|s| w(s)).collect::<Result<_, _>>()?;
    Ok(hx(&kernel::poseidon::poseidon(&ws).map_err(err)?))
}

// ---------------------------------------------------------------------------
// ElGamal / PS
// ---------------------------------------------------------------------------

#[wasm_bindgen]
pub fn elgamal_encrypt(
    mx: &str,
    my: &str,
    pkx: &str,
    pky: &str,
    r: &str,
) -> Result<Vec<String>, JsError> {
    Ok(out_ct(
        &kernel::elgamal::elgamal_encrypt(&g1(mx, my)?, &g1(pkx, pky)?, &w(r)?).map_err(err)?,
    ))
}

#[wasm_bindgen]
pub fn elgamal_decrypt(
    rx: &str,
    ry: &str,
    cx: &str,
    cy: &str,
    sk: &str,
) -> Result<Vec<String>, JsError> {
    let c = ct(rx, ry, cx, cy)?;
    Ok(out_g1(
        &kernel::elgamal::elgamal_decrypt(&c.0, &c.1, &w(sk)?).map_err(err)?,
    ))
}

/// Returns `[sigma1x, sigma1y, sigma2x, sigma2y]`.
#[wasm_bindgen]
pub fn ps_sign(sk_x: &str, sk_y: &str, m: &str, t: &str) -> Result<Vec<String>, JsError> {
    let (s1, s2) = kernel::ps::ps_sign(&w(sk_x)?, &w(sk_y)?, &w(m)?, &w(t)?).map_err(err)?;
    Ok(vec![hx(&s1.0), hx(&s1.1), hx(&s2.0), hx(&s2.1)])
}

/// `pk_x`/`pk_y` are 4 words each: `x_c0, x_c1, y_c0, y_c1`.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn ps_verify(
    pk_x: Vec<String>,
    pk_y: Vec<String>,
    s1x: &str,
    s1y: &str,
    s2x: &str,
    s2y: &str,
    m: &str,
) -> Result<bool, JsError> {
    if pk_x.len() != 4 || pk_y.len() != 4 {
        return Err(JsError::new("G2 point needs 4 words"));
    }
    kernel::ps::ps_verify(
        &g2(&pk_x[0], &pk_x[1], &pk_x[2], &pk_x[3])?,
        &g2(&pk_y[0], &pk_y[1], &pk_y[2], &pk_y[3])?,
        &g1(s1x, s1y)?,
        &g1(s2x, s2y)?,
        &w(m)?,
    )
    .map_err(err)
}

#[wasm_bindgen]
pub fn ps_rerandomize(
    s1x: &str,
    s1y: &str,
    s2x: &str,
    s2y: &str,
    t: &str,
) -> Result<Vec<String>, JsError> {
    let (r1, r2) =
        kernel::ps::ps_rerandomize(&g1(s1x, s1y)?, &g1(s2x, s2y)?, &w(t)?).map_err(err)?;
    Ok(vec![hx(&r1.0), hx(&r1.1), hx(&r2.0), hx(&r2.1)])
}

// ---------------------------------------------------------------------------
// Schnorr
// ---------------------------------------------------------------------------

/// Raw UNREDUCED keccak word.
#[wasm_bindgen]
pub fn batch_commitment(cms: Vec<String>) -> Result<String, JsError> {
    let ws: Vec<W256> = cms.iter().map(|s| w(s)).collect::<Result<_, _>>()?;
    Ok(hx(&kernel::schnorr::batch_commitment(&ws)))
}

/// Returns `[e, s, Rx, Ry]`.
#[wasm_bindgen]
pub fn issuer_schnorr_sign(
    sk_iss: &str,
    h_batch: &str,
    issuer: &str,
    chainid: &str,
    k: &str,
) -> Result<Vec<String>, JsError> {
    let p = kernel::schnorr::issuer_schnorr_sign(
        &w(sk_iss)?,
        &w(h_batch)?,
        &w(issuer)?,
        &w(chainid)?,
        &w(k)?,
    )
    .map_err(err)?;
    Ok(vec![hx(&p.e), hx(&p.s), hx(&p.r.0), hx(&p.r.1)])
}

/// `proof` = `[e, s, Rx, Ry]`.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn issuer_schnorr_verify(
    pkx: &str,
    pky: &str,
    proof: Vec<String>,
    h_batch: &str,
    issuer: &str,
    chainid: &str,
) -> Result<bool, JsError> {
    if proof.len() != 4 {
        return Err(JsError::new("schnorr proof needs 4 words"));
    }
    let p = kernel::schnorr::SchnorrProof {
        e: w(&proof[0])?,
        s: w(&proof[1])?,
        r: g1(&proof[2], &proof[3])?,
    };
    kernel::schnorr::issuer_schnorr_verify(
        &g1(pkx, pky)?,
        &p,
        &w(h_batch)?,
        &w(issuer)?,
        &w(chainid)?,
    )
    .map_err(err)
}

// ---------------------------------------------------------------------------
// Registration NIZK
// ---------------------------------------------------------------------------

/// `sigma`/`e_ct` flattened (4 words each).  Returns
/// `[e, s_m, s_r, s_sk, A_ps x, y, T_C x, y, T_R x, y, T_key x, y]` (12 words).
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn registration_prove(
    sigma: Vec<String>,
    m: &str,
    r: &str,
    pkx: &str,
    pky: &str,
    e_ct: Vec<String>,
    registrant: &str,
    sk: &str,
    chainid: &str,
    registry: &str,
    m_tilde: &str,
    r_tilde: &str,
    sk_tilde: &str,
) -> Result<Vec<String>, JsError> {
    if sigma.len() != 4 || e_ct.len() != 4 {
        return Err(JsError::new("sigma/e_ct need 4 words each"));
    }
    let p = kernel::nizk::registration_prove(
        &g1(&sigma[0], &sigma[1])?,
        &g1(&sigma[2], &sigma[3])?,
        &w(m)?,
        &w(r)?,
        &g1(pkx, pky)?,
        &ct(&e_ct[0], &e_ct[1], &e_ct[2], &e_ct[3])?,
        &w(registrant)?,
        &w(sk)?,
        &w(chainid)?,
        &w(registry)?,
        &w(m_tilde)?,
        &w(r_tilde)?,
        &w(sk_tilde)?,
    )
    .map_err(err)?;
    Ok(vec![
        hx(&p.e),
        hx(&p.s_m),
        hx(&p.s_r),
        hx(&p.s_sk),
        hx(&p.a_ps.0),
        hx(&p.a_ps.1),
        hx(&p.t_c.0),
        hx(&p.t_c.1),
        hx(&p.t_r.0),
        hx(&p.t_r.1),
        hx(&p.t_key.0),
        hx(&p.t_key.1),
    ])
}

/// `proof` = the 12 words `registration_prove` returns; `issuer_x`/`issuer_y`
/// are G2 (4 words each).
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn registration_verify(
    sigma: Vec<String>,
    e_ct: Vec<String>,
    pkx: &str,
    pky: &str,
    issuer_x: Vec<String>,
    issuer_y: Vec<String>,
    proof: Vec<String>,
    registrant: &str,
    chainid: &str,
    registry: &str,
) -> Result<bool, JsError> {
    if sigma.len() != 4 || e_ct.len() != 4 || issuer_x.len() != 4 || issuer_y.len() != 4 {
        return Err(JsError::new("sigma/e_ct/issuer_x/issuer_y need 4 words each"));
    }
    if proof.len() != 12 {
        return Err(JsError::new("registration proof needs 12 words"));
    }
    let p = kernel::nizk::RegistrationProof {
        e: w(&proof[0])?,
        s_m: w(&proof[1])?,
        s_r: w(&proof[2])?,
        s_sk: w(&proof[3])?,
        a_ps: g1(&proof[4], &proof[5])?,
        t_c: g1(&proof[6], &proof[7])?,
        t_r: g1(&proof[8], &proof[9])?,
        t_key: g1(&proof[10], &proof[11])?,
    };
    kernel::nizk::registration_verify(
        &g1(&sigma[0], &sigma[1])?,
        &g1(&sigma[2], &sigma[3])?,
        &ct(&e_ct[0], &e_ct[1], &e_ct[2], &e_ct[3])?,
        &g1(pkx, pky)?,
        &g2(&issuer_x[0], &issuer_x[1], &issuer_x[2], &issuer_x[3])?,
        &g2(&issuer_y[0], &issuer_y[1], &issuer_y[2], &issuer_y[3])?,
        &p,
        &w(registrant)?,
        &w(chainid)?,
        &w(registry)?,
    )
    .map_err(err)
}

// ---------------------------------------------------------------------------
// Chaum-Pedersen approve
// ---------------------------------------------------------------------------

/// Returns `[e, s1, s2, T1x, T1y, T2x, T2y, T3x, T3y]` (9 words).
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn chaum_pedersen_prove(
    e_alice: Vec<String>,
    e_bob: Vec<String>,
    pk_ax: &str,
    pk_ay: &str,
    pk_bx: &str,
    pk_by: &str,
    sk_alice: &str,
    r_prime: &str,
    sender: &str,
    spender: &str,
    chainid: &str,
    registry: &str,
    nonce: &str,
    k1: &str,
    k2: &str,
) -> Result<Vec<String>, JsError> {
    if e_alice.len() != 4 || e_bob.len() != 4 {
        return Err(JsError::new("ciphertexts need 4 words each"));
    }
    let p = kernel::chaum_pedersen::chaum_pedersen_prove(
        &ct(&e_alice[0], &e_alice[1], &e_alice[2], &e_alice[3])?,
        &ct(&e_bob[0], &e_bob[1], &e_bob[2], &e_bob[3])?,
        &g1(pk_ax, pk_ay)?,
        &g1(pk_bx, pk_by)?,
        &w(sk_alice)?,
        &w(r_prime)?,
        &w(sender)?,
        &w(spender)?,
        &w(chainid)?,
        &w(registry)?,
        &w(nonce)?,
        &w(k1)?,
        &w(k2)?,
    )
    .map_err(err)?;
    Ok(vec![
        hx(&p.e),
        hx(&p.s1),
        hx(&p.s2),
        hx(&p.t1.0),
        hx(&p.t1.1),
        hx(&p.t2.0),
        hx(&p.t2.1),
        hx(&p.t3.0),
        hx(&p.t3.1),
    ])
}

/// `proof` = the 9 words `chaum_pedersen_prove` returns.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn chaum_pedersen_verify(
    e_alice: Vec<String>,
    e_bob: Vec<String>,
    pk_ax: &str,
    pk_ay: &str,
    pk_bx: &str,
    pk_by: &str,
    proof: Vec<String>,
    sender: &str,
    spender: &str,
    chainid: &str,
    registry: &str,
    nonce: &str,
) -> Result<bool, JsError> {
    if e_alice.len() != 4 || e_bob.len() != 4 || proof.len() != 9 {
        return Err(JsError::new("bad word counts"));
    }
    let p = kernel::chaum_pedersen::CpProof {
        e: w(&proof[0])?,
        s1: w(&proof[1])?,
        s2: w(&proof[2])?,
        t1: g1(&proof[3], &proof[4])?,
        t2: g1(&proof[5], &proof[6])?,
        t3: g1(&proof[7], &proof[8])?,
    };
    kernel::chaum_pedersen::chaum_pedersen_verify(
        &ct(&e_alice[0], &e_alice[1], &e_alice[2], &e_alice[3])?,
        &ct(&e_bob[0], &e_bob[1], &e_bob[2], &e_bob[3])?,
        &g1(pk_ax, pk_ay)?,
        &g1(pk_bx, pk_by)?,
        &p,
        &w(sender)?,
        &w(spender)?,
        &w(chainid)?,
        &w(registry)?,
        &w(nonce)?,
    )
    .map_err(err)
}

// ---------------------------------------------------------------------------
// Verifiable decryption
// ---------------------------------------------------------------------------

/// Returns `[e, s, T1x, T1y, T2x, T2y]` (6 words).
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn verifiable_decrypt_prove(
    e_ct: Vec<String>,
    sk: &str,
    mx: &str,
    my: &str,
    account: &str,
    chainid: &str,
    t: &str,
) -> Result<Vec<String>, JsError> {
    if e_ct.len() != 4 {
        return Err(JsError::new("ciphertext needs 4 words"));
    }
    let p = kernel::verifiable_decrypt::verifiable_decrypt_prove(
        &ct(&e_ct[0], &e_ct[1], &e_ct[2], &e_ct[3])?,
        &w(sk)?,
        &g1(mx, my)?,
        &w(account)?,
        &w(chainid)?,
        &w(t)?,
    )
    .map_err(err)?;
    Ok(vec![
        hx(&p.e),
        hx(&p.s),
        hx(&p.t1.0),
        hx(&p.t1.1),
        hx(&p.t2.0),
        hx(&p.t2.1),
    ])
}

/// `proof` = the 6 words `verifiable_decrypt_prove` returns.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn verifiable_decrypt_verify(
    e_ct: Vec<String>,
    pkx: &str,
    pky: &str,
    mx: &str,
    my: &str,
    proof: Vec<String>,
    account: &str,
    chainid: &str,
) -> Result<bool, JsError> {
    if e_ct.len() != 4 || proof.len() != 6 {
        return Err(JsError::new("bad word counts"));
    }
    let p = kernel::verifiable_decrypt::VdProof {
        e: w(&proof[0])?,
        s: w(&proof[1])?,
        t1: g1(&proof[2], &proof[3])?,
        t2: g1(&proof[4], &proof[5])?,
    };
    kernel::verifiable_decrypt::verifiable_decrypt_verify(
        &ct(&e_ct[0], &e_ct[1], &e_ct[2], &e_ct[3])?,
        &g1(pkx, pky)?,
        &g1(mx, my)?,
        &p,
        &w(account)?,
        &w(chainid)?,
    )
    .map_err(err)
}

// ---------------------------------------------------------------------------
// A2 issuer re-encryption binding
// ---------------------------------------------------------------------------

/// Returns 21 words:
/// `[e, s_r, s_b, s_s, s_g, A1x,y, A2x,y, A3x,y, A4x,y, A5x,y, Qx,y, Ux,y, Tx,y]`.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn issuer_reenc_prove(
    sk_iss: &str,
    r_prime: &str,
    pk_recx: &str,
    pk_recy: &str,
    e_reg: Vec<String>,
    e_iss: Vec<String>,
    issuer: &str,
    chainid: &str,
    beta: &str,
    gamma: &str,
    k_r: &str,
    k_b: &str,
    k_s: &str,
    k_g: &str,
) -> Result<Vec<String>, JsError> {
    if e_reg.len() != 4 || e_iss.len() != 4 {
        return Err(JsError::new("ciphertexts need 4 words each"));
    }
    let p = kernel::issuer_reenc::issuer_reenc_prove(
        &w(sk_iss)?,
        &w(r_prime)?,
        &g1(pk_recx, pk_recy)?,
        &ct(&e_reg[0], &e_reg[1], &e_reg[2], &e_reg[3])?,
        &ct(&e_iss[0], &e_iss[1], &e_iss[2], &e_iss[3])?,
        &w(issuer)?,
        &w(chainid)?,
        &w(beta)?,
        &w(gamma)?,
        &w(k_r)?,
        &w(k_b)?,
        &w(k_s)?,
        &w(k_g)?,
    )
    .map_err(err)?;
    Ok(vec![
        hx(&p.e),
        hx(&p.s_r),
        hx(&p.s_b),
        hx(&p.s_s),
        hx(&p.s_g),
        hx(&p.a1.0),
        hx(&p.a1.1),
        hx(&p.a2.0),
        hx(&p.a2.1),
        hx(&p.a3.0),
        hx(&p.a3.1),
        hx(&p.a4.0),
        hx(&p.a4.1),
        hx(&p.a5.0),
        hx(&p.a5.1),
        hx(&p.q.0),
        hx(&p.q.1),
        hx(&p.u.0),
        hx(&p.u.1),
        hx(&p.t.0),
        hx(&p.t.1),
    ])
}

/// `proof` = the 21 words `issuer_reenc_prove` returns.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn issuer_reenc_verify(
    pk_issx: &str,
    pk_issy: &str,
    e_reg: Vec<String>,
    e_iss: Vec<String>,
    proof: Vec<String>,
    issuer: &str,
    chainid: &str,
) -> Result<bool, JsError> {
    if e_reg.len() != 4 || e_iss.len() != 4 || proof.len() != 21 {
        return Err(JsError::new("bad word counts"));
    }
    let p = kernel::issuer_reenc::IssuerReencProof {
        e: w(&proof[0])?,
        s_r: w(&proof[1])?,
        s_b: w(&proof[2])?,
        s_s: w(&proof[3])?,
        s_g: w(&proof[4])?,
        a1: g1(&proof[5], &proof[6])?,
        a2: g1(&proof[7], &proof[8])?,
        a3: g1(&proof[9], &proof[10])?,
        a4: g1(&proof[11], &proof[12])?,
        a5: g1(&proof[13], &proof[14])?,
        q: g1(&proof[15], &proof[16])?,
        u: g1(&proof[17], &proof[18])?,
        t: g1(&proof[19], &proof[20])?,
    };
    kernel::issuer_reenc::issuer_reenc_verify(
        &g1(pk_issx, pk_issy)?,
        &ct(&e_reg[0], &e_reg[1], &e_reg[2], &e_reg[3])?,
        &ct(&e_iss[0], &e_iss[1], &e_iss[2], &e_iss[3])?,
        &p,
        &w(issuer)?,
        &w(chainid)?,
    )
    .map_err(err)
}

// ---------------------------------------------------------------------------
// Deposit coupling / B1 depositor binding
// ---------------------------------------------------------------------------

/// Returns 12 words: `[e, s_m, s_s, s_b, A2x,y, A3x,y, A4x,y, P_Ix,y]`.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn deposit_couple_prove(
    m_rec: &str,
    sk_dep: &str,
    e_dep: Vec<String>,
    e_iss: Vec<String>,
    account: &str,
    chainid: &str,
    b: &str,
    k_m: &str,
    k_s: &str,
    k_b: &str,
) -> Result<Vec<String>, JsError> {
    if e_dep.len() != 4 || e_iss.len() != 4 {
        return Err(JsError::new("ciphertexts need 4 words each"));
    }
    let p = kernel::unilateral_a2::deposit_couple_prove(
        &w(m_rec)?,
        &w(sk_dep)?,
        &ct(&e_dep[0], &e_dep[1], &e_dep[2], &e_dep[3])?,
        &ct(&e_iss[0], &e_iss[1], &e_iss[2], &e_iss[3])?,
        &w(account)?,
        &w(chainid)?,
        &w(b)?,
        &w(k_m)?,
        &w(k_s)?,
        &w(k_b)?,
    )
    .map_err(err)?;
    Ok(vec![
        hx(&p.e),
        hx(&p.s_m),
        hx(&p.s_s),
        hx(&p.s_b),
        hx(&p.a2.0),
        hx(&p.a2.1),
        hx(&p.a3.0),
        hx(&p.a3.1),
        hx(&p.a4.0),
        hx(&p.a4.1),
        hx(&p.p_i.0),
        hx(&p.p_i.1),
    ])
}

/// `proof` = the 12 words `deposit_couple_prove` returns.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn deposit_couple_verify(
    pk_depx: &str,
    pk_depy: &str,
    e_dep: Vec<String>,
    e_iss: Vec<String>,
    proof: Vec<String>,
    account: &str,
    chainid: &str,
) -> Result<bool, JsError> {
    if e_dep.len() != 4 || e_iss.len() != 4 || proof.len() != 12 {
        return Err(JsError::new("bad word counts"));
    }
    let p = kernel::unilateral_a2::DepositCouplingProof {
        e: w(&proof[0])?,
        s_m: w(&proof[1])?,
        s_s: w(&proof[2])?,
        s_b: w(&proof[3])?,
        a2: g1(&proof[4], &proof[5])?,
        a3: g1(&proof[6], &proof[7])?,
        a4: g1(&proof[8], &proof[9])?,
        p_i: g1(&proof[10], &proof[11])?,
    };
    kernel::unilateral_a2::deposit_couple_verify(
        &g1(pk_depx, pk_depy)?,
        &ct(&e_dep[0], &e_dep[1], &e_dep[2], &e_dep[3])?,
        &ct(&e_iss[0], &e_iss[1], &e_iss[2], &e_iss[3])?,
        &p,
        &w(account)?,
        &w(chainid)?,
    )
    .map_err(err)
}

/// Returns 21 words: 17 proof words
/// `[e, s_m, s_s, s_r, s_b, A2x,y, A4x,y, B1x,y, B2x,y, A_px,y, P_depx,y]`
/// followed by the 4 `eDepForIss` ciphertext words.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn b1_bind_prove(
    m_dep: &str,
    sk_dep: &str,
    e_dep: Vec<String>,
    pk_issx: &str,
    pk_issy: &str,
    account: &str,
    chainid: &str,
    r: &str,
    b: &str,
    k_m: &str,
    k_s: &str,
    k_r: &str,
    k_b: &str,
) -> Result<Vec<String>, JsError> {
    if e_dep.len() != 4 {
        return Err(JsError::new("e_dep needs 4 words"));
    }
    let (p, e_f) = kernel::b1_binding::b1_bind_prove(
        &w(m_dep)?,
        &w(sk_dep)?,
        &ct(&e_dep[0], &e_dep[1], &e_dep[2], &e_dep[3])?,
        &g1(pk_issx, pk_issy)?,
        &w(account)?,
        &w(chainid)?,
        &w(r)?,
        &w(b)?,
        &w(k_m)?,
        &w(k_s)?,
        &w(k_r)?,
        &w(k_b)?,
    )
    .map_err(err)?;
    let mut out = vec![
        hx(&p.e),
        hx(&p.s_m),
        hx(&p.s_s),
        hx(&p.s_r),
        hx(&p.s_b),
        hx(&p.a2.0),
        hx(&p.a2.1),
        hx(&p.a4.0),
        hx(&p.a4.1),
        hx(&p.b1.0),
        hx(&p.b1.1),
        hx(&p.b2.0),
        hx(&p.b2.1),
        hx(&p.a_p.0),
        hx(&p.a_p.1),
        hx(&p.p_dep.0),
        hx(&p.p_dep.1),
    ];
    out.extend(out_ct(&e_f));
    Ok(out)
}

/// `proof` = the 17 proof words `b1_bind_prove` returns (without the
/// trailing ciphertext).
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn b1_bind_verify(
    pk_depx: &str,
    pk_depy: &str,
    e_dep: Vec<String>,
    pk_issx: &str,
    pk_issy: &str,
    e_dep_for_iss: Vec<String>,
    proof: Vec<String>,
    account: &str,
    chainid: &str,
) -> Result<bool, JsError> {
    if e_dep.len() != 4 || e_dep_for_iss.len() != 4 || proof.len() != 17 {
        return Err(JsError::new("bad word counts"));
    }
    let p = kernel::b1_binding::DepositorBindingProof {
        e: w(&proof[0])?,
        s_m: w(&proof[1])?,
        s_s: w(&proof[2])?,
        s_r: w(&proof[3])?,
        s_b: w(&proof[4])?,
        a2: g1(&proof[5], &proof[6])?,
        a4: g1(&proof[7], &proof[8])?,
        b1: g1(&proof[9], &proof[10])?,
        b2: g1(&proof[11], &proof[12])?,
        a_p: g1(&proof[13], &proof[14])?,
        p_dep: g1(&proof[15], &proof[16])?,
    };
    kernel::b1_binding::b1_bind_verify(
        &g1(pk_depx, pk_depy)?,
        &ct(&e_dep[0], &e_dep[1], &e_dep[2], &e_dep[3])?,
        &g1(pk_issx, pk_issy)?,
        &ct(
            &e_dep_for_iss[0],
            &e_dep_for_iss[1],
            &e_dep_for_iss[2],
            &e_dep_for_iss[3],
        )?,
        &p,
        &w(account)?,
        &w(chainid)?,
    )
    .map_err(err)
}

// ---------------------------------------------------------------------------
// Notes family
// ---------------------------------------------------------------------------

#[wasm_bindgen]
pub fn note_commitment(
    flavor: u32,
    v: &str,
    rho: &str,
    id_hash: &str,
    predicate: &str,
) -> Result<String, JsError> {
    Ok(hx(&kernel::notes::note_commitment(
        flavor as u64,
        &w(v)?,
        &w(rho)?,
        &w(id_hash)?,
        &w(predicate)?,
    )
    .map_err(err)?))
}

#[wasm_bindgen]
pub fn nullifier_b(rho: &str, id_hash: &str) -> Result<String, JsError> {
    Ok(hx(&kernel::notes::nullifier_b(&w(rho)?, &w(id_hash)?).map_err(err)?))
}

#[wasm_bindgen]
pub fn nullifier_a(rho: &str, id_hash: &str) -> Result<String, JsError> {
    Ok(hx(&kernel::notes::nullifier_a(&w(rho)?, &w(id_hash)?).map_err(err)?))
}

#[wasm_bindgen]
pub fn id_hash_b1(m_issuer: &str, sig_rx: &str, sig_ry: &str, sigma_s: &str) -> Result<String, JsError> {
    Ok(hx(&kernel::notes::id_hash_b1(&w(m_issuer)?, &g1(sig_rx, sig_ry)?, &w(sigma_s)?)
        .map_err(err)?))
}

#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn id_hash_a1(
    e_note: Vec<String>,
    m_issuer: &str,
    sig_rx: &str,
    sig_ry: &str,
    sigma_s: &str,
) -> Result<String, JsError> {
    if e_note.len() != 4 {
        return Err(JsError::new("e_note needs 4 words"));
    }
    Ok(hx(&kernel::notes::id_hash_a1(
        &ct(&e_note[0], &e_note[1], &e_note[2], &e_note[3])?,
        &w(m_issuer)?,
        &g1(sig_rx, sig_ry)?,
        &w(sigma_s)?,
    )
    .map_err(err)?))
}

#[wasm_bindgen]
pub fn id_hash_a2(e_note: Vec<String>, e_iss: Vec<String>) -> Result<String, JsError> {
    if e_note.len() != 4 || e_iss.len() != 4 {
        return Err(JsError::new("ciphertexts need 4 words each"));
    }
    Ok(hx(&kernel::notes::id_hash_a2(
        &ct(&e_note[0], &e_note[1], &e_note[2], &e_note[3])?,
        &ct(&e_iss[0], &e_iss[1], &e_iss[2], &e_iss[3])?,
    )
    .map_err(err)?))
}

#[wasm_bindgen]
pub fn identity_leaf(mx: &str, my: &str) -> Result<String, JsError> {
    Ok(hx(&kernel::notes::identity_leaf(&g1(mx, my)?).map_err(err)?))
}

// ---------------------------------------------------------------------------
// buck-wallet: canonical dialect, AB-RCPT/1 envelope, receipt build /
// verify, unilateral flows, issuer ceremony.  Structured inputs cross as
// ONE JSON text of named args (the vector-fixture shapes); receipt cores
// cross as their canonical text -- see buck-wallet's `args` module.
// ---------------------------------------------------------------------------

fn werr(e: kernel::IdError) -> JsError {
    JsError::new(e.0)
}

fn parse_args(args_json: &str) -> Result<serde_json::Value, JsError> {
    serde_json::from_str(args_json).map_err(|e| JsError::new(&format!("args: invalid JSON: {e}")))
}

#[wasm_bindgen]
pub fn wallet_canonical_json(text: &str) -> Result<String, JsError> {
    wallet::canonical::canonical_json(text).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_canonical_identity_data(fields_json: &str) -> Result<String, JsError> {
    wallet::canonical::canonical_identity_data(fields_json).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_receipt_id(canonical: &[u8], prefix_len: usize) -> String {
    wallet::envelope::receipt_id(canonical, prefix_len)
}

#[wasm_bindgen]
pub fn wallet_envelope_text(canonical: &[u8], width: usize) -> String {
    wallet::envelope::envelope_text(canonical, width)
}

#[wasm_bindgen]
pub fn wallet_parse_envelope(text: &str) -> Result<Vec<u8>, JsError> {
    wallet::envelope::parse_envelope(text).map_err(werr)
}

/// Tier-1 verify of a canonical receipt text; returns the RcptResult as
/// a JSON text `{"ok", "reason", "identity_M", "value"}`.
#[wasm_bindgen]
pub fn wallet_verify_receipt(core_text: &str) -> Result<String, JsError> {
    wallet::args::verify_receipt_args(core_text).map_err(werr)
}

/// Build any receipt kind from named JSON args; returns the canonical
/// receipt text.
#[wasm_bindgen]
pub fn wallet_build_receipt(args_json: &str) -> Result<String, JsError> {
    wallet::args::build_receipt_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_mint_unilateral_a2(args_json: &str) -> Result<String, JsError> {
    wallet::args::mint_unilateral_a2_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_make_receipt_a2(args_json: &str) -> Result<String, JsError> {
    wallet::args::make_receipt_a2_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_verify_receipt_a2(args_json: &str) -> Result<String, JsError> {
    wallet::args::verify_receipt_a2_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_mint_unilateral_a1(args_json: &str) -> Result<String, JsError> {
    wallet::args::mint_unilateral_a1_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_make_receipt_a1(args_json: &str) -> Result<String, JsError> {
    wallet::args::make_receipt_a1_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_verify_receipt_a1(args_json: &str) -> Result<String, JsError> {
    wallet::args::verify_receipt_a1_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_issue_credential(args_json: &str) -> Result<String, JsError> {
    wallet::args::issue_credential_args(&parse_args(args_json)?).map_err(werr)
}

// ---------------------------------------------------------------------------
// buck-registry: certificates cross as their wire bytes; the trees and
// the central aggregator are exported CLASSES so a browser wallet can
// hold real accumulator state.
// ---------------------------------------------------------------------------

fn msg32(msg_hash_hex: &str) -> Result<[u8; 32], JsError> {
    Ok(w(msg_hash_hex)?)
}

#[wasm_bindgen]
pub fn registry_schnorr_sign(
    sk: &str,
    msg_hash: &str,
    registry_id: &str,
    chainid: &str,
    k: &str,
) -> Result<Vec<String>, JsError> {
    let p = registry::certificate::registry_schnorr_sign(
        &w(sk)?,
        &msg32(msg_hash)?,
        registry_id,
        &w(chainid)?,
        &w(k)?,
    )
    .map_err(werr)?;
    Ok(vec![hx(&p.e), hx(&p.s), hx(&p.r.0), hx(&p.r.1)])
}

#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn registry_schnorr_verify(
    pkx: &str,
    pky: &str,
    e: &str,
    s: &str,
    rx: &str,
    ry: &str,
    msg_hash: &str,
    registry_id: &str,
    chainid: &str,
) -> Result<bool, JsError> {
    let proof = registry::certificate::RegistrySchnorrProof {
        e: w(e)?,
        s: w(s)?,
        r: g1(rx, ry)?,
    };
    registry::certificate::registry_schnorr_verify(
        &g1(pkx, pky)?,
        &proof,
        &msg32(msg_hash)?,
        registry_id,
        &w(chainid)?,
    )
    .map_err(werr)
}

/// Create + sign a certificate; returns the SignedCertificate WIRE bytes.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn registry_sign_certificate(
    registry_sk: &str,
    registry_id: &str,
    canonical_identity: &str,
    serial: u64,
    issued_at: i64,
    expires_at: i64,
    chainid: &str,
    k: &str,
) -> Result<Vec<u8>, JsError> {
    let signed = registry::certificate::registry_sign_certificate(
        &w(registry_sk)?,
        registry_id,
        canonical_identity,
        serial,
        issued_at,
        expires_at,
        &w(chainid)?,
        &w(k)?,
    )
    .map_err(werr)?;
    Ok(signed.serialize())
}

#[wasm_bindgen]
pub fn registry_verify_certificate(signed_wire: &[u8], chainid: &str) -> Result<bool, JsError> {
    let signed =
        registry::certificate::SignedCertificate::deserialize(signed_wire).map_err(werr)?;
    registry::certificate::registry_verify_certificate(&signed, &w(chainid)?).map_err(werr)
}

#[wasm_bindgen]
pub fn registry_seal_certificate(
    signed_wire: &[u8],
    client_pkx: &str,
    client_pky: &str,
    r: &str,
) -> Result<Vec<u8>, JsError> {
    let signed =
        registry::certificate::SignedCertificate::deserialize(signed_wire).map_err(werr)?;
    let sealed =
        registry::certificate::seal_certificate(&signed, &g1(client_pkx, client_pky)?, &w(r)?)
            .map_err(werr)?;
    Ok(sealed.envelope())
}

#[wasm_bindgen]
pub fn registry_unseal_certificate(envelope: &[u8], client_sk: &str) -> Result<Vec<u8>, JsError> {
    let sealed =
        registry::certificate::SealedCertificate::from_envelope(envelope).map_err(werr)?;
    let signed =
        registry::certificate::unseal_certificate(&sealed, &w(client_sk)?).map_err(werr)?;
    Ok(signed.serialize())
}

fn proof_json(p: &registry::tree::MembershipProof) -> String {
    let sibs: Vec<String> = p.siblings.iter().map(|s| format!("\"{}\"", hx(s))).collect();
    let bits: Vec<String> = p.index_bits.iter().map(|b| b.to_string()).collect();
    format!(
        "{{\"leaf\":\"{}\",\"siblings\":[{}],\"index_bits\":[{}],\"root\":\"{}\",\"leaf_index\":{}}}",
        hx(&p.leaf),
        sibs.join(","),
        bits.join(","),
        hx(&p.root),
        p.leaf_index
    )
}

/// The identity Merkle accumulator as a stateful JS class.
#[wasm_bindgen]
pub struct MerkleTree(registry::tree::IdentityMerkleTree);

#[wasm_bindgen]
impl MerkleTree {
    #[wasm_bindgen(constructor)]
    pub fn new(depth: usize) -> Result<MerkleTree, JsError> {
        Ok(MerkleTree(
            registry::tree::IdentityMerkleTree::new(depth).map_err(werr)?,
        ))
    }

    pub fn from_leaves(leaves: Vec<String>, depth: usize) -> Result<MerkleTree, JsError> {
        let ls: Result<Vec<_>, JsError> = leaves.iter().map(|l| w(l)).collect();
        Ok(MerkleTree(
            registry::tree::IdentityMerkleTree::from_leaves(&ls?, depth).map_err(werr)?,
        ))
    }

    pub fn depth(&self) -> usize {
        self.0.depth()
    }

    pub fn count(&self) -> usize {
        self.0.count()
    }

    pub fn root(&self) -> Result<String, JsError> {
        Ok(hx(&self.0.root().map_err(werr)?))
    }

    pub fn leaves(&self) -> Vec<String> {
        self.0.leaves().iter().map(hx).collect()
    }

    pub fn insert_leaf(&mut self, leaf: &str) -> Result<usize, JsError> {
        Ok(self.0.insert_leaf(w(leaf)?))
    }

    pub fn insert_identity(&mut self, mx: &str, my: &str) -> Result<usize, JsError> {
        self.0.insert_identity(&g1(mx, my)?).map_err(werr)
    }

    pub fn set_leaf(&mut self, index: usize, leaf: &str) -> Result<(), JsError> {
        self.0.set_leaf(index, w(leaf)?).map_err(werr)
    }

    pub fn contains_leaf(&self, leaf: &str) -> Result<bool, JsError> {
        Ok(self.0.contains(&w(leaf)?))
    }

    /// Membership check for an identity point; pass a root hex to also
    /// require the path to fold to it.
    pub fn contains_identity(&self, mx: &str, my: &str, root: Option<String>) -> Result<bool, JsError> {
        let r = match &root {
            Some(s) => Some(w(s)?),
            None => None,
        };
        self.0
            .contains_identity(&g1(mx, my)?, r.as_ref())
            .map_err(werr)
    }

    /// The authentication path at `index`, as a JSON text
    /// `{leaf, siblings, index_bits, root, leaf_index}`.
    pub fn path(&self, index: usize) -> Result<String, JsError> {
        Ok(proof_json(&self.0.path(index).map_err(werr)?))
    }
}

/// The central sub-root aggregator as a stateful JS class.
#[wasm_bindgen]
pub struct Aggregator(registry::aggregator::CentralMerkleService);

#[wasm_bindgen]
impl Aggregator {
    #[wasm_bindgen(constructor)]
    pub fn new(depth: usize) -> Result<Aggregator, JsError> {
        Ok(Aggregator(
            registry::aggregator::CentralMerkleService::new(depth).map_err(werr)?,
        ))
    }

    pub fn identity_root(&self) -> Result<String, JsError> {
        Ok(hx(&self.0.identity_root().map_err(werr)?))
    }

    pub fn enroll(
        &mut self,
        sub_tree_id: &str,
        kind: &str,
        initial_sub_root: &str,
        timestamp: f64,
    ) -> Result<usize, JsError> {
        let rec = self
            .0
            .enroll(sub_tree_id, kind, w(initial_sub_root)?, timestamp)
            .map_err(werr)?;
        Ok(rec.aggregator_leaf_index)
    }

    pub fn update_sub_root(
        &mut self,
        sub_tree_id: &str,
        new_sub_root: &str,
        timestamp: f64,
    ) -> Result<String, JsError> {
        Ok(hx(&self
            .0
            .update_sub_root(sub_tree_id, w(new_sub_root)?, timestamp)
            .map_err(werr)?))
    }

    /// Aggregator path for a sub-tree, as a JSON text
    /// `{sub_root, siblings, index_bits, aggregator_root, sub_tree_id,
    /// aggregator_leaf_index}`.
    pub fn aggregator_proof(&self, sub_tree_id: &str) -> Result<String, JsError> {
        let p = self.0.aggregator_proof(sub_tree_id).map_err(werr)?;
        let sibs: Vec<String> = p.siblings.iter().map(|s| format!("\"{}\"", hx(s))).collect();
        let bits: Vec<String> = p.index_bits.iter().map(|b| b.to_string()).collect();
        Ok(format!(
            "{{\"sub_root\":\"{}\",\"siblings\":[{}],\"index_bits\":[{}],\"aggregator_root\":\"{}\",\"sub_tree_id\":{},\"aggregator_leaf_index\":{}}}",
            hx(&p.sub_root),
            sibs.join(","),
            bits.join(","),
            hx(&p.aggregator_root),
            serde_json::to_string(&p.sub_tree_id).unwrap(),
            p.aggregator_leaf_index
        ))
    }
}

/// Verify a full (sub-tree + aggregator) membership proof pair; the two
/// JSON texts are the `MerkleTree.path` / `Aggregator.aggregator_proof`
/// outputs.
#[wasm_bindgen]
pub fn registry_verify_full_proof(
    sub_proof_json: &str,
    aggregator_proof_json: &str,
) -> Result<bool, JsError> {
    let sp: serde_json::Value = serde_json::from_str(sub_proof_json)
        .map_err(|e| JsError::new(&format!("sub proof: {e}")))?;
    let ap: serde_json::Value = serde_json::from_str(aggregator_proof_json)
        .map_err(|e| JsError::new(&format!("aggregator proof: {e}")))?;
    let jw = |v: &serde_json::Value| -> Result<kernel::W256, JsError> {
        w(v.as_str().ok_or_else(|| JsError::new("expected hex string"))?)
    };
    let arr = |v: &serde_json::Value| -> Result<Vec<kernel::W256>, JsError> {
        v.as_array()
            .ok_or_else(|| JsError::new("expected array"))?
            .iter()
            .map(&jw)
            .collect()
    };
    let bits = |v: &serde_json::Value| -> Result<Vec<u8>, JsError> {
        Ok(v.as_array()
            .ok_or_else(|| JsError::new("expected array"))?
            .iter()
            .map(|b| b.as_u64().unwrap_or(0) as u8)
            .collect())
    };
    let sub = registry::tree::MembershipProof {
        leaf: jw(&sp["leaf"])?,
        siblings: arr(&sp["siblings"])?,
        index_bits: bits(&sp["index_bits"])?,
        root: jw(&sp["root"])?,
        leaf_index: sp["leaf_index"].as_u64().unwrap_or(0) as usize,
    };
    let agg = registry::aggregator::AggregatorMembershipProof {
        sub_root: jw(&ap["sub_root"])?,
        siblings: arr(&ap["siblings"])?,
        index_bits: bits(&ap["index_bits"])?,
        aggregator_root: jw(&ap["aggregator_root"])?,
        sub_tree_id: ap["sub_tree_id"].as_str().unwrap_or("").to_string(),
        aggregator_leaf_index: ap["aggregator_leaf_index"].as_u64().unwrap_or(0) as usize,
    };
    let full = registry::aggregator::FullMembershipProof {
        sub_tree_proof: sub,
        aggregator_proof: agg,
        m_x: [0u8; 32],
        m_y: [0u8; 32],
    };
    full.verify().map_err(werr)
}
