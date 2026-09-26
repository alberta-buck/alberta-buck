//! buck-wallet -- the Alberta Buck wallet kernel.
//!
//! The deterministic wallet layer above the `buck-identity` crypto
//! kernel: THE canonical JSON dialect and identity serialization
//! (`canonical`), the AB-RCPT/2 receipt envelope (`envelope`), tier-1
//! offline receipt verification (`verify`), the per-kind receipt builders
//! (`builders`), the public-issuer / approve receipt verifiers
//! (`receipt`), the unilateral identity-targeted Note flows A1/A2
//! (`flows`), and the credential-issuer ceremony (`issuer`).
//!
//! The executable specification is the Python reference in
//! `alberta_buck/wallet`; this crate matches it bit-for-bit -- canonical
//! bytes, receipt ids, envelope text and every verification predicate --
//! proven by `core/vectors/wallet-kernel-vectors.json` (emitted by the
//! Python reference, replayed by the cargo / pytest / node suites).
//!
//! Determinism rule (the platform doctrine): no randomness and no clocks
//! in the kernel.  Every nonce is an explicit argument, drawn by the
//! caller in exactly the order the Python reference draws them, which is
//! what preserves the pinned `random.Random` streams behind the
//! committed fixtures.
//!
//! Layering: depends on `buck-identity` (curve, Poseidon, keccak, sigma
//! protocols) and `buck-registry` (the identity Merkle accumulator the
//! unilateral receipt flows check membership against) -- mirroring the
//! Python import graph.

pub mod args;
pub mod builders;
pub mod canonical;
pub mod delivery;
pub mod deposit_fold;
pub mod envelope;
pub mod flows;
pub mod issuer;
pub mod jsonv;
pub mod receipt;
pub mod verify;

pub use buck_identity::{G1w, IdError, Result, W256, ZERO_W};

/// An ElGamal ciphertext as its `(R, C)` word-pair pair -- the same shape
/// `buck_identity::elgamal` returns.
pub type Ctw = (G1w, G1w);

/// A note opening -- the SNARK witness tuple `(flavor, v, rho, id_hash,
/// predicate)`, exactly `alberta_buck.wallet.notes.NoteOpening`.  Range
/// checks live in `buck_identity::notes::note_commitment`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NoteOpening {
    pub flavor: u64,
    pub v: W256,
    pub rho: W256,
    pub id_hash: W256,
    pub predicate: W256,
}

impl NoteOpening {
    /// `cm = Poseidon([flavor, v, rho, id_hash, predicate])`.
    pub fn commitment(&self) -> Result<W256> {
        buck_identity::notes::note_commitment(
            self.flavor,
            &self.v,
            &self.rho,
            &self.id_hash,
            &self.predicate,
        )
    }
}

/// A `u64` as a 32-byte big-endian word.
pub fn w_from_u64(v: u64) -> W256 {
    let mut w = [0u8; 32];
    w[24..].copy_from_slice(&v.to_be_bytes());
    w
}

/// A `u128` as a 32-byte big-endian word.
pub fn w_from_u128(v: u128) -> W256 {
    let mut w = [0u8; 32];
    w[16..].copy_from_slice(&v.to_be_bytes());
    w
}

/// The word as `u128` (errors if any of the top 16 bytes is set).
pub fn u128_from_w(w: &W256) -> Result<u128> {
    if w[..16].iter().any(|b| *b != 0) {
        return Err(IdError("word out of u128 range"));
    }
    Ok(u128::from_be_bytes(w[16..].try_into().unwrap()))
}

/// Full-width lowercase hex of a word, `0x`-prefixed -- the wallet's
/// standard scalar/coordinate serialization (`bn254.scalar_to_hex`
/// applies `% ORDER` first; use [`scalar_hex`] for that).
pub fn hex_w(w: &W256) -> String {
    let mut s = String::with_capacity(66);
    s.push_str("0x");
    for b in w {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

/// `bn254.scalar_to_hex`: the word reduced mod ORDER, full-width hex.
pub fn scalar_hex(w: &W256) -> String {
    hex_w(&buck_identity::reduce_mod_order(w))
}

/// A word as a decimal string -- the convention of the delivery document and
/// of every circuit witness (snarkjs and rapidsnark read decimal).
pub fn dec_w(w: &W256) -> String {
    let mut limbs = [0u64; 4]; // little-endian limbs
    for (i, limb) in limbs.iter_mut().enumerate() {
        let off = 32 - 8 * (i + 1);
        *limb = u64::from_be_bytes(w[off..off + 8].try_into().expect("8 bytes"));
    }
    if limbs == [0u64; 4] {
        return "0".to_string();
    }
    let mut digits = Vec::with_capacity(78);
    while limbs != [0u64; 4] {
        let mut rem: u128 = 0;
        for limb in limbs.iter_mut().rev() {
            let cur = (rem << 64) | u128::from(*limb);
            *limb = (cur / 10) as u64;
            rem = cur % 10;
        }
        digits.push(b'0' + rem as u8);
    }
    digits.reverse();
    String::from_utf8(digits).expect("ascii digits")
}

/// Parse a decimal string into a word, refusing anything that does not fit.
pub fn w_from_dec(s: &str) -> Result<W256> {
    if s.is_empty() || s.len() > 78 {
        return Err(IdError("decimal word must be 1..78 digits"));
    }
    let mut limbs = [0u64; 4];
    for c in s.bytes() {
        if !c.is_ascii_digit() {
            return Err(IdError("invalid decimal"));
        }
        let mut carry = u128::from(c - b'0');
        for limb in limbs.iter_mut() {
            let cur = u128::from(*limb) * 10 + carry;
            *limb = cur as u64;
            carry = cur >> 64;
        }
        if carry != 0 {
            return Err(IdError("decimal word exceeds 256 bits"));
        }
    }
    let mut w = [0u8; 32];
    for (i, limb) in limbs.iter().enumerate() {
        let off = 32 - 8 * (i + 1);
        w[off..off + 8].copy_from_slice(&limb.to_be_bytes());
    }
    Ok(w)
}

/// Parse a `0x`-hex string (any length up to 64 nybbles) into a word --
/// the `int(s, 16)` idiom of the Python readers.
pub fn w_from_hex(s: &str) -> Result<W256> {
    let h = s.strip_prefix("0x").unwrap_or(s);
    if h.is_empty() || h.len() > 64 {
        return Err(IdError("hex word must be 1..64 nybbles"));
    }
    let mut w = [0u8; 32];
    let mut oi = 32;
    let bytes = h.as_bytes();
    let mut i = bytes.len();
    let nyb = |c: u8| -> Result<u8> {
        match c {
            b'0'..=b'9' => Ok(c - b'0'),
            b'a'..=b'f' => Ok(c - b'a' + 10),
            b'A'..=b'F' => Ok(c - b'A' + 10),
            _ => Err(IdError("invalid hex")),
        }
    };
    while i > 0 {
        let lo = nyb(bytes[i - 1])?;
        let hi = if i >= 2 { nyb(bytes[i - 2])? } else { 0 };
        oi -= 1;
        w[oi] = (hi << 4) | lo;
        i = i.saturating_sub(2);
    }
    Ok(w)
}
