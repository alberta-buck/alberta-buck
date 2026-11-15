//! Unsigned 256-bit values and 512-bit multiply/divide, without allocation.
//!
//! The little-endian limbs are private; bindings use exactly 32 big-endian
//! bytes. Division rejects a zero denominator or a quotient above uint256,
//! matching OpenZeppelin Math.mulDiv. No intermediate product is truncated.

use core::{cmp::Ordering, fmt};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NetMathError {
    DivisionByZero,
    Overflow,
    FundExceedsIssuance,
    InvalidPrincipal,
    NoNetValue,
    TickOutOfRange,
}

impl fmt::Display for NetMathError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::DivisionByZero => "division by zero",
            Self::Overflow => "uint256 overflow",
            Self::FundExceedsIssuance => "fund exceeds issuance",
            Self::InvalidPrincipal => "invalid principal",
            Self::NoNetValue => "no net value",
            Self::TickOutOfRange => "tick out of range",
        })
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct U256([u64; 4]);

impl U256 {
    pub const ZERO: Self = Self([0; 4]);
    pub const ONE: Self = Self::from_u128(1);
    pub const MAX: Self = Self([u64::MAX; 4]);

    pub const fn from_u128(value: u128) -> Self {
        Self([value as u64, (value >> 64) as u64, 0, 0])
    }

    /// The 256-bit value hi * 2^128 + lo.
    pub const fn from_halves(hi: u128, lo: u128) -> Self {
        Self([lo as u64, (lo >> 64) as u64, hi as u64, (hi >> 64) as u64])
    }

    /// 2^n, for n < 256.
    pub const fn pow2(n: u32) -> Self {
        let mut limbs = [0; 4];
        limbs[(n / 64) as usize] = 1 << (n % 64);
        Self(limbs)
    }

    pub fn from_be_bytes(bytes: [u8; 32]) -> Self {
        let mut limbs = [0; 4];
        for (i, limb) in limbs.iter_mut().enumerate() {
            *limb = u64::from_be_bytes(bytes[(3 - i) * 8..(4 - i) * 8].try_into().unwrap());
        }
        Self(limbs)
    }

    pub fn to_be_bytes(self) -> [u8; 32] {
        let mut bytes = [0; 32];
        for (i, limb) in self.0.iter().enumerate() {
            bytes[(3 - i) * 8..(4 - i) * 8].copy_from_slice(&limb.to_be_bytes());
        }
        bytes
    }

    pub fn as_u128(self) -> Option<u128> {
        (self.0[2] == 0 && self.0[3] == 0)
            .then_some(self.0[0] as u128 | ((self.0[1] as u128) << 64))
    }

    pub fn checked_add(self, rhs: Self) -> Result<Self, NetMathError> {
        let mut limbs = [0; 4];
        let mut carry = 0u128;
        for (i, limb) in limbs.iter_mut().enumerate() {
            let sum = self.0[i] as u128 + rhs.0[i] as u128 + carry;
            *limb = sum as u64;
            carry = sum >> 64;
        }
        if carry != 0 {
            Err(NetMathError::Overflow)
        } else {
            Ok(Self(limbs))
        }
    }

    /// The caller proves self >= rhs. Also used after a 257-bit shift:
    /// wrapping subtraction removes its carried high bit exactly.
    fn wrapping_sub(self, rhs: Self) -> Self {
        let mut limbs = [0; 4];
        let mut borrow = false;
        for (i, limb) in limbs.iter_mut().enumerate() {
            let (value, b1) = self.0[i].overflowing_sub(rhs.0[i]);
            let (value, b2) = value.overflowing_sub(u64::from(borrow));
            *limb = value;
            borrow = b1 || b2;
        }
        Self(limbs)
    }

    pub fn checked_sub(self, rhs: Self) -> Result<Self, NetMathError> {
        if self < rhs {
            Err(NetMathError::Overflow)
        } else {
            Ok(self.wrapping_sub(rhs))
        }
    }
}

impl Ord for U256 {
    fn cmp(&self, rhs: &Self) -> Ordering {
        self.0.iter().rev().cmp(rhs.0.iter().rev())
    }
}

impl PartialOrd for U256 {
    fn partial_cmp(&self, rhs: &Self) -> Option<Ordering> {
        Some(self.cmp(rhs))
    }
}

/// floor(a*b/den), or its ceiling when round_up is true.
///
/// Pre: each operand is uint256. Post: the returned quotient fits uint256;
/// the exact remainder is < den. Failure never returns a wrapped quotient.
pub fn mul_div_256(a: U256, b: U256, den: U256, round_up: bool) -> Result<U256, NetMathError> {
    if den == U256::ZERO {
        return Err(NetMathError::DivisionByZero);
    }
    let mut product = [0u64; 8];
    for i in 0..4 {
        let mut carry = 0u128;
        for j in 0..4 {
            // At most (2^64-1)^2 + 2*(2^64-1) == 2^128-1.
            let value = a.0[i] as u128 * b.0[j] as u128 + product[i + j] as u128 + carry;
            product[i + j] = value as u64;
            carry = value >> 64;
        }
        product[i + 4] = carry as u64;
    }
    let mut rem = U256(product[4..].try_into().unwrap());
    if rem >= den {
        return Err(NetMathError::Overflow);
    }
    let mut quotient = U256::ZERO;
    // The upper half is already the initial remainder. Each iteration
    // divides one more input bit; rem < den is maintained throughout.
    for i in (0..256).rev() {
        let mut carry = (product[i / 64] >> (i % 64)) & 1;
        for limb in &mut rem.0 {
            let next = *limb >> 63;
            *limb = (*limb << 1) | carry;
            carry = next;
        }
        if carry != 0 || rem >= den {
            rem = rem.wrapping_sub(den);
            quotient.0[i / 64] |= 1u64 << (i % 64);
        }
    }
    if round_up && rem != U256::ZERO {
        quotient.checked_add(U256::ONE)
    } else {
        Ok(quotient)
    }
}
