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

/// The Pedersen generator: hashed to the curve, so its discrete log is
/// unknown.  Every blind in the protocol sits on it; see the kernel's `nums`.
#[wasm_bindgen]
pub fn h_pedersen() -> Vec<String> {
    out_g1(&kernel::nums::h_pedersen())
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

/// The hiding presentation `(A, B) = (a*sigma_1, a*sigma_2 + b*Y1)`.
/// Returns `[Ax, Ay, Bx, By]`.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn ps_present(
    s1x: &str,
    s1y: &str,
    s2x: &str,
    s2y: &str,
    y1x: &str,
    y1y: &str,
    a: &str,
    b: &str,
) -> Result<Vec<String>, JsError> {
    let (pa, pb) = kernel::ps::ps_present(&g1(s1x, s1y)?, &g1(s2x, s2y)?, &g1(y1x, y1y)?, &w(a)?, &w(b)?)
        .map_err(err)?;
    Ok(vec![hx(&pa.0), hx(&pa.1), hx(&pb.0), hx(&pb.1)])
}

/// `e(Y1, g_2) == e(G, Y)`; `pk_y` is G2 (4 words).
#[wasm_bindgen]
pub fn ps_key_consistent(pk_y: Vec<String>, y1x: &str, y1y: &str) -> Result<bool, JsError> {
    if pk_y.len() != 4 {
        return Err(JsError::new("G2 point needs 4 words"));
    }
    kernel::ps::ps_key_consistent(&g2(&pk_y[0], &pk_y[1], &pk_y[2], &pk_y[3])?, &g1(y1x, y1y)?)
        .map_err(err)
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
// Registration NIZK (A')
// ---------------------------------------------------------------------------

/// `pres` = `[Ax, Ay, Bx, By]`, `e_ct` flattened (4 words).  Nonces in the
/// Python draw order.  Returns the 13 words
/// `[e, s_m, s_b, s_r, s_sk, C1 x, y, T_C x, y, T_R x, y, T_key x, y]`.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn registration_prove(
    pres: Vec<String>,
    blind: &str,
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
    b_tilde: &str,
    r_tilde: &str,
    sk_tilde: &str,
) -> Result<Vec<String>, JsError> {
    if pres.len() != 4 || e_ct.len() != 4 {
        return Err(JsError::new("pres/e_ct need 4 words each"));
    }
    let p = kernel::nizk::registration_prove(
        &g1(&pres[0], &pres[1])?,
        &g1(&pres[2], &pres[3])?,
        &w(blind)?,
        &w(m)?,
        &w(r)?,
        &g1(pkx, pky)?,
        &ct(&e_ct[0], &e_ct[1], &e_ct[2], &e_ct[3])?,
        &w(registrant)?,
        &w(sk)?,
        &w(chainid)?,
        &w(registry)?,
        &w(m_tilde)?,
        &w(b_tilde)?,
        &w(r_tilde)?,
        &w(sk_tilde)?,
    )
    .map_err(err)?;
    Ok(vec![
        hx(&p.e),
        hx(&p.s_m),
        hx(&p.s_b),
        hx(&p.s_r),
        hx(&p.s_sk),
        hx(&p.c1.0),
        hx(&p.c1.1),
        hx(&p.t_c.0),
        hx(&p.t_c.1),
        hx(&p.t_r.0),
        hx(&p.t_r.1),
        hx(&p.t_key.0),
        hx(&p.t_key.1),
    ])
}

