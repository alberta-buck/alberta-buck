// Identity membership circuit with G1 tie — Phase C.
// Public: identityRoot, PI_x[4], PI_y[4].
// All F_q intermediate results are prover-supplied; circuit verifies relationships.

pragma circom 2.1.4;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/switcher.circom";
include "non_native/fq4.circom";

template MerkleProof(depth) {
    signal input  leaf;
    signal input  pathElements[depth];
    signal input  pathIndices[depth];
    signal output root;
    component switchers[depth];
    component hashers[depth];
    signal levels[depth + 1];
    levels[0] <== leaf;
    for (var i = 0; i < depth; i++) {
        pathIndices[i] * (pathIndices[i] - 1) === 0;
        switchers[i] = Switcher(); switchers[i].sel <== pathIndices[i];
        switchers[i].L <== levels[i]; switchers[i].R <== pathElements[i];
        hashers[i] = Poseidon(2);
        hashers[i].inputs[0] <== switchers[i].outL;
        hashers[i].inputs[1] <== switchers[i].outR;
        levels[i + 1] <== hashers[i].out;
    }
    root <== levels[depth];
}

template IdentityMembershipG1Tie(depth) {
    // ===== PUBLIC ===========================================================
    signal input identityRoot;
    signal input PI_x[4]; signal input PI_y[4];

    // ===== PRIVATE: M, b ====================================================
    signal input Mx[4]; signal input My[4];
    signal input Mx_mod; signal input My_mod;
    signal input b;

    // ===== PRIVATE: T = b*H (prover-supplied) ===============================
    signal input Tx[4]; signal input Ty[4];

    // ===== PRIVATE: F_q intermediate results (all prover-supplied) ==========
    signal input lam[4];
    signal input dx_out[4]; signal input dy_out[4];
    signal input lam_dx_out[4]; signal input lam_sq_out[4];
    signal input t1_out[4]; signal input t2_out[4];
    signal input mx_diff_out[4]; signal input lam_diff_out[4];
    signal input y_calc_out[4];

    // ===== PRIVATE: F_q operation hints =====================================
    signal input sub_need_q_dx; signal input sub_need_q_dy;
    signal input sub_need_q_t1; signal input sub_need_q_t2;
    signal input sub_need_q_mxdiff; signal input sub_need_q_ycalc;
    signal input mk0_lamdx; signal input mk1_lamdx; signal input mk2_lamdx; signal input mk3_lamdx;
    signal input mk0_lamsq; signal input mk1_lamsq; signal input mk2_lamsq; signal input mk3_lamsq;
    signal input mk0_lamdiff; signal input mk1_lamdiff; signal input mk2_lamdiff; signal input mk3_lamdiff;

    // ===== PRIVATE: Merkle path =============================================
    signal input pathElements[depth];
    signal input pathIndices[depth];

    // ===== CONSTRAINT 1: M in identityRoot ==================================
    signal Mx_rec; Mx_rec <== Mx[0] + Mx[1]*(1<<64) + Mx[2]*(1<<128) + Mx[3]*(1<<192);
    Mx_rec === Mx_mod;
    signal My_rec; My_rec <== My[0] + My[1]*(1<<64) + My[2]*(1<<128) + My[3]*(1<<192);
    My_rec === My_mod;
    component leafH = Poseidon(2);
    leafH.inputs[0] <== Mx_mod; leafH.inputs[1] <== My_mod;
    component mp = MerkleProof(depth);
    mp.leaf <== leafH.out;
    for (var i = 0; i < depth; i++) { mp.pathElements[i] <== pathElements[i]; mp.pathIndices[i] <== pathIndices[i]; }
    mp.root === identityRoot;

    // ===== CONSTRAINT 2: PI = M + T (G1 addition, lambda-verified) ==========

    // dx = Tx - Mx; dy = Ty - My
    component c_dx = Fq4Sub();  c_dx.a[0]<==Tx[0];c_dx.a[1]<==Tx[1];c_dx.a[2]<==Tx[2];c_dx.a[3]<==Tx[3]; c_dx.b[0]<==Mx[0];c_dx.b[1]<==Mx[1];c_dx.b[2]<==Mx[2];c_dx.b[3]<==Mx[3]; c_dx.out[0]<==dx_out[0];c_dx.out[1]<==dx_out[1];c_dx.out[2]<==dx_out[2];c_dx.out[3]<==dx_out[3]; c_dx.need_q <== sub_need_q_dx;
    component c_dy = Fq4Sub();  c_dy.a[0]<==Ty[0];c_dy.a[1]<==Ty[1];c_dy.a[2]<==Ty[2];c_dy.a[3]<==Ty[3]; c_dy.b[0]<==My[0];c_dy.b[1]<==My[1];c_dy.b[2]<==My[2];c_dy.b[3]<==My[3]; c_dy.out[0]<==dy_out[0];c_dy.out[1]<==dy_out[1];c_dy.out[2]<==dy_out[2];c_dy.out[3]<==dy_out[3]; c_dy.need_q <== sub_need_q_dy;

    // lam*dx == dy
    component c_lam_dx = Fq4Mul(); c_lam_dx.a[0]<==lam[0];c_lam_dx.a[1]<==lam[1];c_lam_dx.a[2]<==lam[2];c_lam_dx.a[3]<==lam[3]; c_lam_dx.b[0]<==dx_out[0];c_lam_dx.b[1]<==dx_out[1];c_lam_dx.b[2]<==dx_out[2];c_lam_dx.b[3]<==dx_out[3]; c_lam_dx.out[0]<==lam_dx_out[0];c_lam_dx.out[1]<==lam_dx_out[1];c_lam_dx.out[2]<==lam_dx_out[2];c_lam_dx.out[3]<==lam_dx_out[3]; c_lam_dx.k0<==mk0_lamdx;c_lam_dx.k1<==mk1_lamdx;c_lam_dx.k2<==mk2_lamdx;c_lam_dx.k3<==mk3_lamdx;
    component eq_slope = Fq4Eq(); eq_slope.a[0]<==lam_dx_out[0];eq_slope.a[1]<==lam_dx_out[1];eq_slope.a[2]<==lam_dx_out[2];eq_slope.a[3]<==lam_dx_out[3]; eq_slope.b[0]<==dy_out[0];eq_slope.b[1]<==dy_out[1];eq_slope.b[2]<==dy_out[2];eq_slope.b[3]<==dy_out[3]; eq_slope.eq === 1;

    // lam^2
    component c_lam_sq = Fq4Mul(); c_lam_sq.a[0]<==lam[0];c_lam_sq.a[1]<==lam[1];c_lam_sq.a[2]<==lam[2];c_lam_sq.a[3]<==lam[3]; c_lam_sq.b[0]<==lam[0];c_lam_sq.b[1]<==lam[1];c_lam_sq.b[2]<==lam[2];c_lam_sq.b[3]<==lam[3]; c_lam_sq.out[0]<==lam_sq_out[0];c_lam_sq.out[1]<==lam_sq_out[1];c_lam_sq.out[2]<==lam_sq_out[2];c_lam_sq.out[3]<==lam_sq_out[3]; c_lam_sq.k0<==mk0_lamsq;c_lam_sq.k1<==mk1_lamsq;c_lam_sq.k2<==mk2_lamsq;c_lam_sq.k3<==mk3_lamsq;

    // t1 = lam^2 - Mx; t2 = t1 - Tx
    component c_t1 = Fq4Sub(); c_t1.a[0]<==lam_sq_out[0];c_t1.a[1]<==lam_sq_out[1];c_t1.a[2]<==lam_sq_out[2];c_t1.a[3]<==lam_sq_out[3]; c_t1.b[0]<==Mx[0];c_t1.b[1]<==Mx[1];c_t1.b[2]<==Mx[2];c_t1.b[3]<==Mx[3]; c_t1.out[0]<==t1_out[0];c_t1.out[1]<==t1_out[1];c_t1.out[2]<==t1_out[2];c_t1.out[3]<==t1_out[3]; c_t1.need_q <== sub_need_q_t1;
    component c_t2 = Fq4Sub(); c_t2.a[0]<==t1_out[0];c_t2.a[1]<==t1_out[1];c_t2.a[2]<==t1_out[2];c_t2.a[3]<==t1_out[3]; c_t2.b[0]<==Tx[0];c_t2.b[1]<==Tx[1];c_t2.b[2]<==Tx[2];c_t2.b[3]<==Tx[3]; c_t2.out[0]<==t2_out[0];c_t2.out[1]<==t2_out[1];c_t2.out[2]<==t2_out[2];c_t2.out[3]<==t2_out[3]; c_t2.need_q <== sub_need_q_t2;
    component eq_x = Fq4Eq(); eq_x.a[0]<==t2_out[0];eq_x.a[1]<==t2_out[1];eq_x.a[2]<==t2_out[2];eq_x.a[3]<==t2_out[3]; eq_x.b[0]<==PI_x[0];eq_x.b[1]<==PI_x[1];eq_x.b[2]<==PI_x[2];eq_x.b[3]<==PI_x[3]; eq_x.eq === 1;

    // mx_diff = Mx - PI_x; lam * mx_diff; y_calc = lam*mx_diff - My
    component c_mx = Fq4Sub(); c_mx.a[0]<==Mx[0];c_mx.a[1]<==Mx[1];c_mx.a[2]<==Mx[2];c_mx.a[3]<==Mx[3]; c_mx.b[0]<==PI_x[0];c_mx.b[1]<==PI_x[1];c_mx.b[2]<==PI_x[2];c_mx.b[3]<==PI_x[3]; c_mx.out[0]<==mx_diff_out[0];c_mx.out[1]<==mx_diff_out[1];c_mx.out[2]<==mx_diff_out[2];c_mx.out[3]<==mx_diff_out[3]; c_mx.need_q <== sub_need_q_mxdiff;
    component c_ld = Fq4Mul(); c_ld.a[0]<==lam[0];c_ld.a[1]<==lam[1];c_ld.a[2]<==lam[2];c_ld.a[3]<==lam[3]; c_ld.b[0]<==mx_diff_out[0];c_ld.b[1]<==mx_diff_out[1];c_ld.b[2]<==mx_diff_out[2];c_ld.b[3]<==mx_diff_out[3]; c_ld.out[0]<==lam_diff_out[0];c_ld.out[1]<==lam_diff_out[1];c_ld.out[2]<==lam_diff_out[2];c_ld.out[3]<==lam_diff_out[3]; c_ld.k0<==mk0_lamdiff;c_ld.k1<==mk1_lamdiff;c_ld.k2<==mk2_lamdiff;c_ld.k3<==mk3_lamdiff;
    component c_yc = Fq4Sub(); c_yc.a[0]<==lam_diff_out[0];c_yc.a[1]<==lam_diff_out[1];c_yc.a[2]<==lam_diff_out[2];c_yc.a[3]<==lam_diff_out[3]; c_yc.b[0]<==My[0];c_yc.b[1]<==My[1];c_yc.b[2]<==My[2];c_yc.b[3]<==My[3]; c_yc.out[0]<==y_calc_out[0];c_yc.out[1]<==y_calc_out[1];c_yc.out[2]<==y_calc_out[2];c_yc.out[3]<==y_calc_out[3]; c_yc.need_q <== sub_need_q_ycalc;
    component eq_y = Fq4Eq(); eq_y.a[0]<==y_calc_out[0];eq_y.a[1]<==y_calc_out[1];eq_y.a[2]<==y_calc_out[2];eq_y.a[3]<==y_calc_out[3]; eq_y.b[0]<==PI_y[0];eq_y.b[1]<==PI_y[1];eq_y.b[2]<==PI_y[2];eq_y.b[3]<==PI_y[3]; eq_y.eq === 1;
}

component main { public [ identityRoot, PI_x, PI_y ] } = IdentityMembershipG1Tie(10);
