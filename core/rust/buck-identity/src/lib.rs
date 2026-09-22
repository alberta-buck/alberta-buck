//! buck-identity -- the Alberta Buck identity kernel.
//!
//! BN254 curve arithmetic, keccak Fiat-Shamir transcripts, circomlib
//! Poseidon, and every sigma protocol of the BUCK identity layer:
//! Pointcheval-Sanders credentials, ElGamal, the registration NIZK,
//! Chaum-Pedersen approve, issuer Schnorr batch binding, verifiable
//! decryption, the A2 issuer re-encryption binding, deposit coupling, the
//! B1 depositor binding, and the note commitment / nullifier / id-hash
//! family.
//!
//! The executable specification is the Python reference in
//! `alberta_buck/wallet` (py_ecc-backed); this kernel matches it
//! bit-for-bit, proven by the golden vectors in
//! `test/vectors/identity.json` and
//! `core/vectors/identity-kernel-vectors.json`.
//!
//! Determinism rule: every nonce and blinding factor is an explicit
//! argument -- the kernel contains NO randomness.  Callers (the Python
//! wallet shims, the JS wrapper) draw randomness themselves, which is what
//! preserves the pinned `random.Random` streams behind the committed
//! fixtures.
//!
//! Wire convention (the same one `BN254.sol` and the wallet use):
//! scalars and coordinates are 32-byte big-endian words ([`W256`]); a G1
//! point is an `(x, y)` word pair with `(0, 0)` the point at infinity; a
//! G2 point is `((x_c0, x_c1), (y_c0, y_c1))`.  Scalar inputs are reduced
//! mod the group order exactly where the Python reference applies
//! `% ORDER`.

pub mod b1_binding;
pub mod chaum_pedersen;
pub mod domains;
pub mod elgamal;
pub mod issuer_reenc;
pub mod keccak;
pub mod nizk;
pub mod nums;
pub mod notes;
pub mod pairing;
pub mod poseidon;
pub mod ps;
pub mod schnorr;
pub mod unilateral_a2;
pub mod verifiable_decrypt;

use ark_bn254::{Fq, Fq2, Fr, G1Affine, G1Projective, G2Affine, G2Projective};
use ark_ec::AffineRepr;
use ark_ec::CurveGroup;
use ark_ff::{BigInt, BigInteger, PrimeField};

/// A 32-byte big-endian word: uint256, exactly as Solidity sees it.
pub type W256 = [u8; 32];
/// A G1 point as an `(x, y)` word pair; `(0, 0)` is the point at infinity.
pub type G1w = (W256, W256);
/// A G2 point as `((x_c0, x_c1), (y_c0, y_c1))` -- py_ecc `FQ2.coeffs` order.
pub type G2w = ((W256, W256), (W256, W256));

/// Kernel error: message strings mirror the Python reference's exceptions
/// where one exists.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IdError(pub &'static str);

impl core::fmt::Display for IdError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.write_str(self.0)
    }
}

impl std::error::Error for IdError {}

pub type Result<T> = core::result::Result<T, IdError>;

pub const ZERO_W: W256 = [0u8; 32];

// ---------------------------------------------------------------------------
// Word <-> field conversions
// ---------------------------------------------------------------------------

pub(crate) fn bigint_be(w: &W256) -> BigInt<4> {
    let mut limbs = [0u64; 4];
    for (i, limb) in limbs.iter_mut().enumerate() {
        let start = (3 - i) * 8;
        let mut v = 0u64;
        for b in &w[start..start + 8] {
            v = (v << 8) | *b as u64;
        }
        *limb = v;
    }
    BigInt::new(limbs)
}

pub(crate) fn w_from_bigint(b: &BigInt<4>) -> W256 {
    let bytes = b.to_bytes_be();
    let mut w = [0u8; 32];
    w.copy_from_slice(&bytes);
    w
}

/// Base-field element, strict: a coordinate word must be canonical (< q).
pub(crate) fn fq_strict(w: &W256) -> Result<Fq> {
    Fq::from_bigint(bigint_be(w)).ok_or(IdError("coordinate not a canonical Fq element"))
}

/// Scalar reduced mod ORDER -- mirrors the Python reference's `% ORDER`.
/// Public as the sibling-crate (buck-registry / buck-wallet) interop
/// surface, together with [`w_from_fr`], [`g1_from_w`] and [`w_from_g1`].
pub fn fr_mod(w: &W256) -> Fr {
    Fr::from_be_bytes_mod_order(w)
}

pub fn w_from_fr(x: &Fr) -> W256 {
    w_from_bigint(&x.into_bigint())
}

/// Arbitrary-length big-endian bytes reduced mod ORDER -- the Python
/// `int.from_bytes(data, "big") % ORDER` idiom (registry transcript ids,
/// message hashes).
pub fn scalar_from_be_bytes_mod_order(data: &[u8]) -> W256 {
    w_from_fr(&Fr::from_be_bytes_mod_order(data))
}

pub(crate) fn w_from_fq(x: &Fq) -> W256 {
    w_from_bigint(&x.into_bigint())
}

/// True iff `w`, read as a uint256, is < ORDER (the scalar field modulus).
pub(crate) fn w_lt_order(w: &W256) -> bool {
    bigint_be(w) < Fr::MODULUS
}