/// `proof` = the 13 words `registration_prove` returns; `issuer_x`/`issuer_y`
/// are G2 (4 words each).
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn registration_verify(
    pres: Vec<String>,
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
    if pres.len() != 4 || e_ct.len() != 4 || issuer_x.len() != 4 || issuer_y.len() != 4 {
        return Err(JsError::new("pres/e_ct/issuer_x/issuer_y need 4 words each"));
    }
    if proof.len() != 13 {
        return Err(JsError::new("registration proof needs 13 words"));
    }
    let p = kernel::nizk::RegistrationProof {
        e: w(&proof[0])?,
        s_m: w(&proof[1])?,
        s_b: w(&proof[2])?,
        s_r: w(&proof[3])?,
        s_sk: w(&proof[4])?,
        c1: g1(&proof[5], &proof[6])?,
        t_c: g1(&proof[7], &proof[8])?,
        t_r: g1(&proof[9], &proof[10])?,
        t_key: g1(&proof[11], &proof[12])?,
    };
    kernel::nizk::registration_verify(
        &g1(&pres[0], &pres[1])?,
        &g1(&pres[2], &pres[3])?,
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

/// The DLEQ identity opening: the same proof as verifiable decryption, its
/// transcript bound to (account, chainid, registry) under its own tag, so
/// it opens an Identity to one registry and is no receipt.  Returns the 6
/// words `[e, s, T1x, T1y, T2x, T2y]`.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn identity_opening_prove(
    e_ct: Vec<String>,
    sk: &str,
    mx: &str,
    my: &str,
    account: &str,
    chainid: &str,
    registry: &str,
    t: &str,
) -> Result<Vec<String>, JsError> {
    if e_ct.len() != 4 {
        return Err(JsError::new("ciphertext needs 4 words"));
    }
    let p = kernel::verifiable_decrypt::identity_opening_prove(
        &ct(&e_ct[0], &e_ct[1], &e_ct[2], &e_ct[3])?,
        &w(sk)?,
        &g1(mx, my)?,
        &w(account)?,
        &w(chainid)?,
        &w(registry)?,
        &w(t)?,
    )
    .map_err(err)?;
    Ok(vec![hx(&p.e), hx(&p.s), hx(&p.t1.0), hx(&p.t1.1), hx(&p.t2.0), hx(&p.t2.1)])
}

/// `proof` = the 6 words `identity_opening_prove` returns.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn identity_opening_verify(
    e_ct: Vec<String>,
    pkx: &str,
    pky: &str,
    mx: &str,
    my: &str,
    proof: Vec<String>,
    account: &str,
    chainid: &str,
    registry: &str,
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
    kernel::verifiable_decrypt::identity_opening_verify(
        &ct(&e_ct[0], &e_ct[1], &e_ct[2], &e_ct[3])?,
        &g1(pkx, pky)?,
        &g1(mx, my)?,
        &p,
        &w(account)?,
        &w(chainid)?,
        &w(registry)?,
    )
    .map_err(err)
}

// ---------------------------------------------------------------------------
// Holder-derived salts
// ---------------------------------------------------------------------------

/// The field tag of a subtree identifier, the salt derivation's second input.
#[wasm_bindgen]
pub fn tree_tag(tree_id: &str) -> Result<String, JsError> {
    Ok(hx(&kernel::salt::tree_tag(tree_id).map_err(err)?))
}

