// G1 point operations for BN254.
// Point addition in affine coords, λ-verified (no inversion needed).

pragma circom 2.1.4;

include "fq4.circom";

// G1 generator (1, 2)
function G1X0() { return 1; } function G1Y0() { return 2; }

// H_POINT coords — NOT used in 4-limb form directly; pre-decomposed by prover.
// H is the NUMS generator from IdentityRegistry.sol H_X/H_Y.

// ---- Point structure: (x[4], y[4]) -----------------------------------------

// ---- G1 point addition (λ hint, all reduce/k hints provided by prover) -----
//
// Verifies (x3,y3) = (x1,y1) + (x2,y2) using prover-supplied λ and
// all intermediate F_q operation hints.
//
// Checks:
//   1. λ*(x2 - x1) == (y2 - y1)
//   2. x3 == λ² - x1 - x2
//   3. y3 == λ*(x1 - x3) - y1

template G1Add() {
    // Points: (x1,y1), (x2,y2), (x3,y3) — all 4-limb F_q.
    signal input x1[4]; signal input y1[4];
    signal input x2[4]; signal input y2[4];
    signal input x3[4]; signal input y3[4];

    // λ hint.
    signal input lam[4];

    // F_q operation hints.
    signal input sub_reduce_dx;      // for Fq4Sub: x2 - x1
    signal input sub_reduce_dy;      // for Fq4Sub: y2 - y1
    signal input sub_reduce_t1;      // for Fq4Sub: λ² - x1
    signal input sub_reduce_t2;      // for Fq4Sub: (λ² - x1) - x2
    signal input sub_reduce_mxdiff;  // for Fq4Sub: x1 - x3
    signal input sub_reduce_ycalc;   // for Fq4Sub: λ*(x1-x3) - y1

    signal input mul_k0_lamdx; signal input mul_k1_lamdx;
    signal input mul_k2_lamdx; signal input mul_k3_lamdx;
    signal input mul_k0_lamsq; signal input mul_k1_lamsq;
    signal input mul_k2_lamsq; signal input mul_k3_lamsq;
    signal input mul_k0_lamdiff; signal input mul_k1_lamdiff;
    signal input mul_k2_lamdiff; signal input mul_k3_lamdiff;

    // ---- Check 1: λ*(x2 - x1) == (y2 - y1) ----
    component dx = Fq4Sub();
    dx.a[0] <== x2[0]; dx.a[1] <== x2[1]; dx.a[2] <== x2[2]; dx.a[3] <== x2[3];
    dx.b[0] <== x1[0]; dx.b[1] <== x1[1]; dx.b[2] <== x1[2]; dx.b[3] <== x1[3];
    dx.reduce <== sub_reduce_dx;

    component dy = Fq4Sub();
    dy.a[0] <== y2[0]; dy.a[1] <== y2[1]; dy.a[2] <== y2[2]; dy.a[3] <== y2[3];
    dy.b[0] <== y1[0]; dy.b[1] <== y1[1]; dy.b[2] <== y1[2]; dy.b[3] <== y1[3];
    dy.reduce <== sub_reduce_dy;

    component lam_dx = Fq4Mul();
    lam_dx.a[0] <== lam[0]; lam_dx.a[1] <== lam[1]; lam_dx.a[2] <== lam[2]; lam_dx.a[3] <== lam[3];
    lam_dx.b[0] <== dx.out[0]; lam_dx.b[1] <== dx.out[1]; lam_dx.b[2] <== dx.out[2]; lam_dx.b[3] <== dx.out[3];
    lam_dx.k0 <== mul_k0_lamdx; lam_dx.k1 <== mul_k1_lamdx; lam_dx.k2 <== mul_k2_lamdx; lam_dx.k3 <== mul_k3_lamdx;

    component eq1 = Fq4Eq();
    eq1.a[0] <== lam_dx.out[0]; eq1.a[1] <== lam_dx.out[1]; eq1.a[2] <== lam_dx.out[2]; eq1.a[3] <== lam_dx.out[3];
    eq1.b[0] <== dy.out[0]; eq1.b[1] <== dy.out[1]; eq1.b[2] <== dy.out[2]; eq1.b[3] <== dy.out[3];
    eq1.eq === 1;

    // ---- Check 2: x3 == λ² - x1 - x2 ----
    component lam_sq = Fq4Mul();
    lam_sq.a[0] <== lam[0]; lam_sq.a[1] <== lam[1]; lam_sq.a[2] <== lam[2]; lam_sq.a[3] <== lam[3];
    lam_sq.b[0] <== lam[0]; lam_sq.b[1] <== lam[1]; lam_sq.b[2] <== lam[2]; lam_sq.b[3] <== lam[3];
    lam_sq.k0 <== mul_k0_lamsq; lam_sq.k1 <== mul_k1_lamsq; lam_sq.k2 <== mul_k2_lamsq; lam_sq.k3 <== mul_k3_lamsq;

    component t1 = Fq4Sub();
    t1.a[0] <== lam_sq.out[0]; t1.a[1] <== lam_sq.out[1]; t1.a[2] <== lam_sq.out[2]; t1.a[3] <== lam_sq.out[3];
    t1.b[0] <== x1[0]; t1.b[1] <== x1[1]; t1.b[2] <== x1[2]; t1.b[3] <== x1[3];
    t1.reduce <== sub_reduce_t1;

    component t2 = Fq4Sub();
    t2.a[0] <== t1.out[0]; t2.a[1] <== t1.out[1]; t2.a[2] <== t1.out[2]; t2.a[3] <== t1.out[3];
    t2.b[0] <== x2[0]; t2.b[1] <== x2[1]; t2.b[2] <== x2[2]; t2.b[3] <== x2[3];
    t2.reduce <== sub_reduce_t2;

    component eq2 = Fq4Eq();
    eq2.a[0] <== t2.out[0]; eq2.a[1] <== t2.out[1]; eq2.a[2] <== t2.out[2]; eq2.a[3] <== t2.out[3];
    eq2.b[0] <== x3[0]; eq2.b[1] <== x3[1]; eq2.b[2] <== x3[2]; eq2.b[3] <== x3[3];
    eq2.eq === 1;

    // ---- Check 3: y3 == λ*(x1 - x3) - y1 ----
    component mxdiff = Fq4Sub();
    mxdiff.a[0] <== x1[0]; mxdiff.a[1] <== x1[1]; mxdiff.a[2] <== x1[2]; mxdiff.a[3] <== x1[3];
    mxdiff.b[0] <== x3[0]; mxdiff.b[1] <== x3[1]; mxdiff.b[2] <== x3[2]; mxdiff.b[3] <== x3[3];
    mxdiff.reduce <== sub_reduce_mxdiff;

    component lam_diff = Fq4Mul();
    lam_diff.a[0] <== lam[0]; lam_diff.a[1] <== lam[1]; lam_diff.a[2] <== lam[2]; lam_diff.a[3] <== lam[3];
    lam_diff.b[0] <== mxdiff.out[0]; lam_diff.b[1] <== mxdiff.out[1]; lam_diff.b[2] <== mxdiff.out[2]; lam_diff.b[3] <== mxdiff.out[3];
    lam_diff.k0 <== mul_k0_lamdiff; lam_diff.k1 <== mul_k1_lamdiff; lam_diff.k2 <== mul_k2_lamdiff; lam_diff.k3 <== mul_k3_lamdiff;

    component ycalc = Fq4Sub();
    ycalc.a[0] <== lam_diff.out[0]; ycalc.a[1] <== lam_diff.out[1]; ycalc.a[2] <== lam_diff.out[2]; ycalc.a[3] <== lam_diff.out[3];
    ycalc.b[0] <== y1[0]; ycalc.b[1] <== y1[1]; ycalc.b[2] <== y1[2]; ycalc.b[3] <== y1[3];
    ycalc.reduce <== sub_reduce_ycalc;

    component eq3 = Fq4Eq();
    eq3.a[0] <== ycalc.out[0]; eq3.a[1] <== ycalc.out[1]; eq3.a[2] <== ycalc.out[2]; eq3.a[3] <== ycalc.out[3];
    eq3.b[0] <== y3[0]; eq3.b[1] <== y3[1]; eq3.b[2] <== y3[2]; eq3.b[3] <== y3[3];
    eq3.eq === 1;
}