pub(crate) fn w_is_zero(w: &W256) -> bool {
    w.iter().all(|b| *b == 0)
}

// ---------------------------------------------------------------------------
// Word <-> point conversions
// ---------------------------------------------------------------------------

pub fn g1_from_w(p: &G1w) -> Result<G1Affine> {
    if w_is_zero(&p.0) && w_is_zero(&p.1) {
        return Ok(G1Affine::identity());
    }
    let a = G1Affine::new_unchecked(fq_strict(&p.0)?, fq_strict(&p.1)?);
    // G1 has cofactor 1: on-curve == in-group.
    if !a.is_on_curve() {
        return Err(IdError("G1 point not on curve"));
    }
    Ok(a)
}

pub fn w_from_g1(p: &G1Affine) -> G1w {
    match p.xy() {
        Some((x, y)) => (w_from_fq(&x), w_from_fq(&y)),
        None => (ZERO_W, ZERO_W),
    }
}

pub fn w_from_g1p(p: &G1Projective) -> G1w {
    w_from_g1(&p.into_affine())
}

pub(crate) fn g2_from_w(p: &G2w) -> Result<G2Affine> {
    let ((x0, x1), (y0, y1)) = p;
    if w_is_zero(x0) && w_is_zero(x1) && w_is_zero(y0) && w_is_zero(y1) {
        return Ok(G2Affine::identity());
    }
    let x = Fq2::new(fq_strict(x0)?, fq_strict(x1)?);
    let y = Fq2::new(fq_strict(y0)?, fq_strict(y1)?);
    let a = G2Affine::new_unchecked(x, y);
    if !a.is_on_curve() {
        return Err(IdError("G2 point not on curve"));
    }
    if !a.is_in_correct_subgroup_assuming_on_curve() {
        return Err(IdError("G2 point not in the prime-order subgroup"));
    }
    Ok(a)
}

pub(crate) fn w_from_g2(p: &G2Affine) -> G2w {
    match p.xy() {
        Some((x, y)) => (
            (w_from_fq(&x.c0), w_from_fq(&x.c1)),
            (w_from_fq(&y.c0), w_from_fq(&y.c1)),
        ),
        None => ((ZERO_W, ZERO_W), (ZERO_W, ZERO_W)),
    }
}

// ---------------------------------------------------------------------------
// Public curve API (the bn254.py surface: add / mul / neg, generators)
// ---------------------------------------------------------------------------

/// The BN254 group order r (= the Poseidon field modulus F_R).
pub fn order() -> W256 {
    w_from_bigint(&Fr::MODULUS)
}

/// `w mod ORDER` -- the Python reference's `% ORDER` / `scalar_to_word`.
pub fn reduce_mod_order(w: &W256) -> W256 {
    w_from_fr(&fr_mod(w))
}

/// The BN254 base-field modulus q.
pub fn field_modulus() -> W256 {
    w_from_bigint(&Fq::MODULUS)
}

pub fn g1_generator() -> G1w {
    w_from_g1(&G1Affine::generator())
}

pub fn g2_generator() -> G2w {
    w_from_g2(&G2Affine::generator())
}

pub fn g1_add(a: &G1w, b: &G1w) -> Result<G1w> {
    let a = g1_from_w(a)?;
    let b = g1_from_w(b)?;
    Ok(w_from_g1p(&(a + b)))
}

/// `k * P` with `k` reduced mod ORDER -- py_ecc `multiply` semantics for
/// points of the prime-order G1 group.
pub fn g1_mul(p: &G1w, k: &W256) -> Result<G1w> {
    let p = g1_from_w(p)?;
    Ok(w_from_g1p(&(p * fr_mod(k))))
}

pub fn g1_neg(p: &G1w) -> Result<G1w> {
    let p = g1_from_w(p)?;
    Ok(w_from_g1(&(-p)))
}

pub fn g2_add(a: &G2w, b: &G2w) -> Result<G2w> {
    let a = g2_from_w(a)?;
    let b = g2_from_w(b)?;
    Ok(w_from_g2(&(a + b).into_affine()))
}

pub fn g2_mul(p: &G2w, k: &W256) -> Result<G2w> {
    let p = g2_from_w(p)?;
    let r: G2Projective = p * fr_mod(k);
    Ok(w_from_g2(&r.into_affine()))
}

// ---------------------------------------------------------------------------
// Fiat-Shamir transcript builder (internal)
// ---------------------------------------------------------------------------

/// Accumulates 32-byte words in transcript order; `.e()` is the challenge.
/// Word order MUST match the Python `_*_transcript` functions exactly.
pub(crate) struct Transcript(pub Vec<W256>);

impl Transcript {
    pub fn new() -> Self {
        Transcript(Vec::new())
    }

    /// Append a G1 point as its (x, y) word pair.
    pub fn p(&mut self, a: &G1Affine) -> &mut Self {
        let (x, y) = w_from_g1(a);
        self.0.push(x);
        self.0.push(y);
        self
    }

    /// Append a raw word (scalar, address, chainid, hash).
    pub fn w(&mut self, w: &W256) -> &mut Self {
        self.0.push(*w);
        self
    }

    /// The Fiat-Shamir challenge: keccak over the words, reduced mod ORDER.
    pub fn e(&self) -> Fr {
        Fr::from_be_bytes_mod_order(&keccak::keccak_words(&self.0))
    }
}