/// A private subtree's leaf salt, derived from the holder's secret; a new
/// `association_counter` for each re-association.
#[wasm_bindgen]
pub fn derive_salt(holder_secret: &str, tree_id: &str, association_counter: u64) -> Result<String, JsError> {
    Ok(hx(&kernel::salt::derive_salt(&w(holder_secret)?, tree_id, association_counter).map_err(err)?))
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
// B1 depositor binding
// ---------------------------------------------------------------------------

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

/// `id_hash_a2(eNote, eIss, T)` -- `T` is the mint binding's blinded point.
#[wasm_bindgen]
pub fn id_hash_a2(
    e_note: Vec<String>,
    e_iss: Vec<String>,
    t: Vec<String>,
) -> Result<String, JsError> {
    if e_note.len() != 4 || e_iss.len() != 4 || t.len() != 2 {
        return Err(JsError::new("ciphertexts need 4 words each, and T 2"));
    }
    Ok(hx(&kernel::notes::id_hash_a2(
        &ct(&e_note[0], &e_note[1], &e_note[2], &e_note[3])?,
        &ct(&e_iss[0], &e_iss[1], &e_iss[2], &e_iss[3])?,
        &g1(&t[0], &t[1])?,
    )
    .map_err(err)?))
}

#[wasm_bindgen]
pub fn identity_leaf(mx: &str, my: &str) -> Result<String, JsError> {
    Ok(hx(&kernel::notes::identity_leaf(&g1(mx, my)?).map_err(err)?))
}

/// `identity_leaf_salted(M, salt)` -- the hiding leaf of a private subtree.
#[wasm_bindgen]
pub fn identity_leaf_salted(mx: &str, my: &str, salt: &str) -> Result<String, JsError> {
    Ok(hx(
        &kernel::notes::identity_leaf_salted(&g1(mx, my)?, &w(salt)?).map_err(err)?,
    ))
}

/// `receiving_leaf(m_rec, k_recv, salt)` -- the hiding leaf binding an
/// Identity to the receiving key its Notes are addressed to.  Over scalars.
#[wasm_bindgen]
pub fn receiving_leaf(
    m_rec: &str,
    k_recv: &str,
    salt: &str,
) -> Result<String, JsError> {
    Ok(hx(&kernel::notes::receiving_leaf(
        &w(m_rec)?,
        &w(k_recv)?,
        &w(salt)?,
    )
    .map_err(err)?))
}

// ---------------------------------------------------------------------------
// buck-wallet: canonical dialect, AB-RCPT/2 envelope, receipt build /
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

// ---- Notes: receiving key, delivery, mailbox binding, fold witnesses ----

#[wasm_bindgen]
pub fn wallet_receiving_key(args_json: &str) -> Result<String, JsError> {
    wallet::args::receiving_key_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_wrap_mask(args_json: &str) -> Result<String, JsError> {
    wallet::args::wrap_mask_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_deliver_a1(args_json: &str) -> Result<String, JsError> {
    wallet::args::deliver_a1_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_deliver_a2(args_json: &str) -> Result<String, JsError> {
    wallet::args::deliver_a2_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_open_a1(args_json: &str) -> Result<String, JsError> {
    wallet::args::open_a1_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_open_a2(args_json: &str) -> Result<String, JsError> {
    wallet::args::open_a2_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_prove_receiving_binding(args_json: &str) -> Result<String, JsError> {
    wallet::args::prove_receiving_binding_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_verify_receiving_binding(args_json: &str) -> Result<String, JsError> {
    wallet::args::verify_receiving_binding_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_deposit_fold_a1_witness(args_json: &str) -> Result<String, JsError> {
    wallet::args::deposit_fold_a1_witness_args(&parse_args(args_json)?).map_err(werr)
}

#[wasm_bindgen]
pub fn wallet_deposit_fold_a2_witness(args_json: &str) -> Result<String, JsError> {
    wallet::args::deposit_fold_a2_witness_args(&parse_args(args_json)?).map_err(werr)
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

// Tree paths, aggregator paths and envelopes cross as JSON texts in the
// Python reference's shapes (`kernel_vectors._proof_json` and friends).

type Json = serde_json::Value;

fn jparse(text: &str, what: &str) -> Result<Json, JsError> {
    serde_json::from_str(text).map_err(|e| JsError::new(&format!("{what}: invalid JSON: {e}")))
}

fn jword(v: &Json) -> Result<W256, JsError> {
    w(v.as_str().ok_or_else(|| JsError::new("expected hex string"))?)
}

fn jwords(v: &Json) -> Result<Vec<W256>, JsError> {
    v.as_array().ok_or_else(|| JsError::new("expected array"))?.iter().map(jword).collect()
}

fn jbits(v: &Json) -> Result<Vec<u8>, JsError> {
    v.as_array()
        .ok_or_else(|| JsError::new("expected array"))?
        .iter()
        .map(|b| b.as_u64().filter(|b| *b <= 1).map(|b| b as u8).ok_or_else(|| JsError::new("expected bit")))
        .collect()
}

fn juint(v: &Json, what: &str) -> Result<u64, JsError> {
    v.as_u64().ok_or_else(|| JsError::new(&format!("{what}: expected unsigned integer")))
}

fn proof_value(p: &registry::tree::MembershipProof) -> Json {
    serde_json::json!({
        "leaf": hx(&p.leaf),
        "siblings": p.siblings.iter().map(hx).collect::<Vec<_>>(),
        "index_bits": p.index_bits,
        "root": hx(&p.root),
        "leaf_index": p.leaf_index,
    })
}

fn proof_json(p: &registry::tree::MembershipProof) -> String {
    proof_value(p).to_string()
}

fn proof_from(v: &Json) -> Result<registry::tree::MembershipProof, JsError> {
    Ok(registry::tree::MembershipProof {
        leaf: jword(&v["leaf"])?,
        siblings: jwords(&v["siblings"])?,
        index_bits: jbits(&v["index_bits"])?,
        root: jword(&v["root"])?,
        leaf_index: juint(&v["leaf_index"], "leaf_index")? as usize,
    })
}

fn agg_value(p: &registry::aggregator::AggregatorMembershipProof) -> Json {
    serde_json::json!({
        "sub_root": hx(&p.sub_root),
        "siblings": p.siblings.iter().map(hx).collect::<Vec<_>>(),
        "index_bits": p.index_bits,
        "aggregator_root": hx(&p.aggregator_root),
        "sub_tree_id": p.sub_tree_id,
        "aggregator_leaf_index": p.aggregator_leaf_index,
    })
}

fn agg_from(v: &Json) -> Result<registry::aggregator::AggregatorMembershipProof, JsError> {
    Ok(registry::aggregator::AggregatorMembershipProof {
        sub_root: jword(&v["sub_root"])?,
        siblings: jwords(&v["siblings"])?,
        index_bits: jbits(&v["index_bits"])?,
        aggregator_root: jword(&v["aggregator_root"])?,
        sub_tree_id: v["sub_tree_id"].as_str().ok_or_else(|| JsError::new("expected sub_tree_id"))?.to_string(),
        aggregator_leaf_index: juint(&v["aggregator_leaf_index"], "aggregator_leaf_index")? as usize,
    })
}

fn record_value(r: &registry::aggregator::RootRecord) -> Json {
    serde_json::json!({"root": hx(&r.root), "sequence": r.sequence, "posted_at": r.posted_at})
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
        Ok(agg_value(&self.0.aggregator_proof(sub_tree_id).map_err(werr)?).to_string())
    }

    /// Post the current root into the ring; the record as a JSON text
    /// `{root, sequence, posted_at}`.
    pub fn post(&mut self, timestamp: f64) -> Result<String, JsError> {
        Ok(record_value(&self.0.post(timestamp).map_err(werr)?).to_string())
    }

    /// The ring's record of `root`, if it is still retained.
    pub fn root_record(&self, root: &str) -> Result<Option<String>, JsError> {
        Ok(self.0.root_record(&w(root)?).map(|r| record_value(r).to_string()))
    }

    /// Whether a consumer declaring `max_age` accepts `root` at `now`.
    pub fn accepts(&self, root: &str, max_age: f64, now: f64) -> Result<bool, JsError> {
        Ok(self.0.accepts(&w(root)?, max_age, now))
    }

    /// The age of the oldest root the ring still retains.
    pub fn max_retained_age(&self, now: f64) -> f64 {
        self.0.max_retained_age(now)
    }

    /// A sub-tree path joined to this aggregator's path: the one path of
    /// sub-tree depth plus aggregator depth that the circuits take.
    pub fn composed_path(&self, sub_tree_id: &str, sub_proof_json: &str) -> Result<String, JsError> {
        let sub = proof_from(&jparse(sub_proof_json, "sub proof")?)?;
        let full = self.0.full_proof(sub_tree_id, sub, [0u8; 32], [0u8; 32]).map_err(werr)?;
        Ok(proof_json(&full.composed()))
    }

    /// Compose a holder's claims -- a JSON text `[[sub_tree_id, sub_proof],
    /// ...]` -- into one attribute proof against the current root:
    /// `{tree_ids, root, m: {x, y}, proofs: [{sub, aggregator}, ...]}`.
    pub fn prove_attributes(&self, claims_json: &str, mx: &str, my: &str) -> Result<String, JsError> {
        let claims = jparse(claims_json, "claims")?;
        let mut pairs = Vec::new();
        for c in claims.as_array().ok_or_else(|| JsError::new("claims: expected array"))? {
            let id = c[0].as_str().ok_or_else(|| JsError::new("claim: expected sub_tree_id"))?;
            pairs.push((id.to_string(), proof_from(&c[1])?));
        }
        let ap = registry::attributes::prove_attributes(&self.0, pairs, w(mx)?, w(my)?).map_err(werr)?;
        let proofs: Vec<Json> = ap
            .composed
            .proofs
            .iter()
            .map(|f| serde_json::json!({"sub": proof_value(&f.sub_tree_proof), "aggregator": agg_value(&f.aggregator_proof)}))
            .collect();
        Ok(serde_json::json!({
            "tree_ids": ap.tree_ids,
            "root": hx(&ap.root),
            "m": {"x": hx(&w(mx)?), "y": hx(&w(my)?)},
            "proofs": proofs,
        })
        .to_string())
    }

    /// Check an attribute proof as a consumer must: every `required`
    /// sub-tree claimed, every path valid, one root, posted within `max_age`.
    pub fn verify_attributes(
        &self,
        proof_json: &str,
        required: Vec<String>,
        max_age: f64,
        now: f64,
    ) -> Result<bool, JsError> {
        let v = jparse(proof_json, "attribute proof")?;
        let (m_x, m_y) = (jword(&v["m"]["x"])?, jword(&v["m"]["y"])?);
        let mut proofs = Vec::new();
        for p in v["proofs"].as_array().ok_or_else(|| JsError::new("proofs: expected array"))? {
            proofs.push(registry::aggregator::FullMembershipProof {
                sub_tree_proof: proof_from(&p["sub"])?,
                aggregator_proof: agg_from(&p["aggregator"])?,
                m_x,
                m_y,
            });
        }
        let tree_ids = v["tree_ids"]
            .as_array()
            .ok_or_else(|| JsError::new("tree_ids: expected array"))?
            .iter()
            .map(|t| t.as_str().map(str::to_string).ok_or_else(|| JsError::new("tree_ids: expected string")))
            .collect::<Result<Vec<_>, _>>()?;
        let ap = registry::attributes::AttributeProof {
            composed: registry::aggregator::ComposedMembershipProof { proofs },
            tree_ids,
            root: jword(&v["root"])?,
        };
        let req: Vec<&str> = required.iter().map(String::as_str).collect();
        registry::attributes::verify_attributes(&self.0, &ap, &req, max_age, now).map_err(werr)
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
    let full = registry::aggregator::FullMembershipProof {
        sub_tree_proof: proof_from(&jparse(sub_proof_json, "sub proof")?)?,
        aggregator_proof: agg_from(&jparse(aggregator_proof_json, "aggregator proof")?)?,
        m_x: [0u8; 32],
        m_y: [0u8; 32],
    };
    full.verify().map_err(werr)
}

/// An attribute authority's subtree, private (salted leaves) or public.
#[wasm_bindgen]
pub struct FeatureAuthority(registry::feature::FeatureAuthority);

#[wasm_bindgen]
impl FeatureAuthority {
    #[wasm_bindgen(constructor)]
    pub fn new(feature_id: &str, depth: usize, private: bool) -> Result<FeatureAuthority, JsError> {
        Ok(FeatureAuthority(
            registry::feature::FeatureAuthority::new(feature_id, depth, private).map_err(werr)?,
        ))
    }

    pub fn is_private(&self) -> bool {
        self.0.private
    }

    pub fn sub_root(&self) -> Result<String, JsError> {
        Ok(hx(&self.0.sub_root().map_err(werr)?))
    }

    /// Attest `M`; a private subtree needs the holder's `salt`, a public one
    /// refuses it.  Returns `{leaf, leaf_index}` as a JSON text.
    pub fn attest(&mut self, mx: &str, my: &str, salt: Option<String>, attested_at: f64) -> Result<String, JsError> {
        let s = match &salt {
            Some(h) => Some(w(h)?),
            None => None,
        };
        let r = self.0.attest(&g1(mx, my)?, s.as_ref(), attested_at, None).map_err(werr)?;
        Ok(serde_json::json!({"leaf": hx(&r.leaf), "leaf_index": r.leaf_index}).to_string())
    }

    /// Clear `M`'s leaf; the cleared index, or undefined if not attested.
    pub fn revoke(&mut self, mx: &str, my: &str) -> Result<Option<u32>, JsError> {
        Ok(self.0.revoke(&g1(mx, my)?).map_err(werr)?.map(|i| i as u32))
    }

    pub fn has_identity(&self, mx: &str, my: &str) -> Result<bool, JsError> {
        self.0.has_identity(&g1(mx, my)?).map_err(werr)
    }

    /// `M`'s path in this subtree as a JSON text, or undefined.
    pub fn membership_proof_for_identity(&self, mx: &str, my: &str) -> Result<Option<String>, JsError> {
        Ok(self.0.membership_proof_for_identity(&g1(mx, my)?).map_err(werr)?.map(|p| proof_json(&p)))
    }
}

fn envelope_from(v: &Json) -> Result<registry::regulator::InsurerEnvelope, JsError> {
    let small = |k: &str| -> Result<u32, JsError> {
        juint(&v[k], k)?.try_into().map_err(|_| JsError::new(&format!("{k}: out of range")))
    };
    let dep_types = v["dep_types"]
        .as_array()
        .ok_or_else(|| JsError::new("dep_types: expected array"))?
        .iter()
        .map(|d| juint(d, "dep_types").map(|d| d.min(255) as u8))
        .collect::<Result<_, _>>()?;
    let scopes = match v.get("scopes") {
        Some(s) => jwords(s)?.into_iter().collect(),
        None => Default::default(),
    };
    registry::regulator::InsurerEnvelope::new(
        v.get("standing").map_or(Some(true), Json::as_bool).ok_or_else(|| JsError::new("standing: expected bool"))?,
        small("face_band")?.min(255) as u8,
        dep_types,
        small("max_dep_rate")?,
        small("max_premium_rate")?,
        v["expires_at"].as_f64().ok_or_else(|| JsError::new("expires_at: expected number"))?,
        scopes,
    )
    .map_err(werr)
}

fn envelope_value(e: &registry::regulator::InsurerEnvelope) -> Json {
    serde_json::json!({
        "standing": e.standing,
        "face_band": e.face_band,
        "dep_types": e.dep_types.iter().collect::<Vec<_>>(),
        "max_dep_rate": e.max_dep_rate,
        "max_premium_rate": e.max_premium_rate,
        "expires_at": e.expires_at,
        "scopes": e.scopes.iter().map(hx).collect::<Vec<_>>(),
    })
}

/// A face in BuckCredit units: a decimal string, or 0x-hex.
fn face(s: &str) -> Result<u128, JsError> {
    match s.strip_prefix("0x") {
        Some(h) => u128::from_str_radix(h, 16),
        None => s.parse(),
    }
    .map_err(|_| JsError::new("face: expected a u128 decimal or 0x-hex string"))
}

/// A jurisdiction's insurance regulator: one public subtree per predicate.
/// Envelopes cross as JSON texts `{standing, face_band, dep_types,
/// max_dep_rate, max_premium_rate, expires_at, scopes}` (`standing` defaults
/// to true, `scopes` to empty).
#[wasm_bindgen]
pub struct Regulator(registry::regulator::InsuranceRegulator);

#[wasm_bindgen]
impl Regulator {
    #[wasm_bindgen(constructor)]
    pub fn new(jurisdiction: &str, depth: usize) -> Result<Regulator, JsError> {
        Ok(Regulator(
            registry::regulator::InsuranceRegulator::new(jurisdiction, depth).map_err(werr)?,
        ))
    }

    pub fn subtree_id(&self, suffix: &str) -> String {
        self.0.subtree_id(suffix)
    }

    pub fn scope_name(&self, asset_path: &str) -> String {
        self.0.scope_name(asset_path)
    }

    /// Attest an insurer's envelope for this review period; the envelope as
    /// attested (its scopes filled in).
    pub fn attest(
        &mut self,
        mx: &str,
        my: &str,
        envelope_json: &str,
        scope_names: Vec<String>,
        general: bool,
    ) -> Result<String, JsError> {
        let env = envelope_from(&jparse(envelope_json, "envelope")?)?;
        let names: Vec<&str> = scope_names.iter().map(String::as_str).collect();
        Ok(envelope_value(&self.0.attest(&g1(mx, my)?, &env, &names, general).map_err(werr)?).to_string())
    }

    /// The subtree names an attestation proves membership in, in the order
    /// `BuckCredit.attestInsurer` takes their paths.
    pub fn predicate_names(&self, envelope_json: &str, scope_names: Vec<String>) -> Result<Vec<String>, JsError> {
        let env = envelope_from(&jparse(envelope_json, "envelope")?)?;
        let names: Vec<&str> = scope_names.iter().map(String::as_str).collect();
        Ok(self.0.predicate_names(&env, &names))
    }

    /// Clear an insurer from every subtree; the number cleared.
    pub fn revoke(&mut self, mx: &str, my: &str) -> Result<usize, JsError> {
        self.0.revoke(&g1(mx, my)?).map_err(werr)
    }

    pub fn envelope_of(&self, mx: &str, my: &str) -> Result<Option<String>, JsError> {
        Ok(self.0.envelope_of(&g1(mx, my)?).map(|e| envelope_value(e).to_string()))
    }

    /// `M`'s path in the `suffix` subtree as a JSON text, or undefined.
    pub fn membership_proof(&self, mx: &str, my: &str, suffix: &str) -> Result<Option<String>, JsError> {
        Ok(self.0.membership_proof(&g1(mx, my)?, suffix).map_err(werr)?.map(|p| proof_json(&p)))
    }

    /// Every subtree's identifier and root, `[[name, root], ...]`.
    pub fn sub_roots(&self) -> Result<String, JsError> {
        let roots: Vec<(String, String)> =
            self.0.sub_roots().map_err(werr)?.iter().map(|(n, r)| (n.clone(), hx(r))).collect();
        Ok(serde_json::json!(roots).to_string())
    }
}

/// The issuance gate as `BuckCredit.createCredit` applies it: "" if the
/// credit is admitted, otherwise BuckCredit's revert reason.
#[wasm_bindgen]
pub fn registry_check_issuance(
    envelope_json: &str,
    scope: &str,
    face_units: &str,
    dep_type: u8,
    dep_rate: u32,
    premium_rate: u32,
    now: f64,
) -> Result<String, JsError> {
    let env = envelope_from(&jparse(envelope_json, "envelope")?)?;
    Ok(
        match registry::regulator::check_issuance(&env, &w(scope)?, face(face_units)?, dep_type, dep_rate, premium_rate, now) {
            Ok(()) => String::new(),
            Err(e) => e.0.to_string(),
        },
    )
}

/// The smallest face band admitting `face_units`, or FACE_BAND_MAX + 1.
#[wasm_bindgen]
pub fn registry_band_for_face(face_units: &str) -> Result<u8, JsError> {
    Ok(registry::regulator::band_for_face(face(face_units)?))
}

/// The identifier of a scope: keccak256 of its namespaced name.
#[wasm_bindgen]
pub fn registry_scope_id(name: &str) -> Result<String, JsError> {
    Ok(hx(&registry::regulator::scope_id(name).map_err(werr)?))
}

/// The on-chain key of a subtree: keccak256 of its namespaced name.
#[wasm_bindgen]
pub fn registry_subtree_key(name: &str) -> Result<String, JsError> {
    Ok(hx(&registry::regulator::subtree_key(name).map_err(werr)?))
}
