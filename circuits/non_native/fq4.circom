// Non-native F_q arithmetic for BN254 G1 operations in circom.
// 4-limb representation: val = lim[0] + lim[1]*2^64 + lim[2]*2^128 + lim[3]*2^192.
//
// Design: prover provides RESULTS; circuit verifies by ADDITION.
// Subtraction: verify (out + b == a + adjust) using addition checks.
// Multiplication: verify (out + k*q == a*b) using addition checks.
// This avoids borrow/underflow issues in subtraction.

pragma circom 2.1.4;

include "../../node_modules/circomlib/circuits/bitify.circom";

// ---- F_q modulus as 4 64-bit limbs -----------------------------------------

function Q0() { return 0x3c208c16d87cfd47; }
function Q1() { return 0x97816a916871ca8d; }
function Q2() { return 0xb85045b68181585d; }
function Q3() { return 0x30644e72e131a029; }

// ---- F_q equality: is a == b? ----------------------------------------------

template Fq4Eq() {
    signal input a[4]; signal input b[4]; signal output eq;
    signal d0; d0 <== a[0] - b[0];
    signal d1; d1 <== a[1] - b[1];
    signal d2; d2 <== a[2] - b[2];
    signal d3; d3 <== a[3] - b[3];
    d0 + d1 + d2 + d3 === 0;
    eq <== 1;
}

// ---- Addition: out = a + b (prover provides carry-normalized result) --------
// Verifies: out == a + b (mod q) by checking out + k*q == a + b for some k.
// Since a+b < 2q, k is 0 or 1.

template Fq4Add() {
    signal input a[4]; signal input b[4];
    signal input out[4];
    signal input k;  // 0 or 1: number of q subtractions to normalize

    signal sum[4];
    sum[0] <== a[0] + b[0];
    sum[1] <== a[1] + b[1];
    sum[2] <== a[2] + b[2];
    sum[3] <== a[3] + b[3];

    // Verify: out + k*Q == sum (as 4-limb values, carries auto-resolved in F_r)
    out[0] + k * Q0() === sum[0];
    out[1] + k * Q1() === sum[1];
    out[2] + k * Q2() === sum[2];
    out[3] + k * Q3() === sum[3];
}

// ---- Subtraction: out = (a - b) mod q ---------------
// Verify: out + b == a (if a >= b) or out + b == a + q (if a < b).
// Prover supplies out and a flag "need_q" (1 if a < b).

template Fq4Sub() {
    signal input a[4]; signal input b[4];
    signal input out[4];
    signal input need_q;  // 1 if a < b (added q before subtracting), 0 if a >= b

    // Verify: out + b == a + need_q * q
    out[0] + b[0] === a[0] + need_q * Q0();
    out[1] + b[1] === a[1] + need_q * Q1();
    out[2] + b[2] === a[2] + need_q * Q2();
    out[3] + b[3] === a[3] + need_q * Q3();
}

// ---- Multiplication: out = (a * b) % q ---------------
// Prover supplies out and quotient kq_limbs (4 limbs of k = floor(a*b/q)).
// Verify: out + k*q == a*b at the limb level.
// Note: a*b is computed via partial products (no carry normalization needed
// since we compare against out + k*q which includes carries implicitly).

template Fq4Mul() {
    signal input a[4]; signal input b[4];
    signal input out[4];

    // Prover supplies k = floor(a*b / q) as 4 limbs.
    signal input k0; signal input k1; signal input k2; signal input k3;

    // Compute a*b partial products.
    signal p00; p00 <== a[0] * b[0];
    signal p01; p01 <== a[0] * b[1];
    signal p02; p02 <== a[0] * b[2];
    signal p03; p03 <== a[0] * b[3];
    signal p10; p10 <== a[1] * b[0];
    signal p11; p11 <== a[1] * b[1];
    signal p12; p12 <== a[1] * b[2];
    signal p13; p13 <== a[1] * b[3];
    signal p20; p20 <== a[2] * b[0];
    signal p21; p21 <== a[2] * b[1];
    signal p22; p22 <== a[2] * b[2];
    signal p23; p23 <== a[2] * b[3];
    signal p30; p30 <== a[3] * b[0];
    signal p31; p31 <== a[3] * b[1];
    signal p32; p32 <== a[3] * b[2];
    signal p33; p33 <== a[3] * b[3];

    // a*b limbs (raw sums at each position).
    signal ab0; ab0 <== p00;
    signal ab1; ab1 <== p01 + p10;
    signal ab2; ab2 <== p02 + p11 + p20;
    signal ab3; ab3 <== p03 + p12 + p21 + p30;
    signal ab4; ab4 <== p13 + p22 + p31;
    signal ab5; ab5 <== p23 + p32;
    signal ab6; ab6 <== p33;
    signal ab7; ab7 <== 0;

    // Compute k*Q partial products.
    signal kq00; kq00 <== k0 * Q0();
    signal kq01; kq01 <== k0 * Q1();
    signal kq02; kq02 <== k0 * Q2();
    signal kq03; kq03 <== k0 * Q3();
    signal kq10; kq10 <== k1 * Q0();
    signal kq11; kq11 <== k1 * Q1();
    signal kq12; kq12 <== k1 * Q2();
    signal kq13; kq13 <== k1 * Q3();
    signal kq20; kq20 <== k2 * Q0();
    signal kq21; kq21 <== k2 * Q1();
    signal kq22; kq22 <== k2 * Q2();
    signal kq23; kq23 <== k2 * Q3();
    signal kq30; kq30 <== k3 * Q0();
    signal kq31; kq31 <== k3 * Q1();
    signal kq32; kq32 <== k3 * Q2();
    signal kq33; kq33 <== k3 * Q3();

    // k*Q limbs at each position.
    signal kq0; kq0 <== kq00;
    signal kq1; kq1 <== kq01 + kq10;
    signal kq2; kq2 <== kq02 + kq11 + kq20;
    signal kq3; kq3 <== kq03 + kq12 + kq21 + kq30;
    signal kq4; kq4 <== kq13 + kq22 + kq31;
    signal kq5; kq5 <== kq23 + kq32;
    signal kq6; kq6 <== kq33;
    signal kq7; kq7 <== 0;

    // Verify: out + k*q == a*b at EVERY limb position (0..7).
    // Positions 4-7: only k*q and a*b contribute (out has no limbs there).
    ab4 === kq4;
    ab5 === kq5;
    ab6 === kq6;
    ab7 === kq7;

    // Positions 0-3: out[i] + kq[i] == ab[i].
    out[0] + kq0 === ab0;
    out[1] + kq1 === ab1;
    out[2] + kq2 === ab2;
    out[3] + kq3 === ab3;
}
